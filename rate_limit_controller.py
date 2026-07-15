"""rate_limit_controller.py — Central REST budget allocator with priority queue.

Adam 2026-07-15: "we should also consider how to allocate the speed if say
for example we have a break out and we could have an advantage buy selling
at the top but not get banned or stalled for our usage because we used
resources that typically would have been used on slower moving contracts
at the time"

Right. Every Coinbase REST call goes through this controller, tagged with
a priority. When we're near the limit, LOW-priority calls yield to CRITICAL
ones — so a breakout order-placement burst doesn't get throttled because
the scanner + portfolio_snapshot were also running.

Design
------
- Sliding-window rate tracker per endpoint pool (public vs private).
- Priority queue: CRITICAL > HIGH > MEDIUM > LOW.
- When budget is tight, LOW/MEDIUM defer briefly; HIGH tries; CRITICAL
  ALWAYS submits (worth a possible 429 to catch a breakout).
- On 429: exponential backoff on all non-CRITICAL. CRITICAL still fires.
- On recovery: automatic ramp back to full budget.
- Fail-safe: if the controller errors, it approves the call (never block
  a legitimate trade on our own bug).

Coinbase Advanced Trade documented limits (2024):
  Public endpoints:  10 req/sec per IP
  Private endpoints: 30 req/sec per API key

Adam's empirical test (2026-07-15) showed 50+ req/s clean on all endpoints
— confirmed elevated tier. Defaults here are conservative (matching docs)
so we don't push blind; the tier can be discovered dynamically via
`X-RateLimit-*` response headers.

Not a threading primitive — used in the single-threaded main loop. If we
ever move to concurrent REST (asyncio), the tracker windows are the sole
shared state and would need a lock.
"""
from __future__ import annotations

import time
from enum import IntEnum
from typing import Optional


class Priority(IntEnum):
    """Lower value = higher priority (matches heapq semantics if we
    ever switch to a real heap-based queue)."""
    CRITICAL = 0   # order placement/cancel during active trigger
    HIGH = 1       # order status check on a live order, drift-triggered reeval
    MEDIUM = 2     # portfolio_snapshot, mark refresh
    LOW = 3        # scanner refresh, routine health checks, reconciliation


class EndpointKind:
    """Two pools — Coinbase enforces separate limits for public vs private."""
    PUBLIC = "public"
    PRIVATE = "private"


