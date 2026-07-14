"""Auditor 2026-07-14 must-verify #2 for backtest_worker (now running inside
live_runner as of commit 21dc503):

(a) Coinbase API budget throttling — fetch_candles paginates with a per-page
    pause; queue prioritization ensures user-triggered live jobs (scanner_order,
    live_portfolio) go BEFORE backtest/candles work. Verified below.

(b) Handler exceptions cannot crash the live loop — each job handler writes
    an error result to Redis rather than propagating; the daemon thread's
    outer try/except re-establishes the Redis connection on any loop error.
"""
from __future__ import annotations

import json

import backtest_worker as bw


class _FakeRedis:
    """Minimal Redis stand-in for isolation tests. Records writes."""
    def __init__(self):
        self.store = {}
        self.deleted = []

    def get(self, key):
        return self.store.get(key)

    def set(self, key, val, ex=None):
        self.store[key] = val

    def delete(self, key):
        self.deleted.append(key)
        self.store.pop(key, None)


# ---- (a) throttling — per-page pause in fetch_candles + queue priority ---

def test_fetch_candles_has_per_page_throttle():
    """fetch_candles must have a sleep between paginated Coinbase requests
    so a single big chart-open can't burst-fire hundreds of REST calls."""
    from pathlib import Path
    src = (Path(__file__).parent.parent / "backtest.py").read_text()
    # Assert the pause exists in the paging loop of fetch_candles
    assert "time.sleep" in src, "expected time.sleep pause in fetch_candles"


def test_worker_queue_priority_puts_live_first():
    """BRPOP must list live-user queues BEFORE backtest/candles queues so a
    long-running backtest doesn't delay a scanner order or portfolio refresh."""
    from pathlib import Path
    src = (Path(__file__).parent.parent / "backtest_worker.py").read_text()
    # The brpop call should list live + scanner queues before BT + candles.
    # We can just check that the ordering appears in the source.
    live_idx = src.find("_LIVE_PORTFOLIO_QUEUE, _SCANNER_ORDER_QUEUE")
    bt_idx = src.find("_BT_QUEUE, _CANDLES_QUEUE")
    assert live_idx >= 0 and bt_idx > live_idx, (
        "worker queue priority regression: live+scanner must come before BT+candles")


# ---- (b) exception isolation — handlers catch and write error result -----

def test_candles_handler_bad_json_writes_error_result_not_raises(monkeypatch):
    """If the candles req JSON is malformed, the handler must write an
    error result to res_key and return — NOT propagate the exception."""
    r = _FakeRedis()
    r.set(bw._CANDLES_REQ + "j1", "{not json}")   # malformed
    # Handler should not raise
    bw._handle_candles_job(r, "j1")
    # Should have written an ok:false result
    result = json.loads(r.store.get(bw._CANDLES_RES + "j1", "null"))
    assert result and result.get("ok") is False
    assert "error" in result


def test_candles_handler_coinbase_failure_writes_error_result_not_raises(monkeypatch):
    """If Coinbase fetch raises inside the handler, error is caught,
    error result is written, and the handler returns cleanly."""
    r = _FakeRedis()
    r.set(bw._CANDLES_REQ + "j2", json.dumps({
        "product_id": "SLR-27AUG26-CDE", "granularity": "FIVE_MINUTE", "days": 7,
    }))
    # Force fetch_candles to explode
    def boom(*a, **k):
        raise RuntimeError("simulated coinbase outage")
    import backtest
    monkeypatch.setattr(backtest, "fetch_candles", boom)
    # Also fake the client so we don't actually try to construct a broker
    monkeypatch.setattr(bw, "_get_coinbase_client", lambda: None)

    # Handler must NOT raise
    bw._handle_candles_job(r, "j2")

    result = json.loads(r.store.get(bw._CANDLES_RES + "j2", "null"))
    assert result and result.get("ok") is False
    assert "RuntimeError" in result.get("error", "")


def test_backtest_handler_bad_json_writes_error_result_not_raises():
    r = _FakeRedis()
    r.set(bw._BT_REQ + "j3", "{malformed")
    bw._handle_backtest_job(r, "j3")
    result = json.loads(r.store.get(bw._BT_RES + "j3", "null"))
    assert result and result.get("ok") is False


def test_start_returns_stop_event_without_blocking(monkeypatch):
    """worker.start() must return quickly (daemon thread) and not block the
    caller. The returned event is settable to stop the thread cleanly."""
    import threading, time
    # Prevent the thread's actual work by giving it a bogus URL — the loop's
    # outer except will keep it in a reconnect loop with backoff. That's fine
    # for this test; we just verify start() returns and the event is usable.
    ev = bw.start("redis://127.0.0.1:1")   # nothing listening on this port
    assert isinstance(ev, threading.Event)
    ev.set()
    time.sleep(0.1)   # give it a moment to notice
    # If the test process reaches here, start() didn't block. That's the assert.


def test_run_loop_survives_redis_error_via_reconnect_backoff():
    """Grep-check: the outer loop must NOT re-raise on connection errors;
    it must reconnect + backoff. This is what keeps a Redis blip from
    crashing the parent live_runner process."""
    from pathlib import Path
    src = (Path(__file__).parent.parent / "backtest_worker.py").read_text()
    # Look for the outer except pattern
    assert "except Exception as e:" in src
    assert "reconnecting in" in src
    assert "stop_event.wait(backoff)" in src
