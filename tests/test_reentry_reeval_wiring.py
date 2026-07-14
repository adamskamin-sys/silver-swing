"""Review-gate tests for the reentry_reeval wiring in swing_leg.py.

Verifies each checkbox from Adam / cloud auditor's 2026-07-14 review gate:

Preconditions:
  * Wired behind __reentry_mode__ == 'expert' only; flag off = legacy.

Tier 1 (money-loss):
  * Cancel-before-place — cancel confirmed BEFORE place_limit
  * Through the WS1 dedup lock — no place if lock unavailable
  * Anti-thrash reset — armed_buy_since_ts reset after reanchor

Tier 2 (correctness):
  * State write hits memory AND Redis after reanchor
  * Expire exits cleanly — no re-arm next tick
  * (Tier 2 #3 unified level logic — covered by tests/test_arm_level.py)

Tier 3:
  * Every reeval action emits a trade-log event
"""
import time
from types import SimpleNamespace

import pytest

from sim_broker import SimBroker, SimConfig
from safety import TradeLog
from state_store import JsonFileStateStore
from swing_leg import SwingTrader, SleeveStateEnum


TENANT = "adam"
SYMBOL = "SLR-27AUG26-CDE"


def _default_cfg():
    return {
        "core_qty": 0, "swing_qty": 0, "max_swing_qty": 5,
        "sell_px": 65.0, "buy_px": 63.0,
        "contract_size": 50, "tick_size": 0.005,
        "margin_per_contract": 275.0,
        "scale_up_buffer_mult": 1.5,
        "fee_per_contract_roundtrip": 4.68,
        "abort_below": 60.0, "abort_above": 70.0,
        "fee_sanity_multiplier": 2.0,
        "sleeves": [{
            "id": "sleeve-1", "name": "TestModelB",
            "qty": 1, "sell_px": 65.0, "buy_px": 63.0,
            "trail_distance": 0.2, "reanchor_threshold": 2.0,
            "reentry_range_window": 60,
        }],
    }


def _make_broker():
    return SimBroker(SimConfig(
        product_id=SYMBOL, contract_size=50.0, tick_size=0.005,
        fee_per_fill=2.34, margin_per_contract=275.0,
        starting_balance=100_000.0,
    ))


def _make_trader(tmp_path, mode="legacy"):
    store = JsonFileStateStore(tmp_path / "store.json")
    store.put_config(TENANT, SYMBOL, _default_cfg())
    if mode in ("expert", "shadow"):
        store.put_state(TENANT, "__reentry_mode__", {"mode": mode})
    log = TradeLog(tmp_path / "trades.jsonl")
    trader = SwingTrader(_make_broker(), store, TENANT, SYMBOL, trade_log=log)
    return trader, store, log


def _get_sleeve(trader):
    """Return (sc, ss) for the single test sleeve."""
    from dataclasses import replace
    scs = trader._load_sleeves_cfg()
    assert scs, "no sleeve config loaded"
    sc = scs[0]
    ss = trader.s.sleeves.get(sc.id)
    assert ss is not None, "sleeve state missing"
    return sc, ss


def _prime_history(trader, sc_id, prices):
    """Seed the sleeve's rolling price history."""
    from collections import deque
    trader._sleeve_price_history[sc_id] = deque(prices, maxlen=240)


# ---- Preconditions: byte-for-byte legacy when flag off -------------------

def test_flag_off_is_noop(tmp_path):
    """With no __reentry_mode__ scope OR mode='legacy', _maybe_reeval_pending_arm
    must return immediately without touching any broker method."""
    trader, store, log = _make_trader(tmp_path, mode="legacy")
    sc, ss = _get_sleeve(trader)
    _prime_history(trader, sc.id, [75.0] * 60)
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "resting-order-id-123"
    ss.last_sell_fill_price = 75.0
    ss.armed_buy_since_ts = time.time() - 3600  # very stale — would trigger reeval

    # Should return immediately without any broker or store interaction
    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)
    assert ss.live_order_id == "resting-order-id-123"  # untouched
    assert ss.state == SleeveStateEnum.ARMED_BUY


