"""Tests for arm_dedup — Redis-backed cross-process arm lock.

Covers the invariants that matter after the 2026-07-14 multi-writer bug:
  - Two acquires at the same (tenant, symbol, side, price) → second one
    fails within the TTL window.
  - Different price → different key → both acquire OK.
  - Redis unavailable → fail-closed (acquired=False, reason='unavailable').
  - File-backed store → no-op (returns acquired=True).
"""
from __future__ import annotations

import pytest

import arm_dedup


class _FakeRedis:
    def __init__(self, fail: bool = False):
        self._store: dict[str, tuple[str, float]] = {}
        self._fail = fail

    def set(self, key, value, nx=False, ex=None):
        if self._fail:
            raise ConnectionError("fake redis down")
        if nx and key in self._store:
            return None
        self._store[key] = (value, ex or 0)
        return True

    def delete(self, key):
        if self._fail:
            raise ConnectionError("fake redis down")
        self._store.pop(key, None)


class _FakeStoreRedis:
    def __init__(self, fail: bool = False):
        self._r = _FakeRedis(fail=fail)


class _FakeStoreFile:
    pass  # no `_r` attribute — mimics file-backed store


def test_first_acquire_succeeds():
    store = _FakeStoreRedis()
    r = arm_dedup.try_acquire_arm_lock(store, "adam-live", "NOL-20JUL26-CDE",
                                        "SELL", 74.5, 0.01)
    assert r["acquired"] is True
    assert r["key"].endswith(":SELL:7450")


def test_second_acquire_same_key_fails():
    store = _FakeStoreRedis()
    r1 = arm_dedup.try_acquire_arm_lock(store, "adam-live", "NOL", "SELL", 74.5, 0.01)
    r2 = arm_dedup.try_acquire_arm_lock(store, "adam-live", "NOL", "SELL", 74.5, 0.01)
    assert r1["acquired"] is True
    assert r2["acquired"] is False
    assert r2["reason"] == "held"


def test_different_price_different_lock():
    store = _FakeStoreRedis()
    r1 = arm_dedup.try_acquire_arm_lock(store, "adam-live", "NOL", "SELL", 74.5, 0.01)
    r2 = arm_dedup.try_acquire_arm_lock(store, "adam-live", "NOL", "SELL", 74.6, 0.01)
    assert r1["acquired"] is True
    assert r2["acquired"] is True


def test_different_side_different_lock():
    store = _FakeStoreRedis()
    r1 = arm_dedup.try_acquire_arm_lock(store, "adam-live", "NOL", "BUY", 74.5, 0.01)
    r2 = arm_dedup.try_acquire_arm_lock(store, "adam-live", "NOL", "SELL", 74.5, 0.01)
    assert r1["acquired"] is True
    assert r2["acquired"] is True


def test_different_symbol_different_lock():
    store = _FakeStoreRedis()
    r1 = arm_dedup.try_acquire_arm_lock(store, "adam-live", "NOL", "SELL", 74.5, 0.01)
    r2 = arm_dedup.try_acquire_arm_lock(store, "adam-live", "SLR", "SELL", 74.5, 0.01)
    assert r1["acquired"] is True
    assert r2["acquired"] is True


def test_redis_unavailable_fails_closed():
    """The critical safety invariant: if Redis can't be reached, we do NOT
    acquire the lock (fail-closed). Caller must refuse to arm."""
    store = _FakeStoreRedis(fail=True)
    r = arm_dedup.try_acquire_arm_lock(store, "adam-live", "NOL", "SELL", 74.5, 0.01)
    assert r["acquired"] is False
    assert r["reason"] == "unavailable"
    assert "error" in r


def test_file_backed_store_is_noop():
    """Dev / paper-file-backed store has no _r — the lock silently succeeds
    (no cross-process concern locally). Never happens on Render/prod Redis."""
    store = _FakeStoreFile()
    r = arm_dedup.try_acquire_arm_lock(store, "adam", "SLR", "BUY", 60.0, 0.005)
    assert r["acquired"] is True
    assert r["reason"] == "no-redis-noop"


def test_px_ticks_normalizes_float_precision():
    """Two writers computing the same target price via different float
    paths (e.g. 74.5 vs 74.4999999999999) must land on the SAME lock key."""
    k1 = arm_dedup.make_lock_key("adam", "NOL", "SELL", 74.5, 0.01)
    k2 = arm_dedup.make_lock_key("adam", "NOL", "SELL", 74.5000000001, 0.01)
    assert k1 == k2


def test_release_after_acquire():
    store = _FakeStoreRedis()
    r1 = arm_dedup.try_acquire_arm_lock(store, "adam-live", "NOL", "SELL", 74.5, 0.01)
    assert r1["acquired"] is True
    assert arm_dedup.release_arm_lock(store, r1["key"]) is True
    # After release, next acquire should succeed
    r2 = arm_dedup.try_acquire_arm_lock(store, "adam-live", "NOL", "SELL", 74.5, 0.01)
    assert r2["acquired"] is True


def test_release_never_raises_on_redis_error():
    store = _FakeStoreRedis(fail=True)
    # Even when Redis is down, release must not raise; returns False.
    assert arm_dedup.release_arm_lock(store, "some-key") is False


def test_release_none_key_is_noop():
    store = _FakeStoreRedis()
    assert arm_dedup.release_arm_lock(store, None) is True
