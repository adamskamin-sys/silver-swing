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
from strategies import ExitStrategy, SellDirective, BuyDirective, strategy_by_name
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

    # Risk governor (Jim Paul). Defaults are disabled (0 / 1e9) so new
    # products don't inherit SLR-specific bands. Live-tenant boot sets
    # these from mark ± 20×ATR via _ensure_live_abort_bounds().
    abort_below: float = 0.0
    abort_above: float = 1e9

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
    # Adam 2026-07-20: rolling window of actual per-side fees ($/contract) from
    # recent fills — used to auto-calibrate cfg.fee_per_contract_roundtrip
    # away from the hardcoded $4.68 SLR default. Capped at last 20 samples.
    recent_side_fees: list = field(default_factory=list)


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
        # Adam 2026-07-16: primary-swing price history for expert_spread.
        # Same purpose as _sleeve_price_history but scoped to the primary
        # state machine (which doesn't have a sc.id). Appended on every
        # step(). Bounded so long-running processes stay flat in memory.
        # 120 samples × 5s tick ≈ 10 minutes of history — enough for the
        # AS realized-vol estimate (needs ≥5 samples per expert_spread.py).
        from collections import deque as _deque
        self._primary_price_history: _deque = _deque(maxlen=120)
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

    # ---- contract spec source-of-truth -----------------------------------

    def _get_contract_size(self) -> float:
        """Return this product's contract_size, source-of-truth priority:

          1. Cached value on self (populated first-call from broker).
          2. broker.contract_spec()['contract_size'] — Coinbase truth.
          3. self.cfg.contract_size — config value (may be stale/default).
          4. 1.0 last-resort with a CRITICAL log.

        Adam 2026-07-21 ROOT FIX (ENA -$0.32 phony loss on +$24 real cycle):
        prior code used `self.contract_spec_cache` which is never assigned
        anywhere. try/except swallowed the AttributeError, silently
        returning 1. For ENA (real 5000), NER (500), ONDO (1000), LINK
        (50), HYPE (10), AAVE (5), SLR (50) — every realized_pnl credit
        was 1/N of the truth, dashboard showed negatives on real wins,
        Vince/Kelly sizing and loss-streak auto-disable saw phony losses.
        """
        cached = getattr(self, "_contract_size_cached", None)
        if cached and cached > 0:
            return float(cached)
        cs = 0.0
        try:
            if hasattr(self.b, "contract_spec"):
                spec = self.b.contract_spec() or {}
                cs = float(spec.get("contract_size") or 0)
        except Exception:
            cs = 0.0
        if cs <= 0:
            try:
                cs = float(getattr(self.cfg, "contract_size", 0) or 0)
            except Exception:
                cs = 0.0
        if cs <= 0:
            try:
                self._record("contract_size_unknown_fallback_to_1",
                             severity="critical",
                             reason=("broker.contract_spec() + cfg both empty; "
                                     "using 1.0 which will mis-scale realized P&L "
                                     "by the real multiplier. Populate config "
                                     "immediately."))
            except Exception:
                pass
            cs = 1.0
        self._contract_size_cached = cs
        return cs

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

    def _sleeve_cfg_by_id(self, sid: str) -> "SleeveConfig | None":
        for _sc in self._load_sleeves_cfg():
            if getattr(_sc, "id", None) == sid:
                return _sc
        return None

    def _save_state(self) -> None:
        """Persist tick state, but preserve any external writes that landed
        between reload and save.

        Adam 2026-07-19 (problem-scout): reload-on-tick closed the "next
        tick clobbers external write" class, but a diag/dashboard write
        that lands DURING the tick (between reload at t=0 and save at
        t=+50-500ms) still gets clobbered. Fix: re-read Redis right
        before save; for each sleeve, if the Redis blob differs from the
        _reload_snapshot we took at tick start (i.e., an external writer
        touched it during this tick), keep the Redis version instead of
        overwriting with our tick-computed one. Same-tick fills we
        credited via _sleeve_on_fill still stick because they went into
        self.s.sleeves, which we're about to persist.
        """
        import time as _time
        self.s.last_heartbeat_ts = _time.time()
        d = asdict(self.s)
        d["state"] = self.s.state.value
        d["sleeves"] = {sid: s.to_dict() for sid, s in self.s.sleeves.items()}

        # Save-time merge: if a sleeve blob in Redis changed since our
        # reload-time snapshot, the external write wins for that sleeve.
        try:
            snap = getattr(self, "_reload_snapshot", None)
            if snap is not None:
                current = self.store.get_state(self.tenant_id, self.symbol) or {}
                current_sleeves = current.get("sleeves") or {}
                if isinstance(current_sleeves, dict):
                    for sid, redis_blob in current_sleeves.items():
                        baseline = snap.get(sid)
                        if baseline is not None and redis_blob != baseline:
                            # External writer touched this sleeve during our
                            # tick. Preserve their write.
                            d["sleeves"][sid] = redis_blob
                            try:
                                self._record(
                                    "sleeve_external_write_preserved",
                                    sleeve_id=sid, symbol=self.symbol,
                                    severity="info",
                                )
                            except Exception:
                                pass
        except Exception:
            # Merge failure must not block the save.
            pass

        self.store.put_state(self.tenant_id, self.symbol, d)

    def _reload_sleeves_from_redis(self) -> None:
        """Re-sync in-memory sleeve state with Redis at tick start.

        Closes the SLR/HYP/PT/XLP in-memory-clobber class:
          diag force-credit XLP-20DEC30-CDE scan-mrr27ttp --apply → Redis
          gets the fix (state=ARMED_BUY, cycles=1) → next bot tick writes
          in-memory dict (state=HALTED, cycles=0) BACK to Redis → fix lost.

        Fix: re-read the sleeves dict from Redis at the start of every
        multi_sleeve_step. External writes get honored within ~1s. Any
        sleeve in memory but absent from Redis is treated as new (kept);
        sleeves in Redis update the in-memory copy in place.

        Fails open: any Redis read exception leaves in-memory untouched
        (will retry next tick). Malformed entries are skipped, not raised.

        Retirement-cooldown guard (2026-07-19): if the product is in
        retirement cooldown, we skip the reload entirely. Otherwise a
        stale sleeve blob left in Redis (retire path may not have cleared
        the sleeves sub-dict) gets re-hydrated into memory, kept by the
        sweep (if the id is still in cfg), and ticks THROUGH the cooldown
        — exactly the ghost re-inflation the retirement ledger exists to
        prevent.

        Also stamps _reload_snapshot per sleeve so _save_state can detect
        + preserve external writes that landed between reload and save.
        """
        try:
            _in_cd, _cd_reason, _cd_secs = self._is_product_in_retirement_cooldown()
        except Exception:
            _in_cd = False
        if _in_cd:
            # Product is under cooldown. Do not re-hydrate any sleeve blob
            # for this product — the cleanup sweep will drop retired sleeves
            # naturally as configured_ids diverges.
            return
        try:
            d = self.store.get_state(self.tenant_id, self.symbol) or {}
        except Exception:
            return
        persisted = d.get("sleeves") or {}
        if not isinstance(persisted, dict):
            return
        # Fresh baseline for save-time merge (see _save_state).
        self._reload_snapshot = {}
        for sid, raw in persisted.items():
            if not isinstance(raw, dict):
                continue
            try:
                self.s.sleeves[sid] = SleeveState.from_dict(raw, sid)
                # Store a deep-enough copy so save-time compare works.
                self._reload_snapshot[sid] = dict(raw)
            except Exception:
                pass

    def _is_product_in_retirement_cooldown(self) -> tuple[bool, str, float]:
        """Wrapper around retirement_ledger.is_in_cooldown that fails open.
        Returns (in_cooldown, reason, secs_remaining)."""
        try:
            import retirement_ledger as _rl
            return _rl.is_in_cooldown(self.store, self.tenant_id, self.symbol)
        except Exception:
            return False, "", 0.0

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
            elif status in ("CANCELLED", "EXPIRED"):
                self.s.live_order_id = None
                self.s.filled_qty = st.get("filled_qty", 0)
            elif status == "UNKNOWN":
                # Adam 2026-07-20 §3.6 ORPHAN GUARD (primary reconcile):
                # UNKNOWN != gone. Don't clear tracking — retry next
                # reconcile. See sleeve equivalent below for full rationale.
                self._record(
                    "primary_reconcile_order_status_unknown",
                    order_id=self.s.live_order_id, severity="critical",
                    reason="Coinbase UNKNOWN; NOT clearing (avoid ghost + orphan).")
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
                    # Config gone (sleeve removed while order was live +
                    # order then FILLED). Best we can do is clear the id —
                    # the fill happened but there's no sleeve to credit it
                    # to. Adam 2026-07-20: record a severity=critical event
                    # so operator has visibility into the tenant-level
                    # accounting drift. Position changed on Coinbase but
                    # no bot record tracks the fill_price / basis / P&L.
                    self._record(
                        "reconcile_fill_no_config_drift",
                        sleeve_id=sid, order_id=ss.live_order_id,
                        filled_qty=st.get("filled_qty", 0),
                        average_filled_price=st.get("average_filled_price"),
                        severity="critical",
                        reason=("sleeve config was removed while its order "
                                "was live; order then FILLED on Coinbase. "
                                "Position changed but no sleeve to credit — "
                                "tenant-level accounting drifted. Rare "
                                "(user Remove during in-flight fill). Manual "
                                "reconcile of realized_pnl may be needed."))
                    cleared_sleeves.append((sid, ss.live_order_id, "FILLED_NO_CONFIG"))
                    ss.live_order_id = None
                    ss.filled_qty = 0
                else:
                    credited_sleeves.append((sid, ss.live_order_id,
                                             st.get("average_filled_price")))
                    ss.filled_qty = st.get("filled_qty", 0) or sc.qty
                    self._sleeve_on_fill(sc, ss, st.get("average_filled_price"))
            elif status in ("CANCELLED", "EXPIRED"):
                # Terminal states — safe to clear tracking. Also credit any
                # partial fill before clearing (2026-07-15 fix — order can
                # be CANCELLED after a partial).
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
            elif status == "UNKNOWN":
                # Adam 2026-07-20 §3.6 ORPHAN GUARD: UNKNOWN means Coinbase
                # couldn't tell us (transient network / rate limit / API
                # confusion). It does NOT mean the order is gone. Prior code
                # cleared tracking on UNKNOWN, which produced two failure
                # modes:
                #   1. Order was actually FILLED → position dropped but we
                #      never credited the fill → ghost sleeve (own_avg
                #      stays set forever).
                #   2. Order was actually OPEN → tracking cleared, order
                #      keeps sitting on Coinbase → orphan (short risk).
                # Now: credit any partial that came back, then DO NOT
                # clear tracking. Next tick's poll retries. If status
                # stays UNKNOWN across many polls, operator sees the
                # critical log and can force-cancel via diag.
                partial_filled = st.get("filled_qty", 0) or 0
                if partial_filled > 0:
                    sc = sleeves_cfg_by_id.get(sid)
                    if sc is not None:
                        credited_sleeves.append((sid, ss.live_order_id,
                                                 st.get("average_filled_price")))
                        ss.filled_qty = partial_filled
                        self._sleeve_on_fill(sc, ss, st.get("average_filled_price"))
                self._record(
                    "reconcile_order_status_unknown",
                    sleeve_id=sid, order_id=ss.live_order_id,
                    severity="critical",
                    reason=("Coinbase returned UNKNOWN for this order — "
                            "NOT clearing tracking (prior code assumed "
                            "gone; produced ghost + orphan). Will retry "
                            "next reconcile."))
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
                # Adam 2026-07-20 ORPHAN GUARD (config gone edge case):
                # sleeve config was deleted while its resting stop was
                # still live on Coinbase. Prior code cleared tracking
                # without canceling → orphan. Now: try to cancel first.
                # Only clear tracking on success. If cancel fails, KEEP
                # tracking so the orphan-sweep at startup catches it (the
                # sweep looks at Coinbase for orphans, not our state).
                _cfg_gone_ok = False
                try:
                    self.b.cancel(ss.resting_stop_oid)
                    _cfg_gone_ok = True
                    self._record("resting_stop_config_gone_cancelled",
                                 sleeve_id=sid, oid=ss.resting_stop_oid,
                                 source="reconcile")
                except Exception as _cge:
                    self._record("resting_stop_config_gone_cancel_failed",
                                 sleeve_id=sid, oid=ss.resting_stop_oid,
                                 error=str(_cge), severity="critical",
                                 reason=("config deleted but resting stop still "
                                         "live; keeping tracking so it's not "
                                         "invisible if orphan-sweep misses"))
                if _cfg_gone_ok:
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
                # If the sleeve already transitioned to ARMED_BUY the TP fill
                # fired first — cancel the dangling stop so it doesn't create
                # a spurious short if price recovers above stop_px.
                # Adam 2026-07-20 ORPHAN GUARD: only clear tracking on cancel
                # success. Prior code cleared unconditionally after `except`,
                # so failed cancel produced an orphan (Coinbase still has the
                # stop, no sleeve tracks it → §3.8 short risk).
                if str(ss.state) == "ARMED_BUY":
                    _tpb_ok = False
                    try:
                        self.b.cancel(ss.resting_stop_oid)
                        _tpb_ok = True
                        self._record("resting_stop_cancelled_tp_beat_stop",
                                     sleeve_id=sid, oid=ss.resting_stop_oid,
                                     source="reconcile")
                    except Exception as _ce:
                        self._record("resting_stop_cancel_tp_beat_stop_failed",
                                     sleeve_id=sid, oid=ss.resting_stop_oid,
                                     error=str(_ce), severity="critical",
                                     reason=("keeping resting_stop_oid tracked "
                                             "so next reconcile retries; avoids "
                                             "orphan"))
                    if _tpb_ok:
                        ss.resting_stop_oid = None
                        ss.resting_stop_px = None
                        ss.resting_stop_stage = None
                continue  # still resting (or just cancelled above)
            if status == "FILLED":
                try:
                    fill_price = float(st.get("average_filled_price") or 0)
                except Exception:
                    fill_price = 0.0
                filled_qty = int(st.get("filled_qty") or sc.qty or 1)
                credited_stops.append((sid, ss.resting_stop_oid, fill_price, None))
                # Adam 2026-07-15 CRITICAL: use the shared _credit_stop_fill
                # helper so we get the same never-silent-$0 protection as the
                # tick path. If own_avg unresolvable, helper halts the sleeve
                # and returns False — we DON'T clear own_avg_entry or
                # resting_stop_oid so post-mortem has full context.
                credited = self._credit_stop_fill(sc, ss, fill_price,
                                                  filled_qty, source="reconcile",
                                                  oid=ss.resting_stop_oid)
                if not credited:
                    continue  # halted OR dedup-skipped; state left intact
                ss.own_avg_entry = None
                ss.resting_stop_oid = None
                ss.resting_stop_px = None
                ss.resting_stop_stage = None
                ss.state = SleeveStateEnum.ARMED_BUY
                ss.armed_buy_since_ts = _time_recon.time()
                # Adam 2026-07-15: same cycle-state reset as the tick path.
                self._reset_cycle_state_post_sell(ss)
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
            # Adam 2026-07-20 ORPHAN GUARD (cancel-intent handler): only
            # clear tracking on cancel success. Prior code cleared
            # unconditionally after `except log`, so a failed dashboard-
            # triggered cancel produced an orphan. The dashboard button
            # gives the user false confidence ("cancelled") while the
            # order is still live on Coinbase.
            if target is None:
                # Primary strategy cancel
                if self.s.live_order_id:
                    _ci_ok = False
                    try:
                        self.b.cancel(self.s.live_order_id)
                        _ci_ok = True
                    except Exception as e:
                        self._record("cancel_failed", order_id=self.s.live_order_id,
                                     error=str(e), severity="critical",
                                     reason=("dashboard-triggered cancel raised; "
                                             "keeping tracking so next tick / "
                                             "operator retry can clean up"))
                    if _ci_ok:
                        self._record("primary_order_cancelled",
                                     order_id=self.s.live_order_id,
                                     requested_by="dashboard", halted=halt)
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
                        _sci_ok = False
                        try:
                            self.b.cancel(ss.live_order_id)
                            _sci_ok = True
                        except Exception as e:
                            self._record("cancel_failed", sleeve_id=target,
                                         order_id=ss.live_order_id, error=str(e),
                                         severity="critical",
                                         reason=("dashboard-triggered sleeve cancel "
                                                 "raised; keeping tracking to avoid "
                                                 "orphan"))
                        if _sci_ok:
                            self._record("sleeve_order_cancelled",
                                         sleeve_id=target,
                                         order_id=ss.live_order_id,
                                         requested_by="dashboard", halted=halt)
                            ss.live_order_id = None
                            ss.filled_qty = 0
                    if halt:
                        ss.state = SleeveStateEnum.HALTED
                        ss.halt_reason = "paused via dashboard"
                        self._record("sleeve_paused", sleeve_id=target, requested_by="dashboard")
            self._save_state()
        finally:
            self.store.clear_cancel_intent(self.tenant_id, self.symbol)

    # ---- state patch queue (diag scripts → bot in-memory state) -----------

    def _maybe_consume_state_patch(self) -> None:
        """Adam 2026-07-15: apply any pending state_patch to in-memory state
        BEFORE the tick runs. Fixes the diag-vs-live race:

            diag script writes state to disk (realized_pnl → $14.55)
            ↓
            bot's in-memory state still has old value (-$0.55)
            ↓
            next _save_state() clobbers disk back to -$0.55 = write lost

        Now diag writes a PATCH (state_store.put_state_patch) instead of
        the full state; this consumer merges the patch into ss/self.s
        before any tick logic runs, then clears the patch. The next
        _save_state() persists the merged (correct) value.

        Patch shape:
            {"sleeves": {"<sid>": {"realized_pnl": 14.55,
                                    "recent_cycle_pnls_append": 15.10}},
             "top_level": {"realized_pnl": 100.0},   # optional
             "reason": "…",
             "ts": epoch_seconds}

        Field names ending in _append are appended to the target list
        (bounded to 20 entries — matches recent_cycle_pnls convention).
        Other fields are set. Unknown sleeve_ids ignored.
        Records state_patch_applied for audit."""
        if not hasattr(self.store, "get_state_patch"):
            return
        try:
            patch = self.store.get_state_patch(self.tenant_id, self.symbol)
        except Exception:
            return
        if not patch:
            return
        applied_summary = {"sleeves": {}, "top_level": {}}
        try:
            sleeve_patches = patch.get("sleeves") or {}
            for sid, fields in sleeve_patches.items():
                ss = (self.s.sleeves or {}).get(sid)
                if ss is None:
                    applied_summary["sleeves"][sid] = "skipped (unknown sleeve_id)"
                    continue
                applied_fields = {}
                for k, v in fields.items():
                    if k.endswith("_append"):
                        base = k[:-len("_append")]
                        cur = list(getattr(ss, base, None) or [])
                        cur.append(v)
                        if len(cur) > 20:
                            cur = cur[-20:]
                        setattr(ss, base, cur)
                        applied_fields[base] = f"appended({v})"
                    else:
                        prev = getattr(ss, k, None)
                        setattr(ss, k, v)
                        applied_fields[k] = {"prev": prev, "new": v}
                applied_summary["sleeves"][sid] = applied_fields
            top_level = patch.get("top_level") or {}
            for k, v in top_level.items():
                prev = getattr(self.s, k, None)
                setattr(self.s, k, v)
                applied_summary["top_level"][k] = {"prev": prev, "new": v}
        except Exception as e:
            self._record("state_patch_apply_failed",
                         reason=patch.get("reason"), error=str(e),
                         severity="warn")
            # Still clear — a broken patch would loop forever if left.
            try:
                self.store.clear_state_patch(self.tenant_id, self.symbol)
            except Exception:
                pass
            return
        self._record("state_patch_applied",
                     reason=patch.get("reason"),
                     source_ts=patch.get("ts"),
                     applied=applied_summary,
                     severity="info")
        try:
            self.store.clear_state_patch(self.tenant_id, self.symbol)
        except Exception:
            pass
        # Persist immediately so a crash between now and next _save_state
        # doesn't leave the patch stranded in-memory only.
        self._save_state()

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
            # Adam 2026-07-20 ORPHAN GUARD: if primary had a live_order_id
            # at halt time (halt-cancel might have failed silently in the
            # legacy code path), try to cancel it BEFORE clearing tracking.
            # If cancel fails, keep tracking so operator can retry — safer
            # than orphaning a live order.
            if self.s.live_order_id:
                _res_ok = False
                try:
                    self.b.cancel(self.s.live_order_id)
                    _res_ok = True
                except Exception as _e:
                    self._record("primary_resume_cancel_failed",
                                 order_id=self.s.live_order_id, error=str(_e),
                                 severity="critical",
                                 reason=("cancel of stale halt-time order "
                                         "failed on resume; keeping tracking"))
                if _res_ok:
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
                # Adam 2026-07-20 ORPHAN GUARD (sleeve resume): same as
                # primary resume above. Cancel any stale halt-time order
                # before clearing tracking; keep tracking on failure.
                if ss.live_order_id:
                    _sres_ok = False
                    try:
                        self.b.cancel(ss.live_order_id)
                        _sres_ok = True
                    except Exception as _e:
                        self._record("sleeve_resume_cancel_failed",
                                     sleeve_id=sid,
                                     order_id=ss.live_order_id, error=str(_e),
                                     severity="critical",
                                     reason=("cancel of stale halt-time order "
                                             "failed on sleeve resume; keeping "
                                             "tracking"))
                    if _sres_ok:
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
            # Adam 2026-07-20 ORPHAN GUARD (primary _arm): if the cancel
            # raises, we DO NOT proceed to place a new order. Prior code
            # logged the failure and continued, overwriting live_order_id
            # with the new oid — leaving the old order on Coinbase as an
            # orphan (§3.8 short risk). Now: on cancel failure, return.
            # Next tick re-enters _arm with live_order_id still set and
            # retries the cancel.
            try:
                self.b.cancel(self.s.live_order_id)
                self._record("order_cancelled_for_rearm", order_id=self.s.live_order_id)
            except Exception as e:
                self._record("cancel_failed", order_id=self.s.live_order_id,
                             error=str(e), severity="critical",
                             reason=("cancel raised during re-arm; NOT placing "
                                     "new order — old order still tracked, "
                                     "next tick retries. Avoids orphan."))
                self._save_state()
                return
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
        # Adam 2026-07-16: consult expert_spread BEFORE the strategy computes
        # its directive. When expert_spread returns valid, it overrides the
        # directive's limit_price. The 3× Menkveld fee floor inside
        # expert_spread is what prevents HYPE/PT-style bleed cycles where
        # spread < fees. Fail-safe: on any error the legacy directive stands.
        expert_prices = self._expert_pick_primary_prices(pos, current_price)

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
            # Naked-short guard: even when core_qty=0 (shorts "allowed"),
            # refuse to sell more contracts than the exchange actually shows.
            # Covers position_mismatch class (2026-07-16): bot thinks it holds
            # N contracts but exchange=0 — without this, it places a sell order
            # for N contracts it doesn't own. The shorting path (core_qty=0,
            # pos=0, intent=short) sets swing_qty <= 0 initially and arms via
            # ARMED_BUY, so this guard doesn't block intentional shorts.
            if pos < self.s.swing_qty:
                self._record(
                    "arm_sell_refused_position_mismatch",
                    reason=f"exchange pos={pos} < swing_qty={self.s.swing_qty}; refusing to sell contracts we don't hold",
                    position=pos,
                    swing_qty=self.s.swing_qty,
                    severity="critical",
                )
                return
            directive = strat.sell_action(self.s, self.cfg, current_price)
            if directive is None:
                return  # trailing waiting for trigger / trail crossover
            # Override sell price with expert value if we got one AND it's
            # above current mark (never sell below market via limit).
            if expert_prices and expert_prices.get("sell_px", 0) > current_price:
                directive = SellDirective(qty=directive.qty,
                                          limit_price=expert_prices["sell_px"])
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
            # Adam 2026-07-16: initial-entry regime gate. Blocks buys into
            # extended/toxic regimes. Kill switch: expert_arm_gate.MODE = "off".
            if not self._expert_arm_gate_allows(prices_source="primary",
                                                  arm_direction="buy"):
                self._record("primary_arm_denied_by_gate",
                             reason="expert_arm_gate voted deny",
                             current_price=float(current_price))
                return
            # Override buy price with expert value if we got one AND it's
            # below current mark (never buy above market via limit).
            if expert_prices and 0 < expert_prices.get("buy_px", 0) < current_price:
                directive = BuyDirective(qty=directive.qty,
                                         limit_price=expert_prices["buy_px"])
            # Adam 2026-07-16: safety-cap qty via expert_size (median of
            # Van Tharp + half-Kelly + Vince, HARD-capped by user config).
            # Only shrinks — never grows above directive.qty.
            expert_qty = self._expert_size_adjust(
                user_configured_qty=int(directive.qty),
                mark=float(current_price),
                stop_distance=(float(current_price) - float(directive.limit_price or 0))
                                if directive.limit_price else 0.0,
                contract_size=float(getattr(self.cfg, "contract_size", 1) or 1),
                fee_per_roundtrip=float(getattr(self.cfg, "fee_per_contract_roundtrip", 0.0) or 0.0),
                expected_profit_per_contract=(float(current_price) - float(directive.limit_price or 0))
                                                if directive.limit_price else 0.0,
                log_prefix="primary_buy",
            )
            if expert_qty != directive.qty:
                directive = BuyDirective(qty=expert_qty, limit_price=directive.limit_price)
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
        # 2026-07-19: mutual-exclusion guard moved ABOVE _compute_sleeve_stop_
        # loss_qty. When resting_stop_enabled, we defer to the exchange stop
        # regardless of whether the oid is placed yet — no need to compute
        # qty (which requires self.cfg.core_qty and would raise on lightweight
        # test setups anyway).
        if getattr(sc, "resting_stop_enabled", True):
            import time as _t_ssk
            key = f"stop_loss_skip_{sc.id}"
            store = getattr(self, "_stop_loss_skip_last_ts", None)
            if store is None:
                self._stop_loss_skip_last_ts = {}
                store = self._stop_loss_skip_last_ts
            last_ts = int(store.get(key, 0) or 0)
            cur = int(_t_ssk.time())
            if cur - last_ts > 300:
                try:
                    self._record("sleeve_stop_loss_skipped_resting_stop_active",
                                 sleeve_id=sc.id, price=last_price,
                                 trigger=effective_stop,
                                 resting_stop_oid=ss.resting_stop_oid,
                                 resting_stop_px=ss.resting_stop_px)
                except Exception:
                    pass
                store[key] = cur
            return False
        to_sell = self._compute_sleeve_stop_loss_qty(sc, pos)
        if to_sell <= 0:
            self._sleeve_halt(sc, ss,
                              f"stop-loss at {last_price} (≤ {effective_stop}) but core floor "
                              f"{self.cfg.core_qty} blocks the sell (pos={pos})")
            return True
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
        # Adam 2026-07-20 ACCOUNTING FIX: track the market-sell oid as
        # live_order_id so the next tick's fill poller catches FILLED,
        # calls _sleeve_on_fill's SELL branch, and credits realized_pnl +
        # cycles + clears own_avg. Prior code cleared own_avg here
        # UNCONDITIONALLY and never tracked the oid, so _sleeve_on_fill
        # never ran → dashboard showed stale (pre-stop) realized_pnl even
        # though the position sold at a loss. §3.7 allows stop_loss to
        # close red, but the accounting must reflect it.
        #
        # Also: snapshot own_avg to sell_entry_avg first if not already
        # set, so _credit_stop_fill's fallback chain has a basis to
        # compute realized against.
        if ss.own_avg_entry is not None and not (ss.sell_entry_avg
                                                  and float(ss.sell_entry_avg) > 0):
            ss.sell_entry_avg = float(ss.own_avg_entry)
        ss.live_order_id = oid

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
            # Adam 2026-07-16: gate the INSTANT-reanchor path too. Before
            # this, reanchor_on_trigger=True bypassed all cooldown — it was
            # the primary bleed vector (rearm into the same trend that just
            # stopped us). Now consult expert_gate; if experts vote NO,
            # defer to reentry_pending (same as volatility mode) so the
            # gate has a chance to re-evaluate on the next tick as data
            # comes in. Kill switch: expert_gate.MODE = "off" → skip.
            gate_dec = None
            try:
                import expert_gate as _eg
                if getattr(_eg, "MODE", "expert") == "expert":
                    prices = list(self._sleeve_price_history.get(sc.id, []) or [])
                    ofi = None; kyle_lam = None; kyle_base = None
                    try:
                        if self.ms is not None and hasattr(self.ms, "snapshot"):
                            snap = self.ms.snapshot()
                            if isinstance(snap, dict):
                                ofi = snap.get("trade_ofi_60s") or snap.get("ofi") or snap.get("obi")
                                kyle_lam = snap.get("kyle_lambda")
                                kyle_base = snap.get("kyle_lambda_baseline") or snap.get("kyle_lambda_avg")
                    except Exception:
                        pass
                    gate_dec = _eg.reentry_allowed(
                        prices=prices,
                        # elapsed=0.0 → cadence floor guarantees at least
                        # _HARD_CADENCE_FLOOR_SECS wait even in "instant" mode
                        elapsed_since_stop_secs=0.0,
                        reentry_direction="buy",
                        order_flow_imbalance=(float(ofi) if ofi is not None else None),
                        kyle_lambda=(float(kyle_lam) if kyle_lam is not None else None),
                        kyle_baseline=(float(kyle_base) if kyle_base is not None else None),
                    )
                    self._record(
                        "sleeve_reanchor_on_trigger_gate",
                        sleeve_id=sc.id, sleeve_name=sc.name,
                        allow=gate_dec.allow,
                        votes=gate_dec.votes,
                        vote_count=gate_dec.vote_count,
                        total_voters=gate_dec.total_voters,
                        cadence_ok=gate_dec.cadence_ok,
                        cadence_floor_secs=gate_dec.cadence_floor_secs,
                        method=gate_dec.method,
                    )
            except Exception as _e:
                try:
                    self._record("sleeve_reanchor_on_trigger_gate_error",
                                 sleeve_id=sc.id, sleeve_name=sc.name,
                                 error=str(_e), severity="warn")
                except Exception:
                    pass

            if gate_dec is not None and not gate_dec.allow:
                # Experts said no. Defer to the volatility-mode reentry_pending
                # path so the gate re-evaluates as data comes in.
                import time as _t
                ss.reentry_pending = True
                ss.reentry_stop_ts = _t.time()
                ss.pre_stop_range = self._sleeve_recent_range(sc)
                ss.state = SleeveStateEnum.ARMED_BUY
                self._record("sleeve_reanchor_on_trigger_deferred",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             reason="expert_gate denied instant rearm; "
                                    "deferred to reentry_pending")
                return True

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
            # Rate-limited log so this state is visible (same rationale as
            # the "waiting_for_dip" branch above).
            import time as _t_bt2
            key = f"buy_trail_bounce_{sc.id}"
            store = getattr(self, "_buy_trail_wait_last_ts", None)
            if store is None:
                self._buy_trail_wait_last_ts = {}
                store = self._buy_trail_wait_last_ts
            last_ts = int(store.get(key, 0) or 0)
            cur = int(_t_bt2.time())
            if cur - last_ts > 600:
                self._record(
                    "buy_trail_waiting_for_bounce",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    buy_px=sc.buy_px,
                    low_water=round(ss.buy_trail_low_water, 6),
                    last_price=round(float(last_price), 6),
                    bounce_needed=round(ss.buy_trail_low_water + distance - float(last_price), 6),
                    trail_distance=distance,
                    reason=("buy_trail armed at prior dip; waiting for mark "
                            "to bounce trail_distance above the running low"),
                )
                store[key] = cur
            return None

        # Not yet armed: only arm once mark dips at/through buy_px.
        if last_price > sc.buy_px:
            # Adam 2026-07-15: rate-limited log so the operator can see WHY a
            # sleeve stays ARMED_BUY with no CB order — buy_trail is patient
            # by design (wait for mark to dip to buy_px before even tracking
            # a bounce). Was silent for HOURS on CU/HYF/ZEC, showed up as
            # "NO recent block events" in diag_sleeve_ready. Rate-limit to
            # one event per 10min per sleeve to keep the log signal-to-noise
            # high while still surfacing the state on any diag.
            import time as _t_bt
            key = f"buy_trail_wait_{sc.id}"
            store = getattr(self, "_buy_trail_wait_last_ts", None)
            if store is None:
                self._buy_trail_wait_last_ts = {}
                store = self._buy_trail_wait_last_ts
            last_ts = int(store.get(key, 0) or 0)
            cur = int(_t_bt.time())
            if cur - last_ts > 600:
                self._record(
                    "buy_trail_waiting_for_dip",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    buy_px=sc.buy_px, last_price=round(float(last_price), 6),
                    dip_required=round(float(last_price) - sc.buy_px, 6),
                    trail_distance=distance,
                    reason=("mark above buy_px — buy_trail patient by design; "
                            "arms only when mark dips at/through buy_px"),
                )
                store[key] = cur
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
        """Trend gate on the BUY arm. Two independent layers:

        1. SHORT-horizon SMA (existing, ~20-bar per-sleeve rolling price
           history at 5s cadence ≈ 100s lookback). Turtle/Livermore intra-
           day filter — don't buy into a confirmed intraday downtrend.

        2. LONG-horizon canonical filter (Option D-1, 2026-07-19). Faber
           200-day SMA + MOP 12-month TSM on DAILY candles, cached per
           product. Enabled by SWING_TREND_FILTER_ENABLED env flag.
           Blocks BUY when the multi-month trend is DOWN — the canonical
           evidence-backed gate the docstring in expert_params.py flags
           as the biggest missing edge. Kaminski-Lo (2014): stops help
           ONLY under momentum. Without this gate we run stops in
           mean-reverting regimes where they hurt.

        Both layers permissive-fail-open — cold start, missing data, or
        flag off → allow. Only an ACTIVE negative signal blocks.
        """
        # Layer 2: long-horizon canonical filter (feature-flagged)
        try:
            import trend_filter as _tf
            allowed, reason = _tf.long_trend_ok_for_buy(
                self.store, self.tenant_id, self.symbol)
            if not allowed:
                self._record(
                    "sleeve_long_trend_gate_blocked",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    reason=reason, last_price=round(float(last_price), 4),
                )
                return False
        except Exception:
            pass  # fail-open on any error in the filter path

        # Layer 1: existing short-horizon SMA
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
        on green. Opt-in (avg_down_alert_enabled); OFF by default; fail-safe.

        Adam 2026-07-19: emits explicit 'off' light on transitions off
        ARMED_SELL (position sold / stopped / halted) so the dashboard
        clears the yellow dot. Previously the dashboard aggregated ALL
        recent events without a time filter, so a stale amber from a
        previous cycle kept showing on a WAITING sleeve with no position.
        """
        # Edge-triggered clear: if we previously flagged a light but the
        # sleeve is no longer eligible (opt-out, not ARMED_SELL, above avg),
        # emit off so the dashboard drops the indicator.
        def _clear_if_lit(reason: str) -> None:
            prev = self._avg_down_light.get(sc.id)
            if prev and prev != "off":
                self._avg_down_light[sc.id] = "off"
                try:
                    self._record("avg_down_light", sleeve_id=sc.id,
                                 sleeve_name=sc.name, light="off",
                                 reason=reason)
                except Exception:
                    pass

        if not getattr(sc, "avg_down_alert_enabled", False):
            _clear_if_lit("alert disabled on this sleeve")
            return
        if ss.state != SleeveStateEnum.ARMED_SELL:   # only while holding a long
            _clear_if_lit("no held long (state != ARMED_SELL)")
            return
        try:
            avg = ss.own_avg_entry
            if avg is None:
                _clear_if_lit("no own_avg_entry — sleeve doesn't hold contracts")
                return
            if float(last_price) >= float(avg):   # only underwater
                _clear_if_lit("mark above avg — position not underwater")
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

    def _regime_router_mode(self) -> str:
        """Read the __regime_router_mode__ scope. Returns 'off' (default),
        'shadow' (compute + log adjustments without applying), or 'expert'
        (apply). Same rollout pattern as _expert_spread_mode."""
        try:
            m = (self.store.get_state(self.tenant_id, "__regime_router_mode__") or {})
            return str(m.get("mode") or "off").lower()
        except Exception:
            return "off"

    def _current_held_symbols_excluding(self, exclude_symbol: str) -> list[str]:
        """List of product_ids on this tenant that currently hold a nonzero
        position, excluding the given symbol. Used by correlation-aware
        sizing to check what we're already exposed to before arming a new
        buy. Best source: state.__portfolio__ (updated on every fill sync).

        Returns [] on any error — correlation drag falls back to 1.0 (no
        drag), which is the safe direction (never over-restricts sizing)."""
        try:
            pf = self.store.get_state(self.tenant_id, "__portfolio__") or {}
            out = []
            for sym, snap in pf.items():
                if not isinstance(snap, dict):
                    continue
                if sym.startswith("__"):
                    continue
                if sym == exclude_symbol:
                    continue
                if float(snap.get("position_qty") or 0) != 0:
                    out.append(sym)
            return out
        except Exception:
            return []

    def _expert_spread_mode(self) -> str:
        """Adam 2026-07-16: experts ALWAYS decide the spread. Rule per
        feedback_experts_control_spread_and_price memory: every trading
        decision — spread, buy price, stop distance, reentry — is made
        by expert algorithms grounded in academic HFT literature. No
        legacy fallback. The gate is deleted; this method now returns
        "expert" unconditionally. Kept as a method (not inlined) so
        the two call sites read symmetrically and any future kill-switch
        can be added here in one place.

        Prior behavior (before 2026-07-16): tenant-scoped
        __expert_spread_mode__ flag with off/shadow/expert. Kept HYPE
        and PT bleeding on tight spreads because the mode was still
        'off' or 'shadow' on most sleeves + not wired to primary at all.

        Kill switch: if a future incident requires falling back to
        legacy, change this to return "off" and both call sites will
        skip the AS block."""
        return "expert"

    def _expert_pick_primary_prices(self, inventory: int, mark: float) -> Optional[dict]:
        """Ask expert_spread for primary-swing buy/sell prices at current mark.

        Adam 2026-07-16: fixes the HYPE + PT bleed pattern where the primary
        state machine armed swings with spread < fees, guaranteeing losing
        cycles. The 3× Menkveld fee floor inside expert_spread ensures every
        cycle is at least fee-break-even.

        Returns {'buy_px', 'sell_px', 'spread'} on success. Returns None on
        failure — the caller falls back to the strategy's legacy directive
        (which is what shipped before this method existed).

        Reads:
          - price history from self._primary_price_history (populated per-tick)
          - cycle_completion_ts from trade log (for arrival-rate estimate)
          - fee_per_roundtrip, contract_size, tick_size from cfg
          - inventory passed in (position_qty from broker)
          - qty from state.swing_qty

        The AS grid search internally picks γ to maximize E[$/day] with
        tie-breaker to MORE cycles (per feedback_optimize_realized_dollars_per_day).
        """
        try:
            if mark <= 0 or self.s.swing_qty <= 0:
                return None
            history = list(self._primary_price_history)
            if len(history) < 5:
                return None  # expert_spread needs ≥5 samples
            # Cycle completion timestamps for arrival-rate estimate.
            cycle_ts = []
            try:
                if hasattr(self, "trade_log") and self.trade_log:
                    for e in self.trade_log.events():
                        if not isinstance(e, dict):
                            continue
                        if e.get("event_type") != "cycle_completed":
                            continue
                        if e.get("symbol") != self.symbol:
                            continue
                        ts = float(e.get("ts") or 0)
                        if ts > 0:
                            cycle_ts.append(ts)
            except Exception:
                pass
            fee_rt = float(getattr(self.cfg, "fee_per_contract_roundtrip", 0.0) or 0.0)
            tick = float(getattr(self.cfg, "tick_size", 0) or 0)
            contract_size = float(getattr(self.cfg, "contract_size", 1) or 1)
            qty = int(self.s.swing_qty)
            import expert_spread as _es
            dec = _es.grid_search_optimal_gamma(
                mid_price=float(mark),
                price_history=history,
                cycle_completion_ts=cycle_ts,
                fee_per_roundtrip=fee_rt,
                contract_size=contract_size,
                qty=qty,
                tick_size=tick if tick > 0 else None,
                inventory=int(inventory or 0),
            )
            if dec is None:
                return None
            if dec.buy_px <= 0 or dec.sell_px <= 0 or dec.sell_px <= dec.buy_px:
                return None
            # Log so the operator can audit what the expert chose vs mark.
            try:
                self._record(
                    "expert_spread_primary_applied",
                    method=dec.method,
                    citation=dec.citation,
                    mark=round(mark, 6),
                    inventory=int(inventory or 0),
                    swing_qty=qty,
                    fee_per_roundtrip=fee_rt,
                    contract_size=contract_size,
                    expert_buy_px=dec.buy_px,
                    expert_sell_px=dec.sell_px,
                    expert_spread=dec.spread,
                    expected_daily_pnl=dec.expected_daily_pnl,
                    expected_cycles_per_day=dec.expected_cycles_per_day,
                    cost_floor_binding=dec.cost_floor_binding,
                    lambda_widening=dec.lambda_widening,
                )
            except Exception:
                pass
            return {
                "buy_px": float(dec.buy_px),
                "sell_px": float(dec.sell_px),
                "spread": float(dec.spread),
            }
        except Exception as e:
            try:
                self._record("expert_spread_primary_error",
                             error=str(e), severity="warn")
            except Exception:
                pass
            return None

    def _expert_arm_gate_allows(self, prices_source: str,
                                  arm_direction: str = "buy",
                                  sleeve_id: Optional[str] = None) -> bool:
        """Consult expert_arm_gate before firing a fresh BUY.

        Adam 2026-07-16: 6-voter supermajority (Kaufman + Wilder-ADX +
        Cartea-OFI + Kyle-λ + Connors RSI(2) + Bollinger). Blocks buys
        into extended/toxic regimes. Silence = deny.

        prices_source: "primary" reads self._primary_price_history;
                       otherwise treated as a sleeve id key into
                       self._sleeve_price_history.

        Returns True if gate allows (or MODE=off or fail-safe). Returns
        False if experts deny.
        """
        try:
            import expert_arm_gate as _eag
            if getattr(_eag, "MODE", "expert") != "expert":
                return True  # kill switch
            if prices_source == "primary":
                prices = list(self._primary_price_history)
            else:
                prices = list(self._sleeve_price_history.get(prices_source, []) or [])
            # Read microstructure snapshot (None-safe)
            ofi = None; kyle_lam = None; kyle_base = None
            try:
                if self.ms is not None and hasattr(self.ms, "snapshot"):
                    snap = self.ms.snapshot()
                    if isinstance(snap, dict):
                        ofi = snap.get("trade_ofi_60s") or snap.get("ofi") or snap.get("obi")
                        kyle_lam = snap.get("kyle_lambda")
                        kyle_base = snap.get("kyle_lambda_baseline") or snap.get("kyle_lambda_avg")
            except Exception:
                pass
            dec = _eag.arm_allowed(
                prices=prices,
                arm_direction=arm_direction,
                order_flow_imbalance=(float(ofi) if ofi is not None else None),
                kyle_lambda=(float(kyle_lam) if kyle_lam is not None else None),
                kyle_baseline=(float(kyle_base) if kyle_base is not None else None),
            )
            event_name = ("primary_arm_gate_decision" if prices_source == "primary"
                          else "sleeve_arm_gate_decision")
            self._record(
                event_name,
                sleeve_id=sleeve_id,
                allow=dec.allow,
                votes=dec.votes,
                vote_count=dec.vote_count,
                total_voters=dec.total_voters,
                method=dec.method,
                arm_direction=arm_direction,
            )
            return bool(dec.allow)
        except Exception as _e:
            # Fail-safe: if the gate itself errors, allow the arm (legacy
            # behavior) and log the error. We don't want an expert bug to
            # block all trading — that would be worse than no gate at all.
            try:
                self._record("expert_arm_gate_error",
                             sleeve_id=sleeve_id, error=str(_e), severity="warn")
            except Exception:
                pass
            return True

    def _expert_size_adjust(self, user_configured_qty: int, mark: float,
                             stop_distance: float, contract_size: float,
                             fee_per_roundtrip: float,
                             expected_profit_per_contract: float,
                             recent_cycle_pnls=None,
                             log_prefix: str = "primary") -> int:
        """Consult expert_size to determine a SAFETY-CAPPED position size.

        Adam 2026-07-16: experts can only REDUCE user_configured_qty,
        never increase it. Preserves the 'swing 1-2, protect the core'
        rule from project_live_intent.

        Returns the size to ship. Always ≥ 1 (unless user configured 0)
        and always ≤ user_configured_qty.

        Fail-safe: any exception → return user_configured_qty unchanged.
        Kill switch: expert_size.MODE = "off" → returns user_configured_qty.
        """
        try:
            if user_configured_qty <= 0:
                return int(user_configured_qty)
            import expert_size as _esz
            if getattr(_esz, "MODE", "expert") != "expert":
                return int(user_configured_qty)
            # Best-effort account equity from broker
            equity = 0.0
            try:
                # Try common broker interfaces for cash/equity
                if hasattr(self.b, "account_equity"):
                    equity = float(self.b.account_equity() or 0)
                elif hasattr(self.b, "balance"):
                    equity = float(getattr(self.b, "balance", 0) or 0)
            except Exception:
                pass
            if equity <= 0:
                # Portfolio snapshot fallback
                try:
                    pf = self.store.get_state(self.tenant_id, "__portfolio__") or {}
                    equity = float(pf.get("account_equity") or pf.get("cash") or 0)
                except Exception:
                    equity = 0.0
            dec = _esz.optimal_size(
                user_configured_size=int(user_configured_qty),
                account_equity=float(equity),
                stop_distance=float(stop_distance or 0),
                contract_size=float(contract_size or 1),
                mid_price=float(mark or 0),
                fee_per_roundtrip=float(fee_per_roundtrip or 0),
                expected_profit_per_contract=float(expected_profit_per_contract or 0),
                recent_cycle_pnls=recent_cycle_pnls,
            )
            self._record(
                f"expert_size_{log_prefix}_applied",
                method=dec.method,
                citation=dec.citation,
                user_configured=dec.user_configured,
                final_size=dec.size,
                candidates=dec.candidates,
                consensus=dec.consensus,
                menkveld_min_size=dec.menkveld_min_size,
                econ_floor_binding=dec.econ_floor_binding,
                user_cap_binding=dec.user_cap_binding,
                mark=round(float(mark or 0), 6),
                stop_distance=round(float(stop_distance or 0), 6),
            )
            return int(dec.size)
        except Exception as _e:
            try:
                self._record(f"expert_size_{log_prefix}_error",
                             error=str(_e), severity="warn")
            except Exception:
                pass
            return int(user_configured_qty)

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
        distance. Fires for sleeves with stop_loss_enabled=True in
        ARMED_BUY state ONLY. Adam 2026-07-20: while HOLDING a position
        (own_avg_entry > 0), stop_loss_px is FROZEN at whatever value the
        expert set at ARM time. This enforces
        feedback_experts_only_reentry_not_exit (locked 2026-07-19 after
        SLR unratcheted-stop incident). Prior behavior refired every 60s
        during holds, silently tightening the stop toward mark — which
        turned normal wobbles into stop-outs at prices only cents below
        own_avg. Root cause of 34 loss cycles on 2026-07-20 (OND/XLP/
        SLR/HYF/HYP/NOL/XLM/NER/ENA — every product with 3+ losses
        showed the same "sell $0.30 below buy" pattern).

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

        # Gate 1.5 (Adam 2026-07-20): FREEZE while holding. Per
        # feedback_experts_only_reentry_not_exit — once own_avg_entry > 0
        # the exit params (stop_loss_px, sell_px, trail_distance) are
        # FROZEN at the values the expert set at ARM time. This function
        # previously re-fired every 60s during holds and silently tightened
        # stop_loss_px toward mark. Result: 34 loss cycles on 2026-07-20
        # (OND 8, XLP 7, SLR 5, HYF 4, HYP 3, NOL 3, XLM 2 — every product
        # with immediate-fire losses showed the same "sell $0.0003 below
        # buy" pattern where the refresh had raised stop within cents of
        # mark).
        try:
            _own_avg_hold = float(getattr(ss, "own_avg_entry", 0) or 0)
        except (TypeError, ValueError):
            _own_avg_hold = 0.0
        if _own_avg_hold > 0:
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

        # Adam 2026-07-16: stop distance is now chosen by expert consensus,
        # not legacy stop_x_atr multiplier. Per feedback_experts_control_
        # spread_and_price memory: every trading decision uses experts.
        # See expert_stop.py for sources (Wilder/Cartea/Kyle/Menkveld/Van Tharp).
        # Fee floor (Menkveld 2013 3× rt-fees) is HARD — prevents the PT bleed
        # class where stop distance < fees guaranteed losing cycles.
        # Kill switch: set expert_stop.MODE = "off" to revert to legacy.
        try:
            import expert_stop as _est
        except Exception:
            _est = None

        wilder_mult = 2.0  # Wilder canonical baseline for the expert
        try:
            import expert_params
            asset_class = expert_params.asset_class_for(self.symbol) if hasattr(
                expert_params, "asset_class_for") else None
            if asset_class:
                params = expert_params.params_for_class(asset_class) if hasattr(
                    expert_params, "params_for_class") else {}
                if params and "stop_x_atr" in params:
                    wilder_mult = float(params["stop_x_atr"])
        except Exception:
            pass

        # Gather microstructure inputs — None-safe (experts degrade to
        # Wilder baseline if these aren't available).
        ofi = None
        kyle_lam = None
        kyle_base = None
        try:
            if self.ms is not None:
                snap = self.ms.snapshot() if hasattr(self.ms, "snapshot") else {}
                if isinstance(snap, dict):
                    ofi = snap.get("trade_ofi_60s") or snap.get("ofi") or snap.get("obi")
                    kyle_lam = snap.get("kyle_lambda")
                    kyle_base = snap.get("kyle_lambda_baseline") or snap.get("kyle_lambda_avg")
        except Exception:
            pass

        fee_rt = float(getattr(self.cfg, "fee_per_contract_roundtrip", 0.0) or 0.0)
        contract_size = float(getattr(self.cfg, "contract_size", 1) or 1)
        tick_snap = float(getattr(self.cfg, "tick_size", 0) or 0)

        new_stop_px = None
        expert_decision = None
        if _est is not None and getattr(_est, "MODE", "expert") == "expert":
            try:
                expert_decision = _est.optimal_stop_distance(
                    mark=float(last_price),
                    atr_est=float(atr_est),
                    fee_per_roundtrip=fee_rt,
                    contract_size=contract_size,
                    qty=max(1, int(sc.qty or 1)),
                    order_flow_imbalance=(float(ofi) if ofi is not None else None),
                    kyle_lambda=(float(kyle_lam) if kyle_lam is not None else None),
                    kyle_baseline=(float(kyle_base) if kyle_base is not None else None),
                    wilder_multiplier=wilder_mult,
                    tick_size=(tick_snap if tick_snap > 0 else None),
                )
                if expert_decision is not None:
                    new_stop_px = float(expert_decision.stop_px)
                    self._record(
                        "expert_stop_applied",
                        sleeve_id=sc.id, sleeve_name=sc.name,
                        method=expert_decision.method,
                        citation=expert_decision.citation,
                        mark=round(float(last_price), 6),
                        atr_est=round(float(atr_est), 6),
                        stop_distance=expert_decision.stop_distance,
                        stop_px=expert_decision.stop_px,
                        candidates=expert_decision.candidates,
                        consensus=expert_decision.consensus,
                        fee_floor=expert_decision.fee_floor,
                        fee_floor_binding=expert_decision.fee_floor_binding,
                        sanity_cap=expert_decision.sanity_cap,
                        sanity_cap_binding=expert_decision.sanity_cap_binding,
                    )
            except Exception as _e:
                # Never let the expert crash the auto-refresh path.
                try:
                    self._record("expert_stop_error",
                                 sleeve_id=sc.id, sleeve_name=sc.name,
                                 error=str(_e), severity="warn")
                except Exception:
                    pass

        # Fallback to legacy math if expert unavailable / off / errored.
        # Preserves prior behavior as the kill-switch state.
        if new_stop_px is None:
            new_stop_px = float(last_price) - (atr_est * wilder_mult)
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

        # Adam 2026-07-21 (option 2 after "and the 20 minutes is per
        # expert reccomendations?"): staleness gate REMOVED. Was
        # `_AUTO_REFRESH_STALE_AFTER_SECS = 1200s` hardcoded, which
        # violated feedback_experts_all_algo_params (no hardcoded
        # timings — should come from experts). Now expert_reentry
        # consensus can fire on every ARMED_BUY sleeve regardless of
        # armed_at age. Cadence throttle below (60s) still prevents
        # thrashing — matches Ho-Stoll (1981) MM re-quote cadence and
        # Hasbrouck (2007) post-fill drift settling window.
        import time as _t
        now = _t.time()
        armed_ts = float(ss.armed_buy_since_ts or now)

        # Gate 3 (now Gate 2): cadence — throttle to once per minute per
        # sleeve. Matches Ho-Stoll (1981) MM inventory re-quote interval
        # and Cartea-Jaimungal (2015) ch.7 post-fill re-post cadence.
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

        # Adam 2026-07-21: multi-expert consensus decision BEFORE arm_level's
        # Chan+Connors compute. expert_reentry gates on Vince cooldown +
        # Wilder ADX + Kaufman KAMA ER + Faith breakout + Menkveld fee floor
        # in addition to Chan/Connors. If any gate says WAIT or COOL_OFF,
        # skip the re-arm this tick. Kill switch: expert_reentry.MODE = "off"
        # falls back to legacy Chan+Connors path.
        try:
            import expert_reentry as _er
            if getattr(_er, "MODE", "expert") == "expert":
                _sold_ref = float(ss.last_sell_fill_price or last_price)
                _fee_rt = float(getattr(self.cfg, "fee_per_contract_roundtrip", 0) or 0)
                _cs = self._get_contract_size()
                _losing = int(getattr(ss, "cycles_losing_streak", 0) or 0)
                _last_loss = getattr(ss, "last_loss_ts", None)
                _decision = _er.compute_reentry_decision(
                    prices=history,
                    last_sell_price=_sold_ref,
                    spread=current_spread,
                    losing_streak=_losing,
                    fee_per_roundtrip=_fee_rt,
                    contract_size=_cs,
                    qty=max(1, int(sc.qty or 1)),
                    now_ts=now,
                    last_loss_ts=_last_loss,
                )
                self._record(
                    "expert_reentry_decision",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    action=_decision.action,
                    buy_px=_decision.buy_px,
                    wait_secs=_decision.wait_secs,
                    citations=_decision.citations,
                    expert_votes=_decision.expert_votes,
                    losing_streak=_losing,
                    last_loss_ts=_last_loss,
                )
                if _decision.action in ("wait", "cool_off"):
                    # Skip re-arm; caller re-evaluates next tick (or after
                    # wait_secs if we implement a cooldown timer follow-up).
                    ss._last_auto_refresh_ts = now
                    return
                # action == "rebuy" — use decision.buy_px
                if _decision.buy_px is not None and _decision.buy_px > 0:
                    new_buy_px = float(_decision.buy_px)
                else:
                    # Consensus said rebuy but no valid price — fall through
                    # to arm_level as safety net.
                    new_buy_px = None
            else:
                new_buy_px = None
        except Exception as _ee:
            self._record("expert_reentry_error",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         error=str(_ee), severity="warn",
                         reason="expert_reentry raised; falling back to arm_level")
            new_buy_px = None

        # Fallback: legacy Chan+Connors path (also used when
        # expert_reentry.MODE == "off" or when consensus was inconclusive).
        if new_buy_px is None:
            try:
                import arm_level as _al
                new_buy_px = _al.pullback_buy_px(
                    history,
                    spread=current_spread,
                    sold_price=float(last_price),
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
        reanchor pullback logic is unified with expert_reentry.

        Adam 2026-07-15: also consults expert_spread (Avellaneda-Stoikov)
        when __expert_spread_mode__ = 'expert'. AS overrides the legacy
        buy_px pick + optionally moves sell_px too. See
        feedback_experts_control_spread_and_price memory rule."""
        import time as _t
        # Adam 2026-07-15: expert_spread APPLY on intra-cycle walk.
        # When AS is enabled + returns valid, use its buy/sell/spread
        # instead of the legacy arm_level.pullback_buy_px. Different from
        # the post-sell reanchor hook (which fires once per cycle) —
        # this hook fires on every ARMED_BUY tick that reeval decides
        # to walk, so AS is now re-evaluating vol/regime constantly.
        as_decision = None
        as_new_sell_px = None
        try:
            spread_mode = self._expert_spread_mode()
            if spread_mode in ("shadow", "expert"):
                import expert_spread as _es
                # Cycle-completion timestamps (arrival-rate estimate)
                cycle_ts = []
                try:
                    if hasattr(self, "trade_log") and self.trade_log:
                        for e in self.trade_log.events():
                            if not isinstance(e, dict):
                                continue
                            if e.get("event_type") != "sleeve_cycle_completed":
                                continue
                            if e.get("sleeve_id") != sc.id:
                                continue
                            ts = float(e.get("ts") or 0)
                            if ts > 0:
                                cycle_ts.append(ts)
                except Exception:
                    pass
                inventory = 0
                try:
                    inventory = int(self.b.position_qty() or 0)
                except Exception:
                    pass
                fee_rt = float(getattr(self.cfg, "fee_per_contract_roundtrip", 0.0) or 0.0)
                tick = float(getattr(self.cfg, "tick_size", 0) or 0)
                as_decision = _es.grid_search_optimal_gamma(
                    mid_price=float(last_price),
                    price_history=list(self._sleeve_price_history.get(sc.id, []) or []),
                    cycle_completion_ts=cycle_ts,
                    fee_per_roundtrip=fee_rt,
                    contract_size=float(self.cfg.contract_size),
                    qty=int(sc.qty),
                    tick_size=tick if tick > 0 else None,
                    inventory=inventory,
                )
                if as_decision is not None:
                    self._record(
                        "expert_spread_intra_cycle_shadow" if spread_mode == "shadow"
                        else "expert_spread_intra_cycle_decision",
                        sleeve_id=sc.id, sleeve_name=sc.name,
                        method=as_decision.method,
                        legacy_new_buy_px=float(dec.new_buy_px),
                        current_sell_px=float(sc.sell_px),
                        as_buy_px=as_decision.buy_px,
                        as_sell_px=as_decision.sell_px,
                        as_spread=as_decision.spread,
                        as_expected_daily_pnl=as_decision.expected_daily_pnl,
                        as_expected_cycles_per_day=as_decision.expected_cycles_per_day,
                        as_cost_floor_binding=as_decision.cost_floor_binding,
                        mode=spread_mode,
                    )
                    if spread_mode == "expert":
                        as_new_sell_px = as_decision.sell_px
                # Shadow mode: don't apply, fall through to legacy path.
        except Exception as _e:
            try:
                self._record("expert_spread_intra_cycle_error",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             error=str(_e), severity="warn")
            except Exception:
                pass

        # Use the shared level helper (Tier 2 #3 — unified with expert_reentry)
        try:
            if as_decision is not None and self._expert_spread_mode() == "expert":
                # AS APPLY path — use AS buy_px directly, skip legacy arm_level
                new_buy_px = float(as_decision.buy_px)
                self._record(
                    "expert_spread_intra_cycle_applied",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    replaced_legacy_new_buy_px=float(dec.new_buy_px),
                    as_buy_px=new_buy_px,
                    as_sell_px=as_new_sell_px,
                    method=as_decision.method,
                )
            else:
                import arm_level
                spread = max(0.005, float(sc.sell_px) - float(sc.buy_px))
                # Adam 2026-07-17 XLP runaway fix: fallback chain must NOT end
                # at sc.buy_px alone. For a fresh sleeve (no last_sell_fill_price
                # yet), passing current buy_px as sold_ref caused arm_level's
                # `buy_px < sold_ref` clamp to compute buy_px = sc.buy_px - epsilon
                # every iteration → walk-down. XLP hit 65+ replaces in 3 min,
                # buy_px marched from $0.148 → $0.117 chasing itself away from
                # the $0.187 mark. Fallback order: last sell fill > current mark
                # > sell target > current buy. Each falls back only when the
                # previous is 0/None. This preserves the "buy below sold" invariant
                # in every meaningful case without letting sc.buy_px self-anchor.
                sold_ref = float(
                    ss.last_sell_fill_price
                    or last_price
                    or sc.sell_px
                    or sc.buy_px
                )
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
        # Preserved-spread sell_px, then FEE-FLOOR CLAMP (Adam 2026-07-20
        # feedback_no_net_loss_cycles). Bare "+ max(0.005, spread)" was
        # too permissive — half a cent doesn't clear typical fees. On a
        # reeval buy-walk DOWN, the preserved spread can produce a
        # sell_px that fires green but nets red once fees hit. Same
        # class as SLR 2026-07-19 -$0.72 profit-lock loss. Clamp:
        # sell_px_min = new_buy_px + fee_per_ct + max(tick, 5 bps).
        _desired_sell = float(new_buy_px) + max(0.005, float(sc.sell_px) - old_buy_px)
        try:
            _fee_rt = float(getattr(self.cfg, "fee_per_contract_roundtrip", 0) or 0)
            _cs = float(getattr(self.cfg, "contract_size", 0) or 0)
            _q = max(1, int(getattr(sc, "qty", 1) or 1))
            _tick = float(getattr(self.cfg, "tick_size", 0) or 0.0)
            if _fee_rt > 0 and _cs > 0 and float(new_buy_px) > 0:
                _fee_per_unit = _fee_rt / _cs / _q
                _safety = max(_tick if _tick > 0 else 0.0, float(new_buy_px) * 0.0005)
                _floor = float(new_buy_px) + _fee_per_unit + _safety
                if _desired_sell < _floor:
                    _clamped = self._snap_to_tick(_floor) if _tick > 0 else _floor
                    if _clamped < _floor:
                        _clamped = _clamped + (_tick or 0.0)
                    self._record(
                        "reentry_reeval_sell_px_fee_floor_clamp",
                        sleeve_id=sc.id, sleeve_name=sc.name,
                        requested_sell_px=_desired_sell,
                        fee_floor_sell_px=_clamped,
                        new_buy_px=float(new_buy_px),
                        severity="info",
                        reason=("reeval buy-walk sell_px was below buy + "
                                "fees + safety — clamped up per "
                                "feedback_no_net_loss_cycles"),
                    )
                    _desired_sell = _clamped
        except Exception as _e:
            try:
                self._record("reentry_reeval_fee_floor_error",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             error=str(_e), severity="warn")
            except Exception:
                pass
        sc.sell_px = float(_desired_sell)
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
        (no re-arm next tick per Tier 2 #2), persist state.

        Adam 2026-07-20 ORPHAN GUARD: only clear tracking on cancel success.
        Prior code cleared unconditionally after `except pass`, leaving a
        potential orphan if cancel raised."""
        old_oid = ss.live_order_id
        _exp_cancel_ok = True
        if old_oid:
            _exp_cancel_ok = False
            try:
                self.b.cancel(old_oid)
                _exp_cancel_ok = True
            except Exception as _e:
                self._record("reentry_reeval_expire_cancel_failed",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             old_order_id=old_oid, error=str(_e),
                             severity="critical",
                             reason=("cancel raised on expire; keeping "
                                     "tracking to avoid orphan"))
        if _exp_cancel_ok:
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
        """Volatility-contraction re-entry after a stop.

        Adam 2026-07-16: reentry decision now runs through expert_gate
        (majority vote of Kaufman / Wilder-ADX / Cartea-OFI / Kyle-λ /
        Menkveld-cycle-econ + Hasbrouck-derived cadence floor). Prior
        hardcoded 30s + 50% contraction was the PT/HYPE bleed enabler —
        no regime awareness, no toxicity check.

        Kill switch: set expert_gate.MODE = "off" to revert to the
        legacy path below.
        """
        if not ss.reentry_pending:
            return False
        if sc.reentry_mode != "volatility":
            # Config changed under us — clear the pending flag and let normal
            # arm logic take over.
            ss.reentry_pending = False
            return False
        import time as _t
        elapsed = _t.time() - (ss.reentry_stop_ts or 0)

        # ---- Expert-gate consultation (2026-07-16) --------------------
        try:
            import expert_gate as _eg
        except Exception:
            _eg = None

        expert_gate_ran = False
        if _eg is not None and getattr(_eg, "MODE", "expert") == "expert":
            try:
                prices = list(self._sleeve_price_history.get(sc.id, []) or [])
                # Recent cycle PnLs (last N sleeve_cycle_completed events)
                cycle_pnls = []
                try:
                    if hasattr(self, "trade_log") and self.trade_log:
                        for e in self.trade_log.events():
                            if not isinstance(e, dict):
                                continue
                            if e.get("event_type") != "sleeve_cycle_completed":
                                continue
                            if e.get("sleeve_id") != sc.id:
                                continue
                            pnl = e.get("realized_pnl") or e.get("cycle_pnl") or 0
                            try:
                                cycle_pnls.append(float(pnl))
                            except (TypeError, ValueError):
                                pass
                except Exception:
                    pass
                # Microstructure inputs (None-safe)
                ofi = None
                kyle_lam = None
                kyle_base = None
                try:
                    if self.ms is not None and hasattr(self.ms, "snapshot"):
                        snap = self.ms.snapshot()
                        if isinstance(snap, dict):
                            ofi = snap.get("trade_ofi_60s") or snap.get("ofi") or snap.get("obi")
                            kyle_lam = snap.get("kyle_lambda")
                            kyle_base = snap.get("kyle_lambda_baseline") or snap.get("kyle_lambda_avg")
                except Exception:
                    pass
                dec = _eg.reentry_allowed(
                    prices=prices,
                    elapsed_since_stop_secs=float(elapsed),
                    reentry_direction="buy",
                    order_flow_imbalance=(float(ofi) if ofi is not None else None),
                    kyle_lambda=(float(kyle_lam) if kyle_lam is not None else None),
                    kyle_baseline=(float(kyle_base) if kyle_base is not None else None),
                    recent_cycle_pnls=cycle_pnls if cycle_pnls else None,
                )
                expert_gate_ran = True
                self._record(
                    "sleeve_reentry_gate_decision",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    allow=dec.allow,
                    votes=dec.votes,
                    vote_count=dec.vote_count,
                    total_voters=dec.total_voters,
                    cadence_ok=dec.cadence_ok,
                    cadence_floor_secs=dec.cadence_floor_secs,
                    elapsed_since_stop_secs=dec.elapsed_since_stop_secs,
                    method=dec.method,
                    citation=dec.citation,
                )
                if not dec.allow:
                    return False   # experts said no; wait longer
                # Experts said YES — fall through to reanchor + fire below.
            except Exception as _e:
                try:
                    self._record("sleeve_reentry_gate_error",
                                 sleeve_id=sc.id, sleeve_name=sc.name,
                                 error=str(_e), severity="warn")
                except Exception:
                    pass

        # ---- Legacy fallback (kill-switch / expert error path) ----------
        # Only runs if expert_gate didn't return True above. Preserves the
        # historic 30s + 50% contraction behavior as the safety net.
        if not expert_gate_ran:
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
            # Set defaults for the shared telemetry event below
            current_range = self._sleeve_recent_range(sc)
            pre_range = float(ss.pre_stop_range or 0.0)
        else:
            # Expert path took over; compute these for the event payload
            current_range = self._sleeve_recent_range(sc)
            pre_range = float(ss.pre_stop_range or 0.0)

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

        # Adam 2026-07-15: Avellaneda-Stoikov spread computation.
        # Gated behind __expert_spread_mode__ tenant flag:
        #   'off'    (default) — this block does not run
        #   'shadow' — compute + log only, no state change
        #   'expert' — compute + log AND apply AS buy/sell targets,
        #              overriding the legacy experts_reentry pick below
        #
        # When EXPERT mode wins, the legacy `decision` values (buy_px /
        # sell_px) are OVERWRITTEN in place with the AS values before the
        # reanchor call fires. The rest of the pipeline (tick-snap, config
        # persist, ARMED_BUY timer reset) is unchanged — the reanchor
        # helper is agnostic to who picked the numbers.
        as_decision_for_apply = None
        try:
            spread_mode = self._expert_spread_mode()
            if spread_mode in ("shadow", "expert"):
                import expert_spread as _es
                # Gather cycle-completion timestamps for arrival-rate estimate.
                # Fall back to empty list if trade log unavailable — helper
                # then uses the conservative _MIN_ARRIVAL_RATE_PER_HOUR floor.
                cycle_ts = []
                try:
                    if hasattr(self, "trade_log") and self.trade_log:
                        for e in self.trade_log.events():
                            if not isinstance(e, dict):
                                continue
                            if e.get("event_type") != "sleeve_cycle_completed":
                                continue
                            if e.get("sleeve_id") != sc.id:
                                continue
                            ts = float(e.get("ts") or 0)
                            if ts > 0:
                                cycle_ts.append(ts)
                except Exception:
                    pass
                # Position (inventory) — 0 if flat, +qty if long
                inventory = 0
                try:
                    inventory = int(self.b.position_qty() or 0)
                except Exception:
                    pass
                fee_rt = float(getattr(self.cfg, "fee_per_contract_roundtrip", 0.0) or 0.0)
                tick = float(getattr(self.cfg, "tick_size", 0) or 0)
                as_decision = _es.grid_search_optimal_gamma(
                    mid_price=float(sold_price),
                    price_history=prices,
                    cycle_completion_ts=cycle_ts,
                    fee_per_roundtrip=fee_rt,
                    contract_size=float(self.cfg.contract_size),
                    qty=int(sc.qty),
                    tick_size=tick if tick > 0 else None,
                    inventory=inventory,
                )
                if as_decision is not None:
                    self._record(
                        "expert_spread_shadow_decision" if spread_mode == "shadow"
                        else "expert_spread_expert_decision",
                        sleeve_id=sc.id, sleeve_name=sc.name,
                        method=as_decision.method,
                        citation=as_decision.citation,
                        legacy_buy_px=decision.get("buy_px"),
                        legacy_sell_px=decision.get("sell_px"),
                        legacy_spread=(
                            (decision.get("sell_px") or 0) -
                            (decision.get("buy_px") or 0)),
                        as_buy_px=as_decision.buy_px,
                        as_sell_px=as_decision.sell_px,
                        as_spread=as_decision.spread,
                        as_reservation_price=as_decision.reservation_price,
                        as_expected_cycles_per_day=as_decision.expected_cycles_per_day,
                        as_expected_profit_per_cycle=as_decision.expected_profit_per_cycle,
                        as_expected_daily_pnl=as_decision.expected_daily_pnl,
                        as_cost_floor_binding=as_decision.cost_floor_binding,
                        as_lambda_widening=as_decision.lambda_widening,
                        as_inputs=as_decision.inputs,
                        mode=spread_mode,
                    )
                    # EXPERT mode: hand off to the apply step below.
                    if spread_mode == "expert":
                        as_decision_for_apply = as_decision
        except Exception as _e:
            # Never let the spread expert crash the reanchor path.
            try:
                self._record("expert_spread_error",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             error=str(_e), severity="warn")
            except Exception:
                pass
        if not decision.get("should_arm"):
            return
        new_buy = decision.get("buy_px")
        new_sell = decision.get("sell_px")

        # Adam 2026-07-15: EXPERT-mode APPLY. When __expert_spread_mode__
        # is 'expert' and we got a valid AS decision above, override the
        # legacy buy/sell with the Avellaneda-Stoikov values. The AS
        # values already respect the cost floor (Cartea-Jaimungal 2015
        # ch.8 §8.3.2) and are tick-snapped, so we can hand them straight
        # to _reanchor_sleeve. Records expert_spread_applied event so the
        # audit trail shows WHICH cycle used AS vs legacy.
        if as_decision_for_apply is not None:
            as_buy = as_decision_for_apply.buy_px
            as_sell = as_decision_for_apply.sell_px
            if (as_buy > 0 and as_sell > 0 and as_sell > as_buy):
                self._record(
                    "expert_spread_applied",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    replaced_legacy_buy_px=new_buy,
                    replaced_legacy_sell_px=new_sell,
                    as_buy_px=as_buy, as_sell_px=as_sell,
                    as_spread=as_decision_for_apply.spread,
                    as_expected_daily_pnl=as_decision_for_apply.expected_daily_pnl,
                    method=as_decision_for_apply.method,
                    citation=as_decision_for_apply.citation,
                )
                new_buy = as_buy
                new_sell = as_sell
            else:
                self._record(
                    "expert_spread_apply_skipped_invalid",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    as_buy_px=as_buy, as_sell_px=as_sell,
                    reason="AS decision failed sanity: buy<=0, sell<=0, or sell<=buy",
                    severity="warn",
                )

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
        old_stop = float(sc.stop_loss_px or 0.0)
        # Adam day-one rule (formalized in feedback_no_net_loss_cycles.md
        # 2026-07-20): every take-profit sell must clear fees. Clamp any
        # incoming sell_px below the fee floor so a "profit-lock" fire
        # can never close for a net loss. Only stop_loss / ratchet-stop
        # exits are allowed to exit red — they're protective, not TP.
        # SLR 2026-07-19 incident: bought $56.665, reanchor set sell_px
        # $56.710 (+8bps), fees ~11bps, net -$0.72 loss on a "take-profit."
        try:
            _fee_rt = float(getattr(self.cfg, "fee_per_contract_roundtrip", 0) or 0)
            _cs = float(getattr(self.cfg, "contract_size", 0) or 0)
            _q = max(1, int(getattr(sc, "qty", 1) or 1))
            _tick = float(getattr(self.cfg, "tick_size", 0) or 0.0)
            if _fee_rt > 0 and _cs > 0 and float(new_buy_px) > 0:
                _fee_per_unit = _fee_rt / _cs / _q
                _safety = max(_tick if _tick > 0 else 0.0,
                              float(new_buy_px) * 0.0005)
                _floor = float(new_buy_px) + _fee_per_unit + _safety
                if float(new_sell_px) < _floor:
                    _clamped = self._snap_to_tick(_floor) if _tick > 0 else _floor
                    if _clamped < _floor:  # snap rounded us back under
                        _clamped = _clamped + (_tick or 0.0)
                    self._record(
                        "sleeve_reanchor_sell_px_fee_floor_clamp",
                        sleeve_id=sc.id, sleeve_name=sc.name,
                        requested_sell_px=float(new_sell_px),
                        fee_floor_sell_px=_clamped,
                        new_buy_px=float(new_buy_px),
                        fee_per_roundtrip=_fee_rt,
                        contract_size=_cs, qty=_q,
                        severity="info",
                        reason=("expert/config sell_px was below "
                                "buy + fees + safety — clamped up to "
                                "prevent a net-loss profit-lock exit "
                                "per feedback_no_net_loss_cycles"),
                    )
                    new_sell_px = _clamped
        except Exception as _e:
            # Fee floor is a guardrail — never fail-hard here. Log + fall
            # through with the original sell_px so a math error can't
            # block a legitimate reanchor.
            try:
                self._record("sleeve_reanchor_fee_floor_error",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             error=str(_e), severity="warn")
            except Exception:
                pass
        sc.buy_px = float(new_buy_px)
        sc.sell_px = float(new_sell_px)
        sc.trail_trigger = float(new_sell_px)
        # Reanchor stop_loss_px to maintain the same dollar distance below the
        # new buy. Without this, stop_loss_px stays at the pre-reanchor level.
        # If price reanchored DOWN, the stale stop is above the new buy price,
        # causing _maintain_resting_stop to place a stop that fires immediately
        # on the first tick after the buy fills. Root cause of the NEAR/PLAT
        # immediate-sell-after-buy pattern observed 2026-07-17.
        # Only shift when old_buy > 0 to avoid division by zero on fresh config.
        new_stop = 0.0
        if old_buy > 0 and old_stop > 0:
            stop_offset = old_buy - old_stop          # $ below old buy
            new_stop = float(new_buy_px) - stop_offset
            new_stop = self._snap_to_tick(new_stop) if new_stop > 0 else 0.0
            # Hard safety: stop must always be strictly below the new buy.
            # If the offset math produces a stop >= new_buy (edge case: offset
            # was negative, i.e. stop was already above buy in old config),
            # leave stop_loss_px at zero so _maintain_resting_stop skips it.
            if new_stop >= float(new_buy_px):
                new_stop = 0.0
        if new_stop > 0:
            sc.stop_loss_px = new_stop
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
                if new_stop > 0:
                    s["stop_loss_px"] = new_stop
                break
        cfg["sleeves"] = sleeves
        self.store.put_config(self.tenant_id, self.symbol, cfg)
        self._record(
            "sleeve_reanchored",
            sleeve_id=sc.id, sleeve_name=sc.name,
            current_price=current_price,
            old_buy=old_buy, old_sell=old_sell,
            new_buy=new_buy_px, new_sell=new_sell_px,
            old_stop_loss_px=old_stop, new_stop_loss_px=new_stop,
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
        #
        # 2026-07-19 EXTENDED (SLR $56.03 incident): also defer when a sleeve
        # has resting_stop_enabled but hasn't PLACED yet — the ~1s window
        # between a fresh BUY fill and _maintain_resting_stop() writing the
        # oid was a hole. Primary would see mark<stop on the fresh position
        # and market-sell what the sleeve just bought. Now: if ANY sleeve
        # HAS or WILL HAVE a resting stop, primary defers. If the resting
        # stop actually never places (Coinbase failure), the ratchet-stop
        # gap watchdog raises a critical alert — not this path double-selling.
        sleeves = (self.s.sleeves or {}).values()
        sleeves_cfg = {sc.id: sc for sc in self._load_sleeves_cfg()}
        active_resting = [ss.resting_stop_oid for ss in sleeves
                          if getattr(ss, "resting_stop_oid", None)]
        will_have_resting = [
            ss.id for ss in sleeves
            if getattr(sleeves_cfg.get(ss.id), "resting_stop_enabled", False)
            and not getattr(ss, "resting_stop_oid", None)
        ]
        if active_resting or will_have_resting:
            self._record("stop_loss_skipped_resting_stop_active",
                         price=last_price, trigger=trigger,
                         resting_stop_oids=list(active_resting),
                         will_have_resting=list(will_have_resting))
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
        # Adam 2026-07-16: append to primary price history so expert_spread
        # has a live vol estimate on the next arm. Zero-cost when unused.
        try:
            px = float(last_price)
            if px > 0:
                self._primary_price_history.append(px)
        except (TypeError, ValueError):
            pass

        # Adam 2026-07-15: consume any pending state-store patch FIRST.
        # Solves the diag-vs-live race — diag scripts (force-credit backfill,
        # etc.) write a patch that this consumer merges into in-memory state
        # BEFORE the tick's own _save_state() runs and clobbers it.
        # See _maybe_consume_state_patch docstring.
        self._maybe_consume_state_patch()

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
            # Adam 2026-07-20: auto-resume when there is literally nothing to
            # defend against. XLP double-fire 07:07 halted the strategy; Adam
            # manually flattened; sleeves already got removed. Dashboard
            # showed "Strategy halted" with no position + no sleeves = no
            # defensive purpose, just blocks future scanner arms + requires
            # click-through Resume. Now: if halted AND no live_order_id AND
            # no sleeves AND broker position is 0, auto-clear halt state.
            # Loud log so the auto-resume is traceable (not silent).
            try:
                _has_sleeves = bool(self.s.sleeves)
                _has_order = bool(self.s.live_order_id)
                try:
                    _pos_qty = int(self.b.position_qty() or 0)
                except Exception:
                    _pos_qty = -1  # unknown — do NOT auto-resume on error
                if (not _has_sleeves and not _has_order and _pos_qty == 0):
                    _prev_reason = self.s.halt_reason
                    self.s.state = State.ARMED_SELL
                    self.s.halt_reason = None
                    self._save_state()
                    self._record(
                        "strategy_auto_resumed_empty",
                        prev_halt_reason=_prev_reason,
                        severity="info",
                        reason=("halted strategy had no sleeves, no live "
                                "order, and zero broker position — nothing "
                                "to defend against. Auto-cleared halt so "
                                "future scanner arms + manual attach work "
                                "without click-through Resume."),
                    )
                    # Fall through — strategy is now ARMED_SELL with empty
                    # state; the normal tick below handles it correctly.
                else:
                    return
            except Exception as _e:
                # Any failure = keep halted (defensive default). Log loud.
                self._record("strategy_auto_resume_check_failed",
                             error=str(_e), severity="warn",
                             reason="auto-resume check raised; staying halted")
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

        # 0 means "band disabled" — skip the check entirely.  A product seeded
        # from SLR defaults (abort_above=70) for a $3k+ asset would halt every
        # tick without this guard (2026-07-16 CHN incident).
        if self.cfg.abort_above > 0 and self.s.state == State.ARMED_SELL and last_price >= self.cfg.abort_above:
            return self._halt(
                f"price {last_price} ran above abort_above {self.cfg.abort_above} while flat on swing"
            )
        if self.cfg.abort_below > 0 and self.s.state == State.ARMED_BUY and last_price <= self.cfg.abort_below:
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

        # Re-sync sleeve state from Redis first — external writes (diag
        # force-credit, dashboard resume, reconcile autocorrector) get
        # honored within 1s instead of being clobbered by our in-memory
        # copy on the next _save_state(). See _reload_sleeves_from_redis
        # docstring for the SLR/HYP/PT/XLP incident class this closes.
        self._reload_sleeves_from_redis()

        # Reload sleeve configs each tick — user may have added/removed sleeves
        # from the dashboard. Ensure state dict has entries for all configured.
        sleeves_cfg = self._load_sleeves_cfg()
        configured_ids = {sc.id for sc in sleeves_cfg}
        # Drop state for removed sleeves; add fresh state for new ones.
        # Adam 2026-07-20 ORPHAN GUARD: sleeve removal must cancel BOTH
        # live_order_id AND resting_stop_oid before dropping tracking.
        # Prior code only cancelled live_order_id (silently swallowing
        # any exception) and completely ignored resting_stop_oid. If a
        # sleeve held a position with a live stop-limit, the resting
        # stop stayed on Coinbase forever after removal — orphan (§3.8
        # short risk if it triggers with position=0).
        for sid in list(self.s.sleeves.keys()):
            if sid not in configured_ids:
                st_obj = self.s.sleeves[sid]
                # Cancel live order — log failure so operator can trace
                if st_obj.live_order_id:
                    try:
                        self.b.cancel(st_obj.live_order_id)
                    except Exception as _lce:
                        self._record("sleeve_removed_live_order_cancel_failed",
                                     sleeve_id=sid,
                                     order_id=st_obj.live_order_id,
                                     error=str(_lce), severity="critical",
                                     reason=("sleeve removal cancel raised — "
                                             "order may be orphan on Coinbase; "
                                             "startup orphan-sweep or manual "
                                             "diag_cancel needed"))
                # Cancel resting stop-limit if held
                if getattr(st_obj, "resting_stop_oid", None):
                    try:
                        self.b.cancel(st_obj.resting_stop_oid)
                    except Exception as _rce:
                        self._record("sleeve_removed_resting_stop_cancel_failed",
                                     sleeve_id=sid,
                                     oid=st_obj.resting_stop_oid,
                                     error=str(_rce), severity="critical",
                                     reason=("sleeve removal cancel raised — "
                                             "resting stop may be orphan on "
                                             "Coinbase; startup orphan-sweep "
                                             "or manual diag_cancel needed"))
                del self.s.sleeves[sid]
        # Retirement ledger: if the product is in cooldown from a prior
        # retire, refuse to instantiate NEW sleeve state. Existing sleeves
        # keep ticking (safety), but no fresh SleeveState is created for a
        # sleeve id that isn't already in memory. Closes the PT/HYP/SLR
        # ghost class where diag_retire_sleeves removed state but next tick
        # re-inflated from config.
        try:
            import retirement_ledger as _rl
            _in_cd, _cd_reason, _cd_secs = _rl.is_in_cooldown(
                self.store, self.tenant_id, self.symbol
            )
        except Exception:
            _in_cd, _cd_reason, _cd_secs = False, "", 0.0

        for sc in sleeves_cfg:
            if sc.id in self.s.sleeves:
                continue
            if _in_cd:
                self._record(
                    "sleeve_retirement_cooldown_blocked_arm",
                    sleeve_id=sc.id, symbol=self.symbol,
                    reason=_cd_reason, cooldown_secs_remaining=int(_cd_secs),
                    severity="warn",
                )
                continue
            self.s.sleeves[sc.id] = SleeveState(id=sc.id)

        # Run each additional sleeve's state machine independently.
        for sc in sleeves_cfg:
            if sc.id not in self.s.sleeves:
                continue  # blocked by retirement cooldown; skip ticking
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
        # Adam 2026-07-20: NO hardcoded default for stop_loss_px. Per
        # feedback_experts_all_algo_params, every algorithmic parameter comes
        # from expert consensus (§3.15). The expert-consulted fallback at
        # line ~5091 already handles the case where stop_loss_enabled=true +
        # stop_loss_px=0 — computes protective stop via expert_stop
        # (Wilder/CJP/Kyle/Menkveld/Van Tharp). No hardcoded fraction needed.
        # Adam 2026-07-20 §3.8 EXCESS-STOP CANCEL: XLP double-fire → SHORT
        # incident. Prior guard at "fresh place" only REFUSED new stops when
        # sum(sibling stops) + this_qty > pos_qty. But if position shrinks
        # AFTER stops were placed (e.g. one sleeve's stop fires, pos 2→1,
        # but the other sleeve's stop is still on Coinbase covering the now-
        # gone contract), both stops eventually fire → 2 sells against 1
        # long → SHORT. Fix: EVERY tick, sum ALL resting stop qty across
        # sleeves; if total > pos_qty, deterministically cancel excess.
        # Sort by sleeve_id for stable choice of which sleeve loses its stop.
        if pos_qty > 0 and self.s.sleeves:
            _all_stops = []
            for _sid, _ss in (self.s.sleeves or {}).items():
                if not getattr(_ss, "resting_stop_oid", None):
                    continue
                _sc_other = self._sleeve_cfg_by_id(_sid)
                _q = int(getattr(_sc_other, "qty", None) or getattr(_ss, "qty", None) or 1)
                _all_stops.append((_sid, _ss, _q))
            _all_stops.sort(key=lambda x: x[0])  # deterministic order
            _covered = 0
            for _sid, _ss_o, _q in _all_stops:
                if _covered + _q <= pos_qty:
                    _covered += _q
                    continue
                # This sleeve's stop is (fully or partly) beyond pos coverage.
                # Cancel it — position no longer justifies the stop-limit.
                if _sid == sc.id:
                    _oid_to_cancel = ss.resting_stop_oid
                    try:
                        self.b.cancel(_oid_to_cancel)
                        ss.resting_stop_oid = None
                        ss.resting_stop_px = None
                        ss.resting_stop_stage = None
                        self._record(
                            "resting_stop_cancelled_excess_over_position",
                            sleeve_id=sc.id, sleeve_name=sc.name,
                            cancelled_oid=_oid_to_cancel,
                            covered_before_this=_covered,
                            this_qty=_q,
                            pos_qty=pos_qty,
                            severity="critical",
                            reason=("total resting stop qty exceeded actual "
                                    "position — cancelled to prevent double-"
                                    "fire → §3.8 short-risk (XLP incident "
                                    "2026-07-20). This sleeve lost coverage "
                                    "priority via sleeve_id sort order."),
                        )
                        try:
                            self._save_state()
                        except Exception:
                            pass
                    except Exception as _e:
                        self._record(
                            "resting_stop_cancel_excess_failed",
                            sleeve_id=sc.id, sleeve_name=sc.name,
                            oid=_oid_to_cancel, error=str(_e),
                            severity="critical",
                            reason=("excess stop cancel FAILED — short-risk "
                                    "remains. Will retry next tick."),
                        )
                    return  # done for this tick regardless
                # Not our sleeve — some later sleeve step will cancel its
                # own excess when its turn comes. Do nothing here.
                _covered += _q
        if pos_qty <= 0 or sleeve_qty <= 0:
            # Position closed. Before we cancel + clear the stop_oid, check
            # if that stop is what CAUSED pos_qty to hit 0 (i.e. it FILLED).
            # If we blindly cancel + wipe stop_oid, the credit path will
            # never see the fill → own_avg_entry stays set → ghost sleeve
            # (2026-07-19 SLR incident — Adam: "no more fucking ghosts").
            #
            # Root cause of the ghost class this fix addresses: the tick
            # loop runs _maintain_resting_stop BEFORE _maybe_credit_resting_
            # stop_fill in the multi-sleeve step. On a multi-sleeve product
            # where a sibling's stop fires and closes the shared position,
            # every sleeve sees pos_qty=0 and wipes its stop_oid — even
            # sleeves whose OWN stop just filled.
            _fill_credited = False
            if ss.resting_stop_oid:
                try:
                    _oid = ss.resting_stop_oid
                    _st = self.b.order_status(_oid)
                    _status = (_st or {}).get("status")
                    if _status == "FILLED":
                        try:
                            _fp = float(_st.get("average_filled_price") or 0)
                        except (TypeError, ValueError):
                            _fp = 0.0
                        try:
                            _fq = int(_st.get("filled_qty") or sleeve_qty or 1)
                        except (TypeError, ValueError):
                            _fq = sleeve_qty or 1
                        # Route through the shared credit helper so we get
                        # the same dedup + own_avg fallback + never-silent-$0
                        # protection as the tick-credit path.
                        _fill_credited = self._credit_stop_fill(
                            sc, ss, _fp, _fq,
                            source="maintain_stop_pos_zero_detect",
                            oid=_oid,
                        )
                        if _fill_credited:
                            self._record(
                                "resting_stop_credit_via_pos_zero_race_fix",
                                sleeve_id=sc.id, sleeve_name=sc.name,
                                oid=_oid, fill_price=_fp, filled_qty=_fq,
                                severity="info",
                                reason=("_maintain_resting_stop detected pos=0 "
                                        "but stop FILLED — credited before wipe "
                                        "to prevent ghost sleeve"),
                            )
                except Exception as _e:
                    self._record("maintain_stop_pos_zero_status_probe_failed",
                                 sleeve_id=sc.id, sleeve_name=sc.name,
                                 oid=ss.resting_stop_oid, error=str(_e),
                                 severity="warn")
            if ss.resting_stop_oid and not _fill_credited:
                # Not FILLED (CANCELLED / UNKNOWN / probe failed). Try to
                # cancel + clear.
                # Adam 2026-07-20 ORPHAN GUARD: only clear tracking if cancel
                # succeeded. Prior code cleared unconditionally after
                # `except log`, so a failed cancel produced an orphan SELL
                # stop with position=0. If mark drops through the stop, it
                # fires → SHORT (§3.8 violation).
                _np_ok = False
                try:
                    self.b.cancel(ss.resting_stop_oid)
                    _np_ok = True
                except Exception as e:
                    self._record("resting_stop_cancel_failed", sleeve_id=sc.id,
                                 sleeve_name=sc.name, oid=ss.resting_stop_oid,
                                 error=str(e), severity="critical",
                                 reason=("cancel raised on no-position clear; "
                                         "keeping tracking so next tick retries "
                                         "— avoids orphan short-risk"))
                if _np_ok:
                    self._record("resting_stop_cleared", sleeve_id=sc.id,
                                 sleeve_name=sc.name, reason="no_position")
                    ss.resting_stop_oid = None
                    ss.resting_stop_px = None
                    ss.resting_stop_stage = None
                # ROOT FIX 2026-07-20: persist so next tick's reload sees cleared state.
                try:
                    self._save_state()
                except Exception:
                    pass
            # If we credited above, _credit_stop_fill already cleared the
            # oid + advanced state; no further work here.
            return
        # Resolve stage + target price.
        # Adam 2026-07-20 fix (rev 2): HWM was never updated on the
        # resting-stop path (only _sleeve_step / _sleeve_hybrid_step
        # updated it, and those return early when resting_stop_enabled +
        # exit_mode in hybrid/trailing_stop). Result: for the modern
        # config HWM stayed at own_avg forever, so the trail never
        # ratcheted.
        #
        # Rev 2: the gate must be "we HOLD this position" (own_avg > 0),
        # NOT "trail_armed". Positions bought BEFORE the arm-at-buy fix
        # deployed have trail_armed=False on disk; gating on trail_armed
        # meant those positions could never ratchet. Any held position
        # deserves HWM tracking so the trail engages retroactively. Also
        # auto-arm trail_armed for held sleeves so downstream branches
        # (goal_reached, trail_active) can rely on the flag.
        _own_avg_check = float(ss.own_avg_entry or 0.0)
        _hold = _own_avg_check > 0
        # Adam 2026-07-20 ROOT FIX (SLR ratchet-never-executed incident):
        # every mutation on this path must be paired with _save_state()
        # BEFORE the next tick's _reload_sleeves_from_redis (commit
        # 83dd31b) wipes in-memory changes. Prior code updated
        # trail_armed / trail_high_water_price / resting_stop_* in
        # memory but the enclosing tick loop's save wasn't guaranteed
        # to fire before reload. Result: SLR sat with initial $54.680
        # stop for 90+ minutes while HWM climbed to $57.58 — ratchet
        # target was correct in memory but reverted each tick, so the
        # cancel+place never persisted through the reload cycle.
        _dirty = False
        if _hold and not ss.trail_armed:
            ss.trail_armed = True
            _dirty = True
        # Adam 2026-07-19 (§3.4 enforcement): HWM floor at own_avg. Prior
        # code only ratcheted UP from whatever hwm happened to be — if
        # last_price dropped below own_avg between arm and this call, hwm
        # could end up below own_avg. That produced the "trail below
        # entry" numbers on NER, XLP, ZEC (hwm was 0.03-3% under own_avg).
        # Per §3.4: on buy fill, hwm=own_avg; then ratchet UP only.
        own_avg_for_hwm = float(ss.own_avg_entry or 0.0)
        _target_hwm = float(ss.trail_high_water_price or 0.0)
        if own_avg_for_hwm > 0 and _target_hwm < own_avg_for_hwm and (_hold or bool(ss.trail_armed)):
            _target_hwm = own_avg_for_hwm
        if (_hold or bool(ss.trail_armed)) and last_price and float(last_price) > _target_hwm:
            _target_hwm = float(last_price)
        if _target_hwm != float(ss.trail_high_water_price or 0.0):
            ss.trail_high_water_price = _target_hwm
            _dirty = True
        if _dirty:
            try:
                self._save_state()
            except Exception:
                pass  # never fail-hard on the stop path
        hwm = float(ss.trail_high_water_price or 0.0)
        trail_engaged = bool(ss.trail_armed)
        stop_loss_px = float(getattr(sc, "stop_loss_px", 0) or 0)
        sell_px = float(getattr(sc, "sell_px", 0) or 0)
        trail_distance = float(getattr(sc, "trail_distance", 0) or 0)
        # Adam 2026-07-20 (feedback_trail_arm_at_buy_fill +
        # feedback_biggest_rule_dont_lose_take_best): compute the break-
        # even floor. Any trail exit must clear own_avg + fee_safety so
        # the take-profit path never closes in the red. Protective
        # stop_loss (hard_bottom) is separate — it may fire below.
        own_avg = float(ss.own_avg_entry or 0.0)
        # Adam 2026-07-19 CRITICAL BUG FIX (SLR sat at $54.68 for 90+ min):
        # prior code fell back to self.cfg.contract_size (default 1) when
        # contract_spec_cache was empty. For SLR (real contract_size=50),
        # this made _fee_price 50x too large: $3.14/1 = $3.14 per unit
        # instead of $3.14/50 = $0.0628. break_even_floor became $60.21
        # instead of $57.13, pushing target above HWM $57.615, making
        # trail_active False and disabling BOTH ratchet + Part 2 market
        # sell entirely. Bot logged resting_stop_skipped_above_mark every
        # tick for 90 min without acting. Fix: query broker.contract_spec
        # directly (source of truth), fall back to cache, then refuse to
        # fake a fee_price if contract_size is still unknown.
        _cs = self._get_contract_size()
        try:
            _fee_rt = float(getattr(self.cfg, "fee_per_contract_roundtrip", 0) or 0)
            _fee_price = _fee_rt / _cs / max(1, int(sc.qty or 1))
        except Exception:
            _fee_price = 0.0
        try:
            _tick = float(getattr(self.cfg, "tick_size", 0) or 0.01)
        except Exception:
            _tick = 0.01
        break_even_floor = 0.0
        if own_avg > 0:
            break_even_floor = own_avg + _fee_price + max(_tick, own_avg * 0.0005)
        # Adam 2026-07-20 (feedback_experts_only_reentry_not_exit):
        # once a sleeve owns a position, exit params are FROZEN at config
        # values. No expert override, no per-tick adaptive recomputation.
        # The bot's job during a hold is to extract profit at the pre-
        # committed sale price + trail as configured. Experts decide the
        # NEXT re-entry (see _reentry_reeval + scanner re-arm), not the
        # current exit. A moving trail_distance defeats Rule #1
        # (don't lose money AND take profit at its best). Prior expert_
        # trail block deleted; trail_distance is used as-is from sc.
        target_px = None
        stage = None
        # Adam 2026-07-15: checkpoint-then-ratchet model.
        # Before sell_px goal → protective stop only (hard bottom). Trail
        #   dormant. The strategy hasn't earned the right to trail yet — it
        #   still needs the goal to be reached to lock in profit.
        # At/after sell_px goal → trail owns the exit, ratcheting up from a
        #   sell_px baseline. Never exits below sell_px (would give up
        #   already-locked-in profit).
        #
        # trail_engaged bot-side flag is ignored here — the resting stop's
        # stage is driven purely by whether HWM has actually crossed the
        # goal, not by when the bot-side trail-arm flag flipped. That's
        # what Adam meant by "trail stopping should take effect once the
        # trail stop reaches the desired sell gap" — the checkpoint is the
        # goal, not an internal flag.
        goal_reached = (sell_px > 0 and (hwm >= sell_px or last_price >= sell_px))
        # Adam 2026-07-20 (feedback_trail_arm_at_buy_fill): once we own
        # the position, the trail is active from own_avg + fee_safety
        # upward. Not just after sell_px is reached. HWM only matters
        # once it climbs above the break-even floor.
        trail_active = (trail_engaged and own_avg > 0 and hwm > break_even_floor)
        # Adam 2026-07-21 PHASE A (BRACKET DESIGN — Rule #1 fix): the
        # goal_reached → trail branch is DISABLED. Prior behavior placed a
        # "stop" ABOVE mark once HWM crossed sell_px, mislabelled as STOP
        # LOSS on the chip and leaving the position UNPROTECTED against
        # further drops (the protective stop-limit at stop_loss_px got
        # cancelled to make room). Violated rule #1 (don't lose money).
        #
        # New design: this function ONLY maintains the protective stop-limit
        # at stop_loss_px BELOW mark. The profit-target at sell_px ABOVE
        # mark is handled by _maintain_and_credit_profit_lock_limit as a
        # separate resting LIMIT SELL. Both coexist on Coinbase (bracket
        # OCO pattern) — exactly one can fire from a continuous price path.
        # Cite: Merton (1973), barrier options — up-and-out + down-and-in
        # barriers partition the terminal state space non-overlappingly;
        # Almgren-Chriss (2000) J.Risk 3:5-39 for OCO bracket rationale.
        # Adam 2026-07-21 PHASE A: stop-limit is ALWAYS at stop_loss_px,
        # BELOW mark. Never trails past sell_px into "stop above mark"
        # territory. Profit-target at sell_px is handled by
        # _maintain_and_credit_profit_lock_limit as a separate LIMIT SELL.
        if stop_loss_px > 0:
            # Protective stop only. Never moved above mark.
            target_px = stop_loss_px
            stage = "hard_bottom"
        # Adam 2026-07-20 ROOT FIX: guarantee every held position gets a
        # protective stop. Prior code returned WITHOUT PLACING when: no
        # trail (hwm not above break_even) AND no goal reached AND
        # stop_loss_px unconfigured (== 0). Fresh scanner-armed sleeves
        # often ship with stop_loss_px=0, so they'd sit ARMED_SELL with
        # a real position but NO exchange stop until mark climbed —
        # exactly XLM 2026-07-19 fresh $0.18786 buy fill.
        #
        # Fallback: if we hold + resting_stop_enabled + no stage fired,
        # compute a protective stop via expert_stop consensus per §3.15.
        # Wilder 2N + CJP OFI + Kyle λ (median), Menkveld fee floor,
        # Van Tharp 10% cap. Uses THIS contract's own historical delta
        # as an ATR proxy — no hardcoded fraction. Skips only when the
        # user explicitly turned off stop_loss_enabled (Adam's NGS-style
        # "let it ride" carve-out).
        if (not target_px or target_px <= 0) and own_avg > 0 \
                and getattr(sc, "stop_loss_enabled", True):
            _atr_est = 0.0
            try:
                _hist_fb = list(self._sleeve_price_history.get(sc.id, []) or [])
                if len(_hist_fb) >= 3:
                    _d = [abs(_hist_fb[i] - _hist_fb[i - 1])
                          for i in range(1, len(_hist_fb))]
                    _recent = _d[-14:]
                    if _recent:
                        _atr_est = sum(_recent) / len(_recent)
            except Exception:
                pass
            _expert_stop_px = 0.0
            _expert_citation = ""
            try:
                import expert_stop as _est_fb
                if _atr_est > 0 and getattr(_est_fb, "MODE", "expert") == "expert":
                    _fee_rt_fb = float(getattr(self.cfg, "fee_per_contract_roundtrip", 0.0) or 0.0)
                    _cs_fb = self._get_contract_size()
                    _tick_fb = float(getattr(self.cfg, "tick_size", 0) or 0)
                    if _cs_fb > 0:
                        _dec = _est_fb.optimal_stop_distance(
                            mark=float(own_avg),
                            atr_est=float(_atr_est),
                            fee_per_roundtrip=_fee_rt_fb,
                            contract_size=_cs_fb,
                            qty=max(1, int(sc.qty or 1)),
                            wilder_multiplier=2.0,
                            tick_size=(_tick_fb if _tick_fb > 0 else None),
                        )
                        if _dec is not None:
                            _expert_stop_px = float(_dec.stop_px)
                            _expert_citation = _dec.citation
            except Exception:
                pass
            if _expert_stop_px > 0:
                target_px = _expert_stop_px
                stage = "hard_bottom_expert_consensus"
                self._record(
                    "resting_stop_expert_fallback_applied",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    own_avg=own_avg, target_px=target_px,
                    atr_est=round(_atr_est, 8),
                    citation=_expert_citation,
                    reason=("stop_loss_px=0 + no trail/goal stage; expert "
                            "consensus (§3.15) computed protective floor."),
                    severity="info",
                )
            else:
                # Expert unavailable (no history, atr=0, contract_size missing).
                # §3.6 says NEVER leave a held position unprotected — so we do
                # place SOMETHING, but at severity=critical so it's visible and
                # traceable to a data-gap. Uses last-resort own_avg×0.95 ONLY
                # in this narrow "expert couldn't vote" path.
                target_px = own_avg * 0.95
                stage = "hard_bottom_no_expert_data"
                self._record(
                    "resting_stop_default_fallback",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    own_avg=own_avg, target_px=target_px,
                    atr_est=round(_atr_est, 8),
                    reason=("expert_stop unavailable (no ATR history or "
                            "contract_size); using own_avg×0.95 last-resort. "
                            "§3.15 requires expert consensus — backfill "
                            "price history to enable it."),
                    severity="critical",
                )
        if not target_px or target_px <= 0:
            return
        try:
            target_px = self._snap_to_tick(float(target_px))
        except Exception:
            pass
        tick = float(getattr(self.cfg, "tick_size", 0) or 0.01)
        # Adam 2026-07-15: expert-driven vol-adaptive buffer (fleet-wide rule
        # per feedback_optimize_realized_dollars_per_day). Objective is to
        # maximize realized $/day = per-cycle profit × cycles/day:
        #   - Too tight: limit gaps through on fast drops, position exits
        #     unprotected (missed fills = lost cycles = lower cycles/day)
        #   - Too wide: fills far below trigger = per-cycle profit erodes
        # Vol-adaptive: floor at max(2 ticks, 5 bps) for calm regimes; add
        # 0.5×sigma×target_px in high-vol regimes so the gap widens exactly
        # when the market can gap. Sigma is the stdev of log returns on
        # recent ticks — unitless, so sigma × price ≈ typical one-sample
        # price move. Half of that is a well-established fill-catching
        # buffer in the market-microstructure literature (Cartea-Jaimungal
        # optimal-execution: limit-order fill probability rises steeply as
        # offset exceeds 0.5σ, plateaus by 1σ). Cap at 2% of target so an
        # extreme vol spike doesn't blow the limit into oblivion.
        vol_buffer = 0.0
        try:
            _hist = list(self._sleeve_price_history.get(sc.id, []) or [])
            if len(_hist) >= 5:
                import math as _math
                _samples = _hist[-20:]
                _rets = [_math.log(_samples[i] / _samples[i - 1])
                         for i in range(1, len(_samples))
                         if _samples[i - 1] > 0 and _samples[i] > 0]
                if len(_rets) >= 3:
                    _mean = sum(_rets) / len(_rets)
                    _var = sum((r - _mean) ** 2 for r in _rets) / len(_rets)
                    _sigma = _math.sqrt(_var)
                    # 0.5σ × price = typical half-move over one tick interval
                    vol_buffer = min(0.5 * _sigma * target_px,
                                     target_px * 0.02)  # cap 2% of target
        except Exception:
            pass  # fall back to floor formula; never fail-hard here
        # Adam 2026-07-20 §3.5 FIX: stage-aware buffer. Trail stages
        # (profit-lock intent) MUST NOT sell below own_avg + fees, so the
        # limit_px buffer stays minimal and gets hard-floored at
        # break_even. Hard_bottom stages (stop-loss intent) accept a loss,
        # so wider vol-buffer is correct for fill probability.
        # Cite: Almgren-Chriss (2000) J.Risk 3:5-39 and Cartea-Jaimungal-
        # Penalva (2015) ch.4 — limit orders at target-or-better preferred
        # over market for illiquid + profit-lock; miss is preferable to
        # filling below target since stop_loss is the accepted-loss backstop.
        # Also Amihud (2002) J.Fin.Markets 5:31-56 — high-Amihud (illiquid)
        # assets suffer 2-3× effective spread; wide buffer eats entire edge.
        # Root cause: MC-17SEP26 sold at $2,688.25 vs $2,695.50 trigger
        # ($7.25 gap) — vol_buffer of ~$7 dominated on this illiquid
        # contract and pushed limit_px below break_even.
        _trail_stages_for_tight_buffer = {
            "trail", "trail_pre_goal", "trail_floored_at_sell",
            "trail_floored_at_break_even", "break_even_lock",
            "profit_lock",
        }
        if stage in _trail_stages_for_tight_buffer:
            # TIGHT buffer only — no vol widening. §3.5 hard floor.
            buffer = max(tick * 2.0, target_px * 0.0005)
            limit_px = max(0.0, target_px - buffer)
            _floor_bp = own_avg + _fee_price + tick
            if own_avg > 0 and limit_px < _floor_bp:
                # Never let a trail-stage limit fill below break-even.
                limit_px = _floor_bp
        else:
            # HARD_BOTTOM stages: wide vol-buffer for fill certainty.
            buffer = max(tick * 2.0, target_px * 0.0005, vol_buffer)
            limit_px = max(0.0, target_px - buffer)
        # Adam 2026-07-20 Rule #1 (feedback_biggest_rule_dont_lose_take_best):
        # if target_px >= last_price while holding, the stop has been breached
        # — mark dropped below where the stop says the exit must fire. Coinbase
        # rejects stop-limit triggers above mark, so a LIMIT sell at target is
        # the only way to enforce the exit decision (already made at arm time
        # — this is execution, not a new decision).
        #
        # Adam 2026-07-20 XLM 31 §3.6 FIX: extended to hard_bottom stages.
        # Prior code assumed hard_bottom is always < mark ("pre-arm protective
        # floor at a lower price by definition"). That's only true when
        # stop_loss_px < own_avg (real loss floor). Scanner-armed sleeves
        # commonly ship with stop_loss_px slightly ABOVE own_avg (tight
        # profit-lock stop) — mark can drop below that while still above
        # own_avg (e.g., XLM 31: own_avg $0.1849, stop_loss $0.18828, mark
        # $0.1858 → stop above mark, position UNPROTECTED without this fix).
        # Cite: Cartea & Jaimungal, Algorithmic and High-Frequency
        # Trading (Cambridge 2015), ch.10 — stop-triggered execution
        # requires marketable orders when the trigger sits above prevailing
        # mid; limit orders above mid gap into fills, market is deterministic.
        if target_px >= last_price:
            _trail_stages_needing_market_exit = {
                "trail", "trail_pre_goal", "trail_floored_at_sell",
                "trail_floored_at_break_even", "break_even_lock",
                "profit_lock",
            }
            _hard_bottom_breach_stages = {
                "hard_bottom", "hard_bottom_expert_consensus",
                "hard_bottom_no_expert_data",
            }
            _breach_kind = None
            if _hold and trail_active and stage in _trail_stages_needing_market_exit:
                _breach_kind = "trail"
            elif _hold and stage in _hard_bottom_breach_stages:
                _breach_kind = "hard_bottom"
            if _breach_kind is not None:
                # Adam 2026-07-20 IDEMPOTENCY GUARD: if live_order_id already
                # tracks an open exit order, don't re-fire. Prevents double-
                # fire → §3.8 short risk when the exit limit sits unfilled
                # for multiple ticks (illiquid mark hovering just under target).
                if ss.live_order_id:
                    try:
                        _st = self.b.order_status(ss.live_order_id)
                        _open_status = (_st or {}).get("status") in ("OPEN", "PENDING", "QUEUED")
                    except Exception:
                        _open_status = True  # unknown → assume open, don't re-fire
                    if _open_status:
                        return
                # ORPHAN GUARD 2026-07-19 (Adam SLR incident): only null the
                # resting_stop tracking after cancel SUCCEEDS. Previously the
                # None-clear ran unconditionally, so a failed cancel left
                # Coinbase holding the stop-limit while the bot forgot the
                # oid, then we placed a market-sell that closed the long —
                # leaving the resting SELL armed to open a SHORT on the next
                # gap. Constitution §3.8 (no shorting on adam-live).
                _cancel_ok = True
                if ss.resting_stop_oid:
                    _oid_to_cancel = ss.resting_stop_oid
                    try:
                        self.b.cancel(_oid_to_cancel)
                        ss.resting_stop_oid = None
                        ss.resting_stop_px = None
                        ss.resting_stop_stage = None
                    except Exception as _e:
                        _cancel_ok = False
                        self._record("trail_breach_cancel_failed",
                                     sleeve_id=sc.id, sleeve_name=sc.name,
                                     oid=_oid_to_cancel, error=str(_e),
                                     severity="critical",
                                     reason=("skipping market-sell — cancel "
                                             "failed, so placing market-sell "
                                             "would leave the resting stop "
                                             "armed to open a SHORT after "
                                             "position closes. Retry next tick."))
                if not _cancel_ok:
                    # Persist any state changes made so far, then skip the
                    # market-sell for this tick. Next tick will re-enter the
                    # branch and retry the cancel. Position remains protected
                    # by the still-live resting stop in the meantime.
                    try:
                        self._save_state()
                    except Exception:
                        pass
                    return
                # Adam 2026-07-20 §3.5 FIX: replace market-sell with LIMIT
                # sell at target_px (or break_even floor, whichever is higher).
                # Prior market-sell in illiquid products fills at the bid,
                # which can be far below target — MC-17SEP26 fill at $2,688.25
                # vs $2,695.50 trigger cost -$4.49 on a supposed-profit-lock.
                # Cite: Almgren-Chriss (2000) J.Risk 3:5-39; Cartea-Jaimungal-
                # Penalva (2015) ch.4 — limit orders at target-or-better are
                # preferred over market for profit-lock exits, especially on
                # illiquid (Amihud 2002). If the limit doesn't fill, mark
                # keeps falling, and the sleeve's hard_bottom stop-loss is
                # the accepted-loss backstop (§3.7 allows only stop_loss to
                # close red).
                _floor_bp2 = own_avg + _fee_price + tick
                _breach_limit_px = float(target_px)
                # Adam 2026-07-20: only floor at break-even for TRAIL breaches.
                # Hard_bottom is the accepted-loss floor (§3.7); flooring it up
                # would push the limit above mark and it wouldn't fill, leaving
                # the position unprotected. Honor the user's stop_loss_px as-is.
                if _breach_kind == "trail" and own_avg > 0 and _breach_limit_px < _floor_bp2:
                    _breach_limit_px = _floor_bp2
                try:
                    _breach_limit_px = self._snap_to_tick(_breach_limit_px)
                except Exception:
                    pass
                try:
                    oid = self.b.place_limit("SELL", sleeve_qty, float(_breach_limit_px))
                    # Track as live_order_id so the standard fill poller
                    # (lines ~440-475) catches FILLED status and calls
                    # _sleeve_on_fill → credits cycle + clears own_avg +
                    # advances state.
                    ss.live_order_id = oid
                    # Adam 2026-07-20 DISPLAY FIX: dashboard reads
                    # resting_stop_px to render the STOP LOSS chip. Without
                    # this, chip stays "NOT PLACED" even after a real limit
                    # sell is protecting the position — false-positive alarm.
                    # Set both so the chip shows the target price + stage.
                    ss.resting_stop_px = float(_breach_limit_px)
                    ss.resting_stop_stage = f"limit_breach_{_breach_kind}"
                    self._record("trail_breach_limit_sell",
                                 sleeve_id=sc.id, sleeve_name=sc.name,
                                 target_px=float(target_px),
                                 last_price=float(last_price),
                                 limit_px=float(_breach_limit_px),
                                 break_even_floor=float(_floor_bp2),
                                 stage=stage, hwm=float(hwm),
                                 trail_distance=float(trail_distance),
                                 qty=int(sleeve_qty), oid=oid,
                                 reason=("mark dropped below trail target; "
                                         "LIMIT sell at target (Almgren-Chriss / "
                                         "Cartea-Jaimungal) — fill at target or "
                                         "better; miss falls through to stop_loss "
                                         "as accepted-loss backstop per §3.7"),
                                 severity="warn")
                except Exception as _e:
                    self._record("trail_breach_limit_sell_failed",
                                 sleeve_id=sc.id, sleeve_name=sc.name,
                                 target_px=float(target_px),
                                 last_price=float(last_price),
                                 stage=stage, error=str(_e),
                                 severity="critical")
                try:
                    self._save_state()
                except Exception:
                    pass
                return
            # Non-trail stage (hard_bottom) above mark — record + skip.
            self._record("resting_stop_skipped_above_mark",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         target_px=target_px, last_price=last_price, stage=stage)
            return
        # Fresh place — no existing resting order.
        if not ss.resting_stop_oid:
            # Adam 2026-07-20 ROOT FIX (MC/HYF/XLM "NOT PLACED" chip class):
            # Before deciding whether to place, reconcile against Coinbase
            # truth. Three outcomes:
            #   (a) Coinbase has a SELL at ~target_px covering this sleeve
            #       → ADOPT it (set our oid/px/stage) → chip flips PLACED
            #       within one tick. Fixes the state-desync that caused
            #       MC 17SEP26 Scanner 23:48 to show NOT PLACED all night
            #       while a valid stop was already on the book.
            #   (b) Coinbase has SELLs at WRONG prices (stale from prior
            #       cycle, wrong-price orphans) → cancel them so the
            #       broker-authoritative guard below won't misread them
            #       as legitimate coverage. Prior behavior: guard saw them
            #       and refused to place, leaving §3.6 hole.
            #   (c) Nothing on Coinbase → fall through to place path.
            if pos_qty > 0 and target_px and target_px > 0:
                try:
                    _existing_sells = self._broker_query_open_sells()
                    _tol = max(float(getattr(self.cfg, "tick_size", 0.0) or 0.01) * 2,
                               float(target_px) * 0.005)
                    _adopted = False
                    _wrong_price = []
                    for _o in _existing_sells:
                        _osp = float(_o.get("stop_price") or 0)
                        _osz = int(_o.get("size") or 0)
                        if _osz != sleeve_qty:
                            continue  # not our size — sibling coverage
                        if _osp <= 0:
                            continue  # non-stop order (limit sell?) — leave
                        if abs(_osp - float(target_px)) <= _tol:
                            # Match: adopt it into sleeve state.
                            ss.resting_stop_oid = _o.get("order_id")
                            ss.resting_stop_px = _osp
                            ss.resting_stop_stage = stage
                            self._record(
                                "resting_stop_adopted_from_broker",
                                sleeve_id=sc.id, sleeve_name=sc.name,
                                oid=_o.get("order_id"),
                                stop_px=_osp, target_px=float(target_px),
                                stage=stage, size=_osz,
                                severity="info",
                                reason=("existing Coinbase SELL matches our "
                                        "target within tolerance; adopting "
                                        "into sleeve state so chip flips "
                                        "PLACED. Fixes §3.6 state-desync "
                                        "class without creating §3.8 excess."))
                            try:
                                self._save_state()
                            except Exception:
                                pass
                            self._open_sells_cache = None
                            _adopted = True
                            break
                        else:
                            _wrong_price.append(_o)
                    if _adopted:
                        return
                    # No adoption. Cancel wrong-price SELLs at our qty so
                    # they don't block placement via the guard below. Only
                    # cancel same-qty SELLs (sibling sleeve stops have
                    # different qtys per their own configs — leave those).
                    for _o in _wrong_price:
                        _oid = _o.get("order_id")
                        _osp = float(_o.get("stop_price") or 0)
                        if not _oid:
                            continue
                        try:
                            self.b.cancel(_oid)
                            self._record(
                                "resting_stop_wrong_price_cancelled",
                                sleeve_id=sc.id, sleeve_name=sc.name,
                                cancelled_oid=_oid,
                                cancelled_stop_px=_osp,
                                intended_target_px=float(target_px),
                                severity="critical",
                                reason=("stale SELL on Coinbase at wrong "
                                        "price blocked new stop placement "
                                        "via broker-authoritative guard. "
                                        "Cancelling so §3.6 protective "
                                        "stop can be placed this tick."))
                        except Exception as _ce:
                            self._record(
                                "resting_stop_wrong_price_cancel_failed",
                                sleeve_id=sc.id, sleeve_name=sc.name,
                                oid=_oid, error=str(_ce),
                                severity="critical")
                    if _wrong_price:
                        self._open_sells_cache = None
                except Exception:
                    pass  # fail-open — proceed to guards below
            # Adam 2026-07-20 GHOST-SHORT GUARD: multi-sleeve mutual
            # exclusion. Sum up qty of stop-limits ALREADY placed by
            # sibling sleeves on this same product. If placing this
            # sleeve's stop would push the total resting-SELL qty above
            # actual position, THIS sleeve is a ghost (its own_avg is
            # set but it doesn't actually own a slice). Placing the stop
            # would create a §3.8 short-risk: both stops trigger → 2
            # sells against 1 real contract → SHORT.
            #
            # broker.place_stop_limit uses include_pending=False in
            # _no_short_check (rationale: avoids ratchet cancel-then-
            # place false positives). That's correct for the ratchet
            # case but leaves the multi-sleeve ghost gap open. Close it
            # here at the swing_leg layer, where we have per-sleeve
            # context.
            _sibling_stop_qty = 0
            for _other_sid, _other_ss in (self.s.sleeves or {}).items():
                if _other_sid == sc.id:
                    continue
                if getattr(_other_ss, "resting_stop_oid", None):
                    _other_sc = self._sleeve_cfg_by_id(_other_sid) if hasattr(
                        self, "_sleeve_cfg_by_id") else None
                    _oq = int(getattr(_other_sc, "qty", 1) if _other_sc
                              else int(getattr(_other_ss, "qty", 1) or 1))
                    _sibling_stop_qty += _oq
            if _sibling_stop_qty + sleeve_qty > pos_qty:
                self._record(
                    "resting_stop_placement_refused_multi_sleeve",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    sibling_stop_qty=_sibling_stop_qty,
                    this_sleeve_qty=sleeve_qty,
                    actual_position=pos_qty,
                    severity="critical",
                    reason=("placing this stop would push total resting SELL "
                            "qty above actual position — this sleeve is a "
                            "ghost (own_avg set but no real slice). Refusing "
                            "to prevent §3.8 short-risk. Ghost cleanup needed: "
                            "python3 diag_slr_ghost_detector.py --apply "
                            "(or per-product equivalent)."))
                return
            # Adam 2026-07-20 BROKER-AUTHORITATIVE PRE-PLACE GUARD: query
            # Coinbase directly for existing open SELLs on this product. If
            # sum(existing) + this_qty > pos_qty, sibling stops already cover
            # the position — placing another would create the excess that
            # leads to double-fire → SHORT. Sleeve-state check above uses
            # resting_stop_oid tracking which can drift; this uses Coinbase
            # truth. Cached 500ms so multi-sleeve-per-tick doesn't hammer API.
            try:
                _existing = self._broker_query_open_sells()
                # Adam 2026-07-21 PHASE A: only count STOP-LIMITs as
                # protective coverage. Profit-lock LIMITs above mark are
                # NOT protective — they're the other leg of the bracket.
                # Counting them would incorrectly block stop placement.
                _existing_qty = sum(
                    int(o.get("size") or 0) for o in _existing
                    if o.get("kind") == "stop_limit")
                if _existing_qty + sleeve_qty > pos_qty:
                    self._record(
                        "resting_stop_placement_refused_broker_authoritative",
                        sleeve_id=sc.id, sleeve_name=sc.name,
                        existing_broker_sell_qty=_existing_qty,
                        this_sleeve_qty=sleeve_qty,
                        pos_qty=pos_qty,
                        severity="critical",
                        reason=("Coinbase already has SELLs covering this "
                                "position — placing this stop would create "
                                "excess → §3.8 double-fire SHORT risk. "
                                "Sibling coverage sufficient; skipping."),
                    )
                    return
            except Exception:
                pass  # fail-open — proceed to place (sleeve-state check ran)
            try:
                oid = self.b.place_stop_limit("SELL", sleeve_qty,
                                              float(target_px), float(limit_px))
                ss.resting_stop_oid = oid
                ss.resting_stop_px = float(target_px)
                ss.resting_stop_stage = stage
                # Invalidate cache — a fresh order just landed on Coinbase
                self._open_sells_cache = None
                self._record("resting_stop_placed",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             stage=stage, target_px=float(target_px),
                             limit_px=float(limit_px), qty=sleeve_qty, oid=oid)
                # ROOT FIX 2026-07-20: persist so reload-on-tick doesn't wipe.
                try:
                    self._save_state()
                except Exception:
                    pass
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
        # Adam 2026-07-20 RATCHET-NOISE FIX (§3.15 + Amihud-Mendelson 1986):
        # Prior threshold was tick*0.5 — any half-tick move up triggered a
        # cancel+replace, flooding Coinbase with API calls on illiquid
        # products (MAG7C, ENA PERP, HYPE PERP all showed 2-3 back-to-back
        # cancels within 30s intervals in Adam's order log). Amihud-Mendelson
        # (1986) J.Fin.Econ 17(2):223 proves trading frequency should be
        # INVERSELY related to effective spread: illiquid = fewer ratchets.
        #
        # New threshold: expert_liquidity.ratchet_min_improvement_dollars,
        # which is fee_per_roundtrip × tier_multiplier (liquid=1×, medium=2×,
        # illiquid=4×, very_illiquid=8×). If the improvement doesn't clear
        # this dollar amount, don't ratchet — the API-call + fill-risk cost
        # of the replace exceeds the tiny stop-improvement gain.
        #
        # Falls back to tick*0.5 (legacy) when expert_liquidity unavailable
        # or the sleeve hasn't been through the scanner-gate yet.
        # Preferred: expert_liquidity's ratchet_min_improvement_dollars
        # (set by scanner if the sleeve was scanner-armed).
        # Fallback: compute per-tick from expert_liquidity assessment on
        # the sleeve's own price history (closes-only version — no volume
        # data, so uses simplified Roll + realized vol).
        # Last resort: 2× fee_price (means: don't ratchet unless the
        # improvement covers 2× the round-trip fee-per-unit, so the
        # cancel+place API round-trip isn't wasted).
        _min_improvement_price = tick * 0.5  # legacy last-resort
        try:
            _liq_dict = getattr(ss, "liquidity_decision", None)
            _min_dollars = None
            if isinstance(_liq_dict, dict):
                _min_dollars = float(_liq_dict.get(
                    "ratchet_min_improvement_dollars") or 0)
            # If sleeve doesn't have scanner-supplied liquidity_decision,
            # apply the same principle via fee_price × 2 (Amihud-Mendelson
            # 1986 minimum viable trade-cost gate: don't churn the API for
            # improvements smaller than 2× the per-unit fee).
            if _min_dollars and _min_dollars > 0 and self.cfg.contract_size > 0:
                _qty_denom = max(1, int(sc.qty or 1))
                _min_improvement_price = _min_dollars / self.cfg.contract_size / _qty_denom
            elif _fee_price > 0:
                _min_improvement_price = max(tick, 2.0 * _fee_price)
            # Floor at 1 tick so we always have SOME threshold
            if tick > 0 and _min_improvement_price < tick:
                _min_improvement_price = tick
        except Exception:
            pass  # fall back to tick*0.5 legacy
        if target_px > current_px + _min_improvement_price:
            old_oid = ss.resting_stop_oid
            # Adam 2026-07-20 ORPHAN GUARD: fail-closed on cancel failure.
            # Prior code continued to place a new stop even if the cancel
            # raised — if the old order was actually still open on Coinbase,
            # we'd have TWO stops live (both trigger → double-fire → SHORT
            # per §3.8), and the tracking pointed only at the new one =
            # OLD ORDER BECOMES ORPHAN. SLR a6229a92 → orphan 2026-07-20
            # is this class.
            #
            # Now: on cancel failure, RETURN. Old_oid stays tracked. Next
            # tick retries the cancel. Small ratchet-latency cost; large
            # short-risk avoided.
            try:
                self.b.cancel(old_oid)
            except Exception as e:
                self._record("resting_stop_ratchet_cancel_failed",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             old_oid=old_oid, error=str(e),
                             severity="critical",
                             reason=("skipping ratchet this tick — old stop "
                                     "status unknown after cancel raised; "
                                     "placing a new stop now would leave the "
                                     "old one as an orphan SELL (short risk "
                                     "per §3.8). Retry next tick."))
                try:
                    self._save_state()
                except Exception:
                    pass
                return
            # Cancel succeeded — safe to place the ratcheted stop.
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
                # ROOT FIX 2026-07-20: persist the ratchet or reload-on-tick
                # wipes it (SLR sat at $54.680 for 90+ min because of this).
                try:
                    self._save_state()
                except Exception:
                    pass
            except Exception as e:
                # Cancel succeeded (Coinbase confirmed removal) + place
                # failed. Position is now unprotected on the exchange. Clear
                # tracking (nothing to reference) so next tick tries a fresh
                # place from _maintain_resting_stop's "no existing order" path.
                # Severity=critical because we've briefly lost stop coverage.
                self._record("resting_stop_ratchet_place_failed",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             from_px=current_px, to_px=float(target_px),
                             error=str(e), severity="critical",
                             reason=("cancel succeeded, place failed — "
                                     "position UNPROTECTED until next tick's "
                                     "fresh place attempt"))
                ss.resting_stop_oid = None
                ss.resting_stop_px = None
                ss.resting_stop_stage = None
                try:
                    self._save_state()
                except Exception:
                    pass
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
        # Adam 2026-07-20 ORPHAN GUARD (adopt path): if the stale BUY
        # cancel fails, DO NOT proceed to adoption. Prior code continued
        # after `except log` and cleared live_order_id (line 5359), so
        # the stale BUY became an orphan on Coinbase. It could still fill
        # → position exceeds sleeve claim + core → over-accumulation
        # (§3.10 impact). Not a short risk (BUY-side), but still a real
        # accounting drift. Now: fail-closed. Retry adoption next tick.
        old_oid = ss.live_order_id
        if old_oid:
            try:
                self.b.cancel(old_oid)
            except Exception as e:
                self._record("sleeve_orphan_reconcile_cancel_failed",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             oid=old_oid, error=str(e),
                             severity="critical",
                             reason=("stale BUY cancel raised — skipping "
                                     "adoption this tick to avoid orphaning "
                                     "the buy (over-accumulation risk). "
                                     "Retry next tick."))
                return  # DO NOT adopt if we couldn't cancel the stale buy
        # Adopt: set own_avg_entry + flip state
        ss.own_avg_entry = float(avg)
        ss.state = SleeveStateEnum.ARMED_SELL
        # Ghost-recurrence root fix (Adam 2026-07-20): snapshot into
        # sell_entry_avg so _credit_stop_fill has a fallback if own_avg
        # gets cleared before the resting stop credits. Prevents the
        # "own_avg unknown" halt class.
        ss.sell_entry_avg = float(avg)
        ss.live_order_id = None  # cancel succeeded above — safe to clear
        self._save_state()
        self._record(
            "sleeve_orphan_position_adopted",
            sleeve_id=sc.id, sleeve_name=sc.name,
            position_qty=pos_qty, claimed_by_others=claimed_qty,
            unclaimed_qty=unclaimed_qty, core_qty=core_qty,
            adopted_avg=float(avg),
        )

    def _maybe_heal_ghost_sleeve(self, sc: SleeveConfig, ss: SleeveState) -> None:
        """Adam 2026-07-20: inverse of _maybe_reconcile_orphan_position.

        Ghost sleeve = state is ARMED_SELL with own_avg_entry set, but the
        broker has fewer contracts than sum(ARMED_SELL sleeves claim). XLP
        2026-07-20 07:07: two sleeves both WAITING_FOR_SELL while Coinbase
        showed Position: 0. The sleeve thinks it owns qty but the exchange
        disagrees — either a double-fire fired more contracts than existed
        or a stop credited the wrong sleeve.

        Deterministic tiebreaker: sort ARMED_SELL sleeves by sleeve_id.
        The first N sleeves (up to pos_qty) keep their claim; anyone beyond
        pos_qty coverage is a ghost → flip to ARMED_BUY, clear own_avg_entry
        (and sell_entry_avg + resting_stop tracking + live_order_id) so the
        sleeve is ready to buy fresh.

        Runs BEFORE _maintain_resting_stop so ghost sleeves don't waste API
        calls placing stops for contracts they don't own.

        Multi-sleeve safe: every sleeve independently evaluates; the sort
        order ensures both sleeves agree on which is the ghost."""
        if ss.state != SleeveStateEnum.ARMED_SELL:
            return
        if ss.own_avg_entry is None:
            return  # nothing to heal
        try:
            pos_qty = int(self.b.position_qty() or 0)
        except Exception:
            return
        # Collect all ARMED_SELL sleeves with own_avg_entry set, sorted by id.
        armed_sell = []
        for _sid, _ss in (self.s.sleeves or {}).items():
            if _ss.state != SleeveStateEnum.ARMED_SELL:
                continue
            if _ss.own_avg_entry is None:
                continue
            _cfg = self._sleeve_cfg_by_id(_sid)
            _q = int(getattr(_cfg, "qty", None) or getattr(_ss, "qty", None) or 1)
            armed_sell.append((_sid, _ss, _q))
        armed_sell.sort(key=lambda x: x[0])
        core_qty = int(getattr(self.cfg, "core_qty", 0) or 0)
        # Cover pos_qty from the front — sleeves beyond coverage are ghosts.
        available = max(0, pos_qty - core_qty)
        covered = 0
        for _sid, _ss_o, _q in armed_sell:
            if covered + _q <= available:
                covered += _q
                continue
            # This sleeve is fully beyond coverage → ghost.
            if _sid == sc.id and covered >= available:
                _prev_own = ss.own_avg_entry
                _prev_oid = ss.live_order_id
                _prev_stop = ss.resting_stop_oid
                # Cancel any dangling orders first — the sleeve thought it
                # owned contracts, so a resting stop may be on Coinbase.
                if _prev_stop:
                    try:
                        self.b.cancel(_prev_stop)
                    except Exception as _e:
                        self._record(
                            "ghost_sleeve_heal_stop_cancel_failed",
                            sleeve_id=sc.id, sleeve_name=sc.name,
                            oid=_prev_stop, error=str(_e),
                            severity="warn",
                            reason=("ghost heal proceeded despite cancel "
                                    "failure — excess-stop-cancel will retry "
                                    "next tick"),
                        )
                if _prev_oid:
                    try:
                        self.b.cancel(_prev_oid)
                    except Exception:
                        pass
                # Flip state
                ss.state = SleeveStateEnum.ARMED_BUY
                ss.own_avg_entry = None
                ss.sell_entry_avg = None
                ss.live_order_id = None
                ss.resting_stop_oid = None
                ss.resting_stop_px = None
                ss.resting_stop_stage = None
                self._save_state()
                self._record(
                    "ghost_sleeve_healed",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    prev_own_avg=_prev_own,
                    prev_live_order_id=_prev_oid,
                    prev_resting_stop_oid=_prev_stop,
                    pos_qty=pos_qty, core_qty=core_qty,
                    armed_sell_total_claim=sum(q for _, _, q in armed_sell),
                    covered_before_this=covered,
                    this_qty=_q,
                    severity="warn",
                    reason=("ARMED_SELL sleeve had no matching broker qty — "
                            "sum(ARMED_SELL claims) > pos_qty. Deterministic "
                            "sort assigned coverage to earlier sleeve(s); this "
                            "one flipped back to ARMED_BUY to prevent stuck "
                            "WAITING_FOR_SELL + phantom stop placement. Class: "
                            "feedback_no_ghost_sleeves — bot self-heals, "
                            "no diag intervention required."),
                )
            return  # done — one heal per tick per sleeve
        # Fell through: this sleeve was within coverage → legitimate ARMED_SELL

    def _maybe_reblend_on_manual_add(self, sc: SleeveConfig, ss: SleeveState) -> None:
        """Adam 2026-07-20: when Adam manually buys more of a held product
        on Coinbase (scale-in / average-down), the exchange blends the new
        buy into position.avg_entry automatically. The bot's per-sleeve
        own_avg_entry stayed stuck at the pre-add value — dashboard showed
        the OLD avg while Coinbase showed the new blended avg. Adam:
        "I expect that new average to be reflected on the dashboard."

        Detection: sleeve is ARMED_SELL (holds), own_avg_entry is set, no
        buy in flight (live_order_id is None), AND
            broker.position.qty > sum(claimed by ARMED_SELL sleeves) + core
        The excess is a manual add unaccounted for by any sleeve.

        Action: refresh this sleeve's own_avg_entry to
        broker.position.avg_entry (Coinbase's blended). Same principle as
        _maybe_reconcile_orphan_position (trust the exchange's blended
        avg), just for the "already ARMED_SELL, more added" case.

        Idempotent: after refresh, own_avg matches broker.avg_entry so
        next tick's check is a no-op unless another add happens.

        Multi-sleeve behavior: each ARMED_SELL sleeve on this product runs
        this check independently on its own tick. All converge to the same
        broker.avg_entry after the add — matches how Adam thinks about
        aggregate cost basis for a shared position.

        Safety:
          - Skips if live_order_id set (in-flight buy would double-count).
          - Skips if broker.position.qty == 0 (no position; nothing to
            blend into — ghost auto-recover handles that class).
          - Skips if new_avg matches current own_avg within one tick.
        """
        if ss.state != SleeveStateEnum.ARMED_SELL:
            return
        if ss.own_avg_entry is None:
            return  # nothing to blend against; orphan-adopt handles this
        if ss.live_order_id:
            return  # in-flight buy would race the blend
        try:
            broker_qty = int(self.b.position_qty() or 0)
        except Exception:
            return
        if broker_qty <= 0:
            return
        # Sum qty configured for ARMED_SELL sleeves on this product.
        armed_sell_qty = 0
        for _sid, _ss in (self.s.sleeves or {}).items():
            if _ss.state != SleeveStateEnum.ARMED_SELL:
                continue
            _cfg = None
            if hasattr(self, "_sleeve_cfg_by_id"):
                try:
                    _cfg = self._sleeve_cfg_by_id(_sid)
                except Exception:
                    _cfg = None
            armed_sell_qty += int(getattr(_cfg, "qty", 1) if _cfg else 1)
        core_qty = int(getattr(self.cfg, "core_qty", 0) or 0)
        excess = broker_qty - armed_sell_qty - core_qty
        if excess <= 0:
            return  # no unaccounted qty — no manual add detected
        # Read broker's current blended avg.
        try:
            new_avg = float((self.b.position or None).avg_entry or 0)
        except Exception:
            new_avg = 0.0
        if new_avg <= 0:
            return
        prev_avg = float(ss.own_avg_entry)
        tick = float(getattr(self.cfg, "tick_size", 0) or 0.0001)
        if abs(new_avg - prev_avg) < tick:
            return  # already matches broker within one tick — no-op
        ss.own_avg_entry = new_avg
        # Ghost-recurrence root fix: refresh sell_entry_avg to match the
        # new blended basis. Reblend = new cost basis; the persistent
        # fallback must track it or _credit_stop_fill will credit the
        # stale sell_entry_avg on a stop fill and mis-report P&L.
        ss.sell_entry_avg = new_avg
        # Fee-floor re-clamp on avg-UP (Adam 2026-07-20
        # feedback_no_net_loss_cycles): if the manual add pushed own_avg
        # UP (bought more at higher price), the existing sc.sell_px may
        # now be below break-even relative to the new blended basis.
        # A resting stop at the old target would fire green but net red.
        # Only clamp UP — never lower sc.sell_px on an avg-down.
        try:
            _fee_rt = float(getattr(self.cfg, "fee_per_contract_roundtrip", 0) or 0)
            _cs = float(getattr(self.cfg, "contract_size", 0) or 0)
            _q = max(1, int(getattr(sc, "qty", 1) or 1))
            _tick = float(getattr(self.cfg, "tick_size", 0) or 0.0)
            if _fee_rt > 0 and _cs > 0 and new_avg > prev_avg:
                _fee_per_unit = _fee_rt / _cs / _q
                _safety = max(_tick if _tick > 0 else 0.0, new_avg * 0.0005)
                _floor = new_avg + _fee_per_unit + _safety
                if float(sc.sell_px or 0) < _floor:
                    _clamped = self._snap_to_tick(_floor) if _tick > 0 else _floor
                    if _clamped < _floor:
                        _clamped = _clamped + (_tick or 0.0)
                    _prev_sell_px = float(sc.sell_px or 0)
                    sc.sell_px = _clamped
                    # Persist the sell_px lift to config so restart preserves
                    try:
                        _cfg = self.store.get_config(self.tenant_id, self.symbol) or {}
                        for _s in (_cfg.get("sleeves") or []):
                            if _s.get("id") == sc.id:
                                _s["sell_px"] = _clamped
                                break
                        self.store.put_config(self.tenant_id, self.symbol, _cfg)
                    except Exception:
                        pass
                    self._record(
                        "sleeve_reblend_sell_px_fee_floor_clamp",
                        sleeve_id=sc.id, sleeve_name=sc.name,
                        prev_sell_px=_prev_sell_px, new_sell_px=_clamped,
                        prev_own_avg=prev_avg, new_own_avg=new_avg,
                        severity="info",
                        reason=("manual avg-up made existing sell_px "
                                "below break-even + fees — clamped UP "
                                "per feedback_no_net_loss_cycles"),
                    )
        except Exception as _e:
            try:
                self._record("sleeve_reblend_fee_floor_error",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             error=str(_e), severity="warn")
            except Exception:
                pass
        self._record(
            "sleeve_own_avg_reblended_on_manual_add",
            sleeve_id=sc.id, sleeve_name=sc.name,
            prev_own_avg=prev_avg, new_own_avg=new_avg,
            broker_qty=broker_qty, armed_sell_claims=armed_sell_qty,
            core_qty=core_qty, excess=excess,
            severity="info",
            reason=("Coinbase position.qty > sleeve claims + core — manual "
                    "add detected; own_avg refreshed to broker.avg_entry "
                    "so future exit P&L uses the new blended basis"),
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

    def _reset_cycle_state_post_sell(self, ss: SleeveState) -> None:
        """Adam 2026-07-15: reset every cycle-scoped state field so cycle
        N+1 doesn't inherit cycle N's HWM / trail flags / buy-trail state.

        Mirrors what _sleeve_on_fill's SELL branch does (lines 4930+). The
        resting-stop credit paths (tick + reconcile + dedup-skip) previously
        cleared ONLY resting_stop_* + own_avg_entry, missing:
          * trail_armed / trail_high_water_price → chip on cockpit kept
            showing the prior cycle's ratcheted trail level, but the actual
            resting stop on Coinbase was placed at the fresh cycle's
            hard_bottom (way lower). Display/reality divergence + real
            under-protection on the next cycle.
          * stop_loss_hwm → the ratchet lift stayed applied, so effective
            stop-loss appeared to be at the prior HWM, but new-cycle mark
            couldn't validate that placement (sanity check refused, kept
            the wide fallback stop from arm time).
          * hybrid_sell_triggered_ts → old timeout window carried into the
            next cycle
          * buy_trail_armed / buy_trail_low_water → any pending buy-trail
            from a prior arm-buy leg carried into next arm-sell leg

        Centralised so all four call sites (tick success, reconcile success,
        dedup-skip, and any future path) reset identically. If _sleeve_on_fill's
        SELL branch adds/removes fields, this helper needs matching updates.
        """
        ss.trail_armed = False
        ss.trail_high_water_price = 0.0
        ss.hybrid_sell_triggered_ts = None
        ss.buy_trail_armed = False
        ss.buy_trail_low_water = 0.0
        ss.stop_loss_hwm = None

    def _credit_stop_fill(self, sc: SleeveConfig, ss: SleeveState,
                          fill_price: float, filled_qty: int,
                          source: str, oid: Optional[str] = None) -> bool:
        """Adam 2026-07-15 CRITICAL: shared helper for both the tick-side
        credit (_maybe_credit_resting_stop_fill) and the reconcile sweeper.

        Never credits $0 silently. If own_avg_entry is unresolvable at
        credit time, tries sell_entry_avg as a fallback (captured when the
        sell was armed). If still unknown, emits severity=critical event
        and halts the sleeve for review — DOES NOT credit, DOES NOT
        increment cycles, DOES NOT clear own_avg_entry. Prevents the HYPE
        2026-07-15 class of bug where profit=0 was silently added,
        cycles++ fired anyway, and the $15 profit vanished into a phantom
        cycle. Manual fix: diag_force_credit_cycle.py.

        Dedup: consults ss.credited_oids and skips if the passed oid is
        already there. Prevents the tick-vs-reconcile double-credit race
        (both paths poll order_status independently; same FILLED oid
        could get credited twice, inflating realized_pnl in the OPPOSITE
        direction from the HYPE silent-zero bug). Appends oid on success
        (bounded to last 50).

        Returns True on success (caller then clears position tracking +
        advances state), False on halt-for-review OR duplicate-skip.

        source: 'tick' or 'reconcile' — recorded on the event so telemetry
        can distinguish which path credited the fill.
        """
        # Dedup guard: skip if we've already credited this order_id from
        # either path this session. Also serves as an idempotency check for
        # replays after crash-recovery. When we DO skip, we clear the
        # resting_stop_* tracking ourselves + advance state (the fill IS
        # already credited; the caller shouldn't do it again) so the tick
        # loop doesn't keep polling the same OID forever.
        if oid is not None:
            credited = list(getattr(ss, "credited_oids", None) or [])
            if oid in credited:
                import time as _time_dedup
                self._record("resting_stop_credit_dedup_skipped",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             oid=oid, source=source,
                             reason="oid already in credited_oids — "
                                    "prevents double-credit race",
                             severity="info")
                # Clear tracking + advance state — the fill has already been
                # credited by the other path or a prior boot. Leaving it as
                # ARMED_SELL with a stale resting_stop_oid would burn API
                # calls polling a permanently-FILLED order.
                ss.own_avg_entry = None
                ss.resting_stop_oid = None
                ss.resting_stop_px = None
                ss.resting_stop_stage = None
                ss.state = SleeveStateEnum.ARMED_BUY
                ss.armed_buy_since_ts = _time_dedup.time()
                # Reset cycle-scoped state too (mirror the successful-credit path)
                self._reset_cycle_state_post_sell(ss)
                return False
        if fill_price is None or fill_price <= 0:
            self._record("resting_stop_credit_fill_price_missing",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         source=source, filled_qty=filled_qty,
                         severity="critical",
                         reason="average_filled_price missing/invalid")
            self._sleeve_halt(sc, ss,
                              f"resting stop-limit reported FILLED but fill_price "
                              f"invalid ({fill_price}); manual reconcile required")
            return False
        own_avg = float(ss.own_avg_entry or 0)
        avg_source = "own_avg_entry"
        if own_avg <= 0:
            # Fallback: sell_entry_avg captured when the sell was armed
            sell_avg = float(getattr(ss, "sell_entry_avg", 0) or 0)
            if sell_avg > 0:
                own_avg = sell_avg
                avg_source = "sell_entry_avg"
                self._record("resting_stop_credit_avg_fallback_used",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             fallback_source=avg_source, used_avg=own_avg,
                             source=source, severity="warn")
        if own_avg <= 0:
            # Adam 2026-07-20 AUTO-HEAL (feedback_no_ghost_sleeves): before
            # halting, query Coinbase for the most recent BUY fill on this
            # product. That's the actual entry price of the position we just
            # exited — real exchange truth, not sleeve-state approximation.
            # Kills the "manual diag_force_credit required" class Adam has hit
            # repeatedly. HYP-20DEC30 2026-07-20 16:39 was one such case.
            try:
                _fills_resp = self.b.client.list_orders(
                    product_id=self.symbol, order_status="FILLED", limit=50
                )
                _raw_fills = (_fills_resp.to_dict()
                              if hasattr(_fills_resp, "to_dict")
                              else (_fills_resp
                                    if isinstance(_fills_resp, dict) else {}))
                _fill_list = (_raw_fills.get("orders") or [])
                # Sort by last_fill_time DESC to find most recent BUY.
                def _fill_ts_key(_o):
                    return str(_o.get("last_fill_time")
                               or _o.get("created_time") or "")
                _fill_list.sort(key=_fill_ts_key, reverse=True)
                _recent_buy_px = 0.0
                _recent_buy_oid = None
                _recent_buy_ts = None
                for _o in _fill_list:
                    if str(_o.get("side") or "").upper() != "BUY":
                        continue
                    try:
                        _px = float(_o.get("average_filled_price") or 0)
                    except (TypeError, ValueError):
                        _px = 0.0
                    if _px > 0:
                        _recent_buy_px = _px
                        _recent_buy_oid = _o.get("order_id")
                        _recent_buy_ts = _fill_ts_key(_o)
                        break
                if _recent_buy_px > 0:
                    own_avg = _recent_buy_px
                    avg_source = "coinbase_recent_buy_fallback"
                    self._record(
                        "resting_stop_credit_broker_history_fallback",
                        sleeve_id=sc.id, sleeve_name=sc.name,
                        source=source,
                        fallback_source=avg_source,
                        used_avg=own_avg,
                        broker_buy_oid=_recent_buy_oid,
                        broker_buy_ts=_recent_buy_ts,
                        severity="warn",
                        reason=("own_avg_entry AND sell_entry_avg both missing "
                                "— queried Coinbase for most recent BUY fill "
                                "and used its actual fill price as basis. "
                                "Prevents halt + manual diag intervention per "
                                "feedback_no_ghost_sleeves rule."),
                    )
            except Exception as _e:
                self._record("resting_stop_credit_broker_fallback_failed",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             error=str(_e), severity="warn",
                             reason=("broker BUY history query failed — "
                                     "falling through to halt"))
        if own_avg <= 0:
            # No usable entry — halt for review. This is the HYPE 2026-07-15
            # class: cycles++ would have fired with profit=0 and the missing
            # $15 would vanish forever. Halt loudly instead.
            self._record("resting_stop_credit_own_avg_missing",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         source=source, fill_price=fill_price,
                         filled_qty=filled_qty, severity="critical",
                         reason=("own_avg_entry AND sell_entry_avg both missing "
                                 "AND Coinbase broker fallback failed — "
                                 "cannot compute realized P&L. Halted; use "
                                 "diag_force_credit_cycle.py to backfill."))
            self._sleeve_halt(sc, ss,
                              f"resting stop-limit fill @ ${fill_price} but own_avg "
                              f"unknown; run diag_force_credit_cycle.py {self.symbol} "
                              f"{sc.id} <fill> <buy_avg> to reconcile")
            return False
        contract_size = self._get_contract_size()
        gross = (fill_price - own_avg) * filled_qty * contract_size
        # Adam 2026-07-20 problem-scout finding #1: this path credited
        # gross profit while the sibling _sleeve_on_fill (line ~6637)
        # correctly credits `gross - half_fee`. Result: every ratchet-
        # stop / resting-stop credit silently overstated realized_pnl
        # by half_fee × qty. Also poisoned recent_cycle_pnls fed into
        # loss-streak auto-disable + Vince/Kelly sizing, making experts
        # see phony wins. Fix: subtract half_fee here too — the sell
        # side of the round-trip pays its share of the fee. Full round-
        # trip is halved because the buy-back leg will pay the other
        # half via _sleeve_on_fill's rebuy path (line ~6554).
        half_fee = 0.0
        try:
            _rt = float(getattr(self.cfg, "fee_per_contract_roundtrip", 0) or 0)
            if _rt > 0:
                half_fee = (_rt / 2.0) * filled_qty
        except Exception:
            half_fee = 0.0
        profit = gross - half_fee
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
        # Reset consecutive-stops on a successful credit (whether profit or loss,
        # the fill closed the cycle cleanly).
        ss.consecutive_stops = 0
        # Dedup: record this oid so a subsequent race path (tick or reconcile)
        # doesn't credit the same fill twice. Bounded to last 50 to prevent
        # unbounded state growth.
        if oid is not None:
            credited = list(getattr(ss, "credited_oids", None) or [])
            credited.append(oid)
            if len(credited) > 50:
                credited = credited[-50:]
            ss.credited_oids = credited
        self._record(
            "resting_stop_filled_credited",
            sleeve_id=sc.id, sleeve_name=sc.name,
            source=source, fill_price=fill_price,
            own_avg_entry=own_avg, avg_source=avg_source,
            filled_qty=filled_qty, profit=profit, oid=oid,
            new_realized=ss.realized_pnl, new_cycles=ss.cycles,
        )
        return True

    def _maybe_credit_resting_stop_fill(self, sc: SleeveConfig, ss: SleeveState) -> None:
        """Adam 2026-07-15: post-fill state reconciler for the ratchet-stop.

        Fixes Type 3 ghost: sleeve state=ARMED_SELL, own_avg_entry set,
        Coinbase position=0 → the resting stop-limit fired on Coinbase
        but we never credited the exit back to sleeve state. Result:
        cycles never increment, realized_pnl never updates, dashboard
        shows 'phantom profit' from own_avg_entry that we no longer own.

        Runs each tick. When resting_stop_oid exists, polls its status.
        If FILLED: credits the fill via _credit_stop_fill (which halts
        the sleeve if own_avg unresolvable — no silent $0 credit).
        On success: cycles++, realized_pnl += profit, own_avg_entry
        cleared, resting_stop_* cleared, state → ARMED_BUY.
        If CANCELLED externally: clears the oid so _maintain_resting_stop
        places a fresh one next tick.
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
            fill_price = status_info.get("average_filled_price")
            try:
                fill_price = float(fill_price) if fill_price is not None else 0.0
            except Exception:
                fill_price = 0.0
            filled_qty = int(status_info.get("filled_qty") or sc.qty or 1)
            old_oid = ss.resting_stop_oid
            old_stage = ss.resting_stop_stage
            credited = self._credit_stop_fill(sc, ss, fill_price, filled_qty,
                                              source="tick", oid=old_oid)
            if not credited:
                # Sleeve is now HALTED with the missing-avg event. Don't clear
                # own_avg_entry or resting_stop_oid — leaves full context for
                # the operator running diag_force_credit_cycle.py.
                self._save_state()
                return
            # Success: clear position tracking + advance state
            ss.own_avg_entry = None
            ss.resting_stop_oid = None
            ss.resting_stop_px = None
            ss.resting_stop_stage = None
            ss.state = SleeveStateEnum.ARMED_BUY
            ss.armed_buy_since_ts = _time.time()
            # Adam 2026-07-15 CRITICAL: reset cycle-scoped state so cycle N+1
            # doesn't inherit cycle N's HWM/trail flags. Mirror what
            # _sleeve_on_fill SELL branch does. Skipping this caused HYP
            # cycle-3 stop-chip to show cycle-2's ratcheted $68.566 while
            # the actual Coinbase resting stop was $65.18 (way lower) —
            # display divergence + real under-protection.
            self._reset_cycle_state_post_sell(ss)
            self._save_state()
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

    def _maintain_and_credit_profit_lock_limit(
            self, sc: SleeveConfig, ss: SleeveState,
            last_price: float) -> None:
        """Phase A BRACKET DESIGN (Adam 2026-07-21).

        Maintains a resting LIMIT SELL at sell_px whenever a position is
        held. Complements _maintain_resting_stop which handles the
        protective STOP-LIMIT SELL at stop_loss_px below mark. Together
        they form a bracket order:

          - LIMIT above mark   -> profit target (fires on price rise to sell_px)
          - STOP-LIMIT below   -> protective floor (fires on drop through stop_loss_px)

        Only one can fire from a continuous price path (Merton 1973 barrier
        options: up + down barriers on same asset partition state
        non-overlappingly). On fill, this method cancels the co-resting
        stop-limit and credits the cycle at fill_price.

        This fixes the rule-#1 violation where a trail-ratcheted "stop"
        was placed ABOVE mark (mislabelled as STOP LOSS on the chip),
        leaving the position unprotected against pullbacks.

        KILL SWITCH: set Redis key `silver-swing:phase_a_disabled` to any
        truthy value to disable placement while investigating loops.
        Existing orders on the book are unaffected; stop-limit path in
        _maintain_resting_stop continues normally.
        """
        # Adam 2026-07-21 KILL SWITCH: freeze Phase A placement when a
        # cancel-replace loop is detected. Reads once per tick.
        try:
            _disabled = None
            _redis = getattr(self.store, "_r", None) or getattr(
                self.store, "r", None)
            if _redis is not None:
                _disabled = _redis.get("silver-swing:phase_a_disabled")
            if _disabled and str(_disabled).lower() not in ("", "0", "false", "none"):
                return
        except Exception:
            pass
        import time as _time
        if not (ss.own_avg_entry and float(ss.own_avg_entry) > 0):
            return  # not holding
        try:
            pos_qty = int(self.b.position_qty() or 0)
        except Exception:
            return
        if pos_qty <= 0:
            return
        try:
            sell_px = float(getattr(sc, "sell_px", 0) or 0)
        except (TypeError, ValueError):
            sell_px = 0.0
        if sell_px <= 0:
            return  # no target configured
        sleeve_qty = int(getattr(sc, "qty", 1) or 1)

        # Poll existing tracked oid first (fill / cancel detection).
        # _fall_through=True means we should proceed to adoption/placement;
        # False means we've already handled this tick (return).
        _fall_through = False
        if ss.resting_profit_limit_oid:
            try:
                st = self.b.order_status(ss.resting_profit_limit_oid)
            except Exception as _e:
                self._record("profit_lock_limit_status_check_failed",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             oid=ss.resting_profit_limit_oid, error=str(_e))
                return
            status = (st or {}).get("status")
            if status == "OPEN":
                # Reprice check: sell_px may have changed since we placed.
                _tol_open = max(
                    float(getattr(self.cfg, "tick_size", 0.01) or 0.01) * 2,
                    float(sell_px) * 0.005,
                )
                try:
                    _cur_px = float(ss.resting_profit_limit_px or 0)
                except (TypeError, ValueError):
                    _cur_px = 0.0
                if _cur_px > 0 and abs(_cur_px - sell_px) > _tol_open:
                    try:
                        self.b.cancel(ss.resting_profit_limit_oid)
                        self._record(
                            "profit_lock_limit_cancelled_for_reprice",
                            sleeve_id=sc.id, sleeve_name=sc.name,
                            old_oid=ss.resting_profit_limit_oid,
                            old_px=_cur_px, new_sell_px=float(sell_px),
                            reason=("sell_px changed since limit was "
                                    "placed; cancelling to reprice."))
                        ss.resting_profit_limit_oid = None
                        ss.resting_profit_limit_px = None
                        self._save_state()
                        self._open_sells_cache = None
                        _fall_through = True
                    except Exception as _ce:
                        self._record(
                            "profit_lock_limit_reprice_cancel_failed",
                            sleeve_id=sc.id, sleeve_name=sc.name,
                            oid=ss.resting_profit_limit_oid,
                            error=str(_ce), severity="warn")
                        return  # leave tracked oid; retry next tick
                else:
                    return  # price still matches — leave as-is
            elif status == "FILLED":
                fill_price = st.get("average_filled_price")
                try:
                    fill_price = float(fill_price) if fill_price is not None else sell_px
                except Exception:
                    fill_price = sell_px
                filled_qty = int(st.get("filled_qty") or sleeve_qty)
                old_oid = ss.resting_profit_limit_oid
                credited = self._credit_stop_fill(
                    sc, ss, fill_price, filled_qty,
                    source="profit_lock_limit", oid=old_oid)
                if not credited:
                    self._save_state()
                    return
                # Bracket: cancel co-resting stop-limit (only ONE can fire)
                if ss.resting_stop_oid:
                    try:
                        self.b.cancel(ss.resting_stop_oid)
                        self._record("bracket_stop_cancelled_on_profit_fill",
                                     sleeve_id=sc.id, sleeve_name=sc.name,
                                     cancelled_oid=ss.resting_stop_oid,
                                     profit_fill_oid=old_oid,
                                     reason=("profit-lock LIMIT filled at "
                                             f"${fill_price}; cancelling co-"
                                             "resting stop-limit per bracket "
                                             "OCO design."))
                    except Exception as _ce:
                        self._record("bracket_stop_cancel_failed",
                                     sleeve_id=sc.id, sleeve_name=sc.name,
                                     oid=ss.resting_stop_oid, error=str(_ce),
                                     severity="critical",
                                     reason=("stop-limit cancel failed after "
                                             "profit-lock fired; orphan stop "
                                             "may fire → §3.8 short-risk. Next "
                                             "tick's excess-cancel guard will "
                                             "sweep."))
                    ss.resting_stop_oid = None
                    ss.resting_stop_px = None
                    ss.resting_stop_stage = None
                ss.own_avg_entry = None
                ss.resting_profit_limit_oid = None
                ss.resting_profit_limit_px = None
                ss.state = SleeveStateEnum.ARMED_BUY
                ss.armed_buy_since_ts = _time.time()
                self._reset_cycle_state_post_sell(ss)
                self._save_state()
                self._open_sells_cache = None
                return
            elif status in ("CANCELLED", "EXPIRED"):
                self._record("profit_lock_limit_external_cancel_cleared",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             oid=ss.resting_profit_limit_oid, status=status)
                ss.resting_profit_limit_oid = None
                ss.resting_profit_limit_px = None
                self._save_state()
                _fall_through = True
            elif status == "OPEN":
                pass  # already handled above
            elif status == "FILLED":
                pass  # already handled above (returned)
            else:
                return  # UNKNOWN/PENDING — retry next tick

        if ss.resting_profit_limit_oid and not _fall_through:
            return  # still tracked; nothing more to do

        # No tracked oid (or just cleared) — try to ADOPT an existing matching LIMIT on the
        # book, then PLACE fresh if none found.
        try:
            existing = self._broker_query_open_sells()
        except Exception:
            existing = []
        tol = max(
            float(getattr(self.cfg, "tick_size", 0.01) or 0.01) * 2,
            float(sell_px) * 0.005,
        )
        adopted = False
        for _o in existing:
            if _o.get("kind") != "limit":
                continue
            if int(_o.get("size") or 0) != sleeve_qty:
                continue
            lp = float(_o.get("limit_price") or 0)
            if lp > 0 and abs(lp - sell_px) <= tol:
                ss.resting_profit_limit_oid = _o.get("order_id")
                ss.resting_profit_limit_px = lp
                self._record("profit_lock_limit_adopted_from_broker",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             oid=_o.get("order_id"),
                             limit_px=lp, sell_px=float(sell_px),
                             reason="matching LIMIT SELL already on book")
                self._save_state()
                self._open_sells_cache = None
                adopted = True
                break
        if adopted:
            return

        # Adam 2026-07-21 PHASE A MIGRATION SWEEP: cancel any LIMIT SELLs
        # on the book at our sleeve_qty that DON'T match sell_px within
        # tolerance. Cleans up pre-Phase-A trail-breach residue where a
        # LIMIT was placed at a trail-ratcheted price ABOVE mark (tracked
        # in ss.live_order_id, not resting_profit_limit_oid — invisible to
        # this method's normal flow). Without this sweep, we'd place a
        # NEW LIMIT at the correct sell_px while the stale one still sits
        # on the book → 2 LIMITs against 1 pos → §3.8 SHORT risk if mark
        # spikes through both.
        for _o in existing:
            if _o.get("kind") != "limit":
                continue
            if int(_o.get("size") or 0) != sleeve_qty:
                continue
            lp = float(_o.get("limit_price") or 0)
            if lp <= 0:
                continue
            if abs(lp - sell_px) <= tol:
                continue  # matches — already handled by adoption block above
            _stale_oid = _o.get("order_id")
            if not _stale_oid:
                continue
            try:
                self.b.cancel(_stale_oid)
                self._record(
                    "phase_a_migration_stale_limit_cancelled",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    cancelled_oid=_stale_oid,
                    stale_px=lp, correct_sell_px=float(sell_px),
                    severity="critical",
                    reason=("stale LIMIT SELL at wrong price (likely pre-"
                            "Phase-A trail-breach residue). Cancelled to "
                            "prevent §3.8 double-fire when the fresh Phase A "
                            "LIMIT at correct sell_px is placed."))
            except Exception as _ce:
                self._record(
                    "phase_a_migration_stale_limit_cancel_failed",
                    sleeve_id=sc.id, sleeve_name=sc.name,
                    oid=_stale_oid, error=str(_ce),
                    severity="critical")
        # Also clear any stale live_order_id that pointed at a trail-breach
        # LIMIT so subsequent order-poll paths don't chase a dead oid.
        if getattr(ss, "resting_stop_stage", "") in (
                "limit_breach_trail", "limit_breach_hard_bottom"):
            ss.resting_stop_stage = None
            ss.resting_stop_px = None
        # Invalidate cache after our cancels so the place block below sees
        # a clean book on its next _broker_query_open_sells (if it did one).
        self._open_sells_cache = None

        # Place fresh LIMIT SELL at sell_px
        try:
            snapped = self._snap_to_tick(float(sell_px))
        except Exception:
            snapped = float(sell_px)
        try:
            # Phase A BRACKET: include_pending=False so the broker's
            # no_short_check doesn't count the co-resting STOP-LIMIT at
            # stop_loss_px as coverage. Merton (1973) non-overlap: only
            # one can fire from a continuous price path. Mutual exclusion
            # for §3.8 is enforced above by resting_profit_limit_oid
            # tracking + migration sweep for stale wrong-price LIMITs.
            oid = self.b.place_limit(
                "SELL", sleeve_qty, float(snapped),
                include_pending=False)
            ss.resting_profit_limit_oid = oid
            ss.resting_profit_limit_px = float(snapped)
            self._record("profit_lock_limit_placed",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         oid=oid, sell_px=float(snapped),
                         qty=sleeve_qty)
            self._save_state()
            self._open_sells_cache = None
        except Exception as _e:
            self._record("profit_lock_limit_place_failed",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         sell_px=float(snapped),
                         error=str(_e), severity="critical",
                         reason=("failed to place profit-lock LIMIT at "
                                 "sell_px; retry next tick"))

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
        # Adam 2026-07-20 INVERSE of orphan-reconcile: heal ghost sleeves
        # (ARMED_SELL + own_avg set + broker has fewer contracts than
        # sum(sleeve claims)). Must run BEFORE _maintain_resting_stop so
        # ghosts don't place phantom stops. XLP double-fire 2026-07-20
        # 07:07 left two sleeves both WAITING_FOR_SELL with Position: 0.
        self._maybe_heal_ghost_sleeve(sc, ss)
        # Sibling to orphan-reconcile: when the sleeve is ALREADY ARMED_SELL
        # (own_avg set) and Adam manually adds more contracts on Coinbase,
        # refresh own_avg to the exchange's new blended avg so the dashboard
        # + future exit P&L reflect the new cost basis. See method docstring.
        self._maybe_reblend_on_manual_add(sc, ss)

        # [Adam 2026-07-15] Recovery-arm rule (fleet-wide from NGS directive):
        # any underwater sleeve with stop_loss_px=0 auto-arms stop at entry
        # the first time mark recovers to entry. Runs BEFORE ratchet-stop so
        # the newly-armed stop feeds into Stage 1 immediately.
        self._maybe_arm_stop_on_recovery(sc, ss, last_price)

        # [Adam 2026-07-15] Three-stage Coinbase resting stop-limit ratchet.
        # Runs BEFORE the trigger-check paths so a fresh HWM tick propagates
        # to Coinbase within one tick. Failure falls back to bot-side stops.
        self._maintain_resting_stop(sc, ss, last_price)

        # Adam 2026-07-21 PHASE A BRACKET: profit-lock LIMIT SELL at sell_px
        # runs ALONGSIDE the protective stop-limit above. Together they form
        # an OCO bracket (one cancels the other on fill). Fixes rule-#1
        # violation where trail-past-sell put a "stop" above mark.
        self._maintain_and_credit_profit_lock_limit(sc, ss, last_price)

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
        # Gate enforcement: if the expert gate blocked re-entry, reentry_pending
        # is still True. The normal arm path below must not run — it doesn't
        # check reentry_pending and would place a buy immediately, bypassing the
        # cadence floor entirely. This was the NEAR/HYPE churn-loop root cause:
        # gate denied at 0s elapsed, arm path fired anyway 1 tick later.
        if ss.reentry_pending:
            return

        # News blackout: pause new arms during scheduled high-uncertainty
        # windows (FOMC, CPI, NFP). Existing positions ride through unless
        # tier 3 (which halts, handled elsewhere).
        if self._sleeve_in_blackout(sc, ss):
            return

        # Abort governor uses the symbol-level bands. 0 = disabled.
        if self.cfg.abort_above > 0 and ss.state == SleeveStateEnum.ARMED_SELL and last_price >= self.cfg.abort_above:
            return self._sleeve_halt(sc, ss, f"price {last_price} above abort_above {self.cfg.abort_above}")
        if self.cfg.abort_below > 0 and ss.state == SleeveStateEnum.ARMED_BUY and last_price <= self.cfg.abort_below:
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
                # Adam 2026-07-15: regime router (Kaminski-Lo, Chan 2013).
                # When enabled + expert mode, applies qty multiplier from
                # regime classification and gates arms during chop.
                _rr_mode = self._regime_router_mode()
                if _rr_mode in ("shadow", "expert"):
                    try:
                        import regime as _regime
                        import regime_router as _rrouter
                        _prices = list(self._sleeve_price_history.get(sc.id, []) or [])
                        if len(_prices) >= 40:
                            _reg = _regime.classify_regime(
                                [{"close": p} for p in _prices])
                            _adj = _rrouter.regime_adjustments(_reg)
                            self._record(
                                "regime_router_shadow" if _rr_mode == "shadow"
                                else "regime_router_expert",
                                sleeve_id=sc.id, sleeve_name=sc.name,
                                regime=_adj["inputs"].get("regime"),
                                vol_state=_adj["inputs"].get("vol_state"),
                                gamma_multiplier=_adj["gamma_multiplier"],
                                qty_multiplier=_adj["qty_multiplier"],
                                should_arm=_adj["should_arm"],
                                reason=_adj["reason"],
                                current_qty=eff_qty,
                                mode=_rr_mode,
                            )
                            if _rr_mode == "expert":
                                if not _adj["should_arm"]:
                                    self._record(
                                        "regime_router_arm_gated",
                                        sleeve_id=sc.id, sleeve_name=sc.name,
                                        regime=_adj["inputs"].get("regime"),
                                        reason=_adj["reason"],
                                    )
                                    return
                                _orig = eff_qty
                                eff_qty = max(1, int(round(_orig * _adj["qty_multiplier"])))
                                if eff_qty != _orig:
                                    self._record(
                                        "regime_router_qty_adjusted",
                                        sleeve_id=sc.id, sleeve_name=sc.name,
                                        original_qty=_orig, adjusted_qty=eff_qty,
                                        multiplier=_adj["qty_multiplier"],
                                        regime=_adj["inputs"].get("regime"),
                                    )
                    except Exception as _re:
                        self._record("regime_router_error",
                                     sleeve_id=sc.id, error=str(_re),
                                     severity="warn")
                # Adam 2026-07-15: cross-sleeve correlation-aware sizing
                # (Rob Carver ch.10). When enabled, downscale qty by
                # portfolio_correlation_drag — highly-correlated concurrent
                # holdings compound tail risk, so a fifth crypto perp is
                # NOT worth 5× a single one. Off by default; opt-in per
                # sleeve via sc.correlation_sizing_enabled.
                if getattr(sc, "correlation_sizing_enabled", False):
                    try:
                        import correlation as _corr
                        held = self._current_held_symbols_excluding(self.symbol)
                        mult, drag_diag = _corr.portfolio_correlation_drag(
                            self.store, self.tenant_id, self.symbol, held,
                            threshold=float(getattr(sc, "correlation_sizing_threshold", 0.5) or 0.5),
                            min_scale=float(getattr(sc, "correlation_sizing_min_scale", 0.3) or 0.3),
                        )
                        if mult < 1.0:
                            orig = eff_qty
                            eff_qty = max(1, int(round(orig * mult)))
                            self._record(
                                "sleeve_correlation_size_adjusted",
                                sleeve_id=sc.id, sleeve_name=sc.name,
                                original_qty=orig, adjusted_qty=eff_qty,
                                multiplier=round(mult, 4),
                                diagnostics=drag_diag,
                            )
                    except Exception as _e:
                        self._record("correlation_sizing_error",
                                     sleeve_id=sc.id, error=str(_e),
                                     severity="warn")
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
        elif status in ("CANCELLED", "EXPIRED"):
            # Terminal states. Credit any partial fill first, then clear.
            if filled > 0:
                self._record("sleeve_credited_partial_before_clear",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             order_id=ss.live_order_id, status=status,
                             filled_qty=filled)
                ss.filled_qty = filled
                self._sleeve_on_fill(sc, ss, st.get("average_filled_price"))
            self._record("sleeve_order_cleared",
                sleeve_id=sc.id, sleeve_name=sc.name,
                order_id=ss.live_order_id, status=status)
            ss.live_order_id = None
            ss.filled_qty = 0
        elif status == "UNKNOWN":
            # Adam 2026-07-20 §3.6 ORPHAN GUARD (tick path): UNKNOWN != gone.
            # If Coinbase returns a partial fill count, credit it. Do NOT
            # clear live_order_id — retry next tick. Prior code cleared,
            # producing ghost (own_avg stuck) + orphan (order kept on
            # Coinbase). Retry burden is bounded — poller checks every tick.
            if filled > 0:
                self._record("sleeve_credited_partial_before_clear",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             order_id=ss.live_order_id, status=status,
                             filled_qty=filled)
                ss.filled_qty = filled
                self._sleeve_on_fill(sc, ss, st.get("average_filled_price"))
            self._record(
                "sleeve_order_status_unknown",
                sleeve_id=sc.id, sleeve_name=sc.name,
                order_id=ss.live_order_id, severity="critical",
                reason="Coinbase UNKNOWN; NOT clearing (avoid ghost + orphan).")

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

        Two-tier gate (Adam 2026-07-20 — biggest rule: don't lose money):
          Tier 1 (HARD FLOOR): stop_price >= own_avg + fee_safety. A trail
            exit must NEVER close in the red. Only protective stop_loss may.
          Tier 2 (SOFT TARGET): stop_price nets at least the sleeve's
            configured target (sell_px - buy_px round-trip). Keeps trailing
            until HWM climbs enough to clear the target.

        Tier 1 is absolute. Tier 2 is skipped if unconfigured.
        Formula:
          target_net = (sell_px - buy_px) × size × qty − fee_roundtrip × qty
          net_at_stop = (stop_price - cost_basis) × size × qty − fee_roundtrip × qty
        """
        cs = self.cfg.contract_size
        fees = self.cfg.fee_per_contract_roundtrip * sc.qty
        basis = ss.sell_entry_avg
        if basis is None:
            basis = self._sleeve_avg_entry(sc)
            if basis is not None:
                ss.sell_entry_avg = basis
        if basis is None:
            basis = float(ss.own_avg_entry) if ss.own_avg_entry else float(sc.buy_px)
        # TIER 1 (HARD): stop must clear own_avg + fee_safety.
        # fee_safety = per-contract share of roundtrip fee, plus max(1 tick,
        # 5 bps of basis) — same safety margin as feedback_no_net_loss_cycles
        # so the trail path can't sell for less than break-even + fees.
        try:
            fee_per_ct = float(getattr(self.cfg, "fee_per_contract_roundtrip", 0) or 0)
            per_ct_fee_price = fee_per_ct / max(1.0, cs) / max(1, int(sc.qty or 1))
        except Exception:
            per_ct_fee_price = 0.0
        try:
            tick = float(getattr(self.cfg, "tick_size", 0) or 0.01)
        except Exception:
            tick = 0.01
        fee_safety = per_ct_fee_price + max(tick, basis * 0.0005)
        hard_floor = basis + fee_safety
        if stop_price < hard_floor:
            self._record(
                "sleeve_trail_lockin_hard_floor",
                sleeve_id=sc.id, sleeve_name=sc.name,
                stop=stop_price, cost_basis=basis,
                hard_floor=round(hard_floor, 6),
                fee_safety=round(fee_safety, 6),
                severity="info",
                reason="trail exit refused: would close in the red",
            )
            return False
        # TIER 2 (SOFT): sleeve's configured target.
        target_net = (sc.sell_px - sc.buy_px) * cs * sc.qty - fees
        if target_net <= 0:
            return True  # weirdly configured — don't gate on target
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

        # Adam 2026-07-16: initial-entry regime gate on sleeve BUY.
        # 6-voter supermajority. Kill switch: expert_arm_gate.MODE = "off".
        if side == "BUY" and qty > 0:
            if not self._expert_arm_gate_allows(prices_source=sc.id,
                                                  arm_direction="buy",
                                                  sleeve_id=sc.id):
                self._record("sleeve_arm_denied_by_gate",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             reason="expert_arm_gate voted deny",
                             price=float(price))
                return

        # Adam 2026-07-16: safety-cap qty via expert_size on BUY only.
        # Consensus of Van Tharp + half-Kelly + Vince, HARD-capped by
        # sc.qty (user config). Never grows above sc.qty.
        if side == "BUY" and qty > 0:
            try:
                # Recent cycle PnLs for this sleeve (for Kelly + Vince)
                cycle_pnls = []
                try:
                    if hasattr(self, "trade_log") and self.trade_log:
                        for e in self.trade_log.events():
                            if not isinstance(e, dict):
                                continue
                            if e.get("event_type") != "sleeve_cycle_completed":
                                continue
                            if e.get("sleeve_id") != sc.id:
                                continue
                            p = e.get("realized_pnl") or e.get("cycle_pnl") or 0
                            try:
                                cycle_pnls.append(float(p))
                            except (TypeError, ValueError):
                                pass
                except Exception:
                    pass
                # Expected profit per contract from sleeve's spread
                try:
                    spread = float(sc.sell_px or 0) - float(sc.buy_px or 0)
                    expected_profit_per_contract = max(0.0, spread * float(getattr(self.cfg, "contract_size", 1) or 1))
                except (TypeError, ValueError):
                    expected_profit_per_contract = 0.0
                stop_dist = 0.0
                try:
                    if float(sc.stop_loss_px or 0) > 0 and price > float(sc.stop_loss_px or 0):
                        stop_dist = price - float(sc.stop_loss_px or 0)
                except (TypeError, ValueError):
                    pass
                qty = self._expert_size_adjust(
                    user_configured_qty=int(qty),
                    mark=float(price),
                    stop_distance=float(stop_dist),
                    contract_size=float(getattr(self.cfg, "contract_size", 1) or 1),
                    fee_per_roundtrip=float(getattr(self.cfg, "fee_per_contract_roundtrip", 0.0) or 0.0),
                    expected_profit_per_contract=expected_profit_per_contract,
                    recent_cycle_pnls=(cycle_pnls if cycle_pnls else None),
                    log_prefix=f"sleeve_{sc.id}_buy",
                )
                if qty <= 0:
                    # Expert says don't size in — respect but log
                    self._record("sleeve_arm_skipped_expert_size_zero",
                                 sleeve_id=sc.id, sleeve_name=sc.name,
                                 reason="expert_size returned 0")
                    return
            except Exception as _e:
                try:
                    self._record("sleeve_expert_size_error",
                                 sleeve_id=sc.id, sleeve_name=sc.name,
                                 error=str(_e), severity="warn")
                except Exception:
                    pass

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
        # Fee sanity ceiling (Adam 2026-07-20): the legacy _arm path at
        # line 971 always calls self._fee_gate_ok before placing — catches
        # broker fee-blowout bugs (a bad quote can preview a commission
        # 10× normal, e.g., XLP 2026-07-17 incident). But the sleeve
        # arm path never called it, so every sleeve-armed order bypassed
        # the sanity check entirely. Fix: gate here too. If preview says
        # the fee is > fee_sanity_multiplier × expected, return without
        # placing — next tick retries once preview normalizes.
        if not self._fee_gate_ok(side, qty, price):
            self._record("sleeve_arm_blocked_fee_gate",
                         sleeve_id=sc.id, sleeve_name=sc.name,
                         side=side, qty=qty, price=price,
                         reason="_fee_gate_ok returned False — "
                                "previewed commission exceeded sanity "
                                "ceiling; deferring arm to next tick")
            return
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
        """Apply Kelly-fraction sizing if enabled, then Harvey vol-target
        scaling on top. Never sizes UP past cfg.qty × vol_target.MAX_SCALE.

        Order of operations:
          1. cfg.qty (user's declared base)
          2. Kelly quarter-fraction shrink (Van Tharp / Vince) — never
             sizes UP; only shrinks base after losing streaks
          3. Harvey (2018 JPM) vol-target scaling — can shrink or grow
             within [MIN_SCALE, MAX_SCALE]. Reduces drawdowns by
             sizing DOWN in high-vol regimes.

        Both layers feature-flagged. When both off, returns cfg.qty
        unchanged. When both on, Kelly shrinks first, then Harvey
        adjusts the shrunk quantity — conservative composition.
        """
        base = int(sc.qty)
        # Layer 1: Kelly
        if getattr(sc, "kelly_enabled", False):
            try:
                import kelly
                recent = list(getattr(ss, "recent_cycle_pnls", []) or [])
                mult = kelly.compute_kelly_multiplier(
                    recent,
                    kelly_fraction=float(getattr(sc, "kelly_fraction", 0.25) or 0.25),
                    min_cycles=int(getattr(sc, "kelly_min_cycles", 8) or 8),
                )
                base = kelly.size_from_qty(int(sc.qty), mult)
            except Exception as e:
                self._record("kelly_compute_failed", sleeve_id=sc.id, error=str(e))
                base = int(sc.qty)
        # Layer 2: Harvey vol-target (Option D-2, 2026-07-19). Feature-
        # flagged; when off, returns base unchanged.
        try:
            import vol_target
            # Build a daily return series from the sleeve's rolling
            # price history. Prefer daily bars if available; fall back
            # to the tick-level history (which vol_target will scale
            # via its annualization factor).
            history = list(self._sleeve_price_history.get(sc.id, []) or [])
            returns: list[float] = []
            if len(history) >= 6:
                import math as _math
                for i in range(1, len(history)):
                    a, b = history[i - 1], history[i]
                    if a > 0 and b > 0:
                        returns.append(_math.log(b / a))
            return int(vol_target.adjusted_qty(base, returns))
        except Exception as e:
            self._record("vol_target_failed", sleeve_id=sc.id, error=str(e))
            return int(base)

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

    def _broker_query_open_sells(self, force: bool = False) -> list:
        """Return list of Coinbase-open SELL orders on this product. Cached
        500ms per SwingTrader instance to survive multi-sleeve-per-tick calls
        without hammering the REST API.

        Adam 2026-07-20 BROKER-AUTHORITATIVE fix: sleeve-state resting_stop_
        oid tracking can drift from Coinbase truth (fill-not-credited race,
        cross-sleeve state divergence, crash-recovery gaps). This helper is
        the SOURCE OF TRUTH — anything not on Coinbase's book here does not
        actually exist as a resting stop.

        Returns a list of dicts, each with keys: order_id, size (int),
        created_time (iso str). Empty list on any error (fail-open — we
        prefer to place a fresh stop over leaving position unprotected)."""
        import time as _t
        _now = _t.time()
        _cache = getattr(self, "_open_sells_cache", None)
        if _cache and (_now - _cache.get("ts", 0)) < 0.5 and not force:
            return _cache.get("orders", [])
        try:
            _resp = self.b.client.list_orders(
                product_id=self.symbol, order_status="OPEN", limit=100
            )
            _raw = (_resp.to_dict() if hasattr(_resp, "to_dict")
                    else (_resp if isinstance(_resp, dict) else {}))
            _orders = []
            for _o in (_raw.get("orders") or []):
                if str(_o.get("side") or "").upper() != "SELL":
                    continue
                # Extract qty + stop_price + limit_price from
                # order_configuration. stop_price is the trigger; limit_price
                # is the fill floor. Adoption logic needs stop_price to match
                # existing SELLs against sleeve target_px.
                _cfg = _o.get("order_configuration") or {}
                _qty = 0
                _stop_px = 0.0
                _limit_px = 0.0
                for _v in (_cfg.values() if isinstance(_cfg, dict) else []):
                    if isinstance(_v, dict):
                        _q_raw = (_v.get("base_size") or _v.get("size")
                                  or _v.get("quote_size") or 0)
                        try:
                            _qty = int(float(_q_raw))
                        except (TypeError, ValueError):
                            _qty = 0
                        try:
                            _stop_px = float(_v.get("stop_price") or 0)
                        except (TypeError, ValueError):
                            _stop_px = 0.0
                        try:
                            _limit_px = float(_v.get("limit_price") or 0)
                        except (TypeError, ValueError):
                            _limit_px = 0.0
                        if _qty > 0:
                            break
                if _qty <= 0:
                    _qty = 1  # conservative fallback
                # Adam 2026-07-21 Phase A bracket: kind distinguishes
                # protective STOP-LIMIT (below mark, has stop_price) from
                # profit-lock LIMIT (above mark, no stop trigger). Excess-
                # cancel + pre-place guards filter on kind=="stop_limit"
                # so a profit-lock LIMIT never gets misread as protective
                # coverage that blocks stop placement.
                _kind = "stop_limit" if _stop_px > 0 else "limit"
                _orders.append({
                    "order_id": _o.get("order_id"),
                    "size": _qty,
                    "stop_price": _stop_px,
                    "limit_price": _limit_px,
                    "kind": _kind,
                    "created_time": _o.get("created_time"),
                })
        except Exception as _e:
            # Fail-open: return empty so caller treats "no known excess" and
            # falls back to sleeve-state-based checks. Log so the drift is
            # visible instead of swallowed.
            try:
                self._record("broker_open_sells_query_failed",
                             error=str(_e), severity="warn",
                             reason=("broker-authoritative check unavailable "
                                     "this tick; falling back to sleeve state"))
            except Exception:
                pass
            _orders = []
        self._open_sells_cache = {"ts": _now, "orders": _orders}
        return _orders

    def _broker_cancel_excess_sells(self, current_pos_qty: int,
                                      reason: str) -> int:
        """Query broker for open SELLs on this product; if sum(sizes) exceeds
        current_pos_qty, cancel the NEWEST excess first (keeps the oldest
        stop which was likely placed against the earliest legitimate holder).

        Returns count of orders cancelled. Invalidates the cache on cancel."""
        _open_all = self._broker_query_open_sells(force=True)
        if not _open_all:
            return 0
        # Adam 2026-07-21 PHASE A: only sum STOP-LIMITs when checking
        # for excess. Profit-lock LIMITs are the other leg of the bracket
        # (can only fire on price rise) and don't compete with protective
        # stops for double-fire risk.
        _open = [o for o in _open_all if o.get("kind") == "stop_limit"]
        _total = sum(int(o.get("size") or 0) for o in _open)
        if _total <= current_pos_qty:
            return 0
        # Sort NEWEST FIRST so we cancel most-recently placed excess
        _by_time = sorted(_open, key=lambda o: str(o.get("created_time") or ""),
                          reverse=True)
        _excess = _total - current_pos_qty
        _cancelled = 0
        for _o in _by_time:
            if _excess <= 0:
                break
            _sz = int(_o.get("size") or 0)
            _oid = _o.get("order_id")
            if not _oid or _sz <= 0:
                continue
            try:
                self.b.cancel(_oid)
                _cancelled += 1
                _excess -= _sz
                # Clear ANY sleeve-state tracking pointing at this oid so
                # we don't leave dangling resting_stop_oid references.
                for _sid, _ss in (self.s.sleeves or {}).items():
                    if getattr(_ss, "resting_stop_oid", None) == _oid:
                        _ss.resting_stop_oid = None
                        _ss.resting_stop_px = None
                        _ss.resting_stop_stage = None
                self._record(
                    "broker_excess_sell_cancelled",
                    oid=_oid, size=_sz, pos_qty=current_pos_qty,
                    total_open_sells_before=_total,
                    severity="critical",
                    trigger_reason=reason,
                    reason=("broker-authoritative check found sum(open "
                            "SELLs) > pos_qty. Cancelled newest excess "
                            "to prevent §3.8 double-fire → SHORT. This "
                            "runs regardless of sleeve-state drift."),
                )
            except Exception as _e:
                self._record(
                    "broker_excess_sell_cancel_failed",
                    oid=_oid, size=_sz, error=str(_e),
                    severity="critical",
                    reason=("cancel raised — excess sell remains on "
                            "Coinbase, short risk still open. Will retry "
                            "on next hook fire."),
                )
        if _cancelled > 0:
            # Invalidate cache so next check re-queries
            self._open_sells_cache = None
            try:
                self._save_state()
            except Exception:
                pass
        return _cancelled

    def _maybe_calibrate_fee_from_fill(self, order_id: Optional[str],
                                        contract_qty: int) -> None:
        """Sample the actual fee from a just-filled order and, once we have
        enough samples, update cfg.fee_per_contract_roundtrip in Redis so the
        fee-floor clamp uses reality instead of the $4.68 SLR default.

        Adam 2026-07-20: hardcoded $4.68 was correct for SLR silver only;
        applying it universally overstates perps/small futures by 3-10× and
        makes take-profits impossible to fire on those products.

        Idempotent: never mutates cfg unless drift > 10% (deadband prevents
        thrashing on noise). Fail-open: any error skips calibration this
        cycle, doesn't halt trading."""
        if not order_id:
            return
        try:
            st = self.b.order_status(order_id) or {}
        except Exception:
            return
        try:
            total_fees = float(st.get("total_fees") or 0)
            filled = float(st.get("filled_qty") or 0)
        except (TypeError, ValueError):
            return
        if total_fees <= 0 or filled <= 0:
            return
        # Per-CONTRACT fee for this side (filled is contracts, not underlying units)
        fee_per_ct_per_side = total_fees / filled
        # Guard: reject nonsense (e.g., fee > 10% of a $1000 fill = misparse)
        if fee_per_ct_per_side <= 0 or fee_per_ct_per_side > 1000.0:
            return
        # Rolling window on SwingState (persists via _save_state)
        recent = list(getattr(self.s, "recent_side_fees", None) or [])
        recent.append(float(fee_per_ct_per_side))
        if len(recent) > 20:
            recent = recent[-20:]
        self.s.recent_side_fees = recent
        if len(recent) < 5:
            return  # need more samples before touching cfg
        avg_side = sum(recent) / len(recent)
        new_rt = 2.0 * avg_side
        try:
            old_rt = float(getattr(self.cfg, "fee_per_contract_roundtrip", 0) or 0)
        except (TypeError, ValueError):
            old_rt = 0.0
        # Deadband: only update if drift > 10% (or old is zero/unset)
        if old_rt > 0 and abs(new_rt - old_rt) / old_rt < 0.10:
            return
        try:
            self.cfg.fee_per_contract_roundtrip = float(new_rt)
            cfg_dict = self.store.get_config(self.tenant_id, self.symbol) or {}
            cfg_dict["fee_per_contract_roundtrip"] = float(new_rt)
            self.store.put_config(self.tenant_id, self.symbol, cfg_dict)
            self._record(
                "fee_per_contract_roundtrip_auto_calibrated",
                old_rt=round(old_rt, 4),
                new_rt=round(new_rt, 4),
                samples=len(recent),
                latest_side_fee=round(fee_per_ct_per_side, 4),
                severity="info",
                reason=("cfg.fee_per_contract_roundtrip drifted >10% from "
                        "actual Coinbase fees averaged over last N fills. "
                        "Updated so fee-floor clamp + realized P&L reflect "
                        "reality. Kills $4.68-default-for-everything class."),
            )
        except Exception as _e:
            try:
                self._record("fee_auto_calibration_persist_failed",
                             error=str(_e), severity="warn")
            except Exception:
                pass

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
        # Adam 2026-07-20 FEE AUTO-CALIBRATION: sample this fill's actual fee
        # and update cfg.fee_per_contract_roundtrip when drift exceeds 10%.
        # Kills the $4.68 SLR-default-for-everything class (10× overcount on
        # ETH PERP, 4× on OIL/XLM/HYPE — clamps sell_px too high → take-profit
        # rarely fires → cycles close via stop_loss → guaranteed losses).
        self._maybe_calibrate_fee_from_fill(filled_order_id, sc.qty)
        # Adam 2026-07-20 BROKER-AUTHORITATIVE POST-FILL SWEEP: every fill
        # changes broker position. Immediately re-check whether the current
        # open SELLs on Coinbase now exceed the (post-fill) position and
        # cancel any excess. Runs SYNCHRONOUSLY here, not deferred to next
        # tick — closes the "sibling sleeve's stale stop fires before excess-
        # cancel tick runs" race that caused today's 3 SHORTs (MC/HYF/OND).
        try:
            _pos_after = int(self.b.position_qty() or 0)
            if _pos_after >= 0:
                self._broker_cancel_excess_sells(
                    _pos_after, reason="post_fill_sweep"
                )
        except Exception as _e:
            try:
                self._record("post_fill_sweep_error",
                             error=str(_e), severity="warn")
            except Exception:
                pass
        half_fee = (self.cfg.fee_per_contract_roundtrip / 2.0) * sc.qty
        if ss.state == SleeveStateEnum.ARMED_SELL:
            # Sell fill = profit realization. Anchor on the actual FIFO cost
            # basis captured at arm time. This matches the position-row math:
            # you sold contracts you owned, realized P/L happens NOW.
            try: fill = float(fill_price) if fill_price is not None else 0.0
            except (TypeError, ValueError): fill = 0.0
            # Adam 2026-07-15 fix #7 (problem-scout audit): basis fallback chain.
            # Priority: sell_entry_avg (captured at arm time — most accurate)
            # → own_avg_entry (sleeve's own tracking) → broker.position.avg_entry
            # (exchange truth, only valid if we still hold something) → sc.buy_px
            # (config TARGET, not actual fill — last resort, logs a warn because
            # it silently skews P&L when actual fill differed from target).
            basis = None
            basis_source = None
            if ss.sell_entry_avg is not None:
                basis = float(ss.sell_entry_avg)
                basis_source = "sell_entry_avg"
            elif ss.own_avg_entry is not None:
                basis = float(ss.own_avg_entry)
                basis_source = "own_avg_entry"
            else:
                try:
                    broker_pos = self.b.position
                    if getattr(broker_pos, "avg_entry", 0) > 0:
                        basis = float(broker_pos.avg_entry)
                        basis_source = "broker.position.avg_entry"
                except Exception:
                    pass
            if basis is None:
                basis = float(sc.buy_px)
                basis_source = "sc.buy_px (config target — actual fill unknown)"
                self._record("sleeve_on_fill_basis_fallback_to_config",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             fill_price=fill, config_buy_px=sc.buy_px,
                             severity="warn",
                             reason="no sell_entry_avg / own_avg_entry / broker "
                                    "avg — realized P&L uses config target, will "
                                    "differ from actual by fill_slippage")
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
            # Cancel any resting stop-limit on Coinbase. The position was
            # just exited via take-profit; the stop order is dangling and
            # must be cancelled before price can recover above stop_px and
            # fill it (which would create an accidental short).
            # Adam 2026-07-20 ORPHAN GUARD: only clear tracking on cancel
            # success. Prior code cleared unconditionally after `except log`
            # so a raised cancel produced exactly the short-risk the comment
            # above warns about. If cancel fails, keep tracking so the next
            # tick's reconcile / _maybe_credit_resting_stop_fill will re-try.
            if ss.resting_stop_oid:
                _tpf_ok = False
                try:
                    self.b.cancel(ss.resting_stop_oid)
                    _tpf_ok = True
                    self._record("resting_stop_cancelled_on_tp_fill",
                                 sleeve_id=sc.id, sleeve_name=sc.name,
                                 oid=ss.resting_stop_oid)
                except Exception as _ce:
                    self._record("resting_stop_cancel_on_tp_fill_failed",
                                 sleeve_id=sc.id, sleeve_name=sc.name,
                                 oid=ss.resting_stop_oid, error=str(_ce),
                                 severity="critical",
                                 reason=("cancel raised after TP fill; keeping "
                                         "resting_stop_oid tracked so next tick "
                                         "retries — the comment above warns this "
                                         "is a SHORT risk if not cancelled"))
                if _tpf_ok:
                    ss.resting_stop_oid = None
                    ss.resting_stop_px = None
                    ss.resting_stop_stage = None
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
                # Adam 2026-07-21 (expert_reentry Vince gate): record when
                # the losing cycle closed so Vince cooldown timer can compute
                # elapsed time. Only bumped on losses; wins leave it alone.
                import time as _tt
                ss.last_loss_ts = _tt.time()
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
            # Adam 2026-07-15 fix #7: fallback chain — actual fill_price →
            # broker.position.avg_entry → sc.buy_px (last resort + warn).
            # sc.buy_px is the config TARGET, not the actual fill. Using it
            # silently skews the next cycle's realized P&L by fill_slippage.
            own_avg = None
            avg_source = None
            try:
                if fill_price is not None:
                    own_avg = float(fill_price)
                    if own_avg > 0:
                        avg_source = "fill_price"
                    else:
                        own_avg = None
            except (TypeError, ValueError):
                own_avg = None
            if own_avg is None:
                try:
                    broker_pos = self.b.position
                    if getattr(broker_pos, "avg_entry", 0) > 0:
                        own_avg = float(broker_pos.avg_entry)
                        avg_source = "broker.position.avg_entry"
                except Exception:
                    pass
            if own_avg is None:
                own_avg = float(sc.buy_px)
                avg_source = "sc.buy_px (config target — actual fill unknown)"
                self._record("sleeve_rebuy_own_avg_fallback_to_config",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             fill_price=fill_price, config_buy_px=sc.buy_px,
                             severity="warn",
                             reason="fill_price missing AND broker avg unavailable "
                                    "— own_avg_entry uses config target, next "
                                    "cycle's realized will be off by fill_slippage")
            ss.own_avg_entry = own_avg
            ss.state = SleeveStateEnum.ARMED_SELL
            # Adam 2026-07-20 GHOST RECURRENCE ROOT FIX: snapshot own_avg
            # into sell_entry_avg the moment we transition to ARMED_SELL.
            # This is the persistent cost-basis copy that _credit_stop_fill
            # falls back to when own_avg gets cleared by ANY path (reblend,
            # reconcile-autocorrect, sibling sleeve, race). Without this,
            # the fallback chain empties → halt with "own_avg unknown" →
            # the SLR recurrence pattern that halted us all night.
            # Only set if empty — don't clobber a live sell_entry_avg
            # that already captured a differently-priced entry.
            if not (ss.sell_entry_avg and float(ss.sell_entry_avg) > 0):
                ss.sell_entry_avg = own_avg
            # Adam 2026-07-20 (feedback_trail_arm_at_buy_fill +
            # feedback_biggest_rule_dont_lose_take_best):
            # Trail ARMS the moment a buy fills, floored at own_avg. HWM
            # starts at own_avg and only ratchets UP. Every downstream
            # trail-fire path checks stop >= own_avg + fee_safety via
            # _sleeve_lockin_ok, so no take-profit exit can close in the
            # red. Protective stop_loss remains free to fire below own_avg.
            ss.trail_armed = True
            ss.trail_high_water_price = own_avg
            ss.hybrid_sell_triggered_ts = None  # fresh cycle, no prior trigger
            self._record(
                "sleeve_rebuy_completed",
                sleeve_id=sc.id, sleeve_name=sc.name,
                fill_price=fill_price, fees=half_fee,
                realized_pnl_total=ss.realized_pnl,
                own_avg_entry=ss.own_avg_entry,
                own_avg_source=avg_source,
                trail_armed_at_own_avg=True,
                trail_hwm_seed=own_avg,
            )

    def _sleeve_halt(self, sc: SleeveConfig, ss: SleeveState, reason: str) -> None:
        if ss.live_order_id:
            # Adam 2026-07-20 ORPHAN GUARD: only clear tracking if cancel
            # succeeded. Prior code cleared unconditionally after `except:
            # pass`, so a failed cancel produced an orphan order (still
            # live on Coinbase, but no sleeve tracked it — §3.8 short risk
            # if it fires while position=0).
            _cancel_ok = False
            try:
                self.b.cancel(ss.live_order_id)
                _cancel_ok = True
            except Exception as _e:
                self._record("sleeve_halt_cancel_failed",
                             sleeve_id=sc.id, sleeve_name=sc.name,
                             order_id=ss.live_order_id, error=str(_e),
                             severity="critical",
                             reason=("cancel raised during halt — keeping "
                                     "live_order_id tracked so operator can "
                                     "retry or diag-cancel; avoids orphan"))
            if _cancel_ok:
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
        # Adam 2026-07-20 ORPHAN GUARD (primary halt): only clear tracking
        # if cancel succeeded. Same pattern as _sleeve_halt fix above.
        if self.s.live_order_id:
            _cancel_ok = False
            try:
                self.b.cancel(self.s.live_order_id)
                _cancel_ok = True
            except Exception as _e:
                self._record("primary_halt_cancel_failed",
                             order_id=self.s.live_order_id, error=str(_e),
                             severity="critical",
                             reason=("cancel raised during primary halt — "
                                     "keeping tracking to avoid orphan"))
            if _cancel_ok:
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