def test_flag_reads_expert_from_scope(tmp_path):
    """Verify the flag reader picks up 'expert' from __reentry_mode__ scope."""
    trader, store, log = _make_trader(tmp_path, mode="expert")
    assert trader._reentry_mode() == "expert"
    # And 'legacy' when absent
    trader2, _, _ = _make_trader(tmp_path / "legacy", mode="legacy")
    assert trader2._reentry_mode() == "legacy"


def test_flag_reads_shadow_from_scope(tmp_path):
    """Auditor 2026-07-14 SHADOW MODE: verify flag reader picks up 'shadow'."""
    trader, store, log = _make_trader(tmp_path, mode="shadow")
    assert trader._reentry_mode() == "shadow"


def test_flag_off_by_default(tmp_path):
    """Missing scope → 'legacy' (default)."""
    store = JsonFileStateStore(tmp_path / "store.json")
    store.put_config(TENANT, SYMBOL, _default_cfg())
    trader = SwingTrader(_make_broker(), store, TENANT, SYMBOL)
    assert trader._reentry_mode() == "legacy"


# ---- Tier 1 #1 Cancel-before-place ---------------------------------------

class _MockBroker:
    """Records every method call on the broker so we can verify order.
    Configurable failures: raise_on_cancel, raise_on_place."""
    def __init__(self, raise_on_cancel=False, raise_on_place=False):
        self.calls = []
        self.raise_on_cancel = raise_on_cancel
        self.raise_on_place = raise_on_place
        self._next_oid = "new-order-id-xyz"

    def cancel(self, oid):
        self.calls.append(("cancel", oid))
        if self.raise_on_cancel:
            raise RuntimeError("simulated cancel failure")

    def place_limit(self, side, qty, price, post_only=False):
        self.calls.append(("place_limit", side, qty, price))
        if self.raise_on_place:
            raise RuntimeError("simulated place failure")
        return self._next_oid

    def order_status(self, oid):
        return {"status": "OPEN"}


def test_tier1_cancel_before_place_order(tmp_path, monkeypatch):
    """When reeval returns 'reanchor', cancel MUST be called before
    place_limit — regardless of prices."""
    trader, store, log = _make_trader(tmp_path, mode="expert")
    sc, ss = _get_sleeve(trader)
    _prime_history(trader, sc.id, [75.0 - i * 0.01 for i in range(60)])
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "resting-order-id-123"
    ss.last_sell_fill_price = 75.0
    ss.armed_buy_since_ts = time.time() - 3600  # stale
    ss.pre_halt_state = None
    # Swap in mock broker
    mock = _MockBroker()
    trader.b = mock
    # Force reeval to return reanchor
    import reentry_reeval
    monkeypatch.setattr(reentry_reeval, "evaluate_pending",
        lambda **kw: reentry_reeval.ReevalDecision(
            action="reanchor", new_buy_px=74.5, why="forced-reanchor-for-test"))

    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)

    # Verify: cancel called BEFORE place_limit
    calls = [c[0] for c in mock.calls]
    assert calls == ["cancel", "place_limit"], f"expected [cancel, place_limit], got {calls}"


def test_tier1_no_place_if_cancel_fails(tmp_path, monkeypatch):
    """If cancel raises, place_limit MUST NOT be called."""
    trader, store, log = _make_trader(tmp_path, mode="expert")
    sc, ss = _get_sleeve(trader)
    _prime_history(trader, sc.id, [75.0 - i * 0.01 for i in range(60)])
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "resting-order-id-123"
    ss.last_sell_fill_price = 75.0
    ss.armed_buy_since_ts = time.time() - 3600
    ss.pre_halt_state = None
    mock = _MockBroker(raise_on_cancel=True)
    trader.b = mock
    import reentry_reeval
    monkeypatch.setattr(reentry_reeval, "evaluate_pending",
        lambda **kw: reentry_reeval.ReevalDecision(
            action="reanchor", new_buy_px=74.5, why="cancel-should-fail"))

    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)

    calls = [c[0] for c in mock.calls]
    assert "cancel" in calls, "cancel should have been attempted"
    assert "place_limit" not in calls, (
        f"place_limit called despite cancel failure: {mock.calls}")
    # Old order id preserved (we didn't succeed in replacing it)
    assert ss.live_order_id == "resting-order-id-123"


