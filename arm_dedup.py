"""Redis-backed dedup lock for order arming (crew).

Purpose
-------
Robustness follow-up to the multi-writer bug of 2026-07-14, in which
`silver-swing-bot-paper` (with SWING_LIVE_ENGINE=1) and `silver-swing-bot-live`
BOTH wrote to the adam-live scope, and both placed the same arm order on
Coinbase. The in-process guard at swing_leg.py:2423
(`if not ss.live_order_id`) is CORRECT within a single process but has
no visibility into a second process's state — each process passes its
own guard and both fire orders. Result: duplicate SLVR sells at $65.25.

This module adds a cross-process lock via Redis SETNX. Any arm-order
placement first attempts to acquire a lock keyed on
`(tenant, symbol, side, price_ticks)` with a short TTL. If the lock is
already held, ANOTHER writer is placing (or has just placed) the same
order — skip. If Redis is unreachable, we FAIL CLOSED — block the arm
and emit a loud health event, because losing a cycle is safer than
double-placing a real-money order.

Design rules
------------
1. **On top of, not instead of** the in-process guard at swing_leg.py:2423.
   Caller must still check `not ss.live_order_id` first. This lock is the
   cross-process seatbelt AFTER that guard passes.
2. **Fail-closed** on Redis errors. Never place an arm if the dedup lock
   cannot be verified. Emit `arm_blocked_dedup_lock_unavailable` so the
   operator can restore Redis or manually intervene.
3. **Key includes price_ticks** — a genuine reanchor to a new price
   creates a different lock key and is permitted. The 2423 guard already
   handles the case where an existing order is at the SAME price.
4. **TTL 30s** — long enough for a normal arm-place round-trip to
   complete; short enough that a crashed process's stale lock clears
   within seconds. A 30s TTL means at most ~1 stale duplicate per lock
   expiry window in a two-writer failure mode — which is why the 2423
   guard has to stay.

Reference
---------
Standard "Redis SETNX distributed lock" pattern; see Redis docs on SET
with NX + EX flags. Not the Redlock algorithm (that's for multi-master
Redis; overkill for our single Valkey instance).
"""
from __future__ import annotations

import time
from typing import Optional


DEFAULT_TTL_SECS = 30
LOCK_KEY_PREFIX = "swing-lock:arm"


def _redis_from_store(store) -> Optional[object]:
    """Extract the redis client from a RedisJsonStore. Returns None for
    file-backed stores (paper/dev), which means the dedup lock is a no-op
    in dev (safe — no cross-process concern locally)."""
    return getattr(store, "_r", None)


def _px_ticks(price: float, tick_size: float) -> int:
    """Convert a price to an integer tick count. Prevents float-precision
    mismatch in the lock key when two writers compute the same target
    price via slightly different float paths."""
    ts = float(tick_size) if tick_size and tick_size > 0 else 0.0001
    return int(round(float(price) / ts))


def make_lock_key(tenant: str, symbol: str, side: str,
                  price: float, tick_size: float) -> str:
    """Build the Redis key for an arm dedup lock."""
    return (f"{LOCK_KEY_PREFIX}:{tenant}:{symbol}:{side.upper()}:"
            f"{_px_ticks(price, tick_size)}")


def try_acquire_arm_lock(store, tenant: str, symbol: str, side: str,
                         price: float, tick_size: float,
                         ttl_secs: int = DEFAULT_TTL_SECS) -> dict:
    """Attempt to acquire the arm lock for this (tenant, symbol, side, price)
    combo. Returns:
        {"acquired": True,  "key": ..., "value": ...}      # go ahead, place order
        {"acquired": False, "key": ..., "reason": "held"}  # another writer is placing
        {"acquired": False, "key": ..., "reason": "unavailable", "error": ...}
                                                            # Redis error — FAIL CLOSED

    Never raises. Caller MUST check `acquired` before placing an order.
    On `unavailable`, caller MUST refuse the arm and emit a health event."""
    r = _redis_from_store(store)
    if r is None:
        # File-backed store (paper/dev) — no cross-process concern here, so
        # the lock is a no-op. Never happens on Render / prod Redis.
        return {"acquired": True, "key": None, "reason": "no-redis-noop"}
    key = make_lock_key(tenant, symbol, side, price, tick_size)
    # Value = short-lived unique-ish marker; used only for observability if
    # we later add release-by-value. Not required for SETNX semantics.
    value = f"{int(time.time())}:{tenant}"
    try:
        # SET key value NX EX ttl → returns True if acquired, None if key exists
        got = r.set(key, value, nx=True, ex=int(ttl_secs))
        if got:
            return {"acquired": True, "key": key, "value": value}
        return {"acquired": False, "key": key, "reason": "held"}
    except Exception as e:
        return {"acquired": False, "key": key,
                "reason": "unavailable", "error": f"{type(e).__name__}: {e}"}


def release_arm_lock(store, key: Optional[str]) -> bool:
    """Best-effort lock release. Called after the order is placed and
    tracked in ss.live_order_id (so subsequent tick's 2423 guard takes
    over). Failure to release is non-fatal — the TTL will clean up.

    Returns True if released or nothing to release; False on unexpected
    Redis error (still non-fatal from caller's POV)."""
    if not key:
        return True
    r = _redis_from_store(store)
    if r is None:
        return True
    try:
        r.delete(key)
        return True
    except Exception:
        return False
