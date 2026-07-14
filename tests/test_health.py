"""Tests for health — background-job silent-failure tracker.

Two invariants matter here (auditor 2026-07-14 review):
  1. record_ok / record_error MUST NOT raise, ever. A health-emit failure
     cannot be allowed to break the wrapped site.
  2. State written to __health__ scope is readable via get_health().
"""
from __future__ import annotations

import health


class _FakeStore:
    def __init__(self, fail_state: bool = False):
        self._data: dict = {}
        self._fail_state = fail_state

    def get_state(self, tenant, key):
        if self._fail_state:
            raise RuntimeError("fake store down")
        return (self._data.get(tenant) or {}).get(key)

    def put_state(self, tenant, key, value):
        if self._fail_state:
            raise RuntimeError("fake store down")
        self._data.setdefault(tenant, {})[key] = value


class _FakeTradeLog:
    def __init__(self, fail: bool = False):
        self.events: list[dict] = []
        self._fail = fail

    def record(self, event_type, **payload):
        if self._fail:
            raise RuntimeError("fake trade-log down")
        self.events.append({"event_type": event_type, **payload})
        return {"event_type": event_type, **payload}


def test_record_ok_writes_component_state():
    store = _FakeStore()
    health.record_ok(store, "reconcile", "adam-live")
    comps = health.get_health(store, "adam-live")
    assert "reconcile" in comps
    assert "last_ok_ts" in comps["reconcile"]
    assert comps["reconcile"]["last_ok_ts"] > 0


def test_record_error_writes_component_state_and_trade_log():
    store = _FakeStore()
    log = _FakeTradeLog()
    exc = ValueError("something broke")
    health.record_error(store, "expert_guard", "adam-live", exc, trade_log=log)

    comps = health.get_health(store, "adam-live")
    assert "expert_guard" in comps
    assert comps["expert_guard"]["last_error_type"] == "ValueError"
    assert comps["expert_guard"]["last_error_message"] == "something broke"

    assert len(log.events) == 1
    assert log.events[0]["event_type"] == "expert_guard_error"
    assert log.events[0]["error_type"] == "ValueError"
    assert log.events[0]["tenant"] == "adam-live"


def test_multiple_components_coexist():
    store = _FakeStore()
    health.record_ok(store, "reconcile", "adam-live")
    health.record_ok(store, "expert_guard", "adam-live")
    health.record_error(store, "portfolio_risk_tick", "adam-live",
                        RuntimeError("halt path bad"))

    comps = health.get_health(store, "adam-live")
    assert set(comps.keys()) == {"reconcile", "expert_guard", "portfolio_risk_tick"}
    assert "last_ok_ts" in comps["reconcile"]
    assert "last_ok_ts" in comps["expert_guard"]
    assert "last_error_ts" in comps["portfolio_risk_tick"]


def test_record_ok_never_raises_even_when_store_broken():
    """Critical invariant: health-emit failure must not propagate."""
    store = _FakeStore(fail_state=True)
    # Would raise inside if not defensive:
    health.record_ok(store, "reconcile", "adam-live")   # no raise
    health.record_ok(store, "reconcile", "adam-live")   # still no raise


def test_record_error_never_raises_even_when_store_broken():
    store = _FakeStore(fail_state=True)
    log = _FakeTradeLog()
    exc = RuntimeError("wrapped site's error")
    # Both the state write AND trade-log record must not propagate.
    health.record_error(store, "reconcile", "adam-live", exc, trade_log=log)
    # The trade-log record path should still have run since it's independent
    # of the state-write failure:
    assert len(log.events) == 1


def test_record_error_never_raises_even_when_trade_log_broken():
    store = _FakeStore()
    log = _FakeTradeLog(fail=True)
    exc = RuntimeError("underlying failure")
    # log.record raises internally; must not propagate.
    health.record_error(store, "reconcile", "adam-live", exc, trade_log=log)
    # State should still have been written:
    comps = health.get_health(store, "adam-live")
    assert "reconcile" in comps
    assert comps["reconcile"]["last_error_message"] == "underlying failure"


def test_record_error_no_trade_log_is_fine():
    store = _FakeStore()
    exc = RuntimeError("no log wired")
    health.record_error(store, "reconcile", "adam-live", exc)
    comps = health.get_health(store, "adam-live")
    assert "reconcile" in comps


def test_get_health_never_raises_on_broken_store():
    store = _FakeStore(fail_state=True)
    result = health.get_health(store, "adam-live")
    assert result == {}


def test_error_message_truncated_at_500_chars():
    store = _FakeStore()
    long_msg = "x" * 1000
    health.record_error(store, "reconcile", "adam-live", RuntimeError(long_msg))
    comps = health.get_health(store, "adam-live")
    assert len(comps["reconcile"]["last_error_message"]) == 500