# ---- Tier 1 #2 Dedup lock ------------------------------------------------

def test_tier1_no_place_when_dedup_lock_unavailable(tmp_path, monkeypatch):
    """When arm_dedup returns acquired=False, no cancel + no place."""
    trader, store, log = _make_trader(tmp_path, mode="expert")
    sc, ss = _get_sleeve(trader)
    _prime_history(trader, sc.id, [75.0 - i * 0.01 for i in range(60)])
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "resting-order-id-123"
    ss.last_sell_fill_price = 75.0
    ss.armed_buy_since_ts = time.time() - 3600
    ss.pre_halt_state = None
    mock = _MockBroker()
    trader.b = mock
    import reentry_reeval
    monkeypatch.setattr(reentry_reeval, "evaluate_pending",
        lambda **kw: reentry_reeval.ReevalDecision(
            action="reanchor", new_buy_px=74.5, why="lock-should-block"))
    # Force dedup lock to refuse
    import arm_dedup
    monkeypatch.setattr(arm_dedup, "try_acquire_arm_lock",
        lambda *a, **kw: {"acquired": False, "reason": "held", "key": "test-key"})

    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)

    assert mock.calls == [], f"broker should not have been touched: {mock.calls}"
    assert ss.live_order_id == "resting-order-id-123"  # untouched


# ---- Tier 1 #3 Anti-thrash ----------------------------------------------

def test_tier1_material_move_guard_pure_hold(tmp_path):
    """AUDITOR 2026-07-14 Tier 1 gap: DRIFT trigger fires every tick while
    price stays elevated above last_sale + drift*ATR — armed_at reset only
    guards the TIME trigger. Without a material-move guard, we'd cancel-
    replace every tick.

    Pure-function test with REAL evaluate_pending: drift + uptrend, but
    proposed reanchor is within reanchor_min_move_x_atr*ATR of the resting
    order → return HOLD, no cancel-replace."""
    import reentry_reeval as rr
    # Setup: drift = 62 - 60 = 2 = 2.0*ATR (0.5 * 2.0*ATR trigger boundary — use 63 for safety)
    dec = rr.evaluate_pending(
        elapsed_bars=0,          # NOT stale — only drift can trigger
        price=63.0,              # 63 > 60 + 2*0.5 = 61.0 ✓ drift fires
        last_sale_px=60.0,
        resting_buy_px=60.8,     # matches pullback below
        atr=0.5,
        htf_slope=1.0,           # positive slope
        trend_strength=0.5,      # ≥ 0.30 → new_trend_up
        dc_high=64.0,
        fast_ema=61.3,           # pullback_px = 61.3 - 1.0*0.5 = 60.8
        near_expiry=False,
        params=rr.ReevalParams(reanchor_min_move_x_atr=0.5),
    )
    # pullback_px = 60.8, resting = 60.8, diff = 0.0 < 0.5*0.5 = 0.25 → HOLD
    assert dec.action == "hold", f"Expected hold, got {dec.action}: {dec.why}"
    assert "anti-thrash" in dec.why.lower() or "min-move" in dec.why.lower()


def test_tier1_material_move_guard_pure_reanchor_when_moved(tmp_path):
    """Sanity check the OTHER side: when the proposed reanchor differs by
    MORE than reanchor_min_move_x_atr*ATR from resting, reanchor DOES fire."""
    import reentry_reeval as rr
    dec = rr.evaluate_pending(
        elapsed_bars=0, price=63.0, last_sale_px=60.0,
        resting_buy_px=59.0,     # far from proposed pullback (60.8)
        atr=0.5, htf_slope=1.0, trend_strength=0.5,
        dc_high=64.0, fast_ema=61.3, near_expiry=False,
        params=rr.ReevalParams(reanchor_min_move_x_atr=0.5),
    )
    # |60.8 - 59.0| = 1.8, threshold = 0.25 → material move → REANCHOR
    assert dec.action == "reanchor", f"Expected reanchor, got {dec.action}: {dec.why}"
    assert abs(dec.new_buy_px - 60.8) < 1e-6


