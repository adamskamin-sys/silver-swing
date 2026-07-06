"""
swing_leg.py — single-leg-live swing controller with a protected core (spec §2, §3A, §4).

Two buckets:
  core_qty  : never sold. HARD FLOOR. The swing can never take you below this.
  swing_qty : the contracts you actively swing. Grows over time as realized profit
              banks up, capped at max_swing_qty.

Invariant enforced before every sell:  position - swing_qty >= core_qty
If that would break, the bot HALTs instead of selling into the core.

State machine:
  ARMED_SELL --(sell swing_qty @ sell_px fills)--> ARMED_BUY
             --(buy swing_qty @ buy_px fills)--> realize profit, maybe grow --> ARMED_SELL

Only ONE order is ever live on the exchange (spec §2). Fills are confirmed by
order status, never by price. Full fills only flip the state.

Dependencies (all injected — the trader itself doesn't touch Coinbase, disk, or clock):
  broker      : Broker Protocol implementation (CoinbaseBroker or PaperBroker)
  store       : StateStore for config (dashboard-writes) and state (bot-writes)
  trade_log   : optional TradeLog for the audit journal
  kill_switch : optional KillSwitch for the "freeze everything" gate

The Broker Protocol is duck-typed — an object with the four required methods
(place_limit, order_status, cancel, position_qty) works. `preview_order` is
optional; if present, the §2A fee sanity gate is enabled.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Optional, Protocol

from alerting import Notifier, Priority
from state_store import StateStore
from safety import KillSwitch, TradeLog
from strategies import ExitStrategy, strategy_by_name


class State(str, Enum):
    ARMED_SELL = "ARMED_SELL"
    ARMED_BUY = "ARMED_BUY"
    HALTED = "HALTED"


class Broker(Protocol):
    def place_limit(self, side: str, qty: int, price: float) -> str: ...
    def order_status(self, order_id: str) -> dict: ...
    def cancel(self, order_id: str) -> None: ...
    def position_qty(self) -> int: ...


@dataclass
class SwingConfig:
    """Empirical defaults match SLR-27AUG26-CDE as of 2026-07-06 (spec §3A, §4)."""
    core_qty: int = 10
    swing_qty: int = 2
    max_swing_qty: int = 5
    sell_px: float = 65.0
    buy_px: float = 63.0
    contract_size: int = 50                     # troy oz per SLR contract (spec §3A)

    # Scale-up gate (spec §4)
    margin_per_contract: float = 275.0          # ~$275 intraday empirical (was 1000.0 placeholder)
    scale_up_buffer_mult: float = 1.5
    fee_per_contract_roundtrip: float = 4.68    # 2 × $2.34 empirical (was 0.0 placeholder)

    # Risk governor (Jim Paul)
    abort_below: float = 60.0
    abort_above: float = 70.0

    # §2A fee-gate sanity ceiling: halt if the queued-order commission comes
    # back at more than this many × the expected per-side fee. 2× is a starting
    # threshold — a real tier change costs ~10-30%, so 2× catches only
    # data-glitch / broken conditions, not normal drift. [OPEN in spec §2A]
    fee_sanity_multiplier: float = 2.0

    # Exit-mode toggle (spec §5)
    exit_mode: str = "fixed_limit"          # or "trailing_stop"
    trail_trigger: float = 65.0             # arm the trail at/above this price
    trail_distance: float = 0.20            # $0.20 = 40 ticks on SLR
    reanchor_threshold: float = 2.0         # if trailing exit fills > this above sell_px, re-anchor
    tick_size: float = 0.005                # per-instrument (needed for trail-stop fill price)


@dataclass
class SwingState:
    state: State = State.ARMED_SELL
    live_order_id: Optional[str] = None
    filled_qty: int = 0
    swing_qty: int = 2
    last_sell_qty: int = 0
    last_sell_fill_price: Optional[float] = None
    realized_pnl: float = 0.0
    reserved_margin: float = 0.0
    cycles: int = 0
    last_heartbeat_ts: float = 0.0
    # Trailing-stop state (spec §5 "MUST persist")
    trail_armed: bool = False
    trail_high_water_price: float = 0.0


class SwingTrader:
    def __init__(
        self,
        broker: Broker,
        store: StateStore,
        tenant_id: str,
        symbol: str,
        trade_log: Optional[TradeLog] = None,
        kill_switch: Optional[KillSwitch] = None,
        notifier: Optional[Notifier] = None,
    ):
        self.b = broker
        self.store = store
        self.tenant_id = tenant_id
        self.symbol = symbol
        self.log = trade_log
        self.ks = kill_switch
        self.notifier = notifier

        self.cfg = self._load_config()
        self.s = self._load_state()

    # ---- persistence / crash recovery ------------------------------------

    def _load_config(self) -> SwingConfig:
        d = self.store.get_config(self.tenant_id, self.symbol) or {}
        return SwingConfig(**d) if d else SwingConfig()

    def _load_state(self) -> SwingState:
        d = self.store.get_state(self.tenant_id, self.symbol)
        if not d:
            s = SwingState()
            s.swing_qty = self.cfg.swing_qty
            return s
        return SwingState(
            state=State(d["state"]),
            live_order_id=d.get("live_order_id"),
            filled_qty=d.get("filled_qty", 0),
            swing_qty=d.get("swing_qty", self.cfg.swing_qty),
            last_sell_qty=d.get("last_sell_qty", 0),
            last_sell_fill_price=d.get("last_sell_fill_price"),
            realized_pnl=d.get("realized_pnl", 0.0),
            reserved_margin=d.get("reserved_margin", 0.0),
            cycles=d.get("cycles", 0),
            last_heartbeat_ts=d.get("last_heartbeat_ts", 0.0),
            trail_armed=d.get("trail_armed", False),
            trail_high_water_price=d.get("trail_high_water_price", 0.0),
        )

    def _save_state(self) -> None:
        import time as _time
        self.s.last_heartbeat_ts = _time.time()
        self.store.put_state(self.tenant_id, self.symbol, {
            **asdict(self.s),
            "state": self.s.state.value,
        })

    def _notify(self, subject: str, body: str, priority: Priority) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.send(subject, body, priority)
        except Exception:
            pass  # alerting failure must not affect the bot

    def _record(self, event_type: str, **payload) -> None:
        if self.log is None:
            return
        self.log.record(
            event_type,
            tenant=self.tenant_id,
            symbol=self.symbol,
            **payload,
        )

    # ---- reconcile on startup --------------------------------------------

    def reconcile(self) -> None:
        """Trust the book, not memory. Called ONCE on startup.

        - If actual position is already below core, HALT.
        - If we thought an order was live but it's actually done/gone, clear it.
        - Record the reconcile in the trade log for audit.
        """
        pos = self.b.position_qty()
        if pos < self.cfg.core_qty:
            self._record(
                "reconcile_halt",
                actual_position=pos,
                core_qty=self.cfg.core_qty,
            )
            return self._halt(
                f"position {pos} already below core {self.cfg.core_qty}"
            )
        if self.s.live_order_id:
            st = self.b.order_status(self.s.live_order_id)
            if st["status"] in ("FILLED", "CANCELLED", "EXPIRED", "UNKNOWN"):
                self.s.live_order_id = None
                self.s.filled_qty = st.get("filled_qty", 0)
        self._record(
            "reconciled",
            actual_position=pos,
            live_order_id=self.s.live_order_id,
            state=self.s.state.value,
        )
        self._save_state()

    # ---- floor guard -----------------------------------------------------

    def _floor_ok(self, position: int, sell_qty: int) -> bool:
        return position - sell_qty >= self.cfg.core_qty

    # ---- kill switch -----------------------------------------------------

    def _kill_switch_active(self) -> bool:
        return self.ks is not None and self.ks.is_active()

    # ---- §2A fee gate (sanity ceiling only for MVP) ----------------------

    def _fee_gate_ok(self, side: str, qty: int, price: float) -> bool:
        """Return True if the trade should proceed at the actual fee.

        MVP scope: sanity ceiling only. If the previewed commission comes back
        at more than fee_sanity_multiplier × the expected per-side fee, HALT.
        Full 'auto-adjust net to preserve target' logic (spec §2A step 4) is a
        follow-up — for now, catch the fee blowout case and let the user look.

        Brokers that don't implement preview_order pass through unchecked.
        """
        preview_fn = getattr(self.b, "preview_order", None)
        if preview_fn is None:
            return True
        try:
            preview = preview_fn(side, qty, price)
        except Exception as e:
            self._record("fee_gate_preview_failed", side=side, qty=qty, price=price, error=str(e))
            return True  # don't block on a preview API glitch; log for followup
        commission = preview.get("commission_total") if isinstance(preview, dict) else None
        if commission is None:
            return True
        expected = (self.cfg.fee_per_contract_roundtrip / 2) * qty
        ceiling = expected * self.cfg.fee_sanity_multiplier
        if expected > 0 and commission > ceiling:
            self._record(
                "fee_gate_halt",
                side=side, qty=qty, price=price,
                previewed_commission=commission,
                expected=expected,
                ceiling=ceiling,
            )
            self._halt(
                f"fee sanity ceiling: expected ~${expected:.2f}, "
                f"previewed ${commission:.2f} (>{self.cfg.fee_sanity_multiplier}× ceiling)"
            )
            return False
        return True

    # ---- arming ----------------------------------------------------------

    def _arm(self, side: str, qty: int, price: float) -> None:
        if not self._fee_gate_ok(side, qty, price):
            return
        if self.s.live_order_id:
            try:
                self.b.cancel(self.s.live_order_id)
                self._record("order_cancelled_for_rearm", order_id=self.s.live_order_id)
            except Exception as e:
                self._record("cancel_failed", order_id=self.s.live_order_id, error=str(e))
        self.s.live_order_id = self.b.place_limit(side, qty, price)
        self.s.filled_qty = 0
        self._record(
            "order_placed",
            side=side, qty=qty, price=price,
            order_id=self.s.live_order_id,
        )
        self._save_state()

    def _exit_strategy(self) -> ExitStrategy:
        return strategy_by_name(self.cfg.exit_mode)

    def _ensure_armed(self, current_price: float) -> None:
        if self.s.live_order_id or self.s.state == State.HALTED:
            return
        pos = self.b.position_qty()
        strat = self._exit_strategy()
        if self.s.state == State.ARMED_SELL:
            if not self._floor_ok(pos, self.s.swing_qty):
                return self._halt(
                    f"sell {self.s.swing_qty} would breach floor at pos {pos}"
                )
            directive = strat.sell_action(self.s, self.cfg, current_price)
            if directive is None:
                return  # trailing waiting for trigger / trail crossover
            self._arm("SELL", directive.qty, directive.limit_price)
        elif self.s.state == State.ARMED_BUY:
            self._maybe_scale_up()
            directive = strat.buy_action(
                self.s, self.cfg, current_price,
                last_sell_fill_price=self.s.last_sell_fill_price,
            )
            if directive is None:
                return
            self._arm("BUY", directive.qty, directive.limit_price)

    def _maybe_scale_up(self) -> None:
        if self.s.swing_qty >= self.cfg.max_swing_qty:
            return
        free = self.s.realized_pnl - self.s.reserved_margin
        need = self.cfg.margin_per_contract * self.cfg.scale_up_buffer_mult
        if free >= need:
            self.s.swing_qty += 1
            self._record(
                "scaled_up",
                new_swing_qty=self.s.swing_qty,
                free_profit=free,
                needed=need,
            )
            self._save_state()

    # ---- main loop -------------------------------------------------------

    def step(self, last_price: float) -> None:
        if self.s.state == State.HALTED:
            return

        # Kill switch is checked EVERY cycle — no arming, no fill processing.
        # We stop short of halting because the kill switch is meant to be
        # temporary; the strategy should resume when it clears.
        if self._kill_switch_active():
            self._record("kill_switch_pause", reason=self.ks.reason() if self.ks else None)
            return

        # Refresh config from store — dashboard edits take effect next cycle.
        cfg = self._load_config()
        self.cfg = cfg

        if self.s.state == State.ARMED_SELL and last_price >= self.cfg.abort_above:
            return self._halt(
                f"price {last_price} ran above abort_above {self.cfg.abort_above} while flat on swing"
            )
        if self.s.state == State.ARMED_BUY and last_price <= self.cfg.abort_below:
            return self._halt(
                f"price {last_price} fell below abort_below {self.cfg.abort_below} while holding swing"
            )

        self._ensure_armed(last_price)
        if not self.s.live_order_id:
            self._save_state()
            return

        st = self.b.order_status(self.s.live_order_id)
        self.s.filled_qty = st.get("filled_qty", 0)
        if st.get("status") == "FILLED" and self.s.filled_qty >= self.s.swing_qty:
            self._on_fill(fill_price=st.get("average_filled_price"))
        self._save_state()

    def _on_fill(self, fill_price: Optional[float] = None) -> None:
        self._record(
            "order_filled",
            order_id=self.s.live_order_id,
            filled_qty=self.s.filled_qty,
            average_filled_price=fill_price,
            leg=self.s.state.value,
        )
        strat = self._exit_strategy()
        self.s.live_order_id = None
        self.s.filled_qty = 0
        if self.s.state == State.ARMED_SELL:
            self.s.last_sell_qty = self.s.swing_qty
            if fill_price is not None:
                try:
                    self.s.last_sell_fill_price = float(fill_price)
                except (TypeError, ValueError):
                    self.s.last_sell_fill_price = None
            strat.on_sell_filled(self.s, self.cfg, fill_price or 0.0)
            self.s.state = State.ARMED_BUY
        else:
            # Use the actual sell fill price for realized-P&L calc when we have
            # it (trailing exits can fill above cfg.sell_px). Fall back to the
            # configured sell_px for fixed-limit runs.
            effective_sell_px = self.s.last_sell_fill_price or self.cfg.sell_px
            gross = (effective_sell_px - self.cfg.buy_px) * self.cfg.contract_size * self.s.last_sell_qty
            fees = self.cfg.fee_per_contract_roundtrip * self.s.last_sell_qty
            self.s.realized_pnl += gross - fees
            added = self.s.swing_qty - self.s.last_sell_qty
            if added > 0:
                self.s.reserved_margin += added * self.cfg.margin_per_contract
            self.s.cycles += 1
            strat.on_buy_filled(self.s, self.cfg, fill_price or 0.0)
            self.s.state = State.ARMED_SELL
            self._record(
                "cycle_completed",
                cycles=self.s.cycles,
                gross=gross,
                fees=fees,
                realized_pnl_total=self.s.realized_pnl,
                swing_qty=self.s.swing_qty,
                effective_sell_px=effective_sell_px,
            )
        self._save_state()

    def _halt(self, reason: str = "") -> None:
        if self.s.live_order_id:
            try:
                self.b.cancel(self.s.live_order_id)
            except Exception:
                pass
        self.s.live_order_id = None
        self.s.state = State.HALTED
        self._save_state()
        self._record("halt", reason=reason)
        self._notify(
            f"HALT: {self.symbol}",
            f"tenant={self.tenant_id}\nreason: {reason}\ncore_qty={self.cfg.core_qty}, "
            f"swing_qty={self.s.swing_qty}, cycles={self.s.cycles}",
            Priority.CRIT,
        )

    def run(self, price_feed) -> None:
        self.reconcile()
        for last_price in price_feed:
            self.step(last_price)
