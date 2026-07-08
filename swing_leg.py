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
from sleeves import SleeveConfig, SleeveState, SleeveStateEnum


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

    # Stop-loss: fires BEFORE abort_below. abort_below just halts (position
    # keeps bleeding); stop-loss sells first, then halts. Modes for the sell
    # quantity are exposed so the user can pick between "flatten to core"
    # (safest during a crash) and "sell only the original swing size, let
    # accumulated contracts ride" (bet on rebound). Set stop_loss_enabled=False
    # to disable entirely — abort_below still catches the crash as fallback.
    stop_loss_enabled: bool = False
    stop_loss_px: float = 0.0
    stop_loss_qty_mode: str = "all"         # "all" | "original" | "custom"
    stop_loss_qty_custom: int = 0           # only read when mode == "custom"


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
    # Additional sleeves — each runs its own state machine in parallel to
    # the primary strategy above. Empty dict = legacy single-strategy mode.
    sleeves: dict[str, SleeveState] = field(default_factory=dict)
    # Why the primary halted (last _halt() call). Displayed on the dashboard
    # so the user can see what to fix before resuming. Cleared by resume.
    halt_reason: Optional[str] = None


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
        microstructure=None,
    ):
        self.b = broker
        self.store = store
        self.tenant_id = tenant_id
        self.symbol = symbol
        self.log = trade_log
        self.ks = kill_switch
        self.notifier = notifier
        self.ms = microstructure  # MicrostructureFilter or None

        self.cfg = self._load_config()
        self.s = self._load_state()

        # Rolling price history dict reserved for future theory-based
        # strategies (mean reversion, Bollinger). Empty for now; will be
        # populated the same commit those exit_modes are wired in.
        self._sleeve_price_history: dict = {}

    # ---- persistence / crash recovery ------------------------------------

    def _load_config(self) -> SwingConfig:
        d = self.store.get_config(self.tenant_id, self.symbol) or {}
        if not d:
            return SwingConfig()
        # Strip fields SwingConfig doesn't own (sleeves live on a separate model,
        # any unrecognized future field should be tolerated so the dashboard can
        # add config keys without crashing the bot).
        allowed = set(SwingConfig.__dataclass_fields__.keys())
        clean = {k: v for k, v in d.items() if k in allowed}
        return SwingConfig(**clean)

    def _load_state(self) -> SwingState:
        d = self.store.get_state(self.tenant_id, self.symbol)
        if not d:
            s = SwingState()
            s.swing_qty = self.cfg.swing_qty
            s.sleeves = self._init_sleeves_state({})
            return s
        state = SwingState(
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
        state.sleeves = self._init_sleeves_state(d.get("sleeves") or {})
        state.halt_reason = d.get("halt_reason")
        return state

    def _init_sleeves_state(self, persisted: dict) -> dict[str, SleeveState]:
        """Materialize a SleeveState per configured additional sleeve. Missing
        entries (new sleeve just added) start fresh in ARMED_SELL."""
        out: dict[str, SleeveState] = {}
        for sc in self._load_sleeves_cfg():
            raw = persisted.get(sc.id)
            out[sc.id] = SleeveState.from_dict(raw, sc.id) if raw else SleeveState(id=sc.id)
        return out

    def _load_sleeves_cfg(self) -> list[SleeveConfig]:
        """Additional sleeves from cfg.sleeves list. The primary strategy
        (cfg.swing_qty + cfg.sell_px/buy_px/exit_mode) is NOT a sleeve here —
        it's the legacy state machine already on SwingState."""
        raw = self.store.get_config(self.tenant_id, self.symbol) or {}
        return [SleeveConfig.from_dict(s) for s in (raw.get("sleeves") or [])]

    def _save_state(self) -> None:
        import time as _time
        self.s.last_heartbeat_ts = _time.time()
        d = asdict(self.s)
        d["state"] = self.s.state.value
        d["sleeves"] = {sid: s.to_dict() for sid, s in self.s.sleeves.items()}
        self.store.put_state(self.tenant_id, self.symbol, d)

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
            # Position below core is a real invariant break — the "protected core"
            # promise has already been violated. Halt so the user reviews. With
            # core_qty=0 (free trading), this branch never fires.
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
        # Same sweep for sleeves — a live_order_id that persisted across a bot
        # restart (or a live-exchange cancel) points at nothing on the fresh
        # broker. Clear it here so the sleeve state machine can re-arm on the
        # first tick instead of polling a dead id every cycle.
        cleared_sleeves = []
        for sid, ss in self.s.sleeves.items():
            if not ss.live_order_id: continue
            st = self.b.order_status(ss.live_order_id)
            if st.get("status") in ("FILLED", "CANCELLED", "EXPIRED", "UNKNOWN"):
                cleared_sleeves.append((sid, ss.live_order_id, st.get("status")))
                ss.live_order_id = None
                ss.filled_qty = 0
        self._record(
            "reconciled",
            actual_position=pos,
            live_order_id=self.s.live_order_id,
            state=self.s.state.value,
            cleared_sleeves=cleared_sleeves,
        )
        self._save_state()

    # ---- floor guard -----------------------------------------------------

    def _floor_ok(self, position: int, sell_qty: int) -> bool:
        # core_qty <= 0 means no protected core to defend — shorts allowed.
        # Lab tenant defaults to core=0 so every sleeve can open its first
        # cycle by shorting, without needing a seeded long position.
        if self.cfg.core_qty <= 0:
            return True
        return position - sell_qty >= self.cfg.core_qty

    # ---- kill switch -----------------------------------------------------

    def _kill_switch_active(self) -> bool:
        return self.ks is not None and self.ks.is_active()

    # ---- manual intent (dashboard → bot bridge) --------------------------

    def _maybe_execute_intent(self) -> None:
        """Look for a dashboard-queued manual order and execute it.

        Safety rules that override the intent (dashboard also validates, but
        the bot is the last line of defense):
          - SELL that would breach core_qty is REFUSED (logged, cleared)
          - qty <= 0 is REFUSED
          - broker without place_market falls back to aggressive place_limit
        """
        intent = self.store.get_intent(self.tenant_id, self.symbol)
        if not intent:
            return
        try:
            side = str(intent.get("side", "")).upper()
            qty = int(intent.get("qty", 0))
            if side not in ("BUY", "SELL") or qty <= 0:
                self._record("intent_rejected", reason="bad side or qty", intent=intent)
                return
            if side == "SELL":
                pos = self.b.position_qty()
                if not self._floor_ok(pos, qty):
                    self._record(
                        "intent_rejected",
                        reason=f"sell {qty} would breach floor (pos={pos}, core={self.cfg.core_qty})",
                        intent=intent,
                    )
                    self._notify(
                        f"manual trade REFUSED: {self.symbol}",
                        f"tried to SELL {qty} but that breaches core {self.cfg.core_qty} at pos {pos}",
                        Priority.WARN,
                    )
                    return

            # Tag the resulting lot as "manual" so the positions page shows
            # you clicked BUY vs the bot's swing running.
            set_src = getattr(self.b, "set_pending_source", None)
            if callable(set_src):
                set_src("manual")

            order_type = str(intent.get("order_type") or "market").lower()
            limit_price = intent.get("limit_price")

            if order_type == "limit" and limit_price is not None:
                try:
                    px = float(limit_price)
                except (TypeError, ValueError):
                    self._record("intent_rejected", reason="bad limit_price", intent=intent)
                    return
                if px <= 0:
                    self._record("intent_rejected", reason="limit_price <= 0", intent=intent)
                    return
                oid = self.b.place_limit(side, qty, px)
                self._record("manual_limit_order", side=side, qty=qty, order_id=oid,
                             price=px, source="dashboard")
                self._notify(
                    f"manual {side} {qty} LIMIT placed: {self.symbol}",
                    f"limit={px}, order_id={oid}",
                    Priority.INFO,
                )
                return

            place_market = getattr(self.b, "place_market", None)
            if callable(place_market):
                oid = self.b.place_market(side, qty)
                self._record("manual_market_order", side=side, qty=qty, order_id=oid)
            else:
                # Fallback: aggressive limit far from mid — should fill immediately
                # against a normal book.
                spread_est = self.cfg.tick_size * 100
                anchor = intent.get("mark") or self.cfg.sell_px
                px = float(anchor) + spread_est if side == "BUY" else float(anchor) - spread_est
                oid = self.b.place_limit(side, qty, px)
                self._record("manual_limit_order", side=side, qty=qty, order_id=oid, price=px)
            self._notify(
                f"manual {side} {qty} filled: {self.symbol}",
                f"order_id={oid}",
                Priority.INFO,
            )
        except Exception as e:
            self._record("intent_execution_failed", error=str(e), intent=intent)
        finally:
            self.store.clear_intent(self.tenant_id, self.symbol)

    # ---- cancel intent (dashboard cancels a strategy's live order) --------

    def _maybe_execute_cancel_intent(self) -> None:
        """Dashboard queued a cancel for a specific strategy's live order.
        sleeve_id=None targets the primary.

        If intent['halt'] is True, we ALSO set the state machine to HALTED so
        the strategy stops re-arming on the next tick. Without halt, cancelling
        a resting limit order was pointless: the sleeve's next step() saw no
        live_order_id and immediately placed a new one, so the user's Cancel
        click felt like a no-op. halt=True is what "Pause strategy" on the
        dashboard actually means.
        """
        get_ci = getattr(self.store, "get_cancel_intent", None)
        if not callable(get_ci):
            return
        intent = get_ci(self.tenant_id, self.symbol)
        if not intent:
            return
        try:
            target = intent.get("sleeve_id")
            halt = bool(intent.get("halt"))
            if target is None:
                # Primary strategy cancel
                if self.s.live_order_id:
                    try: self.b.cancel(self.s.live_order_id)
                    except Exception as e:
                        self._record("cancel_failed", order_id=self.s.live_order_id, error=str(e))
                    self._record("primary_order_cancelled", order_id=self.s.live_order_id, requested_by="dashboard", halted=halt)
                    self.s.live_order_id = None
                    self.s.filled_qty = 0
                if halt:
                    self.s.state = State.HALTED
                    self.s.halt_reason = "paused via dashboard"
                    self._record("primary_paused", requested_by="dashboard")
            else:
                ss = self.s.sleeves.get(target)
                if ss:
                    if ss.live_order_id:
                        try: self.b.cancel(ss.live_order_id)
                        except Exception as e:
                            self._record("cancel_failed", sleeve_id=target, order_id=ss.live_order_id, error=str(e))
                        self._record("sleeve_order_cancelled", sleeve_id=target, order_id=ss.live_order_id, requested_by="dashboard", halted=halt)
                        ss.live_order_id = None
                        ss.filled_qty = 0
                    if halt:
                        ss.state = SleeveStateEnum.HALTED
                        ss.halt_reason = "paused via dashboard"
                        self._record("sleeve_paused", sleeve_id=target, requested_by="dashboard")
            self._save_state()
        finally:
            self.store.clear_cancel_intent(self.tenant_id, self.symbol)

    # ---- reset intent (dashboard wipes paper state) -----------------------

    def _maybe_consume_reset_intent(self) -> None:
        """Full paper-state wipe. Only applies to paper brokers — the broker
        must implement a reset() method. Live CoinbaseBroker doesn't (and
        shouldn't) — you can't wipe real positions from a dashboard button."""
        if not hasattr(self.store, "get_reset_intent"):
            return
        intent = self.store.get_reset_intent(self.tenant_id, self.symbol)
        if not intent:
            return
        reset_fn = getattr(self.b, "reset", None)
        if not callable(reset_fn):
            self._record("reset_ignored", reason="broker has no reset() — live mode?")
            self.store.clear_reset_intent(self.tenant_id, self.symbol)
            return
        starting_balance = intent.get("starting_balance")
        try:
            reset_fn(starting_balance=starting_balance)
        except TypeError:
            reset_fn()
        # Wipe trader state too — sleeves, cycles, live_order_id, everything.
        self.s = SwingState(swing_qty=self.cfg.swing_qty)
        self.s.sleeves = self._init_sleeves_state({})
        self._save_state()
        # Also drop the persisted paper broker state so a restart mid-reset
        # doesn't restore the pre-reset position from the store. Next snapshot
        # cycle will write fresh state.
        if hasattr(self.store, "clear_paper_state"):
            self.store.clear_paper_state(self.tenant_id, self.symbol)
        self._record(
            "paper_reset",
            starting_balance=starting_balance,
            requested_by=intent.get("requested_by"),
        )
        self.store.clear_reset_intent(self.tenant_id, self.symbol)

    # ---- resume intent (dashboard clears a HALT) --------------------------

    def _maybe_consume_resume_intent(self) -> None:
        """Dashboard posts to /api/resume to clear a HALT. That writes a
        resume_intent to the store; we consume it here and reset state so the
        strategy re-arms next tick. Sleeves halted for their own reasons get
        reset too — the user made a deliberate call to un-pause everything."""
        intent = self.store.get_resume_intent(self.tenant_id, self.symbol) if hasattr(self.store, "get_resume_intent") else None
        if not intent:
            return
        if self.s.state == State.HALTED:
            self.s.state = State.ARMED_SELL
            self.s.halt_reason = None
            self.s.live_order_id = None
            self.s.filled_qty = 0
            self._record("resume", cleared_reason=intent.get("previous_reason"))
        for sid, ss in self.s.sleeves.items():
            if ss.state == SleeveStateEnum.HALTED:
                ss.state = SleeveStateEnum.ARMED_SELL
                ss.live_order_id = None
                ss.filled_qty = 0
                ss.halt_reason = None
                self._record("sleeve_resume", sleeve_id=sid)
        self.store.clear_resume_intent(self.tenant_id, self.symbol)
        self._save_state()

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
        set_src = getattr(self.b, "set_pending_source", None)
        if callable(set_src):
            set_src("strategy", strategy_id=getattr(self, "sleeve_id", None))
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
        # Primary strategy disabled: swing_qty=0 means sleeves own the whole
        # position (Live tenant, Lab tenant, sleeve-only paper configs).
        # Without this guard, ARMED_SELL fires SellDirective(qty=0, price=0.0),
        # which PaperBroker accepts silently but CoinbaseBroker rejects with
        # INVALID_LIMIT_PRICE, taking the worker down on every tick.
        if self.s.swing_qty <= 0:
            return
        pos = self.b.position_qty()
        strat = self._exit_strategy()
        if self.s.state == State.ARMED_SELL:
            if not self._floor_ok(pos, self.s.swing_qty):
                self._record(
                    "arm_sell_skipped",
                    reason="insufficient contracts",
                    position=pos,
                    swing_qty=self.s.swing_qty,
                    core_qty=self.cfg.core_qty,
                )
                return
            directive = strat.sell_action(self.s, self.cfg, current_price)
            if directive is None:
                return  # trailing waiting for trigger / trail crossover
            qty, px = self._ms_adjust("SELL", directive.qty, directive.limit_price, current_price)
            if qty is None:
                return  # filter said pause
            self._arm("SELL", qty, px)
        elif self.s.state == State.ARMED_BUY:
            self._maybe_scale_up()
            directive = strat.buy_action(
                self.s, self.cfg, current_price,
                last_sell_fill_price=self.s.last_sell_fill_price,
            )
            if directive is None:
                return
            qty, px = self._ms_adjust("BUY", directive.qty, directive.limit_price, current_price)
            if qty is None:
                return
            self._arm("BUY", qty, px)

    def _ms_adjust(self, side: str, qty: int, px: float, mark: float):
        """Consult the microstructure filter. Returns (qty, px) or (None, None) to pause."""
        if not self.ms:
            return qty, px
        reason = self.ms.should_pause_arm(side)
        if reason:
            self._record("ms_pause", side=side, reason=reason)
            return None, None
        # Adaptive spread band overrides configured limit if enabled
        if side == "BUY":
            px = self.ms.adjusted_buy_px(px, mark)
        else:
            px = self.ms.adjusted_sell_px(px, mark)
        # Kyle-lambda size taper
        scale = self.ms.size_scale()
        if scale < 1.0:
            qty = max(1, int(qty * scale))
        return qty, px

    def _sleeve_ms_adjust(self, sc, ss, side: str, qty: int, px: float, mark: float):
        """Sleeve-scoped microstructure gate. Only consults the filter when
        the sleeve has microstructure_gate_enabled = true. Same 5 signals as
        the primary (Effective Spread, Autocorr, OBI, VPIN, Kyle-λ), same
        decisions:
          - pause the arm if any signal says stand aside
          - shift limit price via spread band if enabled
          - taper qty via Kyle-λ scale
        Returns (qty, px) — with qty=None to signal 'skip this arm'."""
        if not getattr(sc, "microstructure_gate_enabled", False):
            return qty, px
        if not self.ms:
            return qty, px
        reason = self.ms.should_pause_arm(side)
        if reason:
            self._record("sleeve_ms_pause",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         side=side, reason=reason)
            return None, px
        if side == "BUY":
            px = self.ms.adjusted_buy_px(px, mark)
        else:
            px = self.ms.adjusted_sell_px(px, mark)
        scale = self.ms.size_scale()
        if scale < 1.0:
            new_qty = max(1, int(qty * scale))
            if new_qty < qty:
                self._record("sleeve_ms_size_taper",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             original_qty=qty, tapered_qty=new_qty, scale=scale)
                qty = new_qty
        return qty, px

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

    def _maybe_scale_up_sleeve(self, sc, ss) -> None:
        """Per-sleeve accumulation. Same logic as _maybe_scale_up but scoped
        to this sleeve's own realized_pnl and its own max_qty ceiling. That
        way each sleeve compounds independently — a winning sleeve grows,
        a losing sleeve stays at its starting size.

        Bumps sc.qty in memory AND writes the new qty back to the store so a
        restart preserves the accumulated size.
        """
        if not getattr(sc, "accumulate_enabled", False):
            return
        max_qty = int(getattr(sc, "max_qty", 0) or 0)
        if max_qty <= sc.qty:
            return
        need = self.cfg.margin_per_contract * float(getattr(sc, "scale_up_buffer_mult", 1.5) or 1.5)
        if ss.realized_pnl < need:
            return
        # Enough banked to add one contract. Bump in memory, persist to store,
        # and decrement the sleeve's own realized so the same profit can't be
        # counted twice next cycle. Matches the primary's semantics.
        sc.qty += 1
        ss.realized_pnl -= need
        self._persist_sleeve_qty(sc.id, sc.qty)
        self._record(
            "sleeve_scaled_up",
            sleeve_id=sc.id, sleeve_name=sc.name,
            new_qty=sc.qty, max_qty=max_qty,
            consumed=need,
        )

    def _compute_sleeve_stop_loss_qty(self, sc, position_qty: int) -> int:
        """Same rules as _compute_stop_loss_qty but scoped to a sleeve. Always
        respects the core floor. 'original' means cfg.qty (the starting size,
        not the current possibly-accumulated size)."""
        core = int(self.cfg.core_qty or 0)
        sellable_ceiling = max(0, position_qty - core)
        if sellable_ceiling == 0:
            return 0
        mode = (getattr(sc, "stop_loss_qty_mode", "all") or "all").lower()
        if mode == "original":
            # Use the sleeve's current qty (accumulated size). "Original" here
            # means "just this sleeve, not all your other holdings" — which is
            # what makes intuitive sense at the sleeve level.
            return min(int(sc.qty or 0), sellable_ceiling)
        if mode == "custom":
            return min(max(0, int(getattr(sc, "stop_loss_qty_custom", 0) or 0)), sellable_ceiling)
        return sellable_ceiling  # "all"

    def _sleeve_effective_stop(self, sc, ss) -> float:
        """Compute the effective stop-loss price. If ratchet is enabled AND
        the position has cleared the activation profit threshold, returns
        max(fixed_stop, HWM - ratchet_distance). Otherwise returns the fixed
        stop. Always monotonic-up: once ratcheted higher, never drops."""
        fixed_stop = float(sc.stop_loss_px or 0.0)
        if not sc.stop_loss_ratchet_enabled:
            return fixed_stop
        if ss.stop_loss_hwm is None or ss.own_avg_entry is None:
            return fixed_stop
        # Ratchet only arms once unrealized/contract is above activation.
        unrealized_per_contract = ss.stop_loss_hwm - float(ss.own_avg_entry)
        if unrealized_per_contract < sc.stop_loss_ratchet_activation:
            return fixed_stop
        ratchet_stop = float(ss.stop_loss_hwm) - float(sc.stop_loss_ratchet_distance)
        return max(fixed_stop, ratchet_stop)

    def _maybe_trigger_sleeve_stop_loss(self, sc, ss, last_price: float) -> bool:
        """Per-sleeve stop-loss. Fires either from fixed floor OR from a
        ratcheted stop that walks up with the HWM to preserve gains. On
        trigger: sells at market, then either reanchors (walks buy/sell to
        bracket current price so sleeve keeps trading) or halts.

        Also increments consecutive_stops; if that reaches
        stop_loss_max_consecutive, halts anyway as a safety brake against
        reanchor+stop chains during a bleeding market."""
        if not getattr(sc, "stop_loss_enabled", False):
            return False
        effective_stop = self._sleeve_effective_stop(sc, ss)
        if effective_stop <= 0 or last_price > effective_stop:
            return False
        try:
            pos = int(self.b.position_qty() or 0)
        except Exception as e:
            self._record("sleeve_stop_loss_read_position_failed",
                         sleeve_id=sc.id, error=str(e))
            return False
        if pos <= 0:
            self._sleeve_halt(sc, ss,
                              f"stop-loss at {last_price} (≤ {effective_stop}) but position is 0")
            return True
        to_sell = self._compute_sleeve_stop_loss_qty(sc, pos)
        if to_sell <= 0:
            self._sleeve_halt(sc, ss,
                              f"stop-loss at {last_price} (≤ {effective_stop}) but core floor "
                              f"{self.cfg.core_qty} blocks the sell (pos={pos})")
            return True
        was_ratcheted = effective_stop > float(sc.stop_loss_px or 0.0)
        try:
            source = getattr(self.b, "set_pending_source", None)
            if callable(source):
                source(f"sleeve_stop_loss:{sc.id}")
            oid = self.b.place_market("SELL", to_sell)
            self._record(
                "sleeve_stop_loss_triggered",
                sleeve_id=sc.id, sleeve_name=sc.name,
                price=last_price, trigger=effective_stop,
                ratcheted=was_ratcheted, hwm=ss.stop_loss_hwm,
                sold=to_sell, mode=sc.stop_loss_qty_mode, order_id=oid,
                position_before=pos, position_after=pos - to_sell,
            )
        except Exception as e:
            self._record("sleeve_stop_loss_sell_failed",
                         sleeve_id=sc.id, error=str(e),
                         price=last_price, trigger=effective_stop)

        # Post-trigger housekeeping.
        ss.consecutive_stops = int(ss.consecutive_stops or 0) + 1
        ss.stop_loss_hwm = None  # reset — no longer holding, HWM restarts on next buy
        ss.own_avg_entry = None  # position now flat

        # Safety brake: after N consecutive stops without a winner in between,
        # halt regardless of reanchor/re-entry flags. Requires manual review.
        max_consec = int(sc.stop_loss_max_consecutive or 0)
        if max_consec > 0 and ss.consecutive_stops >= max_consec:
            self._sleeve_halt(sc, ss,
                              f"stop-loss: {ss.consecutive_stops} consecutive stops — halted for review")
            return True

        # Choose post-trigger behavior:
        # 1. If reanchor_on_trigger: walk buy/sell to bracket current price,
        #    stay ARMED_BUY so sleeve resumes trading at new level.
        # 2. Else if reentry_mode == 'volatility': keep sleeve alive in a
        #    "waiting for volatility contraction" state (reentry_pending).
        # 3. Else: halt as before (fixed stop-loss with no auto-recovery).
        if sc.stop_loss_reanchor_on_trigger:
            spread = max(0.005, sc.sell_px - sc.buy_px)
            new_buy = round(last_price - spread / 2, 3)
            new_sell = round(last_price + spread / 2, 3)
            self._reanchor_sleeve(sc, ss, new_buy, new_sell, last_price)
            ss.state = SleeveStateEnum.ARMED_BUY
            return True
        if sc.reentry_mode == "volatility":
            import time as _t
            ss.reentry_pending = True
            ss.reentry_stop_ts = _t.time()
            ss.pre_stop_range = self._sleeve_recent_range(sc)
            ss.state = SleeveStateEnum.ARMED_BUY
            self._record("sleeve_reentry_pending",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         pre_stop_range=ss.pre_stop_range,
                         waiting_for_contraction=sc.reentry_range_contraction)
            return True
        self._sleeve_halt(sc, ss,
                          f"stop-loss: sold {to_sell} @ market at {last_price} (trigger {effective_stop})")
        return True

    # ---- rolling price range for volatility detection ---------------------

    def _sleeve_recent_range(self, sc) -> float:
        """Peak-to-trough range of the last N ticks in this sleeve's price
        history. Used both as the pre-stop baseline (captured at trigger
        time) and post-stop to detect when volatility has contracted enough
        to re-enter. Returns 0 if we don't have enough history yet."""
        window = int(sc.reentry_range_window or 60)
        history = self._sleeve_price_history.get(sc.id)
        if not history:
            return 0.0
        recent = list(history)[-window:]
        if len(recent) < 5:
            return 0.0
        return max(recent) - min(recent)

    def _sleeve_track_price(self, sc, last_price: float) -> None:
        """Append last_price to the sleeve's rolling window. Kept short so
        memory is bounded — window * 4 keeps enough history for pre-stop
        vs post-stop range comparison."""
        from collections import deque as _deque
        if sc.id not in self._sleeve_price_history:
            self._sleeve_price_history[sc.id] = _deque(maxlen=int(sc.reentry_range_window or 60) * 4)
        self._sleeve_price_history[sc.id].append(float(last_price))

    def _maybe_trigger_sleeve_reentry(self, sc, ss, last_price: float) -> bool:
        """Volatility-contraction re-entry after a stop. When current range
        has contracted below pre_stop_range × contraction, place a market
        buy to re-enter at the (lower) new price level. Also reanchors the
        sleeve's buy/sell targets around the new market. Returns True if
        it re-entered."""
        if not ss.reentry_pending:
            return False
        if sc.reentry_mode != "volatility":
            # Config changed under us — clear the pending flag and let normal
            # arm logic take over.
            ss.reentry_pending = False
            return False
        import time as _t
        elapsed = _t.time() - (ss.reentry_stop_ts or 0)
        if elapsed < float(sc.reentry_min_wait_secs or 30.0):
            return False
        current_range = self._sleeve_recent_range(sc)
        pre_range = float(ss.pre_stop_range or 0.0)
        # If we have no pre-stop baseline (edge case: reentry_pending set
        # without proper capture), fall back to time-only trigger after 5×
        # the min wait so the sleeve doesn't get stuck.
        if pre_range <= 0:
            if elapsed < float(sc.reentry_min_wait_secs or 30.0) * 5:
                return False
        else:
            contraction_target = pre_range * float(sc.reentry_range_contraction or 0.5)
            if current_range > contraction_target:
                return False  # volatility hasn't contracted enough yet

        # Reanchor to current price so the buy fires at market immediately.
        spread = max(0.005, sc.sell_px - sc.buy_px)
        new_buy = round(last_price - spread / 2, 3)
        new_sell = round(last_price + spread / 2, 3)
        self._reanchor_sleeve(sc, ss, new_buy, new_sell, last_price)
        ss.reentry_pending = False
        ss.reentry_stop_ts = None
        self._record("sleeve_reentry_fired",
                     sleeve_id=sc.id, sleeve_name=sc.name,
                     elapsed_secs=elapsed, current_range=current_range,
                     pre_stop_range=pre_range,
                     new_buy=new_buy, new_sell=new_sell)
        # Return False so normal arm logic runs on this same tick — the
        # ARMED_BUY state machine will place the buy at new_buy_px.
        return False

    # ---- news blackout check ---------------------------------------------

    def _sleeve_in_blackout(self, sc, ss) -> bool:
        """True if the sleeve is currently inside a news-event blackout
        window and should pause new arms. Tier 2+ = pause; tier 3 = also
        exit any open position (handled separately).

        Consults news_calendar.blackout_for() to check against the module-
        level SCHEDULED_EVENTS list. Also honors any explicit
        blackout_until_ts on the state (manual override or set by an
        earlier event). Bot-side check runs every tick — cheap operation
        since the calendar list is small and stays in memory.
        """
        if not sc.news_blackout_enabled:
            return False
        import time as _t
        now = _t.time()
        # Explicit state override (set by dashboard for manual pauses)
        if ss.blackout_until_ts is not None and now < float(ss.blackout_until_ts):
            return True
        # Scheduled event check
        try:
            from news_calendar import blackout_for
            active = blackout_for(now)
        except Exception as e:
            self._record("sleeve_blackout_check_failed",
                         sleeve_id=sc.id, error=str(e))
            return False
        if not active:
            return False
        # Only respect events at or above this sleeve's configured tier.
        # sc.news_blackout_tier = 2 means "only stand aside for tier 2 and
        # tier 3 events (skip tier 1 tightening-only)."
        if active["tier"] < int(sc.news_blackout_tier or 2):
            return False
        # Cache the end_ts so subsequent ticks in this window are fast.
        ss.blackout_until_ts = active["end_ts"]
        self._record("sleeve_blackout_active",
                     sleeve_id=sc.id, sleeve_name=sc.name,
                     event=active["name"], tier=active["tier"],
                     end_ts=active["end_ts"])
        return True

    def _persist_sleeve_qty(self, sleeve_id: str, new_qty: int) -> None:
        """Write the grown qty back to the sleeves config so the next boot
        starts at the accumulated size, not the original config qty."""
        cfg = self.store.get_config(self.tenant_id, self.symbol) or {}
        sleeves = list(cfg.get("sleeves") or [])
        changed = False
        for s in sleeves:
            if s.get("id") == sleeve_id:
                s["qty"] = int(new_qty)
                changed = True
                break
        if changed:
            cfg["sleeves"] = sleeves
            self.store.put_config(self.tenant_id, self.symbol, cfg)

    def _reanchor_sleeve(self, sc: "SleeveConfig", ss: "SleeveState",
                         new_buy_px: float, new_sell_px: float,
                         current_price: float) -> None:
        """Walk this sleeve's buy/sell targets to bracket the current market
        instead of waiting forever for a dip that isn't coming. Updates BOTH
        the in-memory SleeveConfig (so this tick uses the new prices) AND the
        persisted config in the store (so next boot uses them too).

        Also mutates the config for other tenants sharing the same underlying
        store contract? No — get_config/put_config are scoped by (tenant, symbol),
        so no cross-tenant leak.
        """
        old_buy, old_sell = sc.buy_px, sc.sell_px
        sc.buy_px = float(new_buy_px)
        sc.sell_px = float(new_sell_px)
        sc.trail_trigger = float(new_sell_px)
        cfg = self.store.get_config(self.tenant_id, self.symbol) or {}
        sleeves = list(cfg.get("sleeves") or [])
        for s in sleeves:
            if s.get("id") == sc.id:
                s["buy_px"] = float(new_buy_px)
                s["sell_px"] = float(new_sell_px)
                s["trail_trigger"] = float(new_sell_px)
                break
        cfg["sleeves"] = sleeves
        self.store.put_config(self.tenant_id, self.symbol, cfg)
        self._record(
            "sleeve_reanchored",
            sleeve_id=sc.id, sleeve_name=sc.name,
            current_price=current_price,
            old_buy=old_buy, old_sell=old_sell,
            new_buy=new_buy_px, new_sell=new_sell_px,
            reason=f"price {current_price} moved > {sc.reanchor_threshold} above buy {old_buy}",
        )

    # ---- stop-loss -------------------------------------------------------

    def _compute_stop_loss_qty(self, position_qty: int) -> int:
        """How many contracts to sell on stop-loss trigger. Always respects
        the core floor — never sells contracts that would take the position
        below core_qty. Returns 0 when there's nothing sellable."""
        core = int(self.cfg.core_qty or 0)
        sellable_ceiling = max(0, position_qty - core)
        if sellable_ceiling == 0:
            return 0
        mode = (self.cfg.stop_loss_qty_mode or "all").lower()
        if mode == "all":
            return sellable_ceiling
        if mode == "original":
            # Fall back to swing_qty from config (the STARTING size, not the
            # possibly-scaled-up state.swing_qty). This is what "just the
            # original strategy contracts, let accumulated ride" means.
            return min(int(self.cfg.swing_qty or 0), sellable_ceiling)
        if mode == "custom":
            return min(max(0, int(self.cfg.stop_loss_qty_custom or 0)), sellable_ceiling)
        # Unknown mode = safest default (flatten). Beats silently ignoring the
        # protection the user turned on.
        return sellable_ceiling

    def _maybe_trigger_stop_loss(self, last_price: float) -> bool:
        """If stop-loss is enabled and price fell to/below the trigger, sell
        the configured qty at market and halt. Returns True when it fired
        (caller should stop stepping)."""
        if not getattr(self.cfg, "stop_loss_enabled", False):
            return False
        trigger = float(getattr(self.cfg, "stop_loss_px", 0.0) or 0.0)
        if trigger <= 0 or last_price > trigger:
            return False
        try:
            pos = int(self.b.position_qty() or 0)
        except Exception as e:
            self._record("stop_loss_read_position_failed", error=str(e))
            return False
        if pos <= 0:
            # Nothing to sell — just halt so we stop opening new positions
            # once the crash has already flattened us via some other path.
            self._halt(f"stop-loss triggered at {last_price} (price ≤ {trigger}) but position is 0")
            return True
        to_sell = self._compute_stop_loss_qty(pos)
        if to_sell <= 0:
            self._halt(
                f"stop-loss triggered at {last_price} (price ≤ {trigger}) but "
                f"core floor {self.cfg.core_qty} blocks the sell (pos={pos})"
            )
            return True
        try:
            source = getattr(self.b, "set_pending_source", None)
            if callable(source):
                source("stop_loss")
            oid = self.b.place_market("SELL", to_sell)
            self._record(
                "stop_loss_triggered",
                price=last_price, trigger=trigger, sold=to_sell,
                mode=self.cfg.stop_loss_qty_mode, order_id=oid,
                position_before=pos, position_after=pos - to_sell,
            )
            if self.notifier is not None:
                try:
                    from alerting import Priority
                    self.notifier.send(
                        "stop_loss_triggered",
                        f"symbol={self.symbol} price={last_price} sold={to_sell} @ market",
                        Priority.HIGH,
                    )
                except Exception:
                    pass
        except Exception as e:
            self._record("stop_loss_sell_failed", error=str(e), price=last_price, trigger=trigger)
        self._halt(f"stop-loss: sold {to_sell} @ market at {last_price} (trigger {trigger})")
        return True

    # ---- main loop -------------------------------------------------------

    def step(self, last_price: float) -> None:
        # Dashboard can request a full paper-state wipe. Consume BEFORE any
        # other work so a stale state doesn't try to run on the fresh account.
        self._maybe_consume_reset_intent()

        # Dashboard can request an unhalt via a resume intent. Consume it BEFORE
        # the HALTED early-return so a halted strategy can actually restart.
        self._maybe_consume_resume_intent()

        if self.s.state == State.HALTED:
            return

        # Kill switch is checked EVERY cycle — no arming, no fill processing.
        # We stop short of halting because the kill switch is meant to be
        # temporary; the strategy should resume when it clears.
        if self._kill_switch_active():
            self._record("kill_switch_pause", reason=self.ks.reason() if self.ks else None)
            return

        # Manual intent: dashboard may have queued a market order for us to
        # execute. Consume it BEFORE the strategy step so the state machine
        # sees the resulting position, not the pre-intent one.
        self._maybe_execute_intent()
        self._maybe_execute_cancel_intent()

        # Refresh config from store — dashboard edits take effect next cycle.
        cfg = self._load_config()
        self.cfg = cfg

        # Stop-loss fires BEFORE abort_below so we sell first, then halt.
        # abort_below on its own would halt while the position keeps bleeding.
        if self._maybe_trigger_stop_loss(last_price):
            return

        if self.s.state == State.ARMED_SELL and last_price >= self.cfg.abort_above:
            return self._halt(
                f"price {last_price} ran above abort_above {self.cfg.abort_above} while flat on swing"
            )
        if self.s.state == State.ARMED_BUY and last_price <= self.cfg.abort_below:
            return self._halt(
                f"price {last_price} fell below abort_below {self.cfg.abort_below} while holding swing"
            )

        self._ensure_armed(last_price)
        if self.s.live_order_id:
            st = self.b.order_status(self.s.live_order_id)
            self.s.filled_qty = st.get("filled_qty", 0)
            if st.get("status") == "FILLED" and self.s.filled_qty >= self.s.swing_qty:
                self._on_fill(fill_price=st.get("average_filled_price"))

        # Reload sleeve configs each tick — user may have added/removed sleeves
        # from the dashboard. Ensure state dict has entries for all configured.
        sleeves_cfg = self._load_sleeves_cfg()
        configured_ids = {sc.id for sc in sleeves_cfg}
        # Drop state for removed sleeves; add fresh state for new ones.
        for sid in list(self.s.sleeves.keys()):
            if sid not in configured_ids:
                # Cancel any live order first
                st_obj = self.s.sleeves[sid]
                if st_obj.live_order_id:
                    try: self.b.cancel(st_obj.live_order_id)
                    except Exception: pass
                del self.s.sleeves[sid]
        for sc in sleeves_cfg:
            if sc.id not in self.s.sleeves:
                self.s.sleeves[sc.id] = SleeveState(id=sc.id)

        # Run each additional sleeve's state machine independently.
        for sc in sleeves_cfg:
            self._sleeve_step(sc, self.s.sleeves[sc.id], last_price)

        self._save_state()

    def _sleeve_step(self, sc: SleeveConfig, ss: SleeveState, last_price: float) -> None:
        """Independent state machine for one additional sleeve. Shares broker,
        position, and floor guard with siblings and with the primary strategy."""
        if ss.state == SleeveStateEnum.HALTED:
            return

        # Track price for volatility signal & update HWM for ratcheting stop.
        self._sleeve_track_price(sc, last_price)
        if ss.state == SleeveStateEnum.ARMED_SELL:
            try:
                pos_now = int(self.b.position_qty() or 0)
            except Exception:
                pos_now = 0
            if pos_now >= sc.qty:
                if ss.stop_loss_hwm is None or last_price > ss.stop_loss_hwm:
                    ss.stop_loss_hwm = last_price

        # Per-sleeve stop-loss fires BEFORE the abort governor. May sell +
        # reanchor (keep trading at new level) or sell + set reentry_pending
        # (wait for volatility contraction) or sell + halt (fixed behavior).
        if self._maybe_trigger_sleeve_stop_loss(sc, ss, last_price):
            return

        # Volatility-contraction re-entry: after a stop set reentry_pending,
        # this fires the reanchor when the market has calmed enough.
        self._maybe_trigger_sleeve_reentry(sc, ss, last_price)

        # News blackout: pause new arms during scheduled high-uncertainty
        # windows (FOMC, CPI, NFP). Existing positions ride through unless
        # tier 3 (which halts, handled elsewhere).
        if self._sleeve_in_blackout(sc, ss):
            return

        # Abort governor uses the symbol-level bands.
        if ss.state == SleeveStateEnum.ARMED_SELL and last_price >= self.cfg.abort_above:
            return self._sleeve_halt(sc, ss, f"price {last_price} above abort_above {self.cfg.abort_above}")
        if ss.state == SleeveStateEnum.ARMED_BUY and last_price <= self.cfg.abort_below:
            return self._sleeve_halt(sc, ss, f"price {last_price} below abort_below {self.cfg.abort_below}")

        # Arm if no live order.
        if not ss.live_order_id:
            if ss.state == SleeveStateEnum.ARMED_SELL:
                # Floor guard: sum of all pending sells (primary + sleeves) + this sleeve
                # must not take the position below core_qty. Skipped when core_qty <= 0
                # (Lab tenant / paper account with no core to defend) so sleeves can short.
                pos = self.b.position_qty()
                pending = self._pending_sell_qty_excluding(sc.id)
                if not self._floor_ok(pos - pending, sc.qty):
                    # Transient — try again next tick when more contracts free up.
                    self._record(
                        "sleeve_arm_skipped",
                        sleeve_id=sc.id, sleeve_name=sc.name,
                        reason="insufficient contracts",
                        position=pos, other_pending=pending,
                        sleeve_qty=sc.qty, core_qty=self.cfg.core_qty,
                    )
                    return

                # Mode-specific arm price.
                # fixed_limit / percentage_swing: sell resting at sc.sell_px.
                # trailing_stop: wait for trigger, then track high water, place a
                #   sell one tick below current when pullback exceeds trail_distance.
                # hybrid: sell_px triggers a delay window; within the window a
                #   push through trail_activation_px flips to trailing, otherwise
                #   we market-sell when the delay expires.
                if sc.exit_mode == "trailing_stop":
                    if not ss.trail_armed:
                        if last_price < sc.trail_trigger:
                            return  # not at trigger yet — no order, just wait
                        ss.trail_armed = True
                        ss.trail_high_water_price = last_price
                    if last_price > ss.trail_high_water_price:
                        ss.trail_high_water_price = last_price
                    stop = ss.trail_high_water_price - sc.trail_distance
                    if last_price > stop:
                        return  # still trailing; don't fire yet
                    # Spec §5A minimum lock-in: refuse to fire if the projected
                    # net is below the sleeve's configured target. Keep trailing
                    # until HWM rises enough to lock in at least the target.
                    if not self._sleeve_lockin_ok(sc, ss, stop):
                        return
                    self._sleeve_market_sell(sc, ss, last_price, trail_exit=True)
                elif sc.exit_mode == "hybrid":
                    self._sleeve_hybrid_step(sc, ss, last_price)
                else:
                    ms_qty, ms_px = self._sleeve_ms_adjust(sc, ss, "SELL", sc.qty, sc.sell_px, last_price)
                    if ms_qty is None:
                        return  # microstructure gate said pause
                    self._sleeve_arm(sc, ss, "SELL", ms_qty, ms_px)
            else:  # ARMED_BUY
                # Auto-reanchor: if silver has run more than reanchor_threshold
                # above buy_px while we've been waiting, the buy target is stale
                # — silver isn't going to dip back down to fill it. Walk both
                # targets UP to bracket the current mark, preserving the spread.
                # Only fires in ARMED_BUY (we hold 0 of this sleeve's contracts,
                # so there's no cost basis to disturb). Reanchor once per event
                # to avoid oscillation on a slowly-rising tape.
                spread = sc.sell_px - sc.buy_px
                if spread > 0 and sc.reanchor_threshold > 0 \
                        and last_price - sc.buy_px > sc.reanchor_threshold:
                    new_buy_px = round(last_price - spread / 2, 3)
                    new_sell_px = round(last_price + spread / 2, 3)
                    self._reanchor_sleeve(sc, ss, new_buy_px, new_sell_px, last_price)
                    return  # next tick uses the new targets
                ms_qty, ms_px = self._sleeve_ms_adjust(sc, ss, "BUY", sc.qty, sc.buy_px, last_price)
                if ms_qty is None:
                    return  # microstructure gate said pause
                self._sleeve_arm(sc, ss, "BUY", ms_qty, ms_px)
            return

        # Poll the live order.
        st = self.b.order_status(ss.live_order_id)
        filled = st.get("filled_qty", 0) or 0
        status = st.get("status")
        if status == "FILLED" and filled >= sc.qty:
            self._sleeve_on_fill(sc, ss, st.get("average_filled_price"))
        elif status in ("CANCELLED", "EXPIRED", "UNKNOWN"):
            # Zombie order — most commonly a live_order_id persisted through a
            # restart while the broker was re-created (paper) or the exchange
            # cancelled after a timeout (live). Clear it so the state machine
            # can re-arm next tick instead of polling a dead id forever.
            self._record("sleeve_order_cleared",
                sleeve_id=sc.id, sleeve_name=sc.name,
                order_id=ss.live_order_id, status=status)
            ss.live_order_id = None
            ss.filled_qty = 0

    def _pending_sell_qty_excluding(self, exclude_sleeve_id: Optional[str]) -> int:
        """Total qty of SELL orders currently armed across the primary strategy
        and all sleeves EXCEPT the given one. Used by the floor guard so a
        sleeve considers the other outstanding sells when deciding if it can
        safely arm its own sell."""
        n = 0
        # Primary strategy: if armed sell with a live order, it's pending.
        if self.s.state == State.ARMED_SELL and self.s.live_order_id:
            n += int(self.s.swing_qty)
        for sid, ss in self.s.sleeves.items():
            if sid == exclude_sleeve_id: continue
            sc = next((c for c in self._load_sleeves_cfg() if c.id == sid), None)
            if sc is None: continue
            if ss.state == SleeveStateEnum.ARMED_SELL and ss.live_order_id:
                n += int(sc.qty)
        return n

    def _sleeve_lockin_ok(self, sc: SleeveConfig, ss: SleeveState, stop_price: float) -> bool:
        """Spec §5A minimum lock-in guard for trailing exits.

        The sleeve's target net is the round-trip P/L it was configured for:
          target_net = (sell_px - buy_px) × size × qty − fee_roundtrip × qty
        The projected NET if the trail fires at `stop_price`:
          net_at_stop = (stop_price - cost_basis) × size × qty − fee_roundtrip × qty
        Refuse to fire if net_at_stop < target_net. The trail keeps riding
        until HWM climbs enough that the projected net clears the target.
        """
        cs = self.cfg.contract_size
        fees = self.cfg.fee_per_contract_roundtrip * sc.qty
        # Sleeve's configured target: what the swing was designed to earn.
        target_net = (sc.sell_px - sc.buy_px) * cs * sc.qty - fees
        if target_net <= 0:
            return True  # weirdly configured — don't gate
        basis = ss.sell_entry_avg
        if basis is None:
            basis = self._sleeve_avg_entry(sc)
            if basis is not None:
                ss.sell_entry_avg = basis
        if basis is None:
            basis = float(sc.buy_px)  # last-resort fallback
        net_at_stop = (stop_price - basis) * cs * sc.qty - fees
        if net_at_stop < target_net:
            self._record(
                "sleeve_trail_lockin_skipped",
                sleeve_id=sc.id, sleeve_name=sc.name,
                stop=stop_price, cost_basis=basis,
                projected_net=net_at_stop, target_net=target_net,
            )
            return False
        return True

    def _sleeve_market_sell(self, sc: SleeveConfig, ss: SleeveState, last_price: float, trail_exit: bool = False, hybrid_timeout: bool = False) -> None:
        """Exit at market — the fill happens NOW, not at some limit price that
        the bid may never cross while price rolls over. In paper this fills at
        the current bid; live hits the exchange's market path. If the broker
        has no place_market, fall back to an aggressive limit that crosses."""
        # Anchor realized P/L on what THIS sleeve actually paid for the
        # contracts it's about to sell. Captured BEFORE the fill because
        # after the sell those lots are consumed.
        if ss.sell_entry_avg is None:
            ss.sell_entry_avg = self._sleeve_avg_entry(sc) or float(sc.buy_px)
        set_src = getattr(self.b, "set_pending_source", None)
        if callable(set_src):
            set_src("strategy", strategy_id=sc.id)
        place_market = getattr(self.b, "place_market", None)
        if callable(place_market):
            ss.live_order_id = self.b.place_market("SELL", sc.qty)
            self._record("sleeve_order_placed",
                sleeve_id=sc.id, sleeve_name=sc.name,
                side="SELL", qty=sc.qty, price=last_price,
                trail_exit=trail_exit, hybrid_timeout=hybrid_timeout,
                cost_basis=ss.sell_entry_avg, order_id=ss.live_order_id)
        else:
            tick = self.cfg.tick_size or 0.005
            aggressive_px = last_price - 10 * tick
            self._sleeve_arm(sc, ss, "SELL", sc.qty, aggressive_px)

    def _sleeve_hybrid_step(self, sc: SleeveConfig, ss: SleeveState, last_price: float) -> None:
        """Hybrid exit: sell_px triggers a delay window. Inside the window a
        cross of trail_activation_px flips to trailing (ride the breakout);
        otherwise the sleeve market-sells at the end of the window (took the
        swing at the target).

        Sub-states are encoded on SleeveState:
          hybrid_sell_triggered_ts is None   → waiting for price to reach sell_px
          hybrid_sell_triggered_ts set, trail_armed False → inside delay window
          trail_armed True                    → trailing engaged (rode breakout)
        """
        import time as _time
        # Stage 1: waiting for sell_px to be hit.
        if ss.hybrid_sell_triggered_ts is None:
            if last_price < sc.sell_px:
                return
            ss.hybrid_sell_triggered_ts = _time.time()
            self._record("sleeve_hybrid_triggered",
                sleeve_id=sc.id, sleeve_name=sc.name,
                sell_px=sc.sell_px, last_price=last_price,
                delay_secs=sc.hybrid_delay_secs,
                activation_px=sc.trail_activation_px)
            # Fall through so a tick that clears both sell_px AND activation_px
            # in the same instant can engage the trail immediately.

        # Stage 3: trail already engaged — normal trailing logic.
        if ss.trail_armed:
            if last_price > ss.trail_high_water_price:
                ss.trail_high_water_price = last_price
            stop = ss.trail_high_water_price - sc.trail_distance
            if last_price > stop:
                return
            # Spec §5A: hybrid → trailing inherits the min lock-in rule.
            if not self._sleeve_lockin_ok(sc, ss, stop):
                return
            self._sleeve_market_sell(sc, ss, last_price, trail_exit=True)
            return

        # Stage 2: inside the delay window.
        if last_price >= sc.trail_activation_px:
            # Real breakout — engage trail and let it ride.
            ss.trail_armed = True
            ss.trail_high_water_price = last_price
            self._record("sleeve_hybrid_trail_engaged",
                sleeve_id=sc.id, sleeve_name=sc.name,
                activation_px=sc.trail_activation_px, last_price=last_price)
            return

        elapsed = _time.time() - ss.hybrid_sell_triggered_ts
        if elapsed < sc.hybrid_delay_secs:
            return  # still watching — no order placed yet
        # Delay expired without a breakout — take the swing at market.
        self._record("sleeve_hybrid_timeout_selling",
            sleeve_id=sc.id, sleeve_name=sc.name,
            elapsed=elapsed, delay_secs=sc.hybrid_delay_secs,
            last_price=last_price)
        self._sleeve_market_sell(sc, ss, last_price, hybrid_timeout=True)

    def _sleeve_avg_entry(self, sc: SleeveConfig) -> Optional[float]:
        """Weighted-avg entry price of the contracts this sleeve OWNS, using
        the same FIFO allocation the dashboard shows: sleeve-tagged lots first,
        then unassigned lots FIFO after primary and prior sleeves get their share.
        Returns None if the broker doesn't expose lots or the sleeve owns nothing.
        """
        lots = getattr(self.b, "lots", None)
        if not lots:
            return None
        expanded = []
        for lot in sorted(lots, key=lambda l: getattr(l, "entry_ts", 0)):
            for _ in range(int(getattr(lot, "qty", 0) or 0)):
                expanded.append((float(getattr(lot, "entry_price", 0.0) or 0.0),
                                 getattr(lot, "strategy_id", None)))
        mine = [px for px, sid in expanded if sid == sc.id]
        unassigned = [px for px, sid in expanded if sid != sc.id]
        skip = int(self.cfg.swing_qty or 0)
        for other in self._load_sleeves_cfg():
            if other.id == sc.id:
                break
            skip += int(other.qty or 0)
        pool = unassigned[skip:]
        need = int(sc.qty) - len(mine)
        if need > 0:
            mine.extend(pool[:need])
        if not mine:
            return None
        return sum(mine) / len(mine)

    def _sleeve_arm(self, sc: SleeveConfig, ss: SleeveState, side: str, qty: int, price: float) -> None:
        # For SELL: capture cost basis of the contracts we're about to sell so
        # realized P/L on the fill uses the ACTUAL price paid, not sc.buy_px.
        if side == "SELL" and ss.sell_entry_avg is None:
            ss.sell_entry_avg = self._sleeve_avg_entry(sc) or float(sc.buy_px)
        set_src = getattr(self.b, "set_pending_source", None)
        if callable(set_src):
            set_src("strategy", strategy_id=sc.id)
        try:
            ss.live_order_id = self.b.place_limit(side, qty, price)
        except Exception as e:
            self._record("sleeve_arm_failed", sleeve_id=sc.id, error=str(e))
            return
        self._record(
            "sleeve_order_placed",
            sleeve_id=sc.id, sleeve_name=sc.name,
            side=side, qty=qty, price=price, order_id=ss.live_order_id,
            **({"cost_basis": ss.sell_entry_avg} if side == "SELL" else {}),
        )

    def _sleeve_on_fill(self, sc: SleeveConfig, ss: SleeveState, fill_price) -> None:
        self._record(
            "sleeve_order_filled",
            sleeve_id=sc.id, sleeve_name=sc.name,
            leg=ss.state.value, filled_qty=sc.qty,
            average_filled_price=fill_price,
        )
        ss.live_order_id = None
        half_fee = (self.cfg.fee_per_contract_roundtrip / 2.0) * sc.qty
        if ss.state == SleeveStateEnum.ARMED_SELL:
            # Sell fill = profit realization. Anchor on the actual FIFO cost
            # basis captured at arm time. This matches the position-row math:
            # you sold contracts you owned, realized P/L happens NOW.
            try: fill = float(fill_price) if fill_price is not None else 0.0
            except (TypeError, ValueError): fill = 0.0
            basis = float(ss.sell_entry_avg) if ss.sell_entry_avg is not None else float(sc.buy_px)
            gross = (fill - basis) * self.cfg.contract_size * sc.qty
            ss.realized_pnl += gross - half_fee
            ss.cycles += 1
            ss.last_sell_qty = sc.qty
            ss.last_sell_fill_price = fill if fill else None
            ss.sell_entry_avg = None  # cleared until next arm recomputes
            ss.own_avg_entry = None   # no longer holding own contracts
            ss.state = SleeveStateEnum.ARMED_BUY
            # Trail/hybrid sub-states reset here so the rebuy is a clean slate.
            ss.trail_armed = False
            ss.trail_high_water_price = 0.0
            ss.hybrid_sell_triggered_ts = None
            # Winning cycle completed → reset the consecutive-stop counter
            # (breaks any streak that was accumulating). Also clear the
            # ratcheting HWM — next cycle starts fresh at the new basis.
            ss.consecutive_stops = 0
            ss.stop_loss_hwm = None
            self._record(
                "sleeve_cycle_completed",
                sleeve_id=sc.id, sleeve_name=sc.name,
                cycles=ss.cycles,
                cost_basis=basis, fill_price=fill,
                gross=gross, fees=half_fee,
                realized_pnl_total=ss.realized_pnl,
            )
            # Per-sleeve accumulation. Grow this sleeve's qty (up to max_qty)
            # off its OWN banked profit — each sleeve compounds independently.
            self._maybe_scale_up_sleeve(sc, ss)
        else:
            # Buy-back re-arms the sleeve. Deduct the buy-side fee (round-trip
            # fees are split across both legs so this leg pays its share).
            ss.realized_pnl -= half_fee
            # Anchor the sleeve's own basis to the buy fill so subsequent
            # unrealized display reflects THIS sleeve's independent trading —
            # not the paper gain on lots it inherited from an existing position.
            try:
                ss.own_avg_entry = float(fill_price) if fill_price is not None else float(sc.buy_px)
            except (TypeError, ValueError):
                ss.own_avg_entry = float(sc.buy_px)
            ss.state = SleeveStateEnum.ARMED_SELL
            self._record(
                "sleeve_rebuy_completed",
                sleeve_id=sc.id, sleeve_name=sc.name,
                fill_price=fill_price, fees=half_fee,
                realized_pnl_total=ss.realized_pnl,
                own_avg_entry=ss.own_avg_entry,
            )

    def _sleeve_halt(self, sc: SleeveConfig, ss: SleeveState, reason: str) -> None:
        if ss.live_order_id:
            try: self.b.cancel(ss.live_order_id)
            except Exception: pass
            ss.live_order_id = None
        ss.state = SleeveStateEnum.HALTED
        ss.halt_reason = reason or "halted"
        self._record("sleeve_halted", sleeve_id=sc.id, sleeve_name=sc.name, reason=reason)

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
        half_fee = (self.cfg.fee_per_contract_roundtrip / 2.0) * self.s.swing_qty
        if self.s.state == State.ARMED_SELL:
            # Sell fill = profit realization. Anchor on the position's blended
            # avg entry (broker-tracked). This matches the sleeve semantics:
            # realize immediately, cycles++ on the sell, not on the buy-back.
            try: fill = float(fill_price) if fill_price is not None else 0.0
            except (TypeError, ValueError): fill = 0.0
            pos_avg = getattr(getattr(self.b, "position", None), "avg_entry", 0.0) or float(self.cfg.buy_px)
            gross = (fill - float(pos_avg)) * self.cfg.contract_size * self.s.swing_qty
            self.s.realized_pnl += gross - half_fee
            self.s.cycles += 1
            self.s.last_sell_qty = self.s.swing_qty
            self.s.last_sell_fill_price = fill if fill else None
            strat.on_sell_filled(self.s, self.cfg, fill_price or 0.0)
            self.s.state = State.ARMED_BUY
            # Trail state resets per cycle so the rebuy leg starts clean.
            self.s.trail_armed = False
            self.s.trail_high_water_price = 0.0
            self._record(
                "cycle_completed",
                cycles=self.s.cycles,
                gross=gross, fees=half_fee,
                cost_basis=pos_avg, fill_price=fill,
                realized_pnl_total=self.s.realized_pnl,
                swing_qty=self.s.swing_qty,
            )
        else:
            # Buy-back re-arms. Deduct the buy-side share of round-trip fees.
            self.s.realized_pnl -= half_fee
            added = self.s.swing_qty - self.s.last_sell_qty
            if added > 0:
                self.s.reserved_margin += added * self.cfg.margin_per_contract
            strat.on_buy_filled(self.s, self.cfg, fill_price or 0.0)
            self.s.state = State.ARMED_SELL
            self._record(
                "rebuy_completed",
                fill_price=fill_price, fees=half_fee,
                realized_pnl_total=self.s.realized_pnl,
                swing_qty=self.s.swing_qty,
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
        self.s.halt_reason = reason or None
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