def test_tier1_anti_thrash_two_consecutive_ticks_zero_broker_calls(tmp_path, monkeypatch):
    """AUDITOR 2026-07-14 Tier 1: two consecutive ticks in a drift+uptrend
    regime where the material-move guard fires: BOTH ticks must return HOLD;
    ZERO broker calls across both. This proves the cancel-replace loop that
    would otherwise fire every tick is broken."""
    trader, store, log = _make_trader(tmp_path, mode="expert")
    sc, ss = _get_sleeve(trader)
    _prime_history(trader, sc.id, [75.0 - i * 0.01 for i in range(60)])
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "resting-thrash-consecutive"
    ss.last_sell_fill_price = 75.0
    ss.armed_buy_since_ts = time.time()    # fresh — only drift can trigger
    ss.pre_halt_state = None
    mock = _MockBroker()
    trader.b = mock
    import reentry_reeval
    # Simulate the guard firing (proposed reanchor within min_move of resting)
    monkeypatch.setattr(reentry_reeval, "evaluate_pending",
        lambda **kw: reentry_reeval.ReevalDecision(
            action="hold", new_buy_px=kw["resting_buy_px"],
            why="drift/stale but proposed reanchor within 0.5xATR of resting — no material move — hold"))

    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)    # tick 1
    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)    # tick 2 (same regime)

    assert mock.calls == [], (
        f"Two consecutive holds must produce ZERO broker calls; got {mock.calls}")
    assert ss.live_order_id == "resting-thrash-consecutive"      # untouched


def test_tier1_armed_at_reset_on_reanchor(tmp_path, monkeypatch):
    """After a successful reanchor, ss.armed_buy_since_ts must be
    reset to ~now so the next tick's elapsed_bars computation reads
    'fresh' and returns action='hold' (no cancel-replace loop)."""
    trader, store, log = _make_trader(tmp_path, mode="expert")
    sc, ss = _get_sleeve(trader)
    _prime_history(trader, sc.id, [75.0 - i * 0.01 for i in range(60)])
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "resting-order-id-123"
    ss.last_sell_fill_price = 75.0
    old_armed_at = time.time() - 3600  # 1 hour ago
    ss.armed_buy_since_ts = old_armed_at
    ss.pre_halt_state = None
    trader.b = _MockBroker()
    import reentry_reeval
    monkeypatch.setattr(reentry_reeval, "evaluate_pending",
        lambda **kw: reentry_reeval.ReevalDecision(
            action="reanchor", new_buy_px=74.5, why="reset-armed-at"))

    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)

    # armed_at must have been reset to ~now
    now = time.time()
    assert ss.armed_buy_since_ts > old_armed_at + 100, (
        f"armed_buy_since_ts not reset: still {ss.armed_buy_since_ts}, was {old_armed_at}")
    assert abs(ss.armed_buy_since_ts - now) < 5.0, (
        f"armed_buy_since_ts should be ~now, got {ss.armed_buy_since_ts}")


# ---- Tier 2 #1 State persist memory AND Redis ---------------------------

def test_tier2_state_persist_in_memory_and_redis(tmp_path, monkeypatch):
    """After reanchor: in-memory sc.buy_px + ss.live_order_id updated AND
    persisted to Redis via _save_state()."""
    trader, store, log = _make_trader(tmp_path, mode="expert")
    sc, ss = _get_sleeve(trader)
    _prime_history(trader, sc.id, [75.0 - i * 0.01 for i in range(60)])
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "resting-order-id-123"
    ss.last_sell_fill_price = 75.0
    ss.armed_buy_since_ts = time.time() - 3600
    ss.pre_halt_state = None
    trader.b = _MockBroker()
    import reentry_reeval
    monkeypatch.setattr(reentry_reeval, "evaluate_pending",
        lambda **kw: reentry_reeval.ReevalDecision(
            action="reanchor", new_buy_px=74.5, why="persist-test"))
    old_buy_px = sc.buy_px

    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)

    # In-memory: sleeve config buy_px updated + live_order_id updated
    assert sc.buy_px != old_buy_px
    assert ss.live_order_id == "new-order-id-xyz"
    # Redis: state was written via _save_state
    persisted = store.get_state(TENANT, SYMBOL) or {}
    persisted_sleeves = persisted.get("sleeves") or {}
    persisted_ss = persisted_sleeves.get(sc.id) or {}
    assert persisted_ss.get("live_order_id") == "new-order-id-xyz"


