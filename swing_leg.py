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
    # [crew] Opt-in DEFENSIVE crash guard. When on, this sleeve flattens at
    # market the instant a toxic liquidation cascade runs against the long
    # (VPIN/OFI/Kyle/OBI + Lee-Mykland jump, via crash_guard.py) — faster than
    # the trailing stop for a gap-through. OFF by default: no behavior change
    # until you enable it per-sleeve. Flip-to-short is deferred (needs short exec).
    crash_guard_enabled: bool = False
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
        # [crew] Per-sleeve cascade-lifecycle observations (price + VPIN/OFI +
        # per-tick vol proxy) for the crash-guard re-entry gate. Only populated
        # when a sleeve has crash_guard_enabled — zero cost otherwise.
        self._sleeve_ms_history: dict = {}
        # [crew] Roll-awareness for the crash guard. Near a dated contract's
        # expiry the microstructure signals (VPIN/OFI + basis convergence +
        # thinning book) stop being reliable proxies for a liquidation cascade,
        # so we suppress the microstructure guard inside a blackout window to
        # avoid a false flatten on roll/convergence noise. The price-based
        # stop-loss / trailing stop / abort bands still protect, and Coinbase
        # auto-rolls the position. Hours come from env; 0 = disabled (default,
        # no behavior change). Expiry is cached to avoid a per-tick API call.
        import os as _os
        try:
            self._roll_guard_blackout_hours = float(
                _os.getenv("SWING_ROLL_GUARD_BLACKOUT_HOURS", "0") or 0)
        except (TypeError, ValueError):
            self._roll_guard_blackout_hours = 0.0
        self._roll_expiry_ts: Optional[float] = None
        self._roll_expiry_checked: float = 0.0
        # [crew] Last average-down light per sleeve (edge-trigger notify + dash).
        self._avg_down_light: dict = {}
        # [crew] Last entry-quality light per sleeve (edge-trigger notify + dash).
        self._entry_light: dict = {}

    def _snap_to_tick(self, price: float) -> float:
        """Snap a price to the product's tick_size. Coinbase rejects orders
        whose limit_price isn't a multiple of price_increment with
        INVALID_PRICE_PRECISION — that's what was silently killing every
        arm on 2-decimal-tick futures (e.g., oil at 0.01) while 3-decimal
        silver (0.005 tick) coincidentally worked. Round to nearest tick;
        the extra round(., 8) eats floating-point residue like
        0.29999999999 → 0.3.
        """
        tick = float(self.cfg.tick_size or 0.0)
        if tick <= 0 or price is None:
            return float(price or 0.0)
        return round(round(float(price) / tick) * tick, 8)

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
        # Adam 2026-07-15 fleet-wide rule: defensive against partial state
        # blocks. Older code required d["state"] — a hard KeyError that
        # stranded any sleeve whose state block was seeded with sleeves-only
        # (e.g., Option B scanner arm auto-seed). Now defaults to ARMED_SELL
        # (matches SwingState() default), so the primary state loads cleanly
        # even when only the sleeve sub-dict was pre-seeded.
        state = SwingState(
            state=State(d.get("state") or "ARMED_SELL"),
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
        # Primary swing: if the last known order filled while the bot was down,
        # credit the fill through the normal on_fill path (cycles++, realized,
        # state advance). Otherwise the sleeve stays stuck in the pre-fill
        # state forever — this is the exact bug that ate ZEC's 2026-07-12 cycle.
        credited_primary = None
        if self.s.live_order_id:
            st = self.b.order_status(self.s.live_order_id)
            status = st.get("status")
            if status == "FILLED":
                credited_primary = {"order_id": self.s.live_order_id,
                                    "avg_price": st.get("average_filled_price"),
                                    "filled_qty": st.get("filled_qty", 0)}
                self.s.filled_qty = st.get("filled_qty", 0) or self.s.swing_qty
                self._on_fill(st.get("average_filled_price"))
            elif status in ("CANCELLED", "EXPIRED", "UNKNOWN"):
                self.s.live_order_id = None
                self.s.filled_qty = st.get("filled_qty", 0)
        # Same sweep for sleeves — a live_order_id that persisted across a bot
        # restart (or a live-exchange cancel) points at nothing on the fresh
        # broker. FILLED → credit via _sleeve_on_fill (cycles++, realized,
        # state advance). CANCELLED/EXPIRED/UNKNOWN → clear only.
        sleeves_cfg_by_id = {c.id: c for c in self._load_sleeves_cfg()}
        cleared_sleeves = []
        credited_sleeves = []
        for sid, ss in self.s.sleeves.items():
            if not ss.live_order_id: continue
            st = self.b.order_status(ss.live_order_id)
            status = st.get("status")
            if status == "FILLED":
                sc = sleeves_cfg_by_id.get(sid)
                if sc is None:
                    # Config gone (sleeve removed while order was live). Best we
                    # can do is clear the id — the fill happened but there's no
                    # sleeve to credit it to.
                    cleared_sleeves.append((sid, ss.live_order_id, "FILLED_NO_CONFIG"))
                    ss.live_order_id = None
                    ss.filled_qty = 0
                else:
                    credited_sleeves.append((sid, ss.live_order_id,
                                             st.get("average_filled_price")))
                    ss.filled_qty = st.get("filled_qty", 0) or sc.qty
                    self._sleeve_on_fill(sc, ss, st.get("average_filled_price"))
            elif status in ("CANCELLED", "EXPIRED", "UNKNOWN"):
                # CRITICAL SAFETY (2026-07-15): partial fill / status mismatch
                # on startup — same class as tick-loop bug above. Credit any
                # reported fill_qty before clearing so the sleeve doesn't
                # come up stuck in the pre-fill state.
                partial_filled = st.get("filled_qty", 0) or 0
                if partial_filled > 0:
                    sc = sleeves_cfg_by_id.get(sid)
                    if sc is not None:
                        credited_sleeves.append((sid, ss.live_order_id,
                                                 st.get("average_filled_price")))
                        ss.filled_qty = partial_filled
                        self._sleeve_on_fill(sc, ss, st.get("average_filled_price"))
                cleared_sleeves.append((sid, ss.live_order_id, status))
                ss.live_order_id = None
                ss.filled_qty = 0
        # Adam 2026-07-15: also sweep resting_stop_oid — the ratchet-stop
        # can fire on Coinbase and the tick loop may drop the product from
        # active ticking (ZEC-style: pos→0 → not ticked → _maybe_credit_
        # resting_stop_fill never runs). Reconcile runs periodically from
        # live_runner regardless of tick activity, so it's the right hook
        # for the sweeper. Same code path as _maybe_credit_resting_stop_fill.
        import time as _time_recon
        credited_stops = []
        for sid, ss in self.s.sleeves.items():
            if not ss.resting_stop_oid:
                continue
            sc = sleeves_cfg_by_id.get(sid)
            if sc is None:
                # Sleeve config gone — best-effort clear the id
                ss.resting_stop_oid = None
                ss.resting_stop_px = None
                ss.resting_stop_stage = None
                continue
            try:
                st = self.b.order_status(ss.resting_stop_oid)
            except Exception:
                continue  # transient, retry next reconcile
            status = (st or {}).get("status")
            if status == "OPEN":
                continue  # still resting
            if status == "FILLED":
                try:
                    fill_price = float(st.get("average_filled_price") or 0)
                except Exception:
                    fill_price = 0.0
                filled_qty = int(st.get("filled_qty") or sc.qty or 1)
                own_avg = float(ss.own_avg_entry or 0)
                profit = 0.0
                if own_avg > 0 and fill_price > 0:
                    try:
                        contract_size = float((self.contract_spec_cache or {}).get("contract_size") or 1)
                    except Exception:
                        contract_size = 1.0
                    profit = (fill_price - own_avg) * filled_qty * contract_size
                credited_stops.append((sid, ss.resting_stop_oid, fill_price, profit))
                # Credit
                ss.realized_pnl = float(ss.realized_pnl or 0) + profit
                ss.cycles = int(ss.cycles or 0) + 1
                ss.last_sell_qty = filled_qty
                ss.last_sell_fill_price = fill_price
                try:
                    recent = list(ss.recent_cycle_pnls or [])
                    recent.append(profit)
                    if len(recent) > 20:
                        recent = recent[-20:]
                    ss.recent_cycle_pnls = recent
                except Exception:
                    pass
                ss.own_avg_entry = None
                ss.resting_stop_oid = None
                ss.resting_stop_px = None
                ss.resting_stop_stage = None
                ss.state = SleeveStateEnum.ARMED_BUY
                ss.armed_buy_since_ts = _time_recon.time()
            elif status in ("CANCELLED", "EXPIRED"):
                # External cancel — clear so _maintain_resting_stop places
                # a fresh one next tick (if the sleeve gets ticked at all)
                ss.resting_stop_oid = None
                ss.resting_stop_px = None
                ss.resting_stop_stage = None
        self._record(
            "reconciled",
            actual_position=pos,
            live_order_id=self.s.live_order_id,
            state=self.s.state.value,
            cleared_sleeves=cleared_sleeves,
            credited_sleeves=credited_sleeves,
            credited_primary=credited_primary,
            credited_stops=credited_stops if credited_stops else None,
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

    def _maybe_consume_sleeve_state_reset(self) -> None:
        """Consume a sleeve_state_reset_intent written by migration scripts.
        The intent shape:
          {"clear_hwm": True}                 # clear stop_loss_hwm on ALL sleeves
          {"clear_hwm": ["s1", "s2"]}         # clear only specific sleeve IDs
          {"clear_fields": ["stop_loss_hwm"]} # generic form (extend later)
        Applied to IN-MEMORY state so the next _save_state doesn't clobber the
        migration's Redis write. Cleared after apply."""
        if not hasattr(self.store, "get_intent"):
            return
        intent = None
        try:
            intent = self.store._get_scope(self.tenant_id, self.symbol, "sleeve_state_reset_intent")
        except Exception:
            return
        if not intent:
            return
        clear_hwm = intent.get("clear_hwm")
        if clear_hwm:
            target_ids = None if clear_hwm is True else set(clear_hwm)
            cleared = []
            for sid, ss in self.s.sleeves.items():
                if target_ids is not None and sid not in target_ids:
                    continue
                if ss.stop_loss_hwm is not None:
                    cleared.append((sid, ss.stop_loss_hwm))
                    ss.stop_loss_hwm = None
            if cleared:
                self._record(
                    "sleeve_state_reset_applied",
                    field="stop_loss_hwm",
                    cleared=[{"sleeve_id": sid, "prev_hwm": prev} for sid, prev in cleared],
                )
        # Clear the intent so it doesn't re-apply next tick.
        try:
            self.store._clear_scope(self.tenant_id, self.symbol, "sleeve_state_reset_intent")
        except Exception:
            pass

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
                # Auditor 2026-07-14 Tier 2 (a): reentry_reeval expire halts are
                # deliberate near-expiry exits, NOT safety halts to auto-recover.
                # Resuming them re-arms a buy that will just expire again next
                # tick. Skip; require the user to roll the contract + re-enable
                # the sleeve explicitly.
                import reentry_reeval as _rr
                if _rr.is_expire_halt(ss.halt_reason):
                    self._record("sleeve_resume_skipped_expire",
                                 sleeve_id=sid, halt_reason=ss.halt_reason)
                    continue
                # Restore whatever the sleeve was doing before the halt so a
                # sleeve that halted while ARMED_BUY (mid-cycle, holding no
                # contracts, waiting to rebuy) resumes as ARMED_BUY. Falling
                # back to ARMED_SELL — the old behavior — sold the position
                # AGAIN on every resume and drained OIL from 20 → 0.
                restored = ss.pre_halt_state or SleeveStateEnum.ARMED_SELL.value
                try:
                    ss.state = SleeveStateEnum(restored)
                except ValueError:
                    ss.state = SleeveStateEnum.ARMED_SELL
                ss.pre_halt_state = None
                ss.live_order_id = None
                ss.filled_qty = 0
                ss.halt_reason = None
                self._record("sleeve_resume", sleeve_id=sid, restored_to=ss.state.value)
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
            # [crew:#7] Fail CLOSED. This previously returned True, so a preview
            # API glitch silently DISABLED the fee sanity ceiling and let the arm
            # go through unchecked — exactly when a bad quote could make you
            # overpay. Skip this arm instead; the next tick retries once preview
            # works again. (A sustained outage pauses new arms, which is the safe
            # failure mode for a cost guard.)
            self._record("fee_gate_preview_failed", side=side, qty=qty, price=price, error=str(e))
            return False
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
        # Snap price to tick_size — Coinbase rejects off-tick prices with
        # INVALID_PRICE_PRECISION on 2-decimal-tick products (e.g., oil).
        price = self._snap_to_tick(price)
        if not self._fee_gate_ok(side, qty, price):
            return
        if self.s.live_order_id:
            # [crew:#3] Before cancelling the resting order to re-arm, check what
            # actually filled. Blindly cancelling + resetting filled_qty=0 (below)
            # silently ABANDONS any contracts that already filled — the bot's
            # belief then diverges from the real exchange position, which on a
            # leveraged futures account is how you drift into a margin surprise.
            try:
                _st = self.b.order_status(self.s.live_order_id)
            except Exception as e:
                # Can't confirm the order's fill state — do NOT cancel blindly.
                # Halt so a human reconciles rather than risking abandoned fills.
                return self._halt(
                    f"cannot read order {self.s.live_order_id} status before re-arm "
                    f"({type(e).__name__}: {e}) — halting to avoid abandoning a possible fill"
                )
            _filled = int(_st.get("filled_qty", 0) or 0)
            _status = _st.get("status")
            if _status == "FILLED" or (self.s.swing_qty > 0 and _filled >= self.s.swing_qty):
                # It actually filled — credit it through the normal path instead
                # of cancelling. Don't re-arm the same leg here; the next tick's
                # _ensure_armed places the correct next-leg order.
                self.s.filled_qty = _filled or self.s.swing_qty
                self._on_fill(fill_price=_st.get("average_filled_price"))
                return
            if _filled > 0:
                # PARTIAL fill: real contracts we must not silently drop. Halt
                # for human reconciliation (matches reconcile()'s policy: on a
                # mismatch, HALT — never silently correct).
                return self._halt(
                    f"partial fill on order {self.s.live_order_id}: "
                    f"{_filled}/{self.s.swing_qty} filled before re-arm — halting "
                    f"to avoid abandoning filled contracts"
                )
            # Unfilled → safe to cancel and re-arm at the new price.
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
        # [crew:#6] Don't ratchet to max size off ONE profit chunk. reserved_margin
        # only grows when the buy leg actually fills, so during an ARMED_BUY
        # trailing-wait `free` stays constant and this used to bump swing_qty on
        # EVERY tick until max_swing_qty. Require realized_pnl to have grown since
        # the last scale-up, so one banked profit adds at most one contract before
        # it's committed as margin on the next fill. (Safe: touches no P&L/margin
        # math — the sleeve twin decrements realized_pnl instead, which would
        # double-count against the primary's separate reserved_margin accounting.)
        last = getattr(self, "_last_scaleup_pnl", None)
        if last is not None and self.s.realized_pnl <= last:
            return
        if free >= need:
            self.s.swing_qty += 1
            self._last_scaleup_pnl = self.s.realized_pnl
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
        # Live-tenant safety cap: never sell more than the sleeve's own qty
        # regardless of what the config says. The user set this up to swing
        # 1–2 contracts, not to liquidate the whole holding when a stop
        # trips — "all" mode has been draining positions in bulk.
        if self.tenant_id.endswith("-live"):
            mode = "original"
        if mode == "original":
            # Use the sleeve's current qty (accumulated size). "Original" here
            # means "just this sleeve, not all your other holdings" — which is
            # what makes intuitive sense at the sleeve level.
            return min(int(sc.qty or 0), sellable_ceiling)
        if mode == "custom":
            return min(max(0, int(getattr(sc, "stop_loss_qty_custom", 0) or 0)), sellable_ceiling)
        return sellable_ceiling  # "all"

    def _sleeve_effective_stop(self, sc, ss) -> float:
        """Compute the effective stop-loss price by taking the max (tightest,
        highest-for-LONG) of three candidates:
          1. fixed_stop        — the configured stop_loss_px (base floor)
          2. ratchet_stop      — HWM − ratchet_distance, once activation crossed
          3. protect_realized  — cost_basis − (realized_pnl × frac) / (size × qty)
                                 caps loss on this cycle at frac of what the
                                 sleeve has already booked
        Whichever is highest wins. Always monotonic-up: once ratcheted or
        protect-realized-tightened, never drops on the same position."""
        fixed_stop = float(sc.stop_loss_px or 0.0)
        candidates = [fixed_stop]
        # Ratchet candidate
        if sc.stop_loss_ratchet_enabled \
                and ss.stop_loss_hwm is not None \
                and ss.own_avg_entry is not None:
            unrealized_per_contract = ss.stop_loss_hwm - float(ss.own_avg_entry)
            if unrealized_per_contract >= sc.stop_loss_ratchet_activation:
                candidates.append(float(ss.stop_loss_hwm) - float(sc.stop_loss_ratchet_distance))
        # Protect-realized candidate — only meaningful when the sleeve has
        # positive realized_pnl AND we know the cost basis of what we hold.
        if sc.stop_loss_protect_realized_enabled \
                and ss.own_avg_entry is not None \
                and float(ss.realized_pnl or 0.0) > 0 \
                and int(sc.qty) > 0:
            frac = float(sc.stop_loss_protect_realized_frac or 0.5)
            max_loss_dollars = float(ss.realized_pnl) * frac
            price_move = max_loss_dollars / (float(self.cfg.contract_size) * int(sc.qty))
            candidates.append(float(ss.own_avg_entry) - price_move)
        return max(candidates)

    def _stop_loss_globally_disabled(self) -> bool:
        """Adam-triggered dashboard toggle: pause ALL stop-loss triggers on
        this tenant without editing per-sleeve config. Used before market
        open to avoid whiplash stop-outs. Stored under a well-known control
        scope (same pattern as __account_kill_switch__)."""
        try:
            cfg = self.store.get_config(self.tenant_id, "__stop_loss_disabled__") or {}
            return bool(cfg.get("disabled"))
        except Exception:
            return False

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
        if self._stop_loss_globally_disabled():
            self._record("sleeve_stop_loss_skipped_globally_disabled",
                         sleeve_id=sc.id, price=last_price,
                         trigger=effective_stop)
            return False
        # Market-hours gate: even if the mark shows below the stop, don't
        # attempt to sell during a closed CFM session. The sell would fail,
        # sell_ok would guard against phantom halts, but we'd still burn
        # Coinbase API budget and log noise every tick. Only checks the
        # spec when we're about to fire — not every tick — so the cost
        # is bounded to actual stop-crossing events.
        try:
            spec = self.b.contract_spec() if hasattr(self.b, "contract_spec") else {}
            session_open = spec.get("session_open")
            if session_open is False:
                # Log once per firing attempt so post-mortem can see we
                # correctly declined to sell during closure.
                self._record("sleeve_stop_loss_skipped_closed_market",
                             sleeve_id=sc.id, price=last_price,
                             trigger=effective_stop)
                return False
        except Exception:
            pass  # broker unavailable → fall through to old behavior
        try:
            pos = int(self.b.position_qty() or 0)
        except Exception as e:
            self._record("sleeve_stop_loss_read_position_failed",
                         sleeve_id=sc.id, error=str(e))
            return False
        if pos <= 0:
            # Nothing to sell — sleeve is in ARMED_BUY (already sold, waiting
            # to rebuy) or otherwise flat. Stop-loss doesn't apply; skip
            # silently rather than halting so the cycle continues.
            return False
        to_sell = self._compute_sleeve_stop_loss_qty(sc, pos)
        if to_sell <= 0:
            self._sleeve_halt(sc, ss,
                              f"stop-loss at {last_price} (≤ {effective_stop}) but core floor "
                              f"{self.cfg.core_qty} blocks the sell (pos={pos})")
            return True
        # Adam 2026-07-15 CRITICAL: mutual exclusion with exchange resting stop.
        # If resting_stop_enabled=True, Coinbase already has a stop-limit sitting
        # at effective_stop. When mark crosses, IT fires — and if we ALSO
        # place_market SELL here, both flatten one contract each, taking a +1
        # LONG to -1 SHORT. CU 2026-07-15 12:34:34 double-fire race — problem-
        # scout found this bot-side path was NOT guarded, only the arm-new-order
        # path was. Same fix pattern as swing_leg.py:3653.
        if getattr(sc, "resting_stop_enabled", True) and ss.resting_stop_oid:
            self._record("sleeve_stop_loss_skipped_resting_stop_active",
                         sleeve_id=sc.id, price=last_price,
                         trigger=effective_stop,
                         resting_stop_oid=ss.resting_stop_oid,
                         resting_stop_px=ss.resting_stop_px)
            return False
        was_ratcheted = effective_stop > float(sc.stop_loss_px or 0.0)
        sell_ok = False
        try:
            source = getattr(self.b, "set_pending_source", None)
            if callable(source):
                source(f"sleeve_stop_loss:{sc.id}")
            oid = self.b.place_market("SELL", to_sell)
            sell_ok = True
            self._refresh_portfolio_after_fill()
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

        # If the market SELL didn't actually go through (exchange closed on the
        # weekend, broker rejected, network blip), the position is still held.
        # Do NOT increment consecutive_stops or wipe hwm/own_avg_entry — that
        # would falsely rack up "consecutive stops" without any sells, hit the
        # max-consecutive brake, and halt a sleeve whose position never moved.
        # Just bail out; the next tick will re-check and either the sell
        # succeeds (state advances) or the mark moved back above the stop
        # (nothing needed).
        if not sell_ok:
            return True

        # Post-trigger housekeeping — only when the sell actually fired.
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
            # 2026-07-15: use arm_level.pullback_buy_px (Chan OU + Connors)
            # instead of the naive last_price ± spread/2 formula. Same
            # helper as reentry_reeval + auto-refresh — unified expert
            # math everywhere. Fallback to the naive centering if
            # arm_level returns None (insufficient history).
            try:
                import arm_level as _al
                history = list(self._sleeve_price_history.get(sc.id, []) or [])
                expert_buy = _al.pullback_buy_px(
                    history, spread=spread, sold_price=float(last_price))
                if expert_buy is not None:
                    new_buy = self._snap_to_tick(float(expert_buy))
                    new_sell = self._snap_to_tick(new_buy + spread)
                else:
                    new_buy = self._snap_to_tick(last_price - spread / 2)
                    new_sell = self._snap_to_tick(last_price + spread / 2)
            except Exception:
                new_buy = self._snap_to_tick(last_price - spread / 2)
                new_sell = self._snap_to_tick(last_price + spread / 2)
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

    def _prepare_post_trail_wait(self, sc, ss) -> None:
        """Called at the moment a trail-based sell fires. If the sleeve is
        configured for post-trail re-entry gating (Flavor 3 or Stage-A-only),
        set the state machine into 'wait_volatility' so the next ARMED_BUY
        cycle refuses to re-arm until the wait conditions are satisfied.

        Captures the *current* recent range as the baseline — the wait is
        against contraction below (range × reentry_range_contraction), so a
        big pre-exit range = tolerating a bigger consolidation before
        deciding it's calm. No-op when the mode is 'off'."""
        if getattr(sc, "post_trail_reentry_mode", "off") == "off":
            return
        import time as _time
        ss.post_trail_stage = "wait_volatility"
        ss.post_trail_exit_ts = _time.time()
        ss.post_trail_pre_range = self._sleeve_recent_range(sc)
        ss.post_trail_stage_b_ts = None
        ss.post_trail_stage_b_ref_high = 0.0
        self._record(
            "sleeve_post_trail_wait_armed",
            sleeve_id=sc.id, sleeve_name=sc.name,
            mode=sc.post_trail_reentry_mode,
            pre_range=round(ss.post_trail_pre_range, 4),
        )

    def _sleeve_check_post_trail(self, sc, ss, last_price: float) -> bool:
        """Advance the post-trail re-entry state machine. Returns True if the
        sleeve should NOT arm this tick (still waiting for a stage to satisfy).
        Returns False when the wait is over (either satisfied or timed out),
        clearing the state to 'off' so the normal ARMED_BUY flow can proceed.

        Two-stage sequential ('sequential' mode):
          A: recent range ≤ pre_range × reentry_range_contraction, after
             at least reentry_min_wait_secs of elapsed time.
          B: last_price > post_trail_stage_b_ref_high (a NEW high above the
             price at the moment Stage A satisfied). Also fires on
             post_trail_stage_b_max_wait_secs timeout as a safety valve.

        Stage-A-only ('volatility' mode): completes after A satisfies.
        """
        stage = getattr(ss, "post_trail_stage", "off")
        if stage == "off":
            return False
        import time as _time
        now = _time.time()

        if stage == "wait_volatility":
            elapsed = now - float(ss.post_trail_exit_ts or now)
            min_wait = float(sc.reentry_min_wait_secs or 30.0)
            if elapsed < min_wait:
                return True
            pre_range = float(ss.post_trail_pre_range or 0.0)
            current_range = self._sleeve_recent_range(sc)
            # If we have no pre-exit baseline (edge case), fall back to
            # time-only after 5× the min wait so the sleeve doesn't stall.
            if pre_range <= 0:
                if elapsed < min_wait * 5:
                    return True
            else:
                target = pre_range * float(sc.reentry_range_contraction or 0.5)
                if current_range > target:
                    return True
            # Stage A satisfied.
            mode = getattr(sc, "post_trail_reentry_mode", "off")
            if mode == "volatility":
                ss.post_trail_stage = "off"
                ss.post_trail_exit_ts = None
                ss.post_trail_pre_range = 0.0
                self._record(
                    "sleeve_post_trail_wait_cleared",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    stage="A", mode="volatility",
                    elapsed_secs=round(elapsed, 1),
                    current_range=round(current_range, 4),
                )
                return False
            # Sequential → transition to Stage B, lock the reference high.
            ss.post_trail_stage = "wait_new_high"
            ss.post_trail_stage_b_ts = now
            ss.post_trail_stage_b_ref_high = float(last_price)
            self._record(
                "sleeve_post_trail_stage_a_satisfied",
                sleeve_id=sc.id, sleeve_name=sc.name,
                elapsed_secs=round(elapsed, 1),
                current_range=round(current_range, 4),
                stage_b_ref_high=round(float(last_price), 4),
            )
            return True

        if stage == "wait_new_high":
            stage_b_elapsed = now - float(ss.post_trail_stage_b_ts or now)
            max_wait = float(sc.post_trail_stage_b_max_wait_secs or 3600.0)
            if stage_b_elapsed >= max_wait > 0:
                self._record(
                    "sleeve_post_trail_stage_b_timeout",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    elapsed_secs=round(stage_b_elapsed, 1),
                    max_wait_secs=max_wait,
                    ref_high=round(float(ss.post_trail_stage_b_ref_high or 0.0), 4),
                )
                ss.post_trail_stage = "off"
                ss.post_trail_exit_ts = None
                ss.post_trail_pre_range = 0.0
                ss.post_trail_stage_b_ts = None
                ss.post_trail_stage_b_ref_high = 0.0
                return False
            ref = float(ss.post_trail_stage_b_ref_high or 0.0)
            if ref > 0 and last_price > ref:
                self._record(
                    "sleeve_post_trail_stage_b_satisfied",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    new_high=round(float(last_price), 4),
                    ref_high=round(ref, 4),
                    elapsed_secs=round(stage_b_elapsed, 1),
                )
                ss.post_trail_stage = "off"
                ss.post_trail_exit_ts = None
                ss.post_trail_pre_range = 0.0
                ss.post_trail_stage_b_ts = None
                ss.post_trail_stage_b_ref_high = 0.0
                return False
            return True

        return False

    def _trailing_buy_ready(self, sc, ss, last_price: float):
        """Falling-knife guard on the BUY leg. Returns the price at which
        to arm the buy NOW, or None to wait another tick.

        Semantics (mirror of trailing_stop but for entries):
          Phase 1  mark > sc.buy_px          → not yet dipped; wait
          Phase 2  mark <= sc.buy_px, first  → arm the trail, track low
          Phase 3  mark drops further        → update running low
          Phase 4  mark bounces >= low +     → confirm reversal → arm buy
                   sc.buy_trail_distance

        Expert canon (Livermore's pivot / Turtle breakout confirmation /
        Le Beau entry filter). The arm price is capped at sc.buy_px so
        we NEVER pay more than the original limit — even if a shallow
        dip bounces above buy_px, we cap and fall through to normal
        limit behavior at buy_px.

        Disabled path (buy_trail_enabled=False or distance<=0): returns
        sc.buy_px immediately — identical to the pre-existing behavior.
        """
        if not getattr(sc, "buy_trail_enabled", False):
            return sc.buy_px
        distance = float(getattr(sc, "buy_trail_distance", 0.0) or 0.0)
        if distance <= 0:
            return sc.buy_px

        # Once armed, we STAY armed until the bounce confirms. A brief recovery
        # above buy_px while armed IS a bounce confirmation — it means the
        # market went down and came back, which is exactly what we're waiting
        # for. Don't disarm on recovery; check the bounce first.
        if ss.buy_trail_armed:
            # Still falling — update the running low.
            if last_price < ss.buy_trail_low_water:
                ss.buy_trail_low_water = float(last_price)
                return None
            # Bounce confirmed? Fire at min(mark, buy_px) — cap so we never
            # overpay vs the original target.
            if last_price >= ss.buy_trail_low_water + distance:
                arm_price = min(float(last_price), float(sc.buy_px))
                self._record(
                    "buy_trail_bounce_confirmed",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    low_water=round(ss.buy_trail_low_water, 6),
                    last_price=round(float(last_price), 6),
                    arm_price=round(arm_price, 6),
                    trail_distance=distance,
                )
                ss.buy_trail_armed = False
                ss.buy_trail_low_water = 0.0
                return arm_price
            # Between low and low+distance — still waiting for confirmation.
            return None

        # Not yet armed: only arm once mark dips at/through buy_px.
        if last_price > sc.buy_px:
            return None

        ss.buy_trail_armed = True
        ss.buy_trail_low_water = float(last_price)
        self._record(
            "buy_trail_armed",
            sleeve_id=sc.id, sleeve_name=sc.name,
            buy_px=sc.buy_px,
            last_price=round(float(last_price), 6),
            trail_distance=distance,
        )
        return None

    def _sleeve_trend_ok_for_buy(self, sc, last_price: float) -> bool:
        """Trend gate on the BUY arm. Returns False (block the buy) when the
        filter is enabled AND last_price < the M-bar SMA of the sleeve's
        rolling price history. Turtle / Livermore rule: don't buy into a
        confirmed downtrend. If we don't have enough history yet, allow the
        buy — the filter should be permissive at cold start rather than
        stall the sleeve indefinitely."""
        if not getattr(sc, "entry_trend_filter_enabled", False):
            return True
        window = int(getattr(sc, "entry_trend_sma_window", 20) or 0)
        if window <= 0:
            return True
        history = self._sleeve_price_history.get(sc.id)
        if not history or len(history) < window:
            return True  # cold start — don't block
        recent = list(history)[-window:]
        sma = sum(recent) / len(recent)
        if last_price < sma:
            self._record(
                "sleeve_trend_gate_blocked",
                sleeve_id=sc.id, sleeve_name=sc.name,
                last_price=round(float(last_price), 4),
                sma=round(sma, 4), window=window,
            )
            return False
        return True

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

    def _parse_expiry(self, exp) -> Optional[float]:
        """Best-effort parse of a contract_expiry value (ISO-8601 str / epoch)
        into epoch seconds. Returns None on anything it can't read — the caller
        treats None as 'expiry unknown' and keeps the guard active."""
        if exp is None:
            return None
        try:
            if isinstance(exp, (int, float)):
                v = float(exp)
                return v / 1000.0 if v > 1e12 else v  # tolerate ms epochs
            s = str(exp).strip()
            if not s:
                return None
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None

    def _within_roll_blackout(self) -> bool:
        """True ONLY when we affirmatively know we're within
        SWING_ROLL_GUARD_BLACKOUT_HOURS of the contract's expiry. Fail-safe:
        unknown expiry, no broker spec, or hours<=0 all return False so the
        crash guard stays active — never weakening protection on missing data.
        contract_spec() is a live API call, so the expiry is cached and
        refreshed at most every ~15 minutes."""
        hours = getattr(self, "_roll_guard_blackout_hours", 0.0) or 0.0
        if hours <= 0:
            return False
        import time as _time
        now = _time.time()
        if now - float(getattr(self, "_roll_expiry_checked", 0.0)) >= 900:
            self._roll_expiry_checked = now
            try:
                spec_fn = getattr(self.b, "contract_spec", None)
                spec = spec_fn() if callable(spec_fn) else None
                self._roll_expiry_ts = self._parse_expiry((spec or {}).get("contract_expiry"))
            except Exception:
                pass  # keep last-known; unknown stays unknown
        ts = getattr(self, "_roll_expiry_ts", None)
        if not ts:
            return False
        secs_left = ts - now
        # Within the blackout window ahead of expiry. Guard against a stale
        # far-past timestamp (a wrongly-parsed old contract) firing forever.
        return -86400 < secs_left <= hours * 3600

    def _reversal_position_safe(self, sc, ss):
        """Guard for the OFFENSIVE reversal (flip long->short). Two rules, both
        Adam's, both fail-safe:
          1. NO UN-SLEEVED CONTRACTS. On Coinbase ONE-WAY netting the account
             holds a single net position, so a flip sells straight THROUGH any
             contracts the sleeves don't own — the protected core (core_qty) or
             manually-held / orphan contracts. Refuse if net position exceeds
             what the sleeves hold.
          2. ALL-OR-NOTHING. A reversal is refused unless EVERY sleeve holding
             contracts on this product has reversal enabled. If even one holding
             sleeve is not cleared to short, none may — never a partial short
             that nets against a sleeve that isn't supposed to be short.
        Returns (ok, reason). Any error -> (False, ...) so an accounting hiccup
        can never let a flip run over un-sleeved or not-cleared size."""
        try:
            core = int(getattr(self.cfg, "core_qty", 0) or 0)
            if core > 0:
                return False, f"protected core of {core} present — a reversal would sell the core"
            pos = int(self.b.position_qty() or 0)
            cfgs = {c.id: c for c in self._load_sleeves_cfg()}
            total_held = 0
            for sid, oss in self.s.sleeves.items():
                ocfg = cfgs.get(sid)
                if ocfg is None:
                    continue
                oheld = int(getattr(oss, "current_qty", 0) or 0)
                if oheld <= 0 and oss.state == SleeveStateEnum.ARMED_SELL:
                    oheld = int(getattr(ocfg, "qty", 0) or 0)
                if oheld <= 0:
                    continue
                total_held += oheld
                # ALL-OR-NOTHING: any holding sleeve without reversal on blocks ALL.
                if not getattr(ocfg, "reversal_enabled", False):
                    return False, (f"all-or-nothing: sleeve '{ocfg.name}' holds {oheld} "
                                   "with reversal OFF — no sleeve may short")
            if total_held <= 0:
                return False, "no sleeve holds anything to reverse"
            if pos > total_held:
                return False, (f"un-sleeved contracts present (net {pos} > sleeve-held {total_held}) "
                               "— a reversal would net against them")
            return True, ""
        except Exception as e:
            return False, f"reversal safety check failed: {e}"

    def _maybe_entry_quality_alert(self, sc, ss, last_price: float) -> None:
        """[crew] Entry-quality GREEN LIGHT — notification only, never executes.
        Fires only while WAITING to buy (ARMED_BUY), when the sleeve opts in.
        Scores the moment via scanner_signals.entry_assessment (regime + channel
        + microstructure) and, edge-triggered, records the light + pings on green
        (a clean trend or a calm swing near support). Red = chop / toxic flow /
        crash. Opt-in (entry_quality_alert_enabled); OFF by default; fail-safe."""
        if not getattr(sc, "entry_quality_alert_enabled", False):
            return
        if ss.state != SleeveStateEnum.ARMED_BUY:   # only while waiting to enter
            return
        try:
            import scanner_signals
            prices = list(self._sleeve_price_history.get(sc.id, []) or [])
            if len(prices) < 24:
                return
            # 2026-07-15 fix: filter out None prices to prevent float(None)
            # errors inside scanner_signals.entry_assessment.
            candles = [{"close": float(p)} for p in prices if p is not None]
            if len(candles) < 24:
                return
            ms_snap = self.ms.snapshot() if self.ms else {}
            # 2026-07-15 fix: coerce ofi to a real float (default 0.0) so
            # entry_assessment's downstream float() calls don't blow up
            # when the microstructure snapshot lacks OFI data.
            raw_ofi = (ms_snap or {}).get("trade_ofi_60s") or (ms_snap or {}).get("ofi")
            ofi = 0.0
            if raw_ofi is not None:
                try:
                    ofi = float(raw_ofi)
                except (TypeError, ValueError):
                    pass
            a = scanner_signals.entry_assessment(candles, ms=ms_snap, ofi=ofi)
            rec = a.get("recommendation")
            if rec in ("TREND-ENTER", "SWING-OK"):
                light = "green"
            elif rec in ("AVOID", "CASCADE-SHORT"):
                light = "red"
            else:
                light = "amber"
            if light != self._entry_light.get(sc.id):   # edge-triggered
                self._entry_light[sc.id] = light
                # 2026-07-15 fix: don't pass symbol=self.symbol explicitly —
                # _record() already auto-adds tenant + symbol. Duplicate
                # caused "multiple values for keyword argument 'symbol'".
                self._record("entry_quality_light", sleeve_id=sc.id, sleeve_name=sc.name,
                             light=light, recommendation=rec,
                             entry_quality=a.get("entry_quality"), regime=a.get("regime"),
                             reason=a.get("reason"))
                if light == "green":
                    try:
                        self._notify(f"ENTRY-OK: {self.symbol} / {sc.name}",
                                    f"{rec}: {a.get('reason', '')}", Priority.HIGH)
                    except Exception:
                        pass
        except Exception as e:
            # Adam 2026-07-15: silence repeated "float() argument must be a
            # string or a real number, not 'NoneType'" errors that fire every
            # 5-10s per ARMED_BUY sleeve when a scanner_signals field is None.
            # Alert is opt-in + notification-only — swallow the error but
            # rate-limit the log emission so genuine issues still surface.
            err_msg = str(e)
            key = f"eqa_{sc.id}"
            now_s = int(getattr(self, "_entry_quality_last_err_ts", {}).get(key, 0) or 0)
            import time as _t_eqa
            cur = int(_t_eqa.time())
            if cur - now_s > 300:  # emit at most every 5 min per sleeve
                self._record("entry_quality_alert_error",
                             sleeve_id=sc.id, error=err_msg)
                store = getattr(self, "_entry_quality_last_err_ts", None)
                if store is None:
                    self._entry_quality_last_err_ts = {}
                self._entry_quality_last_err_ts[key] = cur

    def _have_margin_for_one(self, sc) -> bool:
        """Best-effort: is there margin headroom to add ONE more contract?
        Advisory only — on unknown/error returns True (don't block the signal;
        the human sees their own margin)."""
        try:
            fb = self.b.futures_balance() if hasattr(self.b, "futures_balance") else {}
            avail = None
            for k in ("available_margin", "available_balance", "buying_power",
                      "cbi_usd_balance", "futures_buying_power"):
                v = (fb or {}).get(k)
                if isinstance(v, dict):
                    v = v.get("value")
                if v is not None:
                    try:
                        avail = float(v); break
                    except (TypeError, ValueError):
                        pass
            if avail is None:
                return True
            need = float(getattr(self.cfg, "margin_per_contract", 0) or 0) * int(getattr(sc, "qty", 1) or 1)
            return avail >= need
        except Exception:
            return True

    def _maybe_avg_down_alert(self, sc, ss, last_price: float) -> None:
        """[crew] Average-down GREEN LIGHT — notification only, never executes.
        Fires only while HOLDING an underwater long, when the sleeve opts in.
        Computes avg_down_signal and, edge-triggered, records the light + pings
        on green. Opt-in (avg_down_alert_enabled); OFF by default; fail-safe."""
        if not getattr(sc, "avg_down_alert_enabled", False):
            return
        if ss.state != SleeveStateEnum.ARMED_SELL:   # only while holding a long
            return
        try:
            avg = ss.own_avg_entry
            if avg is None or float(last_price) >= float(avg):   # only underwater
                return
            import avg_down_signal
            prices = list(self._sleeve_price_history.get(sc.id, []) or [])
            if len(prices) < 24:
                return
            ms_snap = self.ms.snapshot() if self.ms else {}
            sig = avg_down_signal.average_down_signal(
                prices, ms=ms_snap, position_avg=float(avg), last_price=float(last_price),
                have_margin=self._have_margin_for_one(sc))
            light = sig.get("light")
            if light != self._avg_down_light.get(sc.id):   # edge-triggered
                self._avg_down_light[sc.id] = light
                # 2026-07-15 fix: symbol=self.symbol removed — _record()
                # auto-adds it. Duplicate caused RedisTradeLog.record()
                # "multiple values for keyword argument 'symbol'".
                self._record("avg_down_light", sleeve_id=sc.id, sleeve_name=sc.name,
                             light=light, reason=(sig.get("reasons") or [""])[0],
                             checks=sig.get("checks"))
                if light == "green":
                    try:
                        self._notify(f"AVG-DOWN GREEN: {self.symbol} / {sc.name}",
                                     (sig.get("reasons") or [""])[0], Priority.HIGH)
                    except Exception:
                        pass
        except Exception as e:
            self._record("avg_down_alert_error", sleeve_id=sc.id, error=str(e))

    def _reentry_mode(self) -> str:
        """Read the __reentry_mode__ scope for this tenant. Returns one of
        'expert' (execute the reeval decision), 'shadow' (compute + log the
        decision only — NEVER touch the broker), or 'legacy' (default).
        Fail-safe: any store error → 'legacy'."""
        try:
            m = (self.store.get_state(self.tenant_id, "__reentry_mode__") or {})
            return str(m.get("mode") or "legacy").lower()
        except Exception:
            return "legacy"

    # [crew 2026-07-15] auto-refresh cadence config
    _AUTO_REFRESH_MIN_INTERVAL_SECS = 60.0      # don't fire more than once/min per sleeve
    _AUTO_REFRESH_MIN_DRIFT_PCT = 0.5           # skip if new buy_px within 0.5% of current
    _AUTO_REFRESH_STALE_AFTER_SECS = 1200.0     # only refresh if armed > 20 min
    _AUTO_REFRESH_MIN_HISTORY = 30              # need >=30 price history entries

    def _backfill_sleeve_history_from_coinbase(self, sc) -> int:
        """Populate `_sleeve_price_history[sc.id]` with recent closes fetched
        from Coinbase's candles endpoint, so gated features (auto-refresh,
        reentry_reeval, experts_reentry) can fire immediately after process
        restart without waiting for live ticks to refill the in-memory deque.

        Adam 2026-07-15: "why do we need to wait since we have the historical
        data?" Right — the tick loop takes 20-30 min to refill 30 entries
        for thinly-traded products (XLP), but Coinbase has the history sitting
        there. This helper closes that gap.

        Returns the number of prices backfilled (0 on failure). Safe to call
        even if history already has entries — appends only new closes past
        what's already in the deque.
        """
        try:
            from collections import deque as _deque
            import time as _t
            # Ensure the deque exists
            if sc.id not in self._sleeve_price_history:
                window = int(getattr(sc, "reentry_range_window", 60) or 60) * 4
                self._sleeve_price_history[sc.id] = _deque(maxlen=window)
            ph = self._sleeve_price_history[sc.id]
            if len(ph) >= ph.maxlen // 2:
                return 0  # already has plenty
            # Fetch last 4 hours of 5-min candles from broker
            end = int(_t.time())
            start = end - (4 * 3600)
            resp = self.b.client.get_candles(
                product_id=self.symbol,
                start=str(start),
                end=str(end),
                granularity="FIVE_MINUTE",
            )
            candles = getattr(resp, "candles", None) or resp.get("candles", [])
            closes = []
            for c in candles:
                close = c.get("close") if isinstance(c, dict) else getattr(c, "close", None)
                if close is not None:
                    try:
                        closes.append(float(close))
                    except (TypeError, ValueError):
                        pass
            closes.reverse()  # Coinbase returns newest-first
            # Only backfill if we got a meaningful number
            if len(closes) < 5:
                return 0
            # Prepend to deque so live ticks continue appending on top
            existing = list(ph)
            ph.clear()
            for c in closes:
                ph.append(c)
            for e in existing:
                ph.append(e)
            self._record("sleeve_history_backfilled",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         closes_fetched=len(closes),
                         source="coinbase.get_candles(5m, last 4h)")
            return len(closes)
        except Exception as e:
            self._record("sleeve_history_backfill_error",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         error=str(e))
            return 0

    # [crew 2026-07-15] Stop-loss auto-refresh config
    _STOP_AUTO_REFRESH_INTERVAL_SECS = 60.0    # throttle to 1/min per sleeve
    _STOP_AUTO_REFRESH_MIN_DRIFT_PCT = 5.0     # skip if new stop within 5% of current
    _STOP_AUTO_REFRESH_MIN_HISTORY = 30        # need >=30 bars for ATR

    def _maybe_auto_refresh_stop_loss(self, sc, ss, last_price: float) -> None:
        """Auto-refresh stop_loss_px against current ATR-derived expert
        distance. Fires for sleeves with stop_loss_enabled=True, both
        WAITING (ARMED_BUY) and HELD (ARMED_SELL) states.

        Formula (matches sleeve editor's `applyExpertCanonToForm`):
            new_stop_px = current_price - (expertATR × stop_x_atr)

        expertATR is estimated from the sleeve's own price history
        (rolling std × sqrt(period) proxy for ATR when we don't have
        the tile ATR directly available in the tick loop).

        Safety guards:
          1. Never move stop_loss_px ABOVE current_price (would insta-trigger)
          2. Never move stop_loss_px UP by more than 3% of current price
             per refresh (avoid abrupt tightening)
          3. Only fires if stop_loss_enabled=True on the sleeve
          4. Throttled to once/minute per sleeve (cadence gate)
          5. Skips if drift < 5% of current price (avoid churn)
          6. Skips sleeves with anchor_type=your_contract_avg (Option B —
             defensive sleeves are intentionally static)

        Aligned with north-star rule (maximize profit × cycles): a stop
        sized to CURRENT vol prevents premature stop-outs in high-vol
        regimes AND locks in more profit in low-vol regimes. Both help
        cycle profitability.
        """
        import time as _t
        now = _t.time()

        # Gate 1: stop-loss must be enabled
        if not getattr(sc, "stop_loss_enabled", False):
            return
        try:
            current_stop = float(sc.stop_loss_px or 0)
        except (TypeError, ValueError):
            return
        if current_stop <= 0:
            return

        # Gate 2: Option B anchor-aware skip
        anchor = str(getattr(sc, "anchor_type", "current_market")).lower()
        if anchor == "your_contract_avg":
            return

        # Gate 3: cadence throttle
        last_refresh = float(getattr(ss, "_last_stop_refresh_ts", 0.0) or 0.0)
        if last_refresh and (now - last_refresh) < self._STOP_AUTO_REFRESH_INTERVAL_SECS:
            return

        # Gate 4: sufficient history for ATR estimation
        history = list(self._sleeve_price_history.get(sc.id, []) or [])
        if len(history) < self._STOP_AUTO_REFRESH_MIN_HISTORY:
            # Backfill attempt (same as buy_px auto-refresh)
            backfilled = self._backfill_sleeve_history_from_coinbase(sc)
            if backfilled > 0:
                history = list(self._sleeve_price_history.get(sc.id, []) or [])
            if len(history) < self._STOP_AUTO_REFRESH_MIN_HISTORY:
                ss._last_stop_refresh_ts = now
                return

        # Compute ATR estimate from history: mean(|delta|) is a reasonable
        # proxy for 1-bar ATR when we don't have OHLC. Wilder ATR is more
        # rigorous but requires H/L/C — we only have closes here.
        deltas = [abs(history[i] - history[i - 1]) for i in range(1, len(history))]
        if not deltas:
            ss._last_stop_refresh_ts = now
            return
        # ATR-14-ish: average of last 14 deltas (or all if fewer)
        recent_deltas = deltas[-14:]
        atr_est = sum(recent_deltas) / len(recent_deltas)
        if atr_est <= 0:
            ss._last_stop_refresh_ts = now
            return

        # Multiplier: derived from asset class if we can determine it,
        # else fall back to a reasonable default matching the tile logic.
        # 2.0×ATR is the Turtle canonical; 2.5-3.0× is common for
        # crypto/volatile assets.
        stop_x_atr = 2.5  # default; matches tile's crypto class ballpark
        try:
            import expert_params
            asset_class = expert_params.asset_class_for(self.symbol) if hasattr(
                expert_params, "asset_class_for") else None
            if asset_class:
                params = expert_params.params_for_class(asset_class) if hasattr(
                    expert_params, "params_for_class") else {}
                if params and "stop_x_atr" in params:
                    stop_x_atr = float(params["stop_x_atr"])
        except Exception:
            pass  # fallback default is fine

        new_stop_px = float(last_price) - (atr_est * stop_x_atr)
        try:
            new_stop_px = self._snap_to_tick(new_stop_px)
        except Exception:
            pass

        # Safety guard 1: never above current price
        if new_stop_px >= float(last_price):
            ss._last_stop_refresh_ts = now
            return

        # Safety guard 2: never tighten by more than 3% of current price in
        # one refresh (avoid abrupt stops)
        max_tighten = float(last_price) * 0.03
        if new_stop_px > current_stop:  # tightening (moving stop up)
            if (new_stop_px - current_stop) > max_tighten:
                new_stop_px = current_stop + max_tighten
                try:
                    new_stop_px = self._snap_to_tick(new_stop_px)
                except Exception:
                    pass

        # Gate 5: min drift — skip if new stop is within 5% of current
        drift_pct = abs(new_stop_px - current_stop) / max(abs(current_stop), 1e-9) * 100
        if drift_pct < self._STOP_AUTO_REFRESH_MIN_DRIFT_PCT:
            ss._last_stop_refresh_ts = now
            return

        # Persist: update in-memory + store
        old_stop = current_stop
        sc.stop_loss_px = float(new_stop_px)
        try:
            cfg = self.store.get_config(self.tenant_id, self.symbol) or {}
            sleeves = list(cfg.get("sleeves") or [])
            for s in sleeves:
                if s.get("id") == sc.id:
                    s["stop_loss_px"] = float(new_stop_px)
                    break
            cfg["sleeves"] = sleeves
            self.store.put_config(self.tenant_id, self.symbol, cfg)
        except Exception as e:
            self._record("stop_auto_refresh_persist_error",
                         sleeve_id=sc.id, error=str(e))
            return

        ss._last_stop_refresh_ts = now
        self._record(
            "sleeve_stop_auto_refresh",
            sleeve_id=sc.id, sleeve_name=sc.name,
            old_stop_px=old_stop, new_stop_px=new_stop_px,
            atr_estimate=round(atr_est, 6),
            stop_x_atr=stop_x_atr,
            current_market=float(last_price),
            drift_pct=round(drift_pct, 3),
            source="mean-abs-delta × class stop_x_atr",
        )

    # [crew 2026-07-15] Ghost force-arm config
    _GHOST_ARM_MIN_ARMED_SECS = 60.0      # only fire on sleeves armed >60s (give normal path a chance)
    _GHOST_ARM_INTERVAL_SECS = 60.0       # throttle: once per minute per sleeve

    def _maybe_force_arm_ghost_order(self, sc, ss) -> None:
        """Detect and revive ghost sleeves (state=ARMED_BUY/SELL with
        live_order_id=None) by placing the missing order at Coinbase.

        Same logic as diag_force_arm_missing_orders.py, but runs on the
        tick loop so ghosts never linger more than ~60s. Adam's north-
        star rule (maximize profit × cycles): every minute a sleeve is
        ghosted is a minute of lost cycle potential.

        Five gates prevent inappropriate placement:
          1. State must be ARMED_BUY or ARMED_SELL
          2. live_order_id must be None (ghost)
          3. Sleeve must have been armed >60s (give normal path a chance)
          4. Cadence throttle: once/min per sleeve
          5. buy_px/sell_px must be > 0
        """
        import time as _t
        now = _t.time()

        # Gate 1: must be armed state
        try:
            state_val = str(ss.state.value if hasattr(ss.state, "value") else ss.state).upper()
        except Exception:
            return
        if state_val not in ("ARMED_BUY", "ARMED_SELL"):
            return

        # Gate 2: must be a ghost (no live order)
        if ss.live_order_id:
            return

        # Gate 3: only fire if armed >60s AND we know when it was armed.
        # If armed_since_ts is missing/0, the sleeve was JUST armed by an
        # upstream transition that hasn't stamped a timestamp yet — the
        # normal arm path is about to run in this same tick. Skip and let
        # it work. This prevents the ghost force-arm from racing normal
        # transitions (e.g., reanchor + arm on the same step).
        ts_field = "armed_buy_since_ts" if state_val == "ARMED_BUY" else "armed_sell_since_ts"
        armed_ts = 0.0
        try:
            armed_ts = float(getattr(ss, ts_field, 0) or 0)
        except (TypeError, ValueError):
            pass
        if armed_ts <= 0:
            return   # unknown arm time — assume freshly armed, skip
        if (now - armed_ts) < self._GHOST_ARM_MIN_ARMED_SECS:
            return

        # Gate 4: cadence throttle
        last_arm_ts = float(getattr(ss, "_last_ghost_arm_ts", 0.0) or 0.0)
        if last_arm_ts and (now - last_arm_ts) < self._GHOST_ARM_INTERVAL_SECS:
            return

        # Gate 5: price + qty must be valid
        try:
            if state_val == "ARMED_BUY":
                side = "BUY"
                price = float(sc.buy_px or 0)
            else:
                side = "SELL"
                price = float(sc.sell_px or 0)
            qty = int(sc.qty or 0)
        except (TypeError, ValueError):
            return
        if price <= 0 or qty <= 0:
            return

        # Snap price to tick_size before placing (avoid INVALID_PRICE_PRECISION)
        try:
            snapped_px = self._snap_to_tick(price)
        except Exception:
            snapped_px = price

        # Idempotency: re-check live_order_id (a race with the normal path
        # could have placed since our gate check above)
        if ss.live_order_id:
            return

        # CRITICAL SAFETY (2026-07-15): don't over-accumulate. Multi-sleeve
        # setups are legit — Adam runs multiple sleeves on ZEC (Model B +
        # Custom × 2 = 3 contracts total is CORRECT, not an accumulation
        # bug). The check must compare against SUM of all sleeves' intended
        # qty + tenant core_qty, not just this individual sleeve.
        if side == "BUY":
            try:
                current_pos = int(self.b.position_qty() or 0)
                # Sum every sleeve on this product's qty
                total_sleeve_qty = 0
                for other_ss in (self.s.sleeves or {}).values():
                    other_sc = self._sleeve_cfg_by_id(other_ss.id) if hasattr(
                        self, "_sleeve_cfg_by_id") else None
                    if other_sc is None:
                        # Fallback: assume every armed sleeve wants its own qty
                        # (this sleeve's qty as best estimate)
                        total_sleeve_qty += int(getattr(sc, "qty", 1) or 1)
                    else:
                        total_sleeve_qty += int(getattr(other_sc, "qty", 1) or 1)
                intended_position = total_sleeve_qty + int(
                    getattr(self.cfg, "core_qty", 0) or 0)
                if current_pos >= intended_position:
                    self._record(
                        "ghost_arm_skipped_position_full",
                        sleeve_id=sc.id, sleeve_name=sc.name,
                        current_position=current_pos,
                        intended_position=intended_position,
                        total_sleeve_qty=total_sleeve_qty,
                        core_qty=int(getattr(self.cfg, "core_qty", 0) or 0),
                        reason="portfolio position >= sum(all sleeve qtys) + core; ghost arm would over-accumulate",
                    )
                    ss._last_ghost_arm_ts = now
                    return
            except Exception as e:
                # If we can't check position, be conservative — don't place
                self._record("ghost_arm_position_check_failed",
                             sleeve_id=sc.id, error=str(e))
                ss._last_ghost_arm_ts = now
                return

        # Place the order
        try:
            oid = self.b.place_limit(side, qty, snapped_px)
        except Exception as e:
            self._record("ghost_arm_place_failed",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         side=side, price=snapped_px, error=str(e))
            ss._last_ghost_arm_ts = now
            return

        # place_limit returns the order_id as a plain string
        if not oid or not isinstance(oid, str):
            self._record("ghost_arm_place_no_id",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         side=side, price=snapped_px,
                         returned_type=type(oid).__name__,
                         returned_val=str(oid)[:80])
            ss._last_ghost_arm_ts = now
            return

        # Update sleeve state with the new order_id
        ss.live_order_id = oid
        ss._last_ghost_arm_ts = now
        self._record(
            "sleeve_ghost_armed",
            sleeve_id=sc.id, sleeve_name=sc.name,
            side=side, price=snapped_px, qty=qty,
            order_id=oid,
            armed_hours_ago=round((now - armed_ts) / 3600, 2) if armed_ts > 0 else None,
            reason="normal arm path failed; ghost detected and force-armed on tick loop",
        )

    def _maybe_auto_refresh_stale_sleeve(self, sc, ss, last_price: float) -> None:
        """Universal Level 2 auto-refresh — for any sleeve in ARMED_BUY
        WITHOUT a live order (i.e., waiting to arm but no order placed
        yet), periodically re-derive buy_px/sell_px from the CURRENT
        expert stack (arm_level.pullback_buy_px, backed by Chan OU +
        Connors on current price history).

        Anchored on CURRENT market price, NOT ss.last_sell_fill_price —
        the latter can be ancient (months old) and traps sleeves in
        "waiting to rebuy below a price that will never come" (ZEC case,
        confirmed 2026-07-15 diag: ancient sold_ref $517.55 blocking a
        sleeve when current market is $556).

        Preserves the sleeve's current spread. If refresh would materially
        move buy_px, calls _reanchor_sleeve which persists to store +
        logs a sleeve_reanchored event with old/new prices for audit.

        Additional guardrail: only fires if elapsed time since armed_at
        exceeds SWING_STALE_AFTER_SECS (default 20 min — same window as
        reentry_reeval's stale_after_bars). Prevents thrashing on
        freshly-armed sleeves.

        See project_auto_refresh_design_decisions.md for design context
        (Option A universal ship; Option B anchor-aware follow-up needs
        SleeveConfig.anchor_type schema field).
        """
        # Gate 1: must be ARMED_BUY without a live order (reentry_reeval
        # handles the WITH-live-order case via cancel-replace).
        if ss.state != SleeveStateEnum.ARMED_BUY:
            return
        if ss.live_order_id:
            return

        # Gate 1a — Option B anchor-aware skip: defensive sleeves anchored
        # to Your Contract Avg are intentionally static (protecting cost
        # basis). Auto-refresh would drift them off the cost anchor over
        # time, defeating the "protect the core" purpose. Only refresh
        # sleeves anchored to current_market / custom / strategy_entry.
        anchor = str(getattr(sc, "anchor_type", "current_market")).lower()
        if anchor == "your_contract_avg":
            return

        # Gate 2: staleness — don't fire on freshly-armed sleeves.
        import time as _t
        now = _t.time()
        armed_ts = float(ss.armed_buy_since_ts or now)
        if (now - armed_ts) < self._AUTO_REFRESH_STALE_AFTER_SECS:
            return

        # Gate 3: cadence — throttle to once per minute per sleeve.
        last_refresh = float(getattr(ss, "_last_auto_refresh_ts", 0.0) or 0.0)
        if last_refresh and (now - last_refresh) < self._AUTO_REFRESH_MIN_INTERVAL_SECS:
            return

        # Gate 4: sufficient price history. On process restart, the in-memory
        # deque is empty — attempt backfill from Coinbase candles ONCE before
        # giving up (Adam 2026-07-15: "why wait if we have the historical
        # data?"). Cadence gate below ensures we don't hammer this.
        history = list(self._sleeve_price_history.get(sc.id, []) or [])
        if len(history) < self._AUTO_REFRESH_MIN_HISTORY:
            backfilled = self._backfill_sleeve_history_from_coinbase(sc)
            if backfilled > 0:
                history = list(self._sleeve_price_history.get(sc.id, []) or [])
            if len(history) < self._AUTO_REFRESH_MIN_HISTORY:
                # Still short — record the cadence tick so we don't retry
                # backfill on every tick (respects the once/min throttle).
                ss._last_auto_refresh_ts = now
                return

        # Compute fresh buy_px via arm_level (Chan OU + Connors).
        # CRITICAL: use current market as sold_ref, NOT ancient
        # last_sell_fill_price. See docstring for the ZEC trap.
        current_spread = max(0.005, float(sc.sell_px) - float(sc.buy_px))
        try:
            import arm_level as _al
            new_buy_px = _al.pullback_buy_px(
                history,
                spread=current_spread,
                sold_price=float(last_price),  # current market as reference
            )
        except Exception as e:
            self._record("sleeve_auto_refresh_error", sleeve_id=sc.id,
                         sleeve_name=sc.name, error=str(e))
            ss._last_auto_refresh_ts = now
            return

        if new_buy_px is None:
            ss._last_auto_refresh_ts = now
            return

        try:
            new_buy_px = self._snap_to_tick(float(new_buy_px))
        except Exception:
            pass

        # Gate 5: minimum drift — skip if change is negligible.
        current_buy = float(sc.buy_px)
        drift_pct = abs(new_buy_px - current_buy) / max(abs(current_buy), 1e-9) * 100
        if drift_pct < self._AUTO_REFRESH_MIN_DRIFT_PCT:
            ss._last_auto_refresh_ts = now
            return

        # Compute matching new_sell_px preserving current spread.
        new_sell_px = self._snap_to_tick(new_buy_px + current_spread)

        # Persist via _reanchor_sleeve — writes to store + logs
        # sleeve_reanchored event. Also fires our own event for audit.
        self._reanchor_sleeve(sc, ss, new_buy_px, new_sell_px, last_price)
        ss._last_auto_refresh_ts = now
        self._record(
            "sleeve_auto_refresh",
            sleeve_id=sc.id, sleeve_name=sc.name,
            old_buy_px=current_buy, new_buy_px=new_buy_px,
            old_sell_px=float(sc.sell_px) if False else current_buy + current_spread,
            new_sell_px=new_sell_px, drift_pct=round(drift_pct, 3),
            armed_hours=(now - armed_ts) / 3600,
            current_market=float(last_price),
            source="arm_level.pullback_buy_px (Chan OU + Connors)",
        )

        # 2026-07-15 Phase 1 tile visibility: also write the FULL expert
        # snapshot (7-expert chain: Kaufman + Elder + Ehlers + Chan OU +
        # Connors + VPIN + Vince + KAMA + Fisher) to Redis so the dashboard
        # can render "what the experts say right now" in the sleeve editor.
        # Fail-safe — if compute_reentry errors, we've already done the
        # refresh, so just skip the snapshot write.
        try:
            import experts_reentry as _er
            sold_ref = float(last_price)  # current market — matches auto-refresh anchor
            snapshot_result = _er.compute_reentry(
                prices=history, sold_price=sold_ref, spread=current_spread,
                strategy_qty=int(getattr(sc, "qty", 1) or 1),
            )
            snapshot_payload = {
                "product_id": self.symbol,
                "tenant": self.tenant_id,
                "sleeve_id": sc.id,
                "generated_at": now,
                "current_market": float(last_price),
                "auto_refresh_last_buy_px": new_buy_px,
                "auto_refresh_last_sell_px": new_sell_px,
                "recommended_buy_px": snapshot_result.get("buy_px"),
                "recommended_sell_px": snapshot_result.get("sell_px"),
                "should_arm": snapshot_result.get("should_arm"),
                "reasons": snapshot_result.get("reasons"),
                "expert_snapshot": snapshot_result.get("expert_snapshot"),
            }
            # Write directly to Redis if the store is Redis-backed. Falls
            # back gracefully for JSON-file test envs.
            try:
                if hasattr(self.store, "_r"):
                    import json as _json
                    key = f"expert_snapshot:{self.tenant_id}:{self.symbol}"
                    self.store._r.set(key, _json.dumps(snapshot_payload), ex=300)
            except Exception:
                pass
        except Exception as e:
            self._record("expert_snapshot_write_error",
                         sleeve_id=sc.id, error=str(e))

    def _maybe_reeval_pending_arm(self, sc, ss, last_price: float) -> None:
        """Wire reentry_reeval.evaluate_pending into the ARMED_BUY tick per
        auditor review gate 2026-07-14. Cancel-replace with confirmation.
        WS1 dedup lock. Anti-thrash. Cache-coherent state persist. Feature-
        flagged behind __reentry_mode__ — 'legacy' (default) is byte-for-byte
        original behavior, 'shadow' logs the would-be decision without
        touching the broker (24-48h burn-in per auditor 2026-07-14), 'expert'
        executes the decision.

        Called on every sleeve tick in the ARMED_BUY branch. Returns early
        unless: sleeve is ARMED_BUY, has a live resting order to re-evaluate,
        and the tenant has __reentry_mode__ set to 'expert' or 'shadow'."""
        # Preconditions — legacy path is byte-for-byte identical when any fails.
        if ss.state != SleeveStateEnum.ARMED_BUY:
            return
        if not ss.live_order_id:
            return  # no resting order to re-evaluate
        mode = self._reentry_mode()
        if mode not in ("expert", "shadow"):
            return

        try:
            import reentry_reeval as _rr
            import time as _t
            prices = list(self._sleeve_price_history.get(sc.id, []) or [])
            if len(prices) < 30:
                return  # insufficient history for reeval features

            now = _t.time()
            armed_at = ss.armed_buy_since_ts or now
            # elapsed_bars: approximate 1 bar = 60s (bot ticks are sub-second,
            # so this is a coarse metric — acceptable given reeval's staleness
            # threshold is in the 20+ range).
            elapsed_bars = int((now - armed_at) / 60)

            # ATR-14 approximation from sleeve price history
            recent = prices[-15:]
            if len(recent) < 2:
                return
            atr = sum(abs(recent[i] - recent[i - 1])
                      for i in range(1, len(recent))) / (len(recent) - 1)
            if atr <= 0:
                return

            # htf_slope: last price vs mean of last 60 (higher-timeframe drift)
            htf_window = prices[-60:] if len(prices) >= 60 else prices
            htf_slope = float(last_price) - (sum(htf_window) / len(htf_window))

            # trend_strength: prefer regime.classify_regime's efficiency_ratio;
            # fall back to a neutral 0.3 (below the default threshold, so no
            # spurious "strong trend" declaration on missing data).
            trend_strength = 0.3
            try:
                import regime as _regime
                reg = _regime.classify_regime([{"close": p} for p in prices])
                er = reg.get("efficiency_ratio")
                if er is not None:
                    trend_strength = float(er)
            except Exception:
                pass

            # dc_high: 20-bar Donchian high
            dc_high = max(prices[-20:])

            # fast_ema: 21-period EMA (standard formula)
            k = 2.0 / (21 + 1)
            ema = prices[0]
            for p in prices[1:]:
                ema = p * k + ema * (1 - k)
            fast_ema = ema

            # near_expiry: parse contract_expiry from the sleeve's product config
            near_expiry = False
            try:
                cfg_raw = self.store.get_config(self.tenant_id, self.symbol) or {}
                expiry_str = cfg_raw.get("contract_expiry")
                if expiry_str:
                    from datetime import datetime, timezone
                    ex_norm = str(expiry_str).replace("Z", "+00:00")
                    expiry_dt = datetime.fromisoformat(ex_norm)
                    days_to_expiry = (expiry_dt - datetime.now(timezone.utc)).days
                    near_expiry = days_to_expiry <= 3
            except Exception:
                pass

            dec = _rr.evaluate_pending(
                elapsed_bars=elapsed_bars, price=float(last_price),
                last_sale_px=float(ss.last_sell_fill_price or sc.buy_px),
                resting_buy_px=float(sc.buy_px),
                atr=float(atr), htf_slope=float(htf_slope),
                trend_strength=float(trend_strength),
                dc_high=float(dc_high), fast_ema=float(fast_ema),
                near_expiry=bool(near_expiry),
                params=_rr.ReevalParams(),
            )
        except Exception as e:
            self._record("reentry_reeval_error", sleeve_id=sc.id,
                         sleeve_name=sc.name, error=str(e))
            return

        # Log every decision (Tier 3 requirement) — action + why + old/new px.
        # `mode` field lets the operator distinguish shadow observations from
        # executed decisions when auditing the trade log.
        self._record(
            "reentry_reeval_decision",
            sleeve_id=sc.id, sleeve_name=sc.name,
            action=dec.action, old_buy_px=float(sc.buy_px),
            new_buy_px=dec.new_buy_px, why=dec.why,
            elapsed_bars=elapsed_bars, mode=mode,
        )

        if dec.action == "hold":
            return

        # SHADOW MODE — auditor 2026-07-14: compute + log the decision but
        # place/cancel NOTHING. Used to burn in 24-48h of observed decisions
        # on live before turning execution on for one small sleeve. Emits a
        # dedicated event so audit can separate would-have-been actions from
        # actual actions on the exact same code path.
        if mode == "shadow":
            self._record(
                "reentry_reeval_shadow_action",
                sleeve_id=sc.id, sleeve_name=sc.name,
                would_action=dec.action, old_buy_px=float(sc.buy_px),
                would_new_buy_px=dec.new_buy_px, why=dec.why,
                elapsed_bars=elapsed_bars,
            )
            return

        # EXPERT MODE — execute the decision.
        if dec.action in ("reanchor", "breakout"):
            self._reeval_cancel_replace(sc, ss, dec, last_price)
            return

        if dec.action == "expire":
            self._reeval_expire(sc, ss, dec)
            return

    # Adam 2026-07-15: min drift required before reeval will burn a
    # cancel-replace cycle. Below this threshold, the ~200ms coverage gap
    # between cancel-ack and place-ack isn't worth the risk of missing a
    # fill. Confirmed via diag_missed_fills.py: 2 verified in-gap misses
    # (HYP $66.91 at 08:32, ZEC $554.20 at 08:44) traced to churn where
    # the new_buy_px was within 0.1% of the current.
    _REEVAL_MIN_DRIFT_PCT: float = 0.25

    def _reeval_cancel_replace(self, sc, ss, dec, last_price: float) -> None:
        """CANCEL-first-CONFIRM-then-PLACE. Anti-thrash reset. Persist
        both in-memory and Redis. Uses shared arm_level helper so the
        reanchor pullback logic is unified with expert_reentry."""
        import time as _t
        # Use the shared level helper (Tier 2 #3 — unified with expert_reentry)
        try:
            import arm_level
            spread = max(0.005, float(sc.sell_px) - float(sc.buy_px))
            sold_ref = float(ss.last_sell_fill_price or sc.buy_px)
            unified_buy_px = arm_level.pullback_buy_px(
                list(self._sleeve_price_history.get(sc.id, []) or []),
                spread=spread, sold_price=sold_ref)
            # If unified helper produces a price, use it. Else fall back to
            # reeval's own suggestion — but the invariant (buy < sold_ref)
            # must still hold.
            new_buy_px = unified_buy_px if unified_buy_px is not None else float(dec.new_buy_px)
            # Snap to tick
            try:
                new_buy_px = self._snap_to_tick(float(new_buy_px))
            except Exception:
                pass
        except Exception:
            new_buy_px = float(dec.new_buy_px)

        # Adam 2026-07-15: min-drift gate — skip cancel-replace if the new
        # price is basically the same as the current resting price. The
        # cancel-then-place cycle takes ~200ms during which we're not on
        # the book; if the market wicks in that gap we miss the fill.
        # Verified via diag_missed_fills.py: 2 confirmed in-gap misses
        # from churn where new_buy_px differed by <0.1%.
        current_buy_px = float(sc.buy_px or 0)
        drift_pct = None
        if current_buy_px > 0:
            drift_pct = abs(float(new_buy_px) - current_buy_px) / current_buy_px * 100
        # Always emit the check so we can debug why the gate does/doesn't fire.
        self._record(
            "reentry_reeval_drift_check",
            sleeve_id=sc.id, sleeve_name=sc.name,
            current_buy_px=current_buy_px, new_buy_px=float(new_buy_px),
            dec_new_buy_px=float(dec.new_buy_px) if getattr(dec, "new_buy_px", None) is not None else None,
            drift_pct=(round(drift_pct, 4) if drift_pct is not None else None),
            threshold_pct=self._REEVAL_MIN_DRIFT_PCT,
            will_skip=(drift_pct is not None and drift_pct < self._REEVAL_MIN_DRIFT_PCT),
            reeval_action=getattr(dec, "action", None),
        )
        if drift_pct is not None and drift_pct < self._REEVAL_MIN_DRIFT_PCT:
            self._record(
                "reentry_reeval_replace_skipped_below_drift",
                sleeve_id=sc.id, sleeve_name=sc.name,
                current_buy_px=current_buy_px, new_buy_px=float(new_buy_px),
                drift_pct=round(drift_pct, 4),
                threshold_pct=self._REEVAL_MIN_DRIFT_PCT,
                reeval_action=getattr(dec, "action", None),
            )
            return

        # WS1 dedup lock (Tier 1 #2)
        try:
            import arm_dedup
            tick_size = float(getattr(self.cfg, "tick_size", 0.0001) or 0.0001)
            lock = arm_dedup.try_acquire_arm_lock(
                self.store, self.tenant_id, self.symbol,
                "BUY", new_buy_px, tick_size)
            if not lock.get("acquired"):
                self._record("reentry_reeval_lock_blocked",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             new_buy_px=new_buy_px,
                             reason=lock.get("reason"),
                             error=lock.get("error"))
                return
        except Exception as e:
            self._record("reentry_reeval_lock_error",
                         sleeve_id=sc.id, error=str(e))
            return

        # CANCEL FIRST — must confirm no exception before placing (Tier 1 #1)
        old_oid = ss.live_order_id
        try:
            self.b.cancel(old_oid)
        except Exception as e:
            self._record("reentry_reeval_cancel_failed",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         old_order_id=old_oid, error=str(e))
            return  # DO NOT place if cancel didn't succeed

        # THEN PLACE new order at the reeval price
        try:
            new_oid = self.b.place_limit("BUY", int(sc.qty), float(new_buy_px))
        except Exception as e:
            self._record("reentry_reeval_place_failed",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         new_buy_px=new_buy_px, error=str(e))
            # Cancel succeeded but place failed — sleeve is now orphaned.
            # Clear live_order_id and persist so state is coherent.
            ss.live_order_id = None
            self._save_state()
            return

        # Update sleeve state — memory FIRST, then persist to Redis (Tier 2 #1)
        old_buy_px = float(sc.buy_px)
        ss.live_order_id = new_oid
        sc.buy_px = float(new_buy_px)
        sc.sell_px = float(new_buy_px) + max(0.005, float(sc.sell_px) - old_buy_px)
        ss.armed_buy_since_ts = _t.time()  # anti-thrash reset (Tier 1 #3)
        # Also update persisted config so next boot has the new prices
        try:
            cfg = self.store.get_config(self.tenant_id, self.symbol) or {}
            sleeves = list(cfg.get("sleeves") or [])
            for s in sleeves:
                if s.get("id") == sc.id:
                    s["buy_px"] = float(new_buy_px)
                    s["sell_px"] = float(sc.sell_px)
                    break
            cfg["sleeves"] = sleeves
            self.store.put_config(self.tenant_id, self.symbol, cfg)
        except Exception:
            pass
        # Redis state write
        self._save_state()

        self._record("reentry_reeval_replaced",
                     sleeve_id=sc.id, sleeve_name=sc.name,
                     action=dec.action, why=dec.why,
                     old_order_id=old_oid, new_order_id=new_oid,
                     old_buy_px=old_buy_px, new_buy_px=new_buy_px,
                     unified_via_arm_level=(unified_buy_px is not None
                                             if 'unified_buy_px' in dir() else False))

    def _reeval_expire(self, sc, ss, dec) -> None:
        """Clean expire: cancel resting order, transition sleeve to HALTED
        (no re-arm next tick per Tier 2 #2), persist state."""
        old_oid = ss.live_order_id
        try:
            if old_oid:
                self.b.cancel(old_oid)
        except Exception:
            pass  # best-effort cancel; may already be gone
        ss.live_order_id = None
        if ss.state != SleeveStateEnum.HALTED:
            ss.pre_halt_state = ss.state.value
        ss.state = SleeveStateEnum.HALTED
        import reentry_reeval as _rr
        ss.halt_reason = f"{_rr.EXPIRE_HALT_PREFIX} {dec.why}"
        self._save_state()
        self._record("reentry_reeval_expired",
                     sleeve_id=sc.id, sleeve_name=sc.name,
                     old_order_id=old_oid, why=dec.why)

    def _maybe_reanchor_new_channel(self, sc, ss, last_price: float) -> None:
        """[crew] After a confirmed + settled structural drop, walk the sleeve's
        whole channel (buy/sell/trail + stop reference) DOWN to the new channel
        so targets and the stop track reality instead of stranding above price.

        Uses channel_finder (break-detect + vol-stabilization + adaptive center
        + Donchian floor / Keltner width). It CANNOT fire mid-crash: find_channel
        only reports `stabilized` once volatility has contracted, so the crash
        guard owns the during-crash exit and this only re-establishes the range
        AFTER the drop settles. Re-basing a monotonic-up stop is legitimate here
        precisely because a settled break is a regime change — the old stop
        belonged to a dead channel (Kaminski-Lo: stops are regime-dependent).
        Opt-in (channel_reanchor_enabled); OFF by default; fail-safe on error."""
        if not getattr(sc, "channel_reanchor_enabled", False):
            return
        # Adam's rule: hunt for a new channel ONLY while FLAT and waiting to buy
        # (ARMED_BUY). Never re-anchor a HELD position — don't drag the sell
        # target or stop down to "find a channel" and lock a loss; hold and exit
        # positive. Finding the new channel is a decision for the NEXT entry.
        if ss.state != SleeveStateEnum.ARMED_BUY:
            return
        try:
            import channel_finder
            prices = list(self._sleeve_price_history.get(sc.id, []) or [])
            if len(prices) < 24:
                return
            ch = channel_finder.find_channel(prices, atr=None)
            if not (ch.get("broke") and ch.get("stabilized")):
                return
            new_buy, new_sell, lower = ch.get("buy_px"), ch.get("sell_px"), ch.get("lower")
            if new_buy is None or new_sell is None or new_sell <= new_buy:
                return
            # act only on a MATERIAL downward move so we don't churn on noise
            if float(new_buy) >= float(sc.buy_px):
                return
            dropped = float(sc.buy_px) - float(new_buy)
            old_stop = float(sc.stop_loss_px or 0.0)
            # 1) walk buy/sell/trail down to the new channel (tested primitive)
            self._reanchor_sleeve(sc, ss, float(new_buy), float(new_sell), last_price)
            # 2) re-base the stop to the new regime: reset the ratchet HWM to
            #    current, and lower a stranded fixed stop to the new lower band.
            ss.stop_loss_hwm = float(last_price)
            new_stop = old_stop
            if old_stop > 0 and lower is not None and old_stop > float(lower):
                new_stop = round(float(lower), 6)
                sc.stop_loss_px = new_stop
                try:
                    cfg = self.store.get_config(self.tenant_id, self.symbol) or {}
                    sleeves = list(cfg.get("sleeves") or [])
                    for s in sleeves:
                        if s.get("id") == sc.id:
                            s["stop_loss_px"] = new_stop
                            break
                    cfg["sleeves"] = sleeves
                    self.store.put_config(self.tenant_id, self.symbol, cfg)
                except Exception:
                    pass
            self._record("sleeve_channel_reanchored", sleeve_id=sc.id, sleeve_name=sc.name,
                         new_buy=round(float(new_buy), 6), new_sell=round(float(new_sell), 6),
                         new_center=ch.get("center"), new_stop=new_stop, old_stop=old_stop,
                         dropped=round(dropped, 6), reason=ch.get("reason"))
        except Exception as e:
            self._record("channel_reanchor_error", sleeve_id=sc.id, error=str(e))

    def _sleeve_track_price(self, sc, last_price: float) -> None:
        """Append last_price to the sleeve's rolling window. Kept short so
        memory is bounded — window * 4 keeps enough history for pre-stop
        vs post-stop range comparison."""
        from collections import deque as _deque
        if sc.id not in self._sleeve_price_history:
            self._sleeve_price_history[sc.id] = _deque(maxlen=int(sc.reentry_range_window or 60) * 4)
        _ph = self._sleeve_price_history[sc.id]
        prev = _ph[-1] if _ph else None
        _ph.append(float(last_price))
        # [crew] Cascade-lifecycle observations for the crash-guard re-entry
        # gate. Only maintained when the guard is on (zero cost otherwise).
        # Captures the microstructure trajectory (VPIN/OFI + a per-tick vol
        # proxy) so cascade_state can tell a real all-clear from a dead-cat
        # bounce. Fail-safe: a snapshot error just yields Nones (assess ignores
        # missing keys and stays permissive).
        if getattr(sc, "crash_guard_enabled", False):
            hist = self._sleeve_ms_history.get(sc.id)
            if hist is None:
                hist = self._sleeve_ms_history[sc.id] = _deque(maxlen=64)
            try:
                snap = self.ms.snapshot() if self.ms else {}
            except Exception:
                snap = {}
            vol = None
            try:
                if prev:
                    vol = abs(float(last_price) - float(prev)) / float(prev)
            except (TypeError, ValueError, ZeroDivisionError):
                vol = None
            hist.append({
                "price": float(last_price),
                "vpin": snap.get("vpin") if isinstance(snap, dict) else None,
                "ofi": (snap.get("trade_ofi_60s") or snap.get("ofi")) if isinstance(snap, dict) else None,
                "vol": vol,
            })

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
        new_buy = self._snap_to_tick(last_price - spread / 2)
        new_sell = self._snap_to_tick(last_price + spread / 2)
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

    def _clamp_buy_below_last_sale(self, sc, ss,
                                   new_buy_px: float, new_sell_px: float,
                                   source: str) -> tuple[float, float]:
        """Invariant guard: buy_px must never sit above the sleeve's last
        sell fill price. Applied to every upward-chase reanchor path
        (price-threshold / time / vol-percentile). Without this, the three
        priced-out reanchors reintroduce the "buy above last sale" bug
        that _maybe_expert_reanchor_after_sell fixes for the transition
        point. If the market has walked up past where we sold, we hold —
        we don't chase up above our own exit.

        Returns possibly-clamped (new_buy_px, new_sell_px). Preserves the
        spread when clamping."""
        last_sale = getattr(ss, "last_sell_fill_price", None)
        try:
            last_sale = float(last_sale) if last_sale is not None else None
        except (TypeError, ValueError):
            last_sale = None
        if last_sale is None or last_sale <= 0:
            return new_buy_px, new_sell_px
        if new_buy_px < last_sale:
            return new_buy_px, new_sell_px
        # Clamp buy to just below last sale; preserve the spread on sell.
        spread = float(new_sell_px) - float(new_buy_px)
        try:
            clamped_buy = self._snap_to_tick(float(last_sale) - max(spread / 4.0,
                                                                    float(last_sale) * 0.0005))
        except Exception:
            clamped_buy = float(last_sale) - max(spread / 4.0,
                                                 float(last_sale) * 0.0005)
        clamped_sell = clamped_buy + spread
        # Adam 2026-07-15: rate-limit to 5 min per sleeve. Was firing every
        # 5s per sleeve (once for each _sleeve_step call), spamming the
        # trade log with a purely informational clamp event. Real signal
        # still surfaces (once per 5min), noise stops.
        import time as _t_clamp
        try:
            key = f"reanchor_clamp_{sc.id}"
            store = getattr(self, "_reanchor_clamp_last_ts", None)
            if store is None:
                self._reanchor_clamp_last_ts = {}
                store = self._reanchor_clamp_last_ts
            last_ts = int(store.get(key, 0) or 0)
            cur = int(_t_clamp.time())
            if cur - last_ts > 300:
                self._record(
                    "sleeve_reanchor_clamped_below_last_sale",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    source=source,
                    requested_buy=round(float(new_buy_px), 6),
                    clamped_buy=round(float(clamped_buy), 6),
                    last_sale=round(float(last_sale), 6),
                )
                store[key] = cur
        except Exception:
            pass  # never let logging break the clamp itself
        return clamped_buy, clamped_sell

    def _maybe_expert_reanchor_after_sell(self, sc: "SleeveConfig",
                                          ss: "SleeveState",
                                          sold_price: float) -> None:
        """After a normal sell (ARMED_SELL → ARMED_BUY), run the expert chain
        to pick a buy_px that is regime/cycle/microstructure-aware — instead
        of leaving the OLD buy_px in place. Solves the "buy back above the
        last sale" bug (2026-07-13 OIL round-trip lost $15 that way).

        Opt-out: set sleeve.expert_reentry_enabled = False in config.

        Fail-safe: any error and we leave the sleeve's buy_px unchanged
        (legacy behavior) so this never worsens the state machine."""
        if getattr(sc, "expert_reentry_enabled", True) is False:
            return
        try:
            import experts_reentry as _er
        except Exception:
            return
        prices = list(self._sleeve_price_history.get(sc.id, []) or [])
        if len(prices) < 40:
            return
        spread = max(0.005, float(sc.sell_px) - float(sc.buy_px))
        # Account equity for Vince — pull from portfolio_risk which already
        # knows how to read the __portfolio__ snapshot. Fail-safe to 0.
        account_equity = 0.0
        try:
            import portfolio_risk as _pr
            account_equity = _pr._get_account_equity(self.store, self.tenant_id)
        except Exception:
            pass
        # Worst 1-contract loss for Vince — use the largest historical
        # single-cycle loss * contract_size, guarded to a floor so we don't
        # divide by tiny numbers on a fresh sleeve.
        recent = list(getattr(ss, "recent_cycle_pnls", []) or [])
        worst_loss = 0.0
        if recent:
            worst_cycle = min(recent)
            if worst_cycle < 0 and sc.qty > 0:
                worst_loss = abs(worst_cycle) / max(1, sc.qty)
        worst_loss_per_contract = max(worst_loss, spread * self.cfg.contract_size)
        # Microstructure snap for VPIN gate — best-effort, may be absent.
        ms = None
        try:
            ms = self.store.get_snapshot(self.tenant_id, self.symbol) or {}
        except Exception:
            ms = None
        # Per-product threshold overrides. Precedence (highest wins):
        #   1. Per-sleeve override (sc.reentry_thresholds)
        #   2. Per-product config scope (store.get_config(...).reentry_thresholds)
        #   3. Per-product tuned scope (store __tuned_reentry_params__ — future
        #      tuner writes here; read the symbol's dict)
        #   4. DEFAULT_THRESHOLDS in experts_reentry
        thresholds = None
        try:
            sc_override = getattr(sc, "reentry_thresholds", None)
            if isinstance(sc_override, dict) and sc_override:
                thresholds = dict(sc_override)
        except Exception:
            pass
        if thresholds is None:
            try:
                cfg = self.store.get_config(self.tenant_id, self.symbol) or {}
                cfg_override = cfg.get("reentry_thresholds")
                if isinstance(cfg_override, dict) and cfg_override:
                    thresholds = dict(cfg_override)
            except Exception:
                pass
        if thresholds is None:
            try:
                tuned = self.store.get_state(self.tenant_id, "__tuned_reentry_params__") or {}
                sym_tuned = tuned.get(self.symbol) if isinstance(tuned, dict) else None
                if isinstance(sym_tuned, dict) and sym_tuned:
                    thresholds = dict(sym_tuned)
            except Exception:
                pass
        decision = _er.compute_reentry(
            prices=prices,
            sold_price=float(sold_price),
            spread=spread,
            strategy_qty=int(sc.qty),
            account_equity=float(account_equity or 0.0),
            worst_loss_per_contract=float(worst_loss_per_contract or 0.0),
            recent_cycle_pnls=recent,
            ms=ms,
            thresholds=thresholds,
        )
        # Log the decision regardless of arm — audit trail for the algo.
        self._record(
            "sleeve_expert_reentry_decision",
            sleeve_id=sc.id, sleeve_name=sc.name,
            sold_price=round(float(sold_price), 6),
            should_arm=bool(decision.get("should_arm")),
            buy_px=decision.get("buy_px"),
            sell_px=decision.get("sell_px"),
            capped_qty=decision.get("qty"),
            reasons=decision.get("reasons"),
            expert_snapshot=decision.get("expert_snapshot"),
        )
        if not decision.get("should_arm"):
            return
        new_buy = decision.get("buy_px")
        new_sell = decision.get("sell_px")
        if new_buy is None or new_sell is None or new_sell <= new_buy:
            return
        # Snap to tick and reanchor. The reanchor helper handles both the
        # in-memory sc and the persisted config.
        try:
            new_buy = self._snap_to_tick(float(new_buy))
            new_sell = self._snap_to_tick(float(new_sell))
        except Exception:
            pass
        self._reanchor_sleeve(sc, ss, float(new_buy), float(new_sell),
                              float(sold_price))

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
        # Reset the ARMED_BUY timer — we just moved targets to bracket the
        # current market, so the "priced out" clock restarts from here.
        import time as _time
        ss.armed_buy_since_ts = _time.time()
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
        # Live-tenant safety cap: primary can never sell more than swing_qty
        # on a stop trip. Matches the sleeve cap — protect the core holding.
        if self.tenant_id.endswith("-live"):
            mode = "original"
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
        if self._stop_loss_globally_disabled():
            self._record("stop_loss_skipped_globally_disabled",
                         price=last_price, trigger=trigger)
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
        # Adam 2026-07-15 CRITICAL: mutual exclusion with any sleeve-level
        # resting stops that are also live at Coinbase. If ANY sleeve on this
        # product has resting_stop_oid set, Coinbase already has protective
        # sells sitting on the book. Firing a primary market SELL here on top
        # of that = the double-fire race (CU 2026-07-15 12:34:34) at the
        # product level. Skip the bot-side sell; the exchange stops carry it.
        active_resting = [ss.resting_stop_oid
                          for ss in (self.s.sleeves or {}).values()
                          if getattr(ss, "resting_stop_oid", None)]
        if active_resting:
            self._record("stop_loss_skipped_resting_stop_active",
                         price=last_price, trigger=trigger,
                         resting_stop_oids=list(active_resting))
            return False
        try:
            source = getattr(self.b, "set_pending_source", None)
            if callable(source):
                source("stop_loss")
            oid = self.b.place_market("SELL", to_sell)
            self._refresh_portfolio_after_fill()
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

        # Migration/scripts can request specific state fields be reset without
        # forcing a full bot restart. E.g. after a silver→per-product stop_loss
        # migration, the old ratchet HWM in memory would clobber the cleared
        # Redis value on next tick. This consumes the intent and applies the
        # requested resets to in-memory state before anything else runs.
        self._maybe_consume_sleeve_state_reset()

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
                # Same-tick re-arm — after a fill, immediately place the
                # next-leg order rather than waiting for the next tick.
                # Prevents a rapid opposite-side move from trading past
                # the next target during the ~1s gap.
                if self.s.state != State.HALTED and not self.s.live_order_id:
                    self._ensure_armed(last_price)

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

    def _maintain_resting_stop(self, sc: SleeveConfig, ss: SleeveState,
                               last_price: float) -> None:
        """Three-stage Coinbase resting stop-limit ratchet (Adam 2026-07-15).

        Stage 1 (hard bottom):  stop_px = sc.stop_loss_px — while position
                                is held and mark hasn't crossed sc.sell_px.
        Stage 2 (profit lock):  stop_px = sc.sell_px — once mark crosses
                                the take-profit level. Locks in the win.
        Stage 3 (trail ratchet): stop_px = HWM − trail_distance — once trail
                                is armed. Ratchets UP with every meaningful
                                HWM tick, never DOWN.

        Cancel+replace on meaningful UP moves. NEVER lowers.
        Fails open — if Coinbase rejects, bot-side triggers remain the
        backstop (the market-on-trigger paths in _sleeve_step still fire).
        No-short guard in broker.place_stop_limit ensures qty ≤ position."""
        if not getattr(sc, "resting_stop_enabled", True):
            return
        # Only maintain a resting stop while we have a real position.
        try:
            pos_qty = int(self.b.position_qty() or 0)
        except Exception:
            pos_qty = 0
        sleeve_qty = int(getattr(sc, "qty", 1) or 1)
        if pos_qty <= 0 or sleeve_qty <= 0:
            # Position closed — cancel any lingering resting stop and clear state.
            if ss.resting_stop_oid:
                try:
                    self.b.cancel(ss.resting_stop_oid)
                except Exception as e:
                    self._record("resting_stop_cancel_failed", sleeve_id=sc.id,
                                 sleeve_name=sc.name, oid=ss.resting_stop_oid,
                                 error=str(e))
                self._record("resting_stop_cleared", sleeve_id=sc.id,
                             sleeve_name=sc.name, reason="no_position")
                ss.resting_stop_oid = None
                ss.resting_stop_px = None
                ss.resting_stop_stage = None
            return
        # Resolve stage + target price.
        hwm = float(ss.trail_high_water_price or 0.0)
        trail_engaged = bool(ss.trail_armed)
        stop_loss_px = float(getattr(sc, "stop_loss_px", 0) or 0)
        sell_px = float(getattr(sc, "sell_px", 0) or 0)
        trail_distance = float(getattr(sc, "trail_distance", 0) or 0)
        target_px = None
        stage = None
        if trail_engaged and hwm > 0 and trail_distance > 0:
            target_px = hwm - trail_distance
            stage = "trail"
        elif sell_px > 0 and (hwm >= sell_px or last_price >= sell_px):
            target_px = sell_px
            stage = "profit_lock"
        elif stop_loss_px > 0:
            target_px = stop_loss_px
            stage = "hard_bottom"
        if not target_px or target_px <= 0:
            return
        try:
            target_px = self._snap_to_tick(float(target_px))
        except Exception:
            pass
        tick = float(getattr(self.cfg, "tick_size", 0) or 0.01)
        # Adam 2026-07-15: widen the limit-price offset from "1 tick below"
        # to "max(2 ticks, 0.05% of stop_px)". Fixes the COPR case where
        # stop=$5.97 and limit=$5.97 (indistinguishable at 4-decimal display)
        # — one tick was inside the display rounding. 5 bps floor also
        # provides meaningful slippage buffer for expensive products (BTC)
        # while staying tight on cheap ones. Guarantees the limit is visibly
        # below the stop so the order actually fills after trigger.
        buffer = max(tick * 2.0, target_px * 0.0005)
        limit_px = max(0.0, target_px - buffer)
        # Sanity: never place a stop at or above current mark (would fire
        # immediately as a limit sell against the book — that's a market sell,
        # not a protective stop). This can happen briefly if the market gaps
        # past our intended stop; let bot-side triggers handle it.
        if target_px >= last_price:
            self._record("resting_stop_skipped_above_mark",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         target_px=target_px, last_price=last_price, stage=stage)
            return
        # Fresh place — no existing resting order.
        if not ss.resting_stop_oid:
            try:
                oid = self.b.place_stop_limit("SELL", sleeve_qty,
                                              float(target_px), float(limit_px))
                ss.resting_stop_oid = oid
                ss.resting_stop_px = float(target_px)
                ss.resting_stop_stage = stage
                self._record("resting_stop_placed",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             stage=stage, target_px=float(target_px),
                             limit_px=float(limit_px), qty=sleeve_qty, oid=oid)
            except Exception as e:
                # Fallback: bot-side trigger stays armed as backstop.
                # Adam 2026-07-15: severity=critical per the "resting ratchet-
                # stop must never leave a held position unprotected" rule. A
                # place failure means Coinbase currently has NO stop for a
                # held sleeve — dashboard reconciliation chip should turn red
                # so this is visible without grep-hunting logs.
                self._record("resting_stop_place_failed",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             stage=stage, target_px=float(target_px),
                             error=str(e), severity="critical")
            return
        # Existing resting order — check if we need to ratchet UP.
        current_px = float(ss.resting_stop_px or 0)
        # Meaningful move up = at least one tick higher than current.
        if target_px > current_px + (tick * 0.5):
            old_oid = ss.resting_stop_oid
            try:
                self.b.cancel(old_oid)
            except Exception as e:
                self._record("resting_stop_ratchet_cancel_failed",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             old_oid=old_oid, error=str(e))
                # Continue anyway — old order may have already filled/been
                # cancelled; the place below will either succeed (new stop)
                # or fail (we clear tracking, retry next tick).
            try:
                new_oid = self.b.place_stop_limit("SELL", sleeve_qty,
                                                  float(target_px), float(limit_px))
                self._record("resting_stop_ratcheted",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             from_px=current_px, to_px=float(target_px),
                             stage=stage, old_oid=old_oid, new_oid=new_oid,
                             qty=sleeve_qty)
                ss.resting_stop_oid = new_oid
                ss.resting_stop_px = float(target_px)
                ss.resting_stop_stage = stage
            except Exception as e:
                self._record("resting_stop_ratchet_place_failed",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             from_px=current_px, to_px=float(target_px),
                             error=str(e))
                # Clear tracking so next tick attempts a fresh place.
                ss.resting_stop_oid = None
                ss.resting_stop_px = None
                ss.resting_stop_stage = None
        # target_px <= current_px → never lower (ratchet-up-only invariant)

    def _maybe_reconcile_orphan_position(self, sc: SleeveConfig, ss: SleeveState) -> None:
        """Adam 2026-07-15: if a sleeve is ARMED_BUY but a Coinbase position
        exists that no sleeve claims, adopt it: set own_avg_entry from broker
        and flip state to ARMED_SELL so the sleeve manages the exit properly.

        Fixes the confusing state where:
          - Position: 1 LONG at $553.50 (from a prior sleeve or a manual
            entry)
          - Sleeve state: ARMED_BUY at $556.20 (wanting to buy MORE)
          - Sleeve own_avg_entry: None (doesn't know it owns anything)
          - Dashboard unrealized: $0.00 (because own_avg_entry is None)
          - _sleeve_arm safety: refuses the buy (position full)
          - Result: position sitting unmanaged by any sleeve state machine,
            unrealized displays wrong, "trigger down while LONG" UX

        Only fires when the position is UNCLAIMED — checks that no other
        sleeve on this product has own_avg_entry set. Safety-tight: if any
        other sleeve owns even 1 contract, we don't adopt. Prevents two
        sleeves from claiming the same position.
        """
        # Preconditions
        if ss.state != SleeveStateEnum.ARMED_BUY:
            return
        if ss.own_avg_entry is not None:
            return  # Already owns something — don't adopt more
        # Broker position
        try:
            pos_qty = int(self.b.position_qty() or 0)
        except Exception:
            return
        if pos_qty <= 0:
            return  # Nothing to adopt
        # Sum qty claimed by OTHER sleeves on this product
        claimed_qty = 0
        for other_id, other_ss in (self.s.sleeves or {}).items():
            if other_id == sc.id:
                continue
            if other_ss.own_avg_entry is not None:
                # Use configured qty as the claim size — matches how the
                # position-full safety accounts for sleeve claims.
                other_sc = None
                if hasattr(self, "_sleeve_cfg_by_id"):
                    try:
                        other_sc = self._sleeve_cfg_by_id(other_id)
                    except Exception:
                        other_sc = None
                claimed_qty += int(getattr(other_sc, "qty", 1) if other_sc else 1)
        core_qty = int(getattr(self.cfg, "core_qty", 0) or 0)
        # Unclaimed portion of the current position available to adopt
        unclaimed_qty = pos_qty - claimed_qty - core_qty
        if unclaimed_qty <= 0:
            return
        # Read broker position avg entry — the price we adopt at
        try:
            avg = float((self.b.position or None).avg_entry or 0)
        except Exception:
            avg = 0.0
        if avg <= 0:
            return
        # Cancel any pending buy order — sleeve state changing to ARMED_SELL,
        # buy is meaningless
        old_oid = ss.live_order_id
        if old_oid:
            try:
                self.b.cancel(old_oid)
            except Exception as e:
                self._record("sleeve_orphan_reconcile_cancel_failed",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             oid=old_oid, error=str(e))
                # Continue anyway — worst case is a stale buy order that the
                # position-full safety will refuse on any trigger
        # Adopt: set own_avg_entry + flip state
        ss.own_avg_entry = float(avg)
        ss.state = SleeveStateEnum.ARMED_SELL
        ss.live_order_id = None  # will re-arm SELL on next tick
        self._save_state()
        self._record(
            "sleeve_orphan_position_adopted",
            sleeve_id=sc.id, sleeve_name=sc.name,
            position_qty=pos_qty, claimed_by_others=claimed_qty,
            unclaimed_qty=unclaimed_qty, core_qty=core_qty,
            adopted_avg=float(avg),
        )

    def _maybe_arm_stop_on_recovery(self, sc: SleeveConfig, ss: SleeveState,
                                    last_price: float) -> None:
        """Adam 2026-07-15 (NGS-generalized): any underwater sleeve with
        stop_loss_px=0 auto-arms stop_loss_px = own_avg_entry the first
        time mark climbs back to entry. Then normal three-stage ratchet
        takes over.

        Fleet-wide rule (not per-sleeve toggle). Formalizes the NGS
        directive: 'stop stays off while underwater, then arms at entry
        level and everything goes back to normal.'

        Guards:
          - Fires ONCE per position (once stop_loss_px is set, no re-fire)
          - Requires own_avg_entry to be set (sleeve owns the position)
          - Requires mark to have crossed to or above entry
          - Persists via config write so next tick sees the new stop
        """
        if float(sc.stop_loss_px or 0) > 0:
            return  # already has a stop
        own_avg = float(ss.own_avg_entry or 0)
        if own_avg <= 0:
            return  # sleeve doesn't own anything
        try:
            pos_qty = int(self.b.position_qty() or 0)
        except Exception:
            return
        if pos_qty <= 0:
            return  # nothing to protect
        if float(last_price or 0) < own_avg:
            return  # still underwater, hold the rule
        # Recovery detected — arm stop_loss_px at entry
        sc.stop_loss_px = float(own_avg)
        # Enable stop_loss_enabled flag if it wasn't (so downstream code paths
        # honor the new stop). Recovery-arm implies the user wants protection
        # at breakeven from here on.
        if hasattr(sc, "stop_loss_enabled"):
            sc.stop_loss_enabled = True
        # Persist to config store so restart preserves the arm
        try:
            cfg = self.store.get_config(self.tenant_id, self.symbol) or {}
            for s in (cfg.get("sleeves") or []):
                if s.get("id") == sc.id:
                    s["stop_loss_px"] = float(own_avg)
                    s["stop_loss_enabled"] = True
                    break
            self.store.put_config(self.tenant_id, self.symbol, cfg)
        except Exception as e:
            self._record("stop_recovery_arm_persist_failed",
                         sleeve_id=sc.id, error=str(e))
        self._record(
            "stop_loss_armed_at_entry_recovery",
            sleeve_id=sc.id, sleeve_name=sc.name,
            entry_avg=own_avg, mark=float(last_price),
            new_stop_loss_px=float(own_avg),
        )

    def _maybe_credit_resting_stop_fill(self, sc: SleeveConfig, ss: SleeveState) -> None:
        """Adam 2026-07-15: post-fill state reconciler for the ratchet-stop.

        Fixes Type 3 ghost: sleeve state=ARMED_SELL, own_avg_entry set,
        Coinbase position=0 → the resting stop-limit fired on Coinbase
        but we never credited the exit back to sleeve state. Result:
        cycles never increment, realized_pnl never updates, dashboard
        shows 'phantom profit' from own_avg_entry that we no longer own.

        Runs each tick. When resting_stop_oid exists, polls its status.
        If FILLED: credits the fill (cycles++, realized_pnl += profit,
        own_avg_entry cleared, resting_stop_* cleared, state → ARMED_BUY
        ready for next cycle). If CANCELLED externally: clears the oid
        so _maintain_resting_stop places a fresh one next tick.
        """
        import time as _time
        if not ss.resting_stop_oid:
            return
        try:
            status_info = self.b.order_status(ss.resting_stop_oid)
        except Exception as e:
            self._record("resting_stop_status_check_failed",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         oid=ss.resting_stop_oid, error=str(e))
            return
        status = (status_info or {}).get("status")
        if status == "OPEN":
            return  # still resting, nothing to do
        if status == "FILLED":
            # Compute fill price + credit as exit.
            fill_price = status_info.get("average_filled_price")
            try:
                fill_price = float(fill_price) if fill_price is not None else 0.0
            except Exception:
                fill_price = 0.0
            filled_qty = int(status_info.get("filled_qty") or sc.qty or 1)
            own_avg = float(ss.own_avg_entry or 0)
            profit = 0.0
            if own_avg > 0 and fill_price > 0:
                try:
                    contract_size = float((self.contract_spec_cache or {}).get("contract_size") or 1)
                except Exception:
                    contract_size = 1.0
                profit = (fill_price - own_avg) * filled_qty * contract_size
            ss.realized_pnl = float(ss.realized_pnl or 0) + profit
            ss.cycles = int(ss.cycles or 0) + 1
            ss.last_sell_qty = filled_qty
            ss.last_sell_fill_price = fill_price
            # Track for the recent-cycles list (loss-streak detection)
            try:
                recent = list(ss.recent_cycle_pnls or [])
                recent.append(profit)
                if len(recent) > 20:
                    recent = recent[-20:]
                ss.recent_cycle_pnls = recent
            except Exception:
                pass
            # Clear position tracking + resting stop, advance state
            old_oid = ss.resting_stop_oid
            old_stage = ss.resting_stop_stage
            ss.own_avg_entry = None
            ss.resting_stop_oid = None
            ss.resting_stop_px = None
            ss.resting_stop_stage = None
            ss.state = SleeveStateEnum.ARMED_BUY
            ss.armed_buy_since_ts = _time.time()
            self._save_state()
            self._record(
                "resting_stop_filled_credited",
                sleeve_id=sc.id, sleeve_name=sc.name,
                oid=old_oid, stage=old_stage,
                fill_price=fill_price, own_avg_entry=own_avg,
                filled_qty=filled_qty, profit=profit,
                new_realized=ss.realized_pnl, new_cycles=ss.cycles,
            )
            return
        if status in ("CANCELLED", "EXPIRED"):
            # External cancel — just clear the oid so _maintain_resting_stop
            # can place a fresh one next tick.
            old_oid = ss.resting_stop_oid
            self._record("resting_stop_external_cancel_cleared",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         oid=old_oid, status=status)
            ss.resting_stop_oid = None
            ss.resting_stop_px = None
            ss.resting_stop_stage = None
            self._save_state()
            return
        # UNKNOWN, PENDING, or other — do nothing this tick, retry next.

    def _sleeve_step(self, sc: SleeveConfig, ss: SleeveState, last_price: float) -> None:
        """Independent state machine for one additional sleeve. Shares broker,
        position, and floor guard with siblings and with the primary strategy."""
        if ss.state == SleeveStateEnum.HALTED:
            return

        # Track price for volatility signal & update HWM for ratcheting stop.
        self._sleeve_track_price(sc, last_price)

        # [Adam 2026-07-15] Credit any FILLED resting stop-limit BEFORE any
        # other state check so cycles/realized/own_avg_entry are up-to-date
        # if Coinbase fired the exit since our last tick.
        self._maybe_credit_resting_stop_fill(sc, ss)

        # [Adam 2026-07-15] Auto-adopt orphan position — if we hold contracts
        # but the sleeve thinks it's waiting to buy, flip state to ARMED_SELL
        # so the sleeve manages the exit + unrealized display is accurate.
        # Runs before ratchet-stop so ss.state is correct when maintenance
        # decides what to do.
        self._maybe_reconcile_orphan_position(sc, ss)

        # [Adam 2026-07-15] Recovery-arm rule (fleet-wide from NGS directive):
        # any underwater sleeve with stop_loss_px=0 auto-arms stop at entry
        # the first time mark recovers to entry. Runs BEFORE ratchet-stop so
        # the newly-armed stop feeds into Stage 1 immediately.
        self._maybe_arm_stop_on_recovery(sc, ss, last_price)

        # [Adam 2026-07-15] Three-stage Coinbase resting stop-limit ratchet.
        # Runs BEFORE the trigger-check paths so a fresh HWM tick propagates
        # to Coinbase within one tick. Failure falls back to bot-side stops.
        self._maintain_resting_stop(sc, ss, last_price)

        # [crew] Channel re-anchor: after a confirmed + settled drop, walk the
        # whole channel (buy/sell/trail + stop) down to the new level so nothing
        # strands above price. Opt-in; cannot fire mid-crash. Off by default.
        self._maybe_reanchor_new_channel(sc, ss, last_price)

        # [crew 2026-07-14] reentry_reeval — re-evaluate a PENDING ARMED_BUY
        # entry when it goes stale OR when a new higher trend has formed
        # above the last sale (CU/copper case). Feature-flagged
        # (__reentry_mode__ scope = "expert") — OFF by default. Cancel-
        # replace with dedup lock, anti-thrash armed_at reset, expire
        # exits cleanly. See tests/test_reentry_reeval_wiring.py.
        self._maybe_reeval_pending_arm(sc, ss, last_price)

        # [crew 2026-07-15] Auto-refresh sleeve levels from experts for
        # ARMED_BUY sleeves WITHOUT a live order — closes the gap where a
        # sleeve waiting forever with stale saved buy_px/sell_px from
        # days-ago anchors never got its levels updated. Adam asked for
        # this repeatedly across 2026-07-15 late-night session (ZEC
        # confirmed 6.6% drift, XLP waiting 54.7h with stale levels).
        # Uses arm_level.pullback_buy_px — SAME helper as reentry_reeval.
        # Anchored on CURRENT market price (not ancient last_sell_fill_
        # price which was locking sleeves out of new price regimes).
        self._maybe_auto_refresh_stale_sleeve(sc, ss, last_price)

        # [crew 2026-07-15] Auto-refresh stop_loss_px against current ATR.
        # Adam: stop_loss should adapt to regime change (vol expansion
        # widens stop; vol contraction tightens). Same pattern as the
        # buy_px auto-refresh but for stop_loss_px. Safety guards
        # prevent immediate stop-triggering.
        self._maybe_auto_refresh_stop_loss(sc, ss, last_price)

        # [crew 2026-07-15] TICK-LEVEL GHOST RESURRECTION. Adam: the
        # bot's normal arm-to-place path silently fails for some sleeves
        # (state=ARMED_BUY/SELL, live_order_id=None). Result: price
        # crosses trigger, nothing fills, cycles lost forever. The diag
        # force-arm resurrected 12+ ghosts in one session. This puts the
        # same logic on every tick so ghosts never linger >60s. Root-
        # cause fix pending; this is the safety net.
        self._maybe_force_arm_ghost_order(sc, ss)

        # [crew] Average-down GREEN LIGHT alert (notification only). Opt-in.
        self._maybe_avg_down_alert(sc, ss, last_price)

        # [crew] Entry-quality GREEN LIGHT alert (notification only). Opt-in.
        self._maybe_entry_quality_alert(sc, ss, last_price)

        # [crew] DEFENSIVE crash guard. OFF by default (crash_guard_enabled).
        # When on AND holding (ARMED_SELL), if a toxic liquidation cascade is
        # running against the long, flatten at market NOW via the tested
        # _sleeve_market_sell path — this is the "couldn't get out in time" fix.
        # Reuses microstructure.py's VPIN/OFI/Kyle/OBI sensors + a jump test.
        if (getattr(sc, "crash_guard_enabled", False)
                and ss.state == SleeveStateEnum.ARMED_SELL
                and not self._within_roll_blackout()):
            try:
                import crash_guard
                ms_snap = self.ms.snapshot() if self.ms else {}
                hist = list(self._sleeve_price_history.get(sc.id, []) or [])
                rets = [(hist[i] - hist[i - 1]) / hist[i - 1]
                        for i in range(1, len(hist)) if hist[i - 1]]
                # flip_enabled only makes the assessment COMPUTE the
                # would-flip direction for shadow telemetry — the live sell
                # below still only FLATTENS. No short order is ever placed here.
                flip_on = bool(getattr(sc, "reversal_enabled", False))
                assess = crash_guard.crash_assessment(
                    ms_snap, rets, "LONG",
                    {"guard_enabled": True, "flip_enabled": flip_on})
                if assess.get("action") in ("FLATTEN", "FLATTEN_AND_FLIP"):
                    # Adam 2026-07-15 CRITICAL: same mutual-exclusion rule as
                    # stop-loss paths. If the sleeve has a live resting stop
                    # on Coinbase, that IS our crash protection — don't also
                    # market-sell (double-fire → short).
                    if (getattr(sc, "resting_stop_enabled", True)
                            and getattr(ss, "resting_stop_oid", None)):
                        self._record("crash_guard_flatten_skipped_resting_stop_active",
                                     sleeve_id=sc.id, sleeve_name=sc.name,
                                     resting_stop_oid=ss.resting_stop_oid,
                                     severity=assess.get("severity"))
                        return
                    self._record("crash_guard_flatten", sleeve_id=sc.id, sleeve_name=sc.name,
                                 severity=assess.get("severity"), direction=assess.get("direction"),
                                 fired=assess.get("fired"))
                    # [crew] OFFENSIVE reversal — SHADOW telemetry only. Record
                    # the hypothetical short entry so paper/backtest can score
                    # the flip's P&L (feeds the reversals tile + go-live
                    # gauntlet). NO live short is placed: the short-holding
                    # state machine doesn't exist yet and must be paper-
                    # validated before any real order.
                    if flip_on and assess.get("action") == "FLATTEN_AND_FLIP":
                        rev_ok, rev_reason = self._reversal_position_safe(sc, ss)
                        # reversal_signal = a flip that COULD execute (shadow
                        # P&L counts it); reversal_blocked = a flip refused
                        # because un-sleeved/core contracts are present, so the
                        # short is NOT counted — keeps the shadow evidence honest.
                        self._record(
                            "reversal_signal" if rev_ok else "reversal_blocked",
                            sleeve_id=sc.id, sleeve_name=sc.name,
                            shadow=True, would_flip_to=assess.get("flip_to"),
                            price=round(float(last_price), 6),
                            severity=assess.get("severity"),
                            direction=assess.get("direction"),
                            reason=(assess.get("reason") if rev_ok else rev_reason))
                    try:
                        self._notify(f"CRASH-GUARD flatten: {self.symbol} / {sc.name}",
                                    assess.get("reason", ""), Priority.CRIT)
                    except Exception:
                        pass
                    self._sleeve_market_sell(sc, ss, last_price)
                    return
            except Exception as e:
                self._record("crash_guard_error", sleeve_id=sc.id, error=str(e))

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

                # Adam 2026-07-15 CRITICAL fleet-wide rule: when resting_stop_enabled=True,
                # the exchange-side resting stop-limit is the SOLE exit path for this
                # sleeve. DO NOT also fire bot-side sells (hybrid market-timeout,
                # trailing_stop market exit). Doing both creates a double-fire: at
                # the moment mark crosses sell_px, the resting stop triggers AND the
                # bot fires its own market sell. Both flatten one contract each, taking
                # a +1 LONG to -1 SHORT in a single tick. CU 2026-07-15 12:34:34
                # incident: exactly this race. Broker no-short guard caught subsequent
                # sells but the position was already at -1 by then, and the hybrid
                # loop kept retrying every 5s (each refused) burning API calls.
                # If exit_mode is fixed_limit / percentage_swing, no bot-side market
                # sell fires anyway — the resting_stop coexists with a resting limit
                # sell peacefully. Only hybrid + trailing_stop need the exclusion.
                if (getattr(sc, "resting_stop_enabled", True)
                        and sc.exit_mode in ("hybrid", "trailing_stop")):
                    return  # exchange stop is the sole exit; bot-side skipped

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
                    self._prepare_post_trail_wait(sc, ss)
                    self._sleeve_market_sell(sc, ss, last_price, trail_exit=True)
                elif sc.exit_mode == "hybrid":
                    self._sleeve_hybrid_step(sc, ss, last_price)
                else:
                    self._maybe_emit_ml_shadow(sc)
                    eff_qty = self._kelly_adjusted_qty(sc, ss)
                    eff_price = self._adaptive_spread_price(sc, "SELL", sc.sell_px)
                    ms_qty, ms_px = self._sleeve_ms_adjust(sc, ss, "SELL", eff_qty, eff_price, last_price)
                    if ms_qty is None:
                        return  # microstructure gate said pause
                    self._sleeve_arm(sc, ss, "SELL", ms_qty, ms_px)
            else:  # ARMED_BUY
                # Post-trail re-entry gate (Flavor 3). If a trail exit just
                # fired and the sleeve is configured to wait for volatility
                # contraction + a new high before re-arming, this returns True
                # until both stages satisfy (or Stage B times out). Skips
                # everything below — no reanchor walk, no buy arm.
                if self._sleeve_check_post_trail(sc, ss, last_price):
                    return
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
                    new_buy_px = self._snap_to_tick(last_price - spread / 2)
                    new_sell_px = self._snap_to_tick(last_price + spread / 2)
                    # No-op guard: if spread/2 > reanchor_threshold, the reanchor
                    # condition stays TRUE forever after the first walk (last_price
                    # − new_buy_px == spread/2 > threshold), and every subsequent
                    # tick recomputes the same prices — flooding the log with
                    # identical reanchor events. Only fire if targets actually
                    # move.
                    if new_buy_px == sc.buy_px and new_sell_px == sc.sell_px:
                        return
                    new_buy_px, new_sell_px = self._clamp_buy_below_last_sale(
                        sc, ss, new_buy_px, new_sell_px, source="price_threshold_reanchor")
                    if new_buy_px == sc.buy_px and new_sell_px == sc.sell_px:
                        return
                    self._reanchor_sleeve(sc, ss, new_buy_px, new_sell_px, last_price)
                    return  # next tick uses the new targets
                # Time-based reanchor: if we've been waiting to rebuy for
                # too long with the market above our buy target, walk forward.
                # Only fires when actually priced-out (last_price > buy_px);
                # a sleeve sitting AT its buy target isn't stuck, it's working.
                if spread > 0 and sc.time_reanchor_secs > 0 \
                        and last_price > sc.buy_px and ss.armed_buy_since_ts:
                    import time as _time
                    elapsed = _time.time() - float(ss.armed_buy_since_ts)
                    if elapsed >= float(sc.time_reanchor_secs):
                        new_buy_px = self._snap_to_tick(last_price - spread / 2)
                        new_sell_px = self._snap_to_tick(last_price + spread / 2)
                        # No-op guard (same rationale as the price-threshold path
                        # above): if tick-snap produces the same buy/sell we
                        # already have, don't fire.
                        if new_buy_px == sc.buy_px and new_sell_px == sc.sell_px:
                            return
                        new_buy_px, new_sell_px = self._clamp_buy_below_last_sale(
                            sc, ss, new_buy_px, new_sell_px, source="time_reanchor")
                        if new_buy_px == sc.buy_px and new_sell_px == sc.sell_px:
                            return
                        self._record(
                            "sleeve_time_reanchor",
                            sleeve_id=sc.id, sleeve_name=sc.name,
                            elapsed_secs=round(elapsed, 1),
                            timeout_secs=sc.time_reanchor_secs,
                            old_buy=sc.buy_px, new_buy=new_buy_px,
                            last_price=last_price,
                        )
                        self._reanchor_sleeve(sc, ss, new_buy_px, new_sell_px, last_price)
                        return
                # Volatility-aware reanchor: if last_price is at/above the top
                # N% of recent bars, we're at (or near) a run's peak — market
                # is trending up, not oscillating around our target. Walk
                # forward. Requires enough history to compute the percentile.
                if spread > 0 and sc.vol_reanchor_percentile > 0 \
                        and last_price > sc.buy_px:
                    history = self._sleeve_price_history.get(sc.id)
                    win = int(sc.vol_reanchor_window or 60)
                    if history and len(history) >= win:
                        recent = sorted(list(history)[-win:])
                        idx = int(len(recent) * float(sc.vol_reanchor_percentile) / 100.0)
                        idx = min(idx, len(recent) - 1)
                        threshold = recent[idx]
                        if last_price >= threshold:
                            new_buy_px = self._snap_to_tick(last_price - spread / 2)
                            new_sell_px = self._snap_to_tick(last_price + spread / 2)
                            # No-op guard (same rationale as the price-threshold
                            # path above): if tick-snap produces the same
                            # buy/sell we already have, don't fire.
                            if new_buy_px == sc.buy_px and new_sell_px == sc.sell_px:
                                return
                            new_buy_px, new_sell_px = self._clamp_buy_below_last_sale(
                                sc, ss, new_buy_px, new_sell_px, source="vol_reanchor")
                            if new_buy_px == sc.buy_px and new_sell_px == sc.sell_px:
                                return
                            self._record(
                                "sleeve_vol_reanchor",
                                sleeve_id=sc.id, sleeve_name=sc.name,
                                percentile=sc.vol_reanchor_percentile,
                                threshold=round(threshold, 4),
                                old_buy=sc.buy_px, new_buy=new_buy_px,
                                last_price=last_price,
                                bars_analyzed=win,
                            )
                            self._reanchor_sleeve(sc, ss, new_buy_px, new_sell_px, last_price)
                            return
                # Trend gate: refuse to arm a buy while price is under the
                # M-bar SMA of this sleeve's rolling price history. Prevents
                # the buy leg from filling into a downtrend (falling knife).
                # Only gates while trending down — reanchor rules above handle
                # the "priced-out to the upside" case.
                if not self._sleeve_trend_ok_for_buy(sc, last_price):
                    return
                # [crew] Cascade re-entry gate. When the crash guard is on, do
                # NOT rebuy into an active crash or a dead-cat bounce — the
                # "short uptick then another big crash" trap Adam keeps hitting.
                # cascade_state waits for a SIGNAL-BASED all-clear (VPIN
                # subsided + volatility contracting), not a fixed clock
                # (Lehmann short-term reversal is real but short-lived;
                # Lillo-Farmer long-memory flow + Engle/Bollerslev vol
                # clustering say the selling usually isn't done). Fail-safe:
                # permissive on thin history / errors so it never stalls a
                # sleeve in normal markets.
                if getattr(sc, "crash_guard_enabled", False) and not self._within_roll_blackout():
                    try:
                        import cascade_state
                        obs = list(self._sleeve_ms_history.get(sc.id, []) or [])
                        casc = cascade_state.assess(obs)
                        if casc.get("phase") == "crashing" or casc.get("second_leg_risk"):
                            self._record(
                                "cascade_reentry_hold",
                                sleeve_id=sc.id, sleeve_name=sc.name,
                                phase=casc.get("phase"),
                                vpin_now=casc.get("vpin_now"),
                                reason=casc.get("reason"),
                            )
                            return
                    except Exception as e:
                        self._record("cascade_reentry_error", sleeve_id=sc.id, error=str(e))
                # [crew] Velocity guard — don't buy into a fast/forced drop.
                # Self-scaling (Lee-Mykland jump vs this instrument's own vol) +
                # flow-continuation (VPIN/OFI/Kyle/OBI). Holds the buy only while
                # the drop is dangerous, then releases so it fills at target.
                # Opt-in; the smarter replacement for the blanket bounce-wait.
                # Fail-safe: no data -> doesn't block.
                if getattr(sc, "velocity_gate_enabled", False):
                    try:
                        import knife_gate
                        _kh = list(self._sleeve_price_history.get(sc.id, []) or [])
                        _kr = [(_kh[i] - _kh[i - 1]) / _kh[i - 1]
                               for i in range(1, len(_kh)) if _kh[i - 1]]
                        _kms = self.ms.snapshot() if self.ms else {}
                        _kg = knife_gate.knife_gate(_kr, ms=_kms)
                        if _kg.get("block"):
                            self._record("entry_velocity_hold", sleeve_id=sc.id, sleeve_name=sc.name,
                                         velocity=_kg.get("velocity"), reason=_kg.get("reason"))
                            return
                    except Exception as e:
                        self._record("velocity_gate_error", sleeve_id=sc.id, error=str(e))
                # Trailing-buy (Livermore / Turtle / Le Beau). When enabled,
                # returns None until mark bounces buy_trail_distance above the
                # local low — otherwise returns sc.buy_px (identical to legacy
                # behavior). arm_price is capped at sc.buy_px so we never
                # overpay vs the original target.
                arm_price = self._trailing_buy_ready(sc, ss, last_price)
                if arm_price is None:
                    return  # still tracking the low, don't arm this tick
                self._maybe_emit_ml_shadow(sc)
                eff_qty = self._kelly_adjusted_qty(sc, ss)
                eff_price = self._adaptive_spread_price(sc, "BUY", arm_price)
                ms_qty, ms_px = self._sleeve_ms_adjust(sc, ss, "BUY", eff_qty, eff_price, last_price)
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
            # Same-tick re-arm: after a fill, immediately place the next-leg
            # order rather than waiting ~1s for the next tick. Fixes the gap
            # where a fast opposite-side move (e.g., a downward wick after
            # a sell fill) could trade past the next target before we've
            # placed the order to catch it. Recursion terminates naturally:
            # the arm block either sets live_order_id or returns, and the
            # next entry re-hits the fresh live_order_id.
            if ss.state != SleeveStateEnum.HALTED and not ss.live_order_id:
                self._sleeve_step(sc, ss, last_price)
            return
        elif status in ("CANCELLED", "EXPIRED", "UNKNOWN"):
            # CRITICAL SAFETY (2026-07-15 root cause of HYPE stuck-state bug):
            # if Coinbase reports a non-FILLED status but says some qty was
            # filled (or reports UNKNOWN when the fill actually happened),
            # we MUST credit the fill BEFORE clearing live_order_id — else
            # the sleeve gets stuck in the pre-fill state forever while
            # Coinbase holds the position. Same class as 2026-07-12 bug.
            if filled > 0:
                self._record("sleeve_credited_partial_before_clear",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             order_id=ss.live_order_id, status=status,
                             filled_qty=filled)
                ss.filled_qty = filled
                self._sleeve_on_fill(sc, ss, st.get("average_filled_price"))
                # Fall through — clear the (now credited) id and re-arm next tick.
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
            # Adam 2026-07-15 CRITICAL: if the broker refuses (typically the
            # no-short guard — position already ≤ 0 so this sell would go
            # negative), HALT the sleeve immediately instead of returning
            # silently. Otherwise the caller (_sleeve_hybrid_step) loops
            # every 5s trying the same refused sell forever. CU 2026-07-15
            # ran this loop for minutes before Adam manually intervened.
            try:
                ss.live_order_id = self.b.place_market("SELL", sc.qty)
            except Exception as _e:
                self._record("sleeve_market_sell_refused",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    side="SELL", qty=sc.qty, price=last_price,
                    trail_exit=trail_exit, hybrid_timeout=hybrid_timeout,
                    error=f"{type(_e).__name__}: {_e}",
                    severity="critical")
                self._sleeve_halt(sc, ss,
                    f"market sell refused: {type(_e).__name__}: {_e} "
                    f"— sleeve state may be desynced with position; manual review")
                return
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
            self._prepare_post_trail_wait(sc, ss)
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
        # Snap the limit price to the product's tick_size. Belt-and-suspenders
        # for configs saved before the reanchor snap fix — Coinbase rejects
        # off-tick prices with INVALID_PRICE_PRECISION and the sleeve then
        # spins forever emitting sleeve_arm_failed with no order on the book.
        price = self._snap_to_tick(price)
        # CRITICAL SAFETY (2026-07-15): normal-path over-accumulation guard.
        # Mirrors the ghost-force-arm check in _maybe_force_arm_ghost_order.
        # Adam surfaced this session: HYPE sleeve was in WAITING_FOR_BUY state
        # (stale — missed crediting a fill) and armed a NEW buy at $67.17
        # while Coinbase already held 1 contract at $66.81. Would have
        # doubled the position. Refuse the arm when Coinbase's actual
        # position already >= sum(all sleeves' qty) + core.
        if side == "BUY":
            try:
                current_pos = int(self.b.position_qty() or 0)
                total_sleeve_qty = 0
                for other_ss in (self.s.sleeves or {}).values():
                    other_sc = self._sleeve_cfg_by_id(other_ss.id) if hasattr(
                        self, "_sleeve_cfg_by_id") else None
                    if other_sc is None:
                        total_sleeve_qty += int(getattr(sc, "qty", 1) or 1)
                    else:
                        total_sleeve_qty += int(getattr(other_sc, "qty", 1) or 1)
                intended_position = total_sleeve_qty + int(
                    getattr(self.cfg, "core_qty", 0) or 0)
                if current_pos >= intended_position:
                    self._record(
                        "sleeve_arm_skipped_position_full",
                        sleeve_id=sc.id, sleeve_name=sc.name,
                        side=side, qty=qty, price=price,
                        current_position=current_pos,
                        intended_position=intended_position,
                        total_sleeve_qty=total_sleeve_qty,
                        core_qty=int(getattr(self.cfg, "core_qty", 0) or 0),
                        reason="portfolio position >= sum(all sleeve qtys) + core; sleeve state is stale (missed a fill?)",
                    )
                    return
            except Exception as e:
                # Fail closed for safety — if we can't check the position,
                # don't arm. A missed opportunity beats a doubled position.
                self._record("sleeve_arm_position_check_failed",
                             sleeve_id=sc.id, side=side, error=str(e))
                return
        # Portfolio circuit breaker (Van Tharp rule: 'stop trading when things
        # go wrong'). If aggregate swing P&L across the tenant drops below the
        # configured drawdown threshold, block all new arms until it recovers.
        # Existing orders keep processing — never abandon a live order midflight.
        try:
            import portfolio_risk
            if portfolio_risk.is_halted(self.store, self.tenant_id):
                self._record(
                    "sleeve_arm_skipped_portfolio_halt",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    side=side, qty=qty, price=price,
                    reason=portfolio_risk.halt_reason(self.store, self.tenant_id),
                )
                return
        except Exception as e:
            self._record("portfolio_risk_check_failed",
                         sleeve_id=sc.id, error=str(e))
        # News blackout (Van Tharp / Cartea-Jaimungal rule): scheduled
        # macro events (FOMC, CPI, NFP) whipsaw silver/futures ±$1 in 30s.
        # Any sleeve with news_blackout_enabled respects its configured tier:
        #   tier 2+ → pause new arms during the blackout window
        #   tier 3  → also flatten (handled by _maybe_trigger_stop_loss path)
        if getattr(sc, "news_blackout_enabled", False):
            try:
                from news_calendar import blackout_for
                active = blackout_for()
                if active and active["tier"] >= int(sc.news_blackout_tier or 2):
                    self._record(
                        "sleeve_arm_skipped_news_blackout",
                        sleeve_id=sc.id, sleeve_name=sc.name,
                        side=side, qty=qty, price=price,
                        event=active["name"], tier=active["tier"],
                        blackout_ends_ts=active["end_ts"],
                    )
                    return
            except Exception as e:
                self._record("news_blackout_check_failed",
                             sleeve_id=sc.id, error=str(e))
        # Book-imbalance gate (Chan/Harris rule): refuse to arm a leg whose
        # expected direction fights the current book pressure. Cheap: reads
        # a 5s-cached top-25 snapshot from Coinbase.
        if getattr(sc, "book_imbalance_gate_enabled", False):
            if not self._book_imbalance_ok_for(sc, side):
                self._record(
                    "sleeve_arm_skipped_book_imbalance",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    side=side, qty=qty, price=price,
                )
                return
        # Trade-tape OFI gate: refuse to arm when the EXECUTED trade tape
        # (last N seconds of signed prints) opposes our direction. Stronger
        # signal than book OBI per Cont-Kukanov-Stoikov 2014 — resting depth
        # can be spoofed, executed volume can't. Zero cost if the
        # MicrostructureFilter isn't wired (permissive-default).
        if getattr(sc, "trade_ofi_gate_enabled", False):
            if not self._trade_ofi_ok_for(sc, side):
                ms = getattr(self, "ms", None)
                ofi_val = None
                if ms is not None:
                    try:
                        ofi_val = ms.trade_ofi.ofi(
                            float(getattr(sc, "trade_ofi_window_secs", 60.0) or 60.0)
                        )
                    except Exception:
                        pass
                self._record(
                    "sleeve_arm_skipped_trade_ofi",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    side=side, qty=qty, price=price,
                    trade_ofi=ofi_val,
                    threshold=float(getattr(sc, "trade_ofi_threshold", 0.65) or 0.65),
                )
                return
        # Cross-asset correlation gate: don't fresh-long silver into a
        # copper crash (or oil into a natgas dump). Only gates BUY arms —
        # SELL arms must always be allowed so we can exit into a crash
        # instead of being blocked from cutting risk. Dynamic-correlation
        # mode (opt-in) also inspects any product with rolling-30d Pearson
        # ≥ threshold, catching macro cross-family co-movement.
        if getattr(sc, "correlation_gate_enabled", False):
            try:
                import correlation
                crash = correlation.peer_crash_check(
                    self.store, self.tenant_id, self.symbol, side,
                    window_secs=float(getattr(sc, "correlation_window_secs", 3600.0)),
                    crash_threshold_pct=float(getattr(sc, "correlation_crash_pct", 3.0)),
                    use_dynamic_correlation=bool(getattr(sc, "correlation_dynamic_enabled", False)),
                    correlation_threshold=float(getattr(sc, "correlation_dynamic_threshold", 0.6)),
                )
                if crash:
                    self._record(
                        "sleeve_arm_skipped_peer_crash",
                        sleeve_id=sc.id, sleeve_name=sc.name,
                        side=side, qty=qty, price=price,
                        **crash,
                    )
                    return
            except Exception as e:
                self._record("correlation_check_failed",
                             sleeve_id=sc.id, error=str(e))
        # Funding-rate gate (crypto perps). Block BUY arms when funding is
        # strongly positive — you'd be paying to hold long during a probable
        # reversal (Aksoy-Cheng / Hasbrouck).
        if getattr(sc, "funding_gate_enabled", False) and side == "BUY":
            try:
                import funding_signals
                if funding_signals.is_perp(self.symbol):
                    snap = self.store.get_snapshot(self.tenant_id, self.symbol) or {}
                    fr = funding_signals.funding_rate_of(snap)
                    thr = float(getattr(sc, "funding_gate_threshold", 0.0005) or 0.0005)
                    if not funding_signals.funding_gate_ok_for_buy(fr, thr):
                        self._record(
                            "sleeve_arm_skipped_funding_positive",
                            sleeve_id=sc.id, sleeve_name=sc.name,
                            side=side, qty=qty, price=price,
                            funding_rate=fr, threshold=thr,
                        )
                        return
            except Exception as e:
                self._record("funding_check_failed",
                             sleeve_id=sc.id, error=str(e))
        # Cross-exchange fair-value gate (Binance reference for crypto).
        # Refuse arms when Coinbase price diverges too far from Binance mid.
        if getattr(sc, "crossex_gate_enabled", False):
            try:
                import crossex
                ok, div = crossex.crossex_gate_ok(
                    self.symbol,
                    float(price or 0),
                    float(getattr(sc, "crossex_max_divergence_pct", 1.0) or 1.0),
                )
                if not ok:
                    self._record(
                        "sleeve_arm_skipped_crossex_divergence",
                        sleeve_id=sc.id, sleeve_name=sc.name,
                        side=side, qty=qty, price=price,
                        divergence_pct=div,
                        max_pct=float(getattr(sc, "crossex_max_divergence_pct", 1.0) or 1.0),
                    )
                    return
            except Exception as e:
                self._record("crossex_check_failed",
                             sleeve_id=sc.id, error=str(e))
        # For SELL: capture cost basis of the contracts we're about to sell so
        # realized P/L on the fill uses the ACTUAL price paid, not sc.buy_px.
        if side == "SELL" and ss.sell_entry_avg is None:
            ss.sell_entry_avg = self._sleeve_avg_entry(sc) or float(sc.buy_px)
        # Penny-inside: if the target price is within N ticks of the current
        # best on our side, snap one tick INSIDE to jump the queue at that
        # level. Only applies when we're close to market — never widens a
        # fresh arm placed far from the book.
        original_px = price
        if getattr(sc, "penny_inside_enabled", False):
            price = self._penny_inside_price(sc, side, price)
        set_src = getattr(self.b, "set_pending_source", None)
        if callable(set_src):
            set_src("strategy", strategy_id=sc.id)
        post_only = bool(getattr(sc, "post_only_enabled", False))
        # Post-only would-cross guard. Adam hit this on OIL Model B: sell fired
        # at $75.02, buy target set at $74.76. Market later dropped to $74.30
        # (below the buy target). A limit BUY at $74.76 with market at $74.30
        # would be a TAKER order (crosses the ask to grab liquidity) — Coinbase
        # rejects post-only takers. The sleeve then spun in ARMED_BUY, retrying
        # forever, never completing the cycle. Fix: peek at the book, and if
        # our limit would cross, drop post_only for THIS arm so we take the
        # (better-than-limit) fill and complete the cycle. Losing the maker
        # rebate on one rebuy is far better than a dead cycle. Same guard for
        # SELL: if sell price is below best bid, we'd take liquidity → drop
        # post_only rather than infinite-spin.
        if post_only:
            get_book = getattr(self.b, "get_orderbook", None)
            if callable(get_book):
                try:
                    book = get_book(limit=1)
                except Exception:
                    book = None
                if book:
                    bids = book.get("bids") or []
                    asks = book.get("asks") or []
                    best_bid = float(bids[0][0]) if bids else 0.0
                    best_ask = float(asks[0][0]) if asks else 0.0
                    would_cross = (
                        (side == "BUY" and best_ask > 0 and price >= best_ask)
                        or (side == "SELL" and best_bid > 0 and price <= best_bid)
                    )
                    if would_cross:
                        self._record(
                            "post_only_dropped_would_cross",
                            sleeve_id=sc.id, sleeve_name=sc.name,
                            side=side, price=price,
                            best_bid=best_bid, best_ask=best_ask,
                        )
                        post_only = False
        # Cross-process dedup lock (arm_dedup, added 2026-07-14 after the
        # two-writer duplicate-orders incident). On TOP of the in-process
        # guard at 2423 (`not ss.live_order_id`) — the 2423 guard is
        # single-process-only and cannot see another writer's state, so
        # two processes both pass their own guard and both place the same
        # order. This Redis SETNX lock catches that. Fail-closed: if
        # Redis is unreachable, we BLOCK the arm and emit a loud health
        # event, because losing an arm cycle beats double-placing a
        # real-money order.
        try:
            import arm_dedup as _dedup
            _arm_lock = _dedup.try_acquire_arm_lock(
                self.store, self.tenant_id, self.symbol, side, price,
                float(getattr(self.cfg, "tick_size", 0.0001) or 0.0001))
        except Exception as _le:
            _arm_lock = {"acquired": False, "reason": "unavailable",
                         "error": f"{type(_le).__name__}: {_le}"}
        if not _arm_lock.get("acquired"):
            self._record(
                ("sleeve_arm_blocked_dedup_lock"
                 if _arm_lock.get("reason") == "held"
                 else "sleeve_arm_blocked_dedup_lock_unavailable"),
                sleeve_id=sc.id, sleeve_name=sc.name,
                side=side, qty=qty, price=price,
                reason=_arm_lock.get("reason"),
                error=_arm_lock.get("error"),
                lock_key=_arm_lock.get("key"),
            )
            return
        try:
            # Not every broker signature supports post_only (paper backtest
            # broker fixtures, etc.). Try with, fall back without.
            try:
                ss.live_order_id = self.b.place_limit(side, qty, price,
                                                     post_only=post_only)
            except TypeError:
                ss.live_order_id = self.b.place_limit(side, qty, price)
        except Exception as e:
            # Post-only rejection safety net. If the book peek above missed a
            # would-cross (race between book snapshot and order submission,
            # or non-standard error) and Coinbase rejected the post-only
            # order, retry once WITHOUT post_only so the cycle can complete.
            err = str(e)
            looks_like_post_only_reject = post_only and (
                "post" in err.lower() and ("only" in err.lower() or "cross" in err.lower())
                or "would cross" in err.lower()
                or "immediate" in err.lower() and "reject" in err.lower()
            )
            if looks_like_post_only_reject:
                try:
                    try:
                        ss.live_order_id = self.b.place_limit(side, qty, price,
                                                             post_only=False)
                    except TypeError:
                        ss.live_order_id = self.b.place_limit(side, qty, price)
                    self._record(
                        "post_only_retried_without",
                        sleeve_id=sc.id, sleeve_name=sc.name,
                        side=side, qty=qty, price=price,
                        original_error=err,
                    )
                    post_only = False  # for the sleeve_order_placed record below
                except Exception as e2:
                    self._record("sleeve_arm_failed", sleeve_id=sc.id, error=str(e2),
                                 side=side, qty=qty, price=price, post_only=False,
                                 post_only_retry_after=err)
                    return
            else:
                self._record("sleeve_arm_failed", sleeve_id=sc.id, error=err,
                             side=side, qty=qty, price=price, post_only=post_only)
                return
        self._record(
            "sleeve_order_placed",
            sleeve_id=sc.id, sleeve_name=sc.name,
            side=side, qty=qty, price=price, order_id=ss.live_order_id,
            post_only=post_only,
            **({"penny_inside_from": original_px} if price != original_px else {}),
            **({"cost_basis": ss.sell_entry_avg} if side == "SELL" else {}),
        )

    def _book_imbalance_ok_for(self, sc, side: str) -> bool:
        """Return False if the current top-N book imbalance strongly opposes
        this side (Chan/Harris: don't fight the tape). Cached 5s so this
        costs at most ~1 book fetch per product per 5s under heavy tick
        load. Returns True (permissive) on any error — the gate should
        NEVER block trading when the book fetch fails, only when the book
        actively opposes us.
        """
        get_book = getattr(self.b, "get_orderbook", None)
        if not callable(get_book):
            return True
        import time as _time
        now = _time.time()
        cache = getattr(self, "_book_cache", None)
        if cache and (now - cache["ts"]) < 5.0:
            book = cache["book"]
        else:
            try:
                book = get_book(limit=25)
            except Exception:
                return True
            self._book_cache = {"ts": now, "book": book}
        levels = max(1, int(getattr(sc, "book_imbalance_depth_levels", 5)))
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            return True  # empty book (session closed / broker error) → don't gate
        bid_size = sum(s for _, s in bids[:levels])
        ask_size = sum(s for _, s in asks[:levels])
        total = bid_size + ask_size
        if total <= 0:
            return True
        bid_ratio = bid_size / total
        # bid_ratio > threshold means buy pressure dominant → sellers about
        # to get run through. Refuse to arm a SELL right now — wait for the
        # imbalance to normalize. Symmetrical for BUYs on ask pressure.
        if side == "SELL":
            thr = float(getattr(sc, "book_imbalance_sell_threshold", 0.65) or 0.65)
            if bid_ratio > thr:
                return False
        else:  # BUY
            thr = float(getattr(sc, "book_imbalance_buy_threshold", 0.65) or 0.65)
            if (1.0 - bid_ratio) > thr:  # ask pressure = 1 - bid pressure
                return False
        return True

    def _kelly_adjusted_qty(self, sc, ss) -> int:
        """Apply Kelly-fraction sizing if enabled. Never sizes UP (only ≤ cfg.qty)."""
        if not getattr(sc, "kelly_enabled", False):
            return int(sc.qty)
        try:
            import kelly
            recent = list(getattr(ss, "recent_cycle_pnls", []) or [])
            mult = kelly.compute_kelly_multiplier(
                recent,
                kelly_fraction=float(getattr(sc, "kelly_fraction", 0.25) or 0.25),
                min_cycles=int(getattr(sc, "kelly_min_cycles", 8) or 8),
            )
            return kelly.size_from_qty(int(sc.qty), mult)
        except Exception as e:
            self._record("kelly_compute_failed", sleeve_id=sc.id, error=str(e))
            return int(sc.qty)

    def _adaptive_spread_price(self, sc, side: str, arm_price: float) -> float:
        """When adaptive spread is enabled, widen the arm price to account
        for current realized vol vs baseline. Returns the (possibly wider)
        arm price. No effect when disabled or insufficient data."""
        if not getattr(sc, "adaptive_spread_enabled", False):
            return arm_price
        try:
            import adaptive_spread
            snap = self.store.get_snapshot(self.tenant_id, self.symbol) or {}
            history = snap.get("price_history") or []
            window = float(getattr(sc, "adaptive_spread_vol_window_secs", 300.0) or 300.0)
            rv = adaptive_spread.realized_vol_from_history(history, window_secs=window)
            # Baseline: compute rv over a longer window as the "normal" reference.
            baseline = adaptive_spread.realized_vol_from_history(history,
                                                                  window_secs=window * 12)
            mult = adaptive_spread.spread_multiplier(
                rv, baseline,
                max_multiplier=float(getattr(sc, "adaptive_spread_max_multiplier", 2.0) or 2.0),
            )
            if mult <= 1.0:
                return arm_price
            new_sell, new_buy = adaptive_spread.adjusted_targets(sc.sell_px, sc.buy_px, mult)
            widened = new_sell if side == "SELL" else new_buy
            self._record(
                "adaptive_spread_widened",
                sleeve_id=sc.id, sleeve_name=sc.name,
                side=side, multiplier=round(mult, 3),
                orig_price=arm_price, widened_price=widened,
                realized_vol=round(rv, 6) if rv else None,
                baseline_vol=round(baseline, 6) if baseline else None,
            )
            return widened
        except Exception as e:
            self._record("adaptive_spread_failed", sleeve_id=sc.id, error=str(e))
            return arm_price

    def _maybe_emit_ml_shadow(self, sc) -> None:
        """If ml_shadow_enabled, extract features + run predictor + log signal.
        Purely observational — does not gate the arm."""
        if not getattr(sc, "ml_shadow_enabled", False):
            return
        try:
            import ml_predictor
            snap = self.store.get_snapshot(self.tenant_id, self.symbol) or {}
            features = ml_predictor.extract_features(snap)
            if not features:
                return
            score = ml_predictor.predict(features)
            threshold = float(getattr(sc, "ml_signal_threshold", 0.3) or 0.3)
            if abs(score) < threshold:
                return
            baseline_mark = float(snap.get("last_mark") or 0)
            ml_predictor.emit_ml_shadow_signal(
                self.store, self.tenant_id, self.symbol,
                features, score, baseline_mark,
            )
        except Exception as e:
            self._record("ml_shadow_failed", sleeve_id=sc.id, error=str(e))

    def _trade_ofi_ok_for(self, sc, side: str) -> bool:
        """Trade-tape OFI gate. Mirror of _book_imbalance_ok_for but reads
        the EXECUTED trade tape via microstructure.trade_ofi. Cont-Kukanov-
        Stoikov (2014) + Cartea-Jaimungal: trade OFI is a stronger short-
        term direction predictor than book OBI because resting orders can
        be spoofed but executed trades cannot.

        Returns False (BLOCK the arm) when the OFI magnitude exceeds the
        threshold AND the sign opposes the intended arm side:
          SELL + OFI > +threshold → refuse (buyers dominant, price likely
            to keep rising through our sell target)
          BUY  + OFI < -threshold → refuse (sellers dominant, don't fill
            into continued weakness)

        Permissive-default: True when MicrostructureFilter isn't wired or
        the trade tape hasn't accumulated enough samples yet.
        """
        ms = getattr(self, "ms", None)
        if ms is None:
            return True
        try:
            window = float(getattr(sc, "trade_ofi_window_secs", 60.0) or 60.0)
            ofi = ms.trade_ofi.ofi(window)
        except Exception:
            return True
        if ofi is None:
            return True
        thr = float(getattr(sc, "trade_ofi_threshold", 0.65) or 0.65)
        if side.upper() == "SELL" and ofi > thr:
            return False
        if side.upper() == "BUY" and ofi < -thr:
            return False
        return True

    def _penny_inside_price(self, sc, side: str, target_price: float) -> float:
        """Snap one tick INSIDE the best place-to-be for queue priority.

        Two-tier logic (Larry Harris / Rishi Narang):
        1. WALL-AWARE (preferred): if there's a WALL (level with >= wall_min_ratio
           of top-N total size) within max_dist of target_price on our side,
           snap one tick INSIDE that wall. When the wall clears we fill FIRST
           at a way better price than resting AT the wall itself.
        2. BEST-PRICE fallback: if no wall found, snap one tick inside the
           current best on our side (the original penny-inside logic).

        Uses the 5s-cached book snapshot from _book_imbalance_ok_for when
        available. Returns the original target if the broker doesn't expose
        depth or the target is too far from market.
        """
        tick = float(self.cfg.tick_size or 0.005)
        if tick <= 0:
            return target_price
        max_dist = float(sc.penny_inside_max_ticks or 5) * tick

        # Try wall-aware first (needs book depth). Reuse the same cached book
        # the imbalance gate populated — one book fetch shared across both.
        book = None
        get_book = getattr(self.b, "get_orderbook", None)
        if callable(get_book):
            import time as _time
            now = _time.time()
            cache = getattr(self, "_book_cache", None)
            if cache and (now - cache["ts"]) < 5.0:
                book = cache["book"]
            else:
                try:
                    book = get_book(limit=25)
                    self._book_cache = {"ts": now, "book": book}
                except Exception:
                    book = None
        if book and (book.get("bids") or book.get("asks")):
            wall_price = self._find_wall(book, side, target_price, max_dist)
            if wall_price is not None:
                # Snap one tick INSIDE the wall on our side. SELL side wall
                # is above us in price → snap wall - tick. BUY side wall is
                # below us → snap wall + tick.
                if side == "SELL":
                    candidate = self._snap_to_tick(wall_price - tick)
                else:
                    candidate = self._snap_to_tick(wall_price + tick)
                # Sanity: must remain on the correct side of top-of-book.
                best_bid, best_ask = self._best_from_book(book)
                if side == "SELL" and candidate > best_bid:
                    return candidate
                if side == "BUY" and (best_ask <= 0 or candidate < best_ask):
                    return candidate

        # Best-price fallback (no depth or no wall in range).
        try:
            spec = self.b.contract_spec() if hasattr(self.b, "contract_spec") else {}
            best_bid = float(spec.get("best_bid") or 0)
            best_ask = float(spec.get("best_ask") or 0)
        except Exception:
            return target_price
        if side == "SELL":
            if best_ask <= 0 or abs(target_price - best_ask) > max_dist:
                return target_price
            candidate = self._snap_to_tick(best_ask - tick)
            if candidate > best_bid and candidate < target_price + max_dist:
                return candidate
        else:
            if best_bid <= 0 or abs(target_price - best_bid) > max_dist:
                return target_price
            candidate = self._snap_to_tick(best_bid + tick)
            if candidate < best_ask and candidate > target_price - max_dist:
                return candidate
        return target_price

    def _best_from_book(self, book: dict) -> tuple[float, float]:
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = float(bids[0][0]) if bids else 0.0
        best_ask = float(asks[0][0]) if asks else 0.0
        return best_bid, best_ask

    def _find_wall(self, book: dict, side: str, target_price: float,
                    max_dist: float, wall_min_ratio: float = 0.35,
                    levels: int = 10) -> float | None:
        """Return the price of the biggest wall on our side within max_dist
        of target_price, OR None if no such wall exists. 'Wall' = a single
        price level whose size >= wall_min_ratio × sum(top-`levels` sizes)
        on that side. Default 0.35 means a level with 35%+ of the top-10
        depth qualifies — anything materially bigger than the median level.
        """
        side_rows = (book.get("asks") or []) if side == "SELL" else (book.get("bids") or [])
        if not side_rows:
            return None
        top = side_rows[:levels]
        total_size = sum(sz for _, sz in top)
        if total_size <= 0:
            return None
        min_wall_size = total_size * wall_min_ratio
        best_wall_px = None
        best_wall_sz = 0.0
        for px, sz in top:
            if sz < min_wall_size:
                continue
            if abs(px - target_price) > max_dist:
                continue
            if sz > best_wall_sz:
                best_wall_sz = sz
                best_wall_px = px
        return best_wall_px

    def _sleeve_on_fill(self, sc: SleeveConfig, ss: SleeveState, fill_price) -> None:
        # Capture order_id BEFORE clearing so the fill event carries it — makes
        # repair scripts (find unclaimed order_ids) trivial to write.
        filled_order_id = ss.live_order_id
        self._record(
            "sleeve_order_filled",
            sleeve_id=sc.id, sleeve_name=sc.name,
            leg=ss.state.value, filled_qty=sc.qty,
            average_filled_price=fill_price,
            order_id=filled_order_id,
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
            # Trailing-buy state reset too — new cycle, no prior low to honor.
            ss.buy_trail_armed = False
            ss.buy_trail_low_water = 0.0
            # Winning cycle completed → reset the consecutive-stop counter
            # (breaks any streak that was accumulating). Also clear the
            # ratcheting HWM — next cycle starts fresh at the new basis.
            ss.consecutive_stops = 0
            ss.stop_loss_hwm = None
            # Timestamp for time-based reanchor — starts counting from the
            # moment this cycle finished the sell leg.
            import time as _time
            ss.armed_buy_since_ts = _time.time()
            # Cycle P&L tracking (for loss-streak auto-disable + TCA display).
            # A cycle "won" if this fill's realized delta > 0; "lost" if <= 0.
            cycle_pnl = float(ss.realized_pnl) - float(ss.last_cycle_realized or 0.0)
            ss.last_cycle_realized = float(ss.realized_pnl)
            recent = list(getattr(ss, "recent_cycle_pnls", []) or [])
            recent.append(round(cycle_pnl, 4))
            if len(recent) > 20:
                recent = recent[-20:]
            ss.recent_cycle_pnls = recent
            # Expert-driven re-entry (2026-07-13). After a sell, compute a
            # buy_px that respects regime (Kaufman), cycle phase (Ehlers),
            # higher-TF direction (Elder), OU mean-reversion band (Chan),
            # statistical oversold (Connors), and cap qty by risk-of-ruin
            # (Vince). Fail-safe — falls back to legacy behavior on any error.
            try:
                self._maybe_expert_reanchor_after_sell(sc, ss, fill)
            except Exception as _e:
                self._record("expert_reanchor_error",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             error=str(_e))
            if cycle_pnl > 0:
                ss.cycles_losing_streak = 0
            else:
                ss.cycles_losing_streak = int(getattr(ss, "cycles_losing_streak", 0) or 0) + 1
            # TCA slippage: compare fill price to sell_px (what we ARMED at).
            # Positive slippage = we filled BETTER than target (rare on limit).
            # Negative = we filled WORSE (market sell during a drop, penny-inside
            # sacrifice, etc.). Logged in the cycle_completed event so post-mortem
            # can spot sleeves consistently getting bad fills.
            expected_px = float(sc.sell_px or 0)
            slippage_price = fill - expected_px if expected_px > 0 else 0.0
            slippage_dollars = slippage_price * self.cfg.contract_size * sc.qty
            self._record(
                "sleeve_cycle_completed",
                sleeve_id=sc.id, sleeve_name=sc.name,
                cycles=ss.cycles,
                cost_basis=basis, fill_price=fill,
                gross=gross, fees=half_fee,
                realized_pnl_total=ss.realized_pnl,
                cycle_pnl=round(cycle_pnl, 4),
                cycles_losing_streak=ss.cycles_losing_streak,
                expected_sell_px=expected_px,
                slippage_price=round(slippage_price, 4),
                slippage_dollars=round(slippage_dollars, 2),
            )
            # Auto-disable: N losing cycles in a row → halt the sleeve. Van
            # Tharp: stop trading when things go wrong. Prevents watching a
            # broken strategy bleed for weeks.
            auto_disable_thr = int(getattr(sc, "auto_disable_after_losses", 0) or 0)
            if auto_disable_thr > 0 and ss.cycles_losing_streak >= auto_disable_thr:
                reason = (f"auto-disabled after {ss.cycles_losing_streak} losing "
                          f"cycles in a row (config threshold {auto_disable_thr})")
                self._sleeve_halt(sc, ss, reason)
                self._record("sleeve_auto_disabled_loss_streak",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             streak=ss.cycles_losing_streak,
                             threshold=auto_disable_thr,
                             recent_pnls=recent)
                return
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
        # Snapshot the state BEFORE overwriting to HALTED so resume can restore
        # it. Without this, resume forces every sleeve to ARMED_SELL — which
        # sells the position AGAIN on a sleeve that halted while ARMED_BUY,
        # bleeding contracts on every halt/resume cycle. Adam's OIL position
        # drained from 20 → 0 that way before this fix landed.
        if ss.state != SleeveStateEnum.HALTED:
            ss.pre_halt_state = ss.state.value
        ss.state = SleeveStateEnum.HALTED
        ss.halt_reason = reason or "halted"
        self._record("sleeve_halted", sleeve_id=sc.id, sleeve_name=sc.name,
                     reason=reason, pre_halt_state=ss.pre_halt_state)

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
