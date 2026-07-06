"""
Safety layer MVP (spec §9B, §12 step 4) — three pieces that must exist before
any strategy cleverness runs on real money.

1. TradeLog — append-only JSONL journal of every consequential event (orders,
   fills, halts, reconcile checks). Read by dashboard, tax reporting, and the
   "why did it do that at 2am" post-mortem.

2. Reconciler — compares the bot's believed state to what the exchange actually
   shows. Drift between the two is where quiet disasters live. On mismatch:
   HALT — do not attempt to correct silently.

3. KillSwitch — a flag in the StateStore any process (dashboard, phone, panic
   ssh) can flip. The bot checks it every cycle and refuses to arm new legs
   while it's on. Distinct from per-instrument HALT: this is "freeze everything
   right now."

Explicitly OUT OF SCOPE for this MVP (each is its own follow-up):
- SMS / Telegram / Discord alerting — needs external integration; log for now.
- Heartbeat / dead-man's switch — needs a separate watcher process (Render cron
  or uptime service).
- Roll handling near expiry — separate concern; SLR-27AUG26-CDE has 52 days.
- Max-daily-loss circuit breaker — belongs at the account-margin governor layer
  once >1 instrument runs.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional, Protocol


# ============================================================================
# TradeLog
# ============================================================================


class TradeLog:
    """Append-only JSONL journal. Every write is one line; log rotation and
    reads are the reader's problem."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event_type: str, **payload) -> dict:
        entry = {"ts": time.time(), "event_type": event_type, **payload}
        with open(self.path, "a") as f:
            f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        return entry

    def events(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def tail(self, n: int) -> list[dict]:
        """Last N events. Full re-read; fine for the small MVP scale."""
        return list(self.events())[-n:]


class RedisTradeLog:
    """Redis-backed trade log. LPUSH new events onto a list, LTRIM to bound.
    Reads take the last N via LRANGE."""

    def __init__(self, url: str, key: str = "silver-swing:trades", max_len: int = 10000):
        import redis
        self._r = redis.Redis.from_url(url, decode_responses=True)
        self._key = key
        self._max_len = max_len

    def record(self, event_type: str, **payload) -> dict:
        entry = {"ts": time.time(), "event_type": event_type, **payload}
        line = json.dumps(entry, sort_keys=True, default=str)
        pipe = self._r.pipeline()
        pipe.lpush(self._key, line)
        pipe.ltrim(self._key, 0, self._max_len - 1)
        pipe.execute()
        return entry

    def events(self) -> Iterator[dict]:
        # oldest → newest, matching file version
        raw = self._r.lrange(self._key, 0, -1)
        for line in reversed(raw):
            yield json.loads(line)

    def tail(self, n: int) -> list[dict]:
        raw = self._r.lrange(self._key, 0, n - 1)
        return [json.loads(line) for line in reversed(raw)]


def make_trade_log(data_dir: str):
    """Pick file or Redis trade log based on REDIS_URL env var."""
    url = os.getenv("REDIS_URL")
    if url:
        return RedisTradeLog(url)
    return TradeLog(f"{data_dir}/trades.jsonl")


# ============================================================================
# Reconciler
# ============================================================================


class _BrokerReconcileView(Protocol):
    """Minimum broker surface Reconciler needs — both CoinbaseBroker and
    PaperBroker satisfy this via `position_qty()`."""
    def position_qty(self) -> int: ...


@dataclass
class ReconcileResult:
    ok: bool
    believed_position: int
    actual_position: int
    believed_order_id: Optional[str]
    mismatches: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.ok:
            return f"reconcile OK: position={self.actual_position}"
        return "reconcile MISMATCH: " + "; ".join(self.mismatches)


def reconcile(
    broker: _BrokerReconcileView,
    believed_position: int,
    believed_order_id: Optional[str] = None,
) -> ReconcileResult:
    """Compare the bot's believed state to the exchange truth.

    On mismatch, DO NOT attempt to correct — that's how you double-close a
    position or double-cancel a live order. Return the mismatch and let the
    caller decide (usually: HALT and alert).

    Order reconciliation lives here too once the Broker adds list_open_orders().
    """
    actual_position = broker.position_qty()
    mismatches = []
    if actual_position != believed_position:
        mismatches.append(
            f"position: believed={believed_position}, actual={actual_position}"
        )
    return ReconcileResult(
        ok=not mismatches,
        believed_position=believed_position,
        actual_position=actual_position,
        believed_order_id=believed_order_id,
        mismatches=mismatches,
    )


# ============================================================================
# KillSwitch
# ============================================================================


# Account-scoped, not symbol-scoped. Uses a magic "symbol" name in the store
# so we don't need a new StateStore method for account-level config.
_KILL_SWITCH_SCOPE = "__account_kill_switch__"


class _StateStoreForKillSwitch(Protocol):
    def get_config(self, tenant_id: str, symbol: str) -> Optional[dict]: ...
    def put_config(self, tenant_id: str, symbol: str, config: dict) -> None: ...


class KillSwitch:
    """Global "freeze everything now" flag. Persisted in the StateStore so
    ANY process — bot, dashboard, phone-triggered API — can flip it and every
    other process picks it up on next read.

    Distinct from a per-instrument HALT: HALT is "this instrument's strategy
    tripped a guard." Kill switch is "the human said stop, everywhere."
    """

    def __init__(self, store: _StateStoreForKillSwitch, tenant_id: str):
        self.store = store
        self.tenant_id = tenant_id

    def _read(self) -> dict:
        return self.store.get_config(self.tenant_id, _KILL_SWITCH_SCOPE) or {}

    def _write(self, cfg: dict) -> None:
        self.store.put_config(self.tenant_id, _KILL_SWITCH_SCOPE, cfg)

    def is_active(self) -> bool:
        return bool(self._read().get("active"))

    def reason(self) -> Optional[str]:
        return self._read().get("reason")

    def activate(self, reason: str = "") -> None:
        self._write({
            "active": True,
            "reason": reason,
            "activated_ts": time.time(),
        })

    def clear(self, cleared_by: str = "") -> None:
        """Deactivate. Per spec §10, this must be behind a UI confirm step —
        the KillSwitch class itself just does the flip; the confirm lives at
        the caller (dashboard)."""
        prev = self._read()
        self._write({
            "active": False,
            "reason": None,
            "cleared_ts": time.time(),
            "cleared_by": cleared_by,
            "previous_reason": prev.get("reason"),
        })