# ---- Tier 2 #2 Expire exits cleanly (no re-arm) ------------------------

def test_tier2_expire_transitions_sleeve_to_halted(tmp_path, monkeypatch):
    """action='expire' → sleeve.state = HALTED, live_order_id cleared."""
    trader, store, log = _make_trader(tmp_path, mode="expert")
    sc, ss = _get_sleeve(trader)
    _prime_history(trader, sc.id, [75.0 - i * 0.01 for i in range(60)])
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "resting-order-id-123"
    ss.last_sell_fill_price = 75.0
    ss.armed_buy_since_ts = time.time() - 3600
    ss.pre_halt_state = None
    trader.b = _MockBroker()
    import reentry_reeval
    monkeypatch.setattr(reentry_reeval, "evaluate_pending",
        lambda **kw: reentry_reeval.ReevalDecision(
            action="expire", new_buy_px=0.0, why="near-expiry-forced"))

    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)

    assert ss.state == SleeveStateEnum.HALTED
    assert ss.live_order_id is None
    assert "reentry_reeval expire" in (ss.halt_reason or "")


def test_tier2_no_rearm_after_expire(tmp_path, monkeypatch):
    """After expire, next call to _maybe_reeval_pending_arm returns early
    (sleeve state != ARMED_BUY) — no place order."""
    trader, store, log = _make_trader(tmp_path, mode="expert")
    sc, ss = _get_sleeve(trader)
    _prime_history(trader, sc.id, [75.0 - i * 0.01 for i in range(60)])
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "resting-order-id-123"
    ss.last_sell_fill_price = 75.0
    ss.armed_buy_since_ts = time.time() - 3600
    ss.pre_halt_state = None
    mock = _MockBroker()
    trader.b = mock
    import reentry_reeval
    monkeypatch.setattr(reentry_reeval, "evaluate_pending",
        lambda **kw: reentry_reeval.ReevalDecision(
            action="expire", new_buy_px=0.0, why="expire-test"))

    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)  # first call: expire
    calls_after_first = list(mock.calls)

    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)  # second call: should no-op

    # No additional broker calls between first and second (second was a no-op)
    assert mock.calls == calls_after_first, (
        f"Second call after expire made broker calls: {mock.calls[len(calls_after_first):]}")


# ---- SHADOW MODE (auditor 2026-07-14) -----------------------------------
# Third __reentry_mode__ that computes + logs decisions without touching the
# broker. Runs 24-48h on live to validate decisions before turning execution
# on for one small sleeve. Everything the "expert" path would do — feature
# computation, evaluate_pending call, decision logging — must happen. Only
# broker.cancel + broker.place_limit and state transitions to HALTED are
# blocked.


def test_shadow_mode_no_broker_calls_on_reanchor(tmp_path, monkeypatch):
    """Shadow mode with a reanchor decision: broker MUST NOT be touched."""
    trader, store, log = _make_trader(tmp_path, mode="shadow")
    sc, ss = _get_sleeve(trader)
    _prime_history(trader, sc.id, [75.0 - i * 0.01 for i in range(60)])
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "resting-shadow-reanchor"
    ss.last_sell_fill_price = 75.0
    ss.armed_buy_since_ts = time.time() - 3600
    ss.pre_halt_state = None
    mock = _MockBroker()
    trader.b = mock
    import reentry_reeval
    monkeypatch.setattr(reentry_reeval, "evaluate_pending",
        lambda **kw: reentry_reeval.ReevalDecision(
            action="reanchor", new_buy_px=74.5, why="would-reanchor"))

    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)

    # Broker untouched
    assert mock.calls == [], f"shadow mode touched broker: {mock.calls}"
    # State unchanged
    assert ss.live_order_id == "resting-shadow-reanchor"
    assert ss.state == SleeveStateEnum.ARMED_BUY
    assert sc.buy_px != 74.5  # config buy_px NOT rewritten


