"""Unit tests for the RateLimitController.

Covers:
  - Priority ordering (CRITICAL always passes, LOW backs off first)
  - Sliding-window rate tracking
  - 429 backoff behavior
  - Header-based limit updates
  - Fail-open on internal error
"""
from __future__ import annotations

import time

import pytest

from rate_limit_controller import (
    EndpointKind,
    Priority,
    RateLimitController,
    get_controller,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset the global singleton between tests to avoid leaking state."""
    yield
    ctrl = get_controller()
    ctrl.reset()


def test_critical_always_passes():
    ctrl = RateLimitController(public_limit=5.0, private_limit=5.0)
    # Fill up the budget with LOW calls until they start being denied
    for _ in range(20):
        ctrl.acquire(Priority.LOW, EndpointKind.PRIVATE)
    # CRITICAL should still pass even if we're above the docs' limit
    assert ctrl.acquire(Priority.CRITICAL, EndpointKind.PRIVATE) is True


def test_low_priority_denied_when_utilization_high():
    ctrl = RateLimitController(public_limit=10.0, private_limit=10.0)
    # Fill 60% of budget with approved calls
    for _ in range(6):
        assert ctrl.acquire(Priority.HIGH, EndpointKind.PRIVATE) is True
    # LOW is gated at 50% — should be denied now
    assert ctrl.acquire(Priority.LOW, EndpointKind.PRIVATE) is False


def test_medium_priority_denied_at_70_pct():
    ctrl = RateLimitController(public_limit=10.0, private_limit=10.0)
    for _ in range(7):
        ctrl.acquire(Priority.HIGH, EndpointKind.PRIVATE)
    # MEDIUM gated at 70% — right at the boundary should reject
    assert ctrl.acquire(Priority.MEDIUM, EndpointKind.PRIVATE) is False


def test_high_priority_still_passes_at_80_pct():
    ctrl = RateLimitController(public_limit=10.0, private_limit=10.0)
    for _ in range(8):
        ctrl.acquire(Priority.HIGH, EndpointKind.PRIVATE)
    # HIGH gated at 95% — one more should still pass
    assert ctrl.acquire(Priority.HIGH, EndpointKind.PRIVATE) is True


def test_429_triggers_backoff_for_noncritical():
    ctrl = RateLimitController()
    ctrl.record_response(EndpointKind.PRIVATE, status_code=429)
    # In backoff — LOW/MEDIUM/HIGH all denied
    assert ctrl.acquire(Priority.HIGH, EndpointKind.PRIVATE) is False
    assert ctrl.acquire(Priority.MEDIUM, EndpointKind.PRIVATE) is False
    assert ctrl.acquire(Priority.LOW, EndpointKind.PRIVATE) is False
    # But CRITICAL always passes
    assert ctrl.acquire(Priority.CRITICAL, EndpointKind.PRIVATE) is True


def test_backoff_ramps_on_repeated_429():
    ctrl = RateLimitController()
    initial = ctrl._current_backoff[EndpointKind.PRIVATE]
    ctrl.record_response(EndpointKind.PRIVATE, status_code=429)
    after_1 = ctrl._current_backoff[EndpointKind.PRIVATE]
    ctrl.record_response(EndpointKind.PRIVATE, status_code=429)
    after_2 = ctrl._current_backoff[EndpointKind.PRIVATE]
    assert after_1 > initial
    assert after_2 > after_1
    # Capped at max
    for _ in range(10):
        ctrl.record_response(EndpointKind.PRIVATE, status_code=429)
    assert ctrl._current_backoff[EndpointKind.PRIVATE] <= ctrl.BACKOFF_MAX_SECS


def test_successful_response_resets_backoff():
    ctrl = RateLimitController()
    ctrl.record_response(EndpointKind.PRIVATE, status_code=429)
    ctrl.record_response(EndpointKind.PRIVATE, status_code=429)
    assert ctrl._current_backoff[EndpointKind.PRIVATE] > ctrl.BACKOFF_INITIAL_SECS
    ctrl.record_response(EndpointKind.PRIVATE, status_code=200)
    assert ctrl._current_backoff[EndpointKind.PRIVATE] == ctrl.BACKOFF_INITIAL_SECS


def test_header_updates_limit():
    ctrl = RateLimitController(public_limit=10.0, private_limit=30.0)
    # Coinbase-style X-RateLimit-Limit header should update our local view
    ctrl.record_response(
        EndpointKind.PRIVATE,
        status_code=200,
        headers={"X-RateLimit-Limit": "50"},
    )
    assert ctrl._limits[EndpointKind.PRIVATE] == 50.0


def test_sliding_window_prunes_old_entries():
    ctrl = RateLimitController(private_limit=100.0, window_secs=0.1)
    ctrl.acquire(Priority.HIGH, EndpointKind.PRIVATE)
    ctrl.acquire(Priority.HIGH, EndpointKind.PRIVATE)
    assert ctrl._current_rate(EndpointKind.PRIVATE, time.time()) > 0
    time.sleep(0.15)
    # Old entries should be pruned; rate returns to 0
    assert ctrl._current_rate(EndpointKind.PRIVATE, time.time()) == 0


def test_public_and_private_pools_are_independent():
    ctrl = RateLimitController(public_limit=5.0, private_limit=100.0)
    # Fill public
    for _ in range(5):
        ctrl.acquire(Priority.HIGH, EndpointKind.PUBLIC)
    # Public HIGH should still allow one more (gate at 95%)
    # Private HIGH should be fine — different pool
    assert ctrl.acquire(Priority.HIGH, EndpointKind.PRIVATE) is True


def test_stats_reports_expected_fields():
    ctrl = RateLimitController()
    ctrl.acquire(Priority.HIGH, EndpointKind.PRIVATE)
    ctrl.record_response(EndpointKind.PRIVATE, status_code=429)
    s = ctrl.stats()
    assert "public_rate_per_sec" in s
    assert "private_rate_per_sec" in s
    assert "private_util_pct" in s
    assert "counts" in s
    assert s["counts"]["429_seen"] == 1


def test_singleton_returns_same_instance():
    a = get_controller()
    b = get_controller()
    assert a is b


def test_fail_open_on_bad_input():
    ctrl = RateLimitController()
    # Unknown kind should fall back to PUBLIC and not raise
    result = ctrl.acquire(Priority.LOW, "nonexistent_pool")
    assert result is True  # Fell through to PUBLIC, which was empty


def test_wait_and_acquire_returns_true_for_critical_immediately():
    ctrl = RateLimitController()
    ctrl.record_response(EndpointKind.PRIVATE, status_code=429)
    # CRITICAL bypasses the wait
    start = time.time()
    result = ctrl.wait_and_acquire(Priority.CRITICAL, EndpointKind.PRIVATE, timeout_secs=5.0)
    elapsed = time.time() - start
    assert result is True
    assert elapsed < 0.1  # Should be immediate, not waiting
