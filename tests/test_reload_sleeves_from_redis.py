"""Tests for SwingTrader._reload_sleeves_from_redis — kills the SLR/HYP/PT/XLP
in-memory clobber class.

Invariants:
  - External Redis write is honored: bot's in-memory sleeve is REPLACED by
    the Redis version on the next tick's reload
  - Sleeves in memory but ABSENT from Redis are preserved (new sleeves the
    bot just added; config-driven cleanup runs separately)
  - Malformed sleeve entries in Redis are skipped, not raised
  - Redis read exception fails-open (in-memory untouched; retries next tick)
  - Missing state entry entirely (fresh product) is a no-op
"""
from __future__ import annotations

import pytest

from swing_leg import SwingTrader, SleeveState


class _MinimalStore:
    """Minimal StateStore Protocol impl."""
    def __init__(self):
        self._state: dict[tuple, dict] = {}
        self._config: dict[tuple, dict] = {}
        self._raise_on_state = False

    def get_state(self, tenant, symbol):
        if self._raise_on_state:
            raise ConnectionError("simulated Redis down")
        return self._state.get((tenant, symbol))

    def put_state(self, tenant, symbol, state):
        self._state[(tenant, symbol)] = state

    def get_config(self, tenant, symbol):
        return self._config.get((tenant, symbol))

    def put_config(self, tenant, symbol, config):
        self._config[(tenant, symbol)] = config

    def get_snapshot(self, tenant, symbol): return None
    def put_snapshot(self, tenant, symbol, snap): pass


def _make_trader(store):
    """Minimal trader instance to exercise _reload_sleeves_from_redis in isolation.
    Bypasses __init__ (which would want a broker etc.) via __new__."""
    t = SwingTrader.__new__(SwingTrader)
    t.store = store
    t.tenant_id = "adam-live"
    t.symbol = "TEST-CDE"
    # Minimal state — just needs a .sleeves dict for the reload to write to
    from swing_leg import SwingState
    t.s = SwingState()
    t.s.sleeves = {}
    return t


def test_external_redis_write_is_honored():
    """The bug this fix closes: bot has sleeve HALTED in memory, diag writes
    ARMED_BUY to Redis, reload picks it up on next tick."""
    store = _MinimalStore()
    trader = _make_trader(store)
    # In-memory: sleeve in HALTED state
    from swing_leg import State
    trader.s.sleeves["scan-abc"] = SleeveState(
        id="scan-abc", state=State.HALTED, cycles=0, realized_pnl=0.0,
        halt_reason="stuck", own_avg_entry=None,
        resting_stop_oid="old-oid",
    )
    # Redis: same sleeve, but cleaned up (diag force-credit result)
    store.put_state("adam-live", "TEST-CDE", {
        "sleeves": {
            "scan-abc": {
                "state": "ARMED_BUY",
                "cycles": 1,
                "realized_pnl": 0.75,
                "halt_reason": None,
                "own_avg_entry": None,
                "resting_stop_oid": None,
            }
        }
    })
    trader._reload_sleeves_from_redis()
    ss = trader.s.sleeves["scan-abc"]
    assert ss.state == State.ARMED_BUY
    assert ss.cycles == 1
    assert ss.realized_pnl == 0.75
    assert ss.halt_reason is None
    assert ss.resting_stop_oid is None


def test_in_memory_only_sleeve_preserved():
    """Sleeve in memory but not yet in Redis (newly added) is preserved."""
    store = _MinimalStore()
    trader = _make_trader(store)
    from swing_leg import State
    trader.s.sleeves["scan-new"] = SleeveState(
        id="scan-new", state=State.ARMED_BUY, cycles=0,
    )
    # Redis has NO state for this product yet
    trader._reload_sleeves_from_redis()
    # New sleeve should still be there
    assert "scan-new" in trader.s.sleeves
    assert trader.s.sleeves["scan-new"].state == State.ARMED_BUY


def test_malformed_entry_is_skipped():
    """A garbage entry in Redis doesn't crash the reload; other entries
    still get applied."""
    store = _MinimalStore()
    trader = _make_trader(store)
    from swing_leg import State
    trader.s.sleeves["scan-good"] = SleeveState(id="scan-good", state=State.ARMED_BUY)
    store.put_state("adam-live", "TEST-CDE", {
        "sleeves": {
            "scan-bad": "not-a-dict",            # will be skipped
            "scan-good": {"state": "HALTED"},    # will be applied
        }
    })
    trader._reload_sleeves_from_redis()
    assert trader.s.sleeves["scan-good"].state == State.HALTED
    # scan-bad was skipped, not added
    assert "scan-bad" not in trader.s.sleeves


def test_redis_read_exception_fails_open():
    """If Redis is unreachable, reload leaves in-memory state alone.
    Bot keeps ticking (avoids total-halt on transient Redis issues)."""
    store = _MinimalStore()
    trader = _make_trader(store)
    from swing_leg import State
    trader.s.sleeves["scan-abc"] = SleeveState(
        id="scan-abc", state=State.ARMED_BUY, cycles=5,
    )
    store._raise_on_state = True
    # Should not raise
    trader._reload_sleeves_from_redis()
    # In-memory untouched
    assert trader.s.sleeves["scan-abc"].state == State.ARMED_BUY
    assert trader.s.sleeves["scan-abc"].cycles == 5


def test_no_redis_entry_is_noop():
    """Fresh product with no state block yet — reload should not crash."""
    store = _MinimalStore()
    trader = _make_trader(store)
    trader.s.sleeves = {}
    trader._reload_sleeves_from_redis()
    assert trader.s.sleeves == {}


def test_sleeves_scope_missing_from_state_is_noop():
    """State exists but has no 'sleeves' key — reload is a no-op."""
    store = _MinimalStore()
    trader = _make_trader(store)
    from swing_leg import State
    trader.s.sleeves["scan-abc"] = SleeveState(id="scan-abc", state=State.HALTED)
    store.put_state("adam-live", "TEST-CDE", {"state": "ARMED_SELL"})  # no sleeves key
    trader._reload_sleeves_from_redis()
    # In-memory sleeve unchanged
    assert trader.s.sleeves["scan-abc"].state == State.HALTED