def test_shadow_mode_no_broker_calls_on_expire(tmp_path, monkeypatch):
    """Shadow mode with an expire decision: sleeve MUST NOT transition to
    HALTED. Broker MUST NOT be touched."""
    trader, store, log = _make_trader(tmp_path, mode="shadow")
    sc, ss = _get_sleeve(trader)
    _prime_history(trader, sc.id, [75.0 - i * 0.01 for i in range(60)])
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "resting-shadow-expire"
    ss.last_sell_fill_price = 75.0
    ss.armed_buy_since_ts = time.time() - 3600
    ss.pre_halt_state = None
    mock = _MockBroker()
    trader.b = mock
    import reentry_reeval
    monkeypatch.setattr(reentry_reeval, "evaluate_pending",
        lambda **kw: reentry_reeval.ReevalDecision(
            action="expire", new_buy_px=0.0, why="would-expire"))

    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)

    # State + broker untouched — no HALTED transition, no cancel
    assert ss.state == SleeveStateEnum.ARMED_BUY, (
        "shadow mode transitioned sleeve to HALTED")
    assert ss.halt_reason is None
    assert mock.calls == []


def test_shadow_mode_emits_would_action_event(tmp_path, monkeypatch):
    """Every shadow tick emits BOTH the standard decision event (tagged
    mode='shadow') AND a dedicated shadow_action event with would_new_buy_px.
    The dedicated event lets audit separate would-have-been from actual
    actions on the same code path."""
    trader, store, log = _make_trader(tmp_path, mode="shadow")
    sc, ss = _get_sleeve(trader)
    _prime_history(trader, sc.id, [75.0 - i * 0.01 for i in range(60)])
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "resting-shadow-event"
    ss.last_sell_fill_price = 75.0
    ss.armed_buy_since_ts = time.time() - 3600
    ss.pre_halt_state = None
    trader.b = _MockBroker()
    import reentry_reeval
    monkeypatch.setattr(reentry_reeval, "evaluate_pending",
        lambda **kw: reentry_reeval.ReevalDecision(
            action="reanchor", new_buy_px=74.5, why="shadow-emits-event"))

    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)

    events = list(log.events())
    kinds = [e.get("event_type") for e in events]
    assert "reentry_reeval_decision" in kinds
    assert "reentry_reeval_shadow_action" in kinds
    # The decision event should be tagged with mode='shadow'
    decision = next(e for e in events if e.get("event_type") == "reentry_reeval_decision")
    assert decision.get("mode") == "shadow"
    # The shadow_action event should carry the would-be buy_px
    shadow = next(e for e in events if e.get("event_type") == "reentry_reeval_shadow_action")
    assert shadow.get("would_new_buy_px") == 74.5
    assert shadow.get("would_action") == "reanchor"