class RateLimitController:
    """Tracks recent REST call rate and gates new calls by priority."""

    # Documented Coinbase Advanced Trade limits per second
    DEFAULT_PUBLIC_LIMIT_PER_SEC = 10.0
    DEFAULT_PRIVATE_LIMIT_PER_SEC = 30.0

    # Backoff state
    BACKOFF_INITIAL_SECS = 0.1     # 100ms
    BACKOFF_MULTIPLIER = 2.0
    BACKOFF_MAX_SECS = 5.0         # cap
    BACKOFF_RECOVERY_ON_SUCCESS = True

    # Sliding window in seconds — 1s matches Coinbase's per-second bucket
    WINDOW_SECS = 1.0

    def __init__(self,
                 public_limit: float = DEFAULT_PUBLIC_LIMIT_PER_SEC,
                 private_limit: float = DEFAULT_PRIVATE_LIMIT_PER_SEC,
                 window_secs: float = WINDOW_SECS):
        self._limits = {
            EndpointKind.PUBLIC: float(public_limit),
            EndpointKind.PRIVATE: float(private_limit),
        }
        self._window = float(window_secs)
        # timestamp lists — one deque-ish list per endpoint pool
        self._recent: dict[str, list[float]] = {
            EndpointKind.PUBLIC: [],
            EndpointKind.PRIVATE: [],
        }
        # Backoff state (per pool)
        self._backoff_until: dict[str, float] = {
            EndpointKind.PUBLIC: 0.0,
            EndpointKind.PRIVATE: 0.0,
        }
        self._current_backoff: dict[str, float] = {
            EndpointKind.PUBLIC: self.BACKOFF_INITIAL_SECS,
            EndpointKind.PRIVATE: self.BACKOFF_INITIAL_SECS,
        }
        # Telemetry
        self._stats: dict[str, int] = {
            "approved": 0,
            "throttled": 0,
            "critical_forced": 0,
            "429_seen": 0,
            "recovered": 0,
        }

    # ---- Internal helpers ------------------------------------------------

    def _prune(self, kind: str, now: float) -> None:
        """Drop timestamps older than the sliding window."""
        cutoff = now - self._window
        arr = self._recent.setdefault(kind, [])
        # Trim from front (list is time-ordered)
        i = 0
        for i, t in enumerate(arr):
            if t >= cutoff:
                break
        else:
            i = len(arr)
        if i > 0:
            del arr[:i]

    def _current_rate(self, kind: str, now: float) -> float:
        """Requests per second, over the sliding window."""
        self._prune(kind, now)
        return len(self._recent[kind]) / self._window

    def _budget_utilization(self, kind: str, now: float) -> float:
        """0.0 (no calls) .. 1.0 (at limit) .. >1.0 (over limit)."""
        limit = self._limits.get(kind, 1.0)
        if limit <= 0:
            return 0.0
        return self._current_rate(kind, now) / limit

    # ---- Public API ------------------------------------------------------

    def acquire(self, priority: Priority, kind: str = EndpointKind.PUBLIC) -> bool:
        """Reserve a REST call slot. Returns True if approved (caller
        should make the request), False if the caller should defer.

        Priority semantics:
          CRITICAL — always True (breakout / order execution — worth a 429)
          HIGH     — True unless we're deep in backoff
          MEDIUM   — True unless utilization > 70% OR we're in backoff
          LOW      — True only if utilization < 50% AND not in backoff
        """
        now = time.time()
        # Fail-safe wrap — never block a call on our own bug
        try:
            kind = kind if kind in self._limits else EndpointKind.PUBLIC
            in_backoff = now < self._backoff_until[kind]
            util = self._budget_utilization(kind, now)

            if priority == Priority.CRITICAL:
                # Always approve. Record for tracking. Consumes budget but
                # doesn't get denied by it.
                self._recent[kind].append(now)
                self._stats["approved"] += 1
                self._stats["critical_forced"] += (1 if (in_backoff or util >= 1.0) else 0)
                return True

            if in_backoff:
                # In backoff — only CRITICAL passes (already handled above)
                self._stats["throttled"] += 1
                return False

            # Priority-graded utilization gates
            gate = {
                Priority.HIGH: 0.95,      # allow up to 95% util
                Priority.MEDIUM: 0.70,    # allow up to 70% util
                Priority.LOW: 0.50,       # allow up to 50% util
            }.get(priority, 0.70)

            if util >= gate:
                self._stats["throttled"] += 1
                return False

            self._recent[kind].append(now)
            self._stats["approved"] += 1
            return True
        except Exception:
            return True  # fail-open — never block a real trade

    def wait_and_acquire(self, priority: Priority,
                         kind: str = EndpointKind.PUBLIC,
                         timeout_secs: float = 5.0) -> bool:
        """Block for up to timeout_secs, retrying acquire() every 50ms.
        Returns True if we got budget, False if we timed out.

        CRITICAL always returns True immediately (never blocks).
        """
        if priority == Priority.CRITICAL:
            return self.acquire(priority, kind)
        deadline = time.time() + timeout_secs
        while time.time() < deadline:
            if self.acquire(priority, kind):
                return True
            time.sleep(0.05)
        return False

    def record_response(self, kind: str, status_code: Optional[int] = None,
                        headers: Optional[dict] = None) -> None:
        """Update controller state based on a response. Reads Coinbase's
        X-RateLimit-* headers if present to sync with server view.

        Args:
            kind: EndpointKind.PUBLIC or EndpointKind.PRIVATE
            status_code: HTTP status code (429 triggers backoff)
            headers: response headers dict — parses X-RateLimit-Limit and
                     X-RateLimit-Remaining if present to update our limit
                     dynamically.
        """
        try:
            now = time.time()
            kind = kind if kind in self._limits else EndpointKind.PUBLIC

            # Parse rate-limit headers to update our local limits
            if headers:
                # Case-insensitive header lookup
                lower_hdrs = {k.lower(): v for k, v in headers.items()}
                lim = lower_hdrs.get("x-ratelimit-limit")
                if lim:
                    try:
                        # Coinbase's X-RateLimit-Limit is per-window (usually 1 sec)
                        self._limits[kind] = float(lim)
                    except (TypeError, ValueError):
                        pass

            # Handle throttle response
            if status_code == 429:
                self._stats["429_seen"] += 1
                # Backoff duration — check Retry-After header, else use current
                retry_after = None
                if headers:
                    ra = {k.lower(): v for k, v in headers.items()}.get("retry-after")
                    if ra:
                        try:
                            retry_after = float(ra)
                        except (TypeError, ValueError):
                            pass
                delay = retry_after if retry_after else self._current_backoff[kind]
                self._backoff_until[kind] = now + delay
                # Ramp backoff for the next 429
                self._current_backoff[kind] = min(
                    self.BACKOFF_MAX_SECS,
                    self._current_backoff[kind] * self.BACKOFF_MULTIPLIER,
                )
                return

            # Success — reset backoff if it was elevated
            if status_code is not None and 200 <= status_code < 300:
                if self._current_backoff[kind] > self.BACKOFF_INITIAL_SECS:
                    self._current_backoff[kind] = self.BACKOFF_INITIAL_SECS
                    self._stats["recovered"] += 1
        except Exception:
            pass  # fail-open

    def stats(self) -> dict:
        """Snapshot of controller state for telemetry / debugging."""
        now = time.time()
        return {
            "public_rate_per_sec": round(self._current_rate(EndpointKind.PUBLIC, now), 2),
            "private_rate_per_sec": round(self._current_rate(EndpointKind.PRIVATE, now), 2),
            "public_util_pct": round(self._budget_utilization(EndpointKind.PUBLIC, now) * 100, 1),
            "private_util_pct": round(self._budget_utilization(EndpointKind.PRIVATE, now) * 100, 1),
            "public_limit": self._limits[EndpointKind.PUBLIC],
            "private_limit": self._limits[EndpointKind.PRIVATE],
            "public_in_backoff": now < self._backoff_until[EndpointKind.PUBLIC],
            "private_in_backoff": now < self._backoff_until[EndpointKind.PRIVATE],
            "counts": dict(self._stats),
        }

    def reset(self) -> None:
        """Test helper — clear all tracking state."""
        self._recent = {k: [] for k in self._recent}
        self._backoff_until = {k: 0.0 for k in self._backoff_until}
        self._current_backoff = {k: self.BACKOFF_INITIAL_SECS for k in self._current_backoff}
        self._stats = {k: 0 for k in self._stats}


# Module-level singleton — one controller for the whole process. Broker
# and any other REST caller share this instance. If we ever run multiple
# processes hitting Coinbase with the same API key/IP, they need to
# coordinate via Redis or accept that each has its own view.
_GLOBAL_CONTROLLER: Optional[RateLimitController] = None


def get_controller() -> RateLimitController:
    """Return (create if needed) the process-wide RateLimitController."""
    global _GLOBAL_CONTROLLER
    if _GLOBAL_CONTROLLER is None:
        _GLOBAL_CONTROLLER = RateLimitController()
    return _GLOBAL_CONTROLLER