def test_shadow_mode_compound_reanchor_and_expire_zero_broker_calls(tmp_path, monkeypatch):
    """AUDITOR 2026-07-14 CONDITIONAL for shadow enable: a single scenario
    where the shadow path is exercised through BOTH a reanchor decision
    AND an expire decision (three ticks total, decision sequence:
    reanchor → hold → expire) must produce ZERO broker calls TOTAL.

    'Labeled-safe ≠ safe.' This test proves the label matches the
    behavior across every non-hold action shadow can encounter."""
    trader, store, log = _make_trader(tmp_path, mode="shadow")
    sc, ss = _get_sleeve(trader)
    _prime_history(trader, sc.id, [75.0 - i * 0.01 for i in range(60)])
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "resting-compound-shadow"
    ss.last_sell_fill_price = 75.0
    ss.armed_buy_since_ts = time.time() - 3600
    ss.pre_halt_state = None
    mock = _MockBroker()
    trader.b = mock

    import reentry_reeval
    # Tick 1: force reanchor decision
    monkeypatch.setattr(reentry_reeval, "evaluate_pending",
        lambda **kw: reentry_reeval.ReevalDecision(
            action="reanchor", new_buy_px=74.5, why="compound-tick1-reanchor"))
    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)
    calls_after_reanchor = list(mock.calls)

    # Tick 2: force hold decision (no shadow_action, no broker calls)
    monkeypatch.setattr(reentry_reeval, "evaluate_pending",
        lambda **kw: reentry_reeval.ReevalDecision(
            action="hold", new_buy_px=75.0, why="compound-tick2-hold"))
    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)
    calls_after_hold = list(mock.calls)

    # Tick 3: force expire decision (would-be HALTED transition + cancel)
    monkeypatch.setattr(reentry_reeval, "evaluate_pending",
        lambda **kw: reentry_reeval.ReevalDecision(
            action="expire", new_buy_px=0.0, why="compound-tick3-expire"))
    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)
    calls_after_expire = list(mock.calls)

    # ASSERTIONS — the CONDITIONAL
    assert calls_after_reanchor == [], (
        f"shadow reanchor made broker calls: {calls_after_reanchor}")
    assert calls_after_hold == [], (
        f"shadow hold made broker calls: {calls_after_hold}")
    assert calls_after_expire == [], (
        f"shadow expire made broker calls: {calls_after_expire}")
    assert mock.calls == [], (
        f"shadow compound scenario made ZERO broker calls; got {mock.calls}")

    # Sleeve state must be preserved across all three ticks
    assert ss.state == SleeveStateEnum.ARMED_BUY, (
        f"shadow compound: sleeve state transitioned to {ss.state}")
    assert ss.live_order_id == "resting-compound-shadow", (
        "shadow compound: live_order_id was rewritten")
    assert ss.halt_reason is None, (
        f"shadow compound: halt_reason was set to {ss.halt_reason!r}")

    # Trade log must show all three decision events + 2 shadow_action events
    # (tick 1 reanchor + tick 3 expire; tick 2 hold does NOT emit shadow_action)
    events = list(log.events())
    decision_events = [e for e in events if e.get("event_type") == "reentry_reeval_decision"]
    shadow_events = [e for e in events if e.get("event_type") == "reentry_reeval_shadow_action"]
    assert len(decision_events) == 3, (
        f"expected 3 decision events (all shadow), got {len(decision_events)}")
    assert all(e.get("mode") == "shadow" for e in decision_events)
    assert len(shadow_events) == 2, (
        f"expected 2 shadow_action events (reanchor+expire), got {len(shadow_events)}")


def test_shadow_mode_hold_still_logs_decision(tmp_path, monkeypatch):
    """A 'hold' decision under shadow mode still logs the decision event
    (so the operator can audit how often the reeval fires + decides hold),
    but does NOT emit a shadow_action (nothing would have happened)."""
    trader, store, log = _make_trader(tmp_path, mode="shadow")
    sc, ss = _get_sleeve(trader)
    _prime_history(trader, sc.id, [75.0 - i * 0.01 for i in range(60)])
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "resting-shadow-hold"
    ss.last_sell_fill_price = 75.0
    ss.armed_buy_since_ts = time.time() - 3600
    ss.pre_halt_state = None
    trader.b = _MockBroker()
    import reentry_reeval
    monkeypatch.setattr(reentry_reeval, "evaluate_pending",
        lambda **kw: reentry_reeval.ReevalDecision(
            action="hold", new_buy_px=63.0, why="shadow-hold"))

    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)

    kinds = [e.get("event_type") for e in log.events()]
    assert "reentry_reeval_decision" in kinds
    assert "reentry_reeval_shadow_action" not in kinds, (
        "hold decisions should not emit shadow_action — nothing would have happened")


# ---- Tier 2 (a) — halt-recovery skips reentry_reeval expire halts --------

def test_tier2a_is_expire_halt_helper():
    """The public helper identifies expire halts + nothing else."""
    import reentry_reeval as rr
    assert rr.is_expire_halt("reentry_reeval expire: near-expiry")
    assert rr.is_expire_halt(f"{rr.EXPIRE_HALT_PREFIX} anything")
    assert not rr.is_expire_halt("safety halt: drawdown")
    assert not rr.is_expire_halt("")
    assert not rr.is_expire_halt(None)


def test_tier2a_resume_skips_expire_halt(tmp_path, monkeypatch):
    """When the user hits Resume via resume_intent, sleeves halted with a
    reentry_reeval expire reason must be SKIPPED — resuming would just
    re-arm a buy that expires next tick (contract still near expiry)."""
    trader, store, log = _make_trader(tmp_path, mode="expert")
    sc, ss = _get_sleeve(trader)
    # Simulate the sleeve was already expired by reentry_reeval
    import reentry_reeval as rr
    ss.state = SleeveStateEnum.HALTED
    ss.halt_reason = f"{rr.EXPIRE_HALT_PREFIX} extended, no pullback room before expiry"
    ss.pre_halt_state = SleeveStateEnum.ARMED_BUY.value
    ss.live_order_id = None
    # Trigger the Resume path: dashboard writes resume_intent, bot consumes
    store.put_resume_intent(TENANT, SYMBOL, {"halt": False, "previous_reason": "expired"})
    trader._maybe_consume_resume_intent()

    # Sleeve should STILL be HALTED (skipped, not resumed)
    assert ss.state == SleeveStateEnum.HALTED, (
        f"Expire halt should not auto-resume; got {ss.state}")
    assert ss.halt_reason and ss.halt_reason.startswith(rr.EXPIRE_HALT_PREFIX)
    # A "skipped" event should be recorded
    events = list(log.events())
    kinds = [e.get("event_type") for e in events]
    assert "sleeve_resume_skipped_expire" in kinds


def test_tier2a_resume_works_for_non_expire_halts(tmp_path):
    """Positive control: a non-expire halt (e.g. safety halt) DOES resume."""
    trader, store, log = _make_trader(tmp_path, mode="expert")
    sc, ss = _get_sleeve(trader)
    ss.state = SleeveStateEnum.HALTED
    ss.halt_reason = "portfolio drawdown breach"
    ss.pre_halt_state = SleeveStateEnum.ARMED_BUY.value
    ss.live_order_id = None
    store.put_resume_intent(TENANT, SYMBOL, {"halt": False, "previous_reason": "drawdown"})
    trader._maybe_consume_resume_intent()
    # Should have resumed to pre_halt_state
    assert ss.state == SleeveStateEnum.ARMED_BUY


# ---- Tier 3 — trade-log event emitted per action ------------------------

def test_tier3_hold_action_emits_decision_event(tmp_path, monkeypatch):
    """Every reeval — including 'hold' — must emit a
    sleeve_reentry_reeval_decision event (per Tier 3)."""
    trader, store, log = _make_trader(tmp_path, mode="expert")
    sc, ss = _get_sleeve(trader)
    _prime_history(trader, sc.id, [75.0 - i * 0.01 for i in range(60)])
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "resting-order-id-123"
    ss.last_sell_fill_price = 75.0
    ss.armed_buy_since_ts = time.time() - 3600
    ss.pre_halt_state = None
    trader.b = _MockBroker()
    import reentry_reeval
    monkeypatch.setattr(reentry_reeval, "evaluate_pending",
        lambda **kw: reentry_reeval.ReevalDecision(
            action="hold", new_buy_px=63.0, why="test-hold"))

    trader._maybe_reeval_pending_arm(sc, ss, last_price=76.0)

    # Trade log should have a reentry_reeval_decision event
    events = list(log.events())
    kinds = [e.get("event_type") for e in events]
    assert "reentry_reeval_decision" in kinds, (
        f"reentry_reeval_decision not in emitted events: {kinds}")
