"""
StateStore — the load-bearing "where state lives" abstraction (spec §12 step 1).

Two things share this store:
  - CONFIG: levels, sizes, toggles, presets. Written by the dashboard (or hand-edited
    in dev). Read by the bot every loop, so changes take effect on the next cycle.
  - STATE: current leg, live order id, filled qty, realized P&L, swing size, cycles.
    Written by the bot. Read by the dashboard for display.

Everything is namespaced by (tenant_id, symbol) from day one so the multi-tenant
step (spec §9A) is a data-migration, not a rewrite. Single-tenant deployments
just use a fixed tenant_id like "adam".

Backends:
  - JsonFileStateStore — single JSON file, atomic write-tmp-then-rename. Fine for
    local dev and a single-process bot. NOT safe for concurrent writers.
  - (future) RedisStateStore / PostgresStateStore for prod. Same Protocol, drop-in
    swap. Deployment §11 targets Render Key Value or Postgres.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Protocol


class StateStore(Protocol):
    """Namespaced key-value store split into three scopes:
      config    — human-writes (dashboard), read by bot each loop
      state     — bot-writes, read by dashboard for display
      snapshot  — bot-writes derived numbers (equity, unrealized, margin) for
                  the dashboard. Not read by the strategy — never a source of
                  truth, always regenerable from broker + fills.
    """

    def get_config(self, tenant_id: str, symbol: str) -> Optional[dict]: ...
    def put_config(self, tenant_id: str, symbol: str, config: dict) -> None: ...
    def get_state(self, tenant_id: str, symbol: str) -> Optional[dict]: ...
    def put_state(self, tenant_id: str, symbol: str, state: dict) -> None: ...
    def get_snapshot(self, tenant_id: str, symbol: str) -> Optional[dict]: ...
    def put_snapshot(self, tenant_id: str, symbol: str, snapshot: dict) -> None: ...
    def list_symbols(self, tenant_id: str) -> list[str]: ...
    def list_tenants(self) -> list[str]: ...


class JsonFileStateStore:
    """Single-file JSON backend for local dev.

    File layout:
        {
          "<tenant_id>": {
            "<symbol>": {"config": {...}, "state": {...}}
          }
        }

    Writes go through a tmp file + os.replace (atomic on POSIX), so a crash mid-write
    leaves the previous state intact rather than a half-written file.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())

    def _save(self, data: dict) -> None:
        # Use a PID-suffixed tmp so we don't collide with the Node dashboard's
        # tmp file when both write concurrently (otherwise whichever renames
        # first wins and the other gets ENOENT).
        tmp = self.path.with_suffix(self.path.suffix + f".tmp-{os.getpid()}")
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def _get_scope(self, tenant_id: str, symbol: str, scope: str) -> Optional[dict]:
        return self._load().get(tenant_id, {}).get(symbol, {}).get(scope)

    def _put_scope(self, tenant_id: str, symbol: str, scope: str, value: dict) -> None:
        data = self._load()
        data.setdefault(tenant_id, {}).setdefault(symbol, {})[scope] = value
        self._save(data)

    def get_config(self, tenant_id: str, symbol: str) -> Optional[dict]:
        return self._get_scope(tenant_id, symbol, "config")

    def put_config(self, tenant_id: str, symbol: str, config: dict) -> None:
        self._put_scope(tenant_id, symbol, "config", config)

    def get_state(self, tenant_id: str, symbol: str) -> Optional[dict]:
        return self._get_scope(tenant_id, symbol, "state")

    def put_state(self, tenant_id: str, symbol: str, state: dict) -> None:
        self._put_scope(tenant_id, symbol, "state", state)

    def get_snapshot(self, tenant_id: str, symbol: str) -> Optional[dict]:
        return self._get_scope(tenant_id, symbol, "snapshot")

    def put_snapshot(self, tenant_id: str, symbol: str, snapshot: dict) -> None:
        self._put_scope(tenant_id, symbol, "snapshot", snapshot)

    def get_paper_state(self, tenant_id: str, symbol: str) -> Optional[dict]:
        """Persisted PaperBroker state (position, balance, lots, etc.). Only
        used in paper mode — live reads position from Coinbase directly."""
        return self._get_scope(tenant_id, symbol, "paper_state")

    def put_paper_state(self, tenant_id: str, symbol: str, state: dict) -> None:
        self._put_scope(tenant_id, symbol, "paper_state", state)

    def clear_paper_state(self, tenant_id: str, symbol: str) -> None:
        data = self._load()
        block = data.get(tenant_id, {}).get(symbol, {})
        if "paper_state" in block:
            del block["paper_state"]
            self._save(data)

    def get_intent(self, tenant_id: str, symbol: str) -> Optional[dict]:
        """Dashboard-writes/bot-reads pending manual order intent."""
        return self._get_scope(tenant_id, symbol, "intent")

    def put_intent(self, tenant_id: str, symbol: str, intent: dict) -> None:
        self._put_scope(tenant_id, symbol, "intent", intent)

    def clear_intent(self, tenant_id: str, symbol: str) -> None:
        data = self._load()
        block = data.get(tenant_id, {}).get(symbol, {})
        if "intent" in block:
            del block["intent"]
            self._save(data)

    def get_resume_intent(self, tenant_id: str, symbol: str) -> Optional[dict]:
        """Dashboard-writes/bot-reads request to clear a HALT and re-arm."""
        return self._get_scope(tenant_id, symbol, "resume_intent")

    def put_resume_intent(self, tenant_id: str, symbol: str, intent: dict) -> None:
        self._put_scope(tenant_id, symbol, "resume_intent", intent)

    def clear_resume_intent(self, tenant_id: str, symbol: str) -> None:
        data = self._load()
        block = data.get(tenant_id, {}).get(symbol, {})
        if "resume_intent" in block:
            del block["resume_intent"]
            self._save(data)

    def get_reset_intent(self, tenant_id: str, symbol: str) -> Optional[dict]:
        """Dashboard-writes/bot-reads request to wipe paper trading state."""
        return self._get_scope(tenant_id, symbol, "reset_intent")

    def put_reset_intent(self, tenant_id: str, symbol: str, intent: dict) -> None:
        self._put_scope(tenant_id, symbol, "reset_intent", intent)

    def clear_reset_intent(self, tenant_id: str, symbol: str) -> None:
        data = self._load()
        block = data.get(tenant_id, {}).get(symbol, {})
        if "reset_intent" in block:
            del block["reset_intent"]
            self._save(data)

    def get_cancel_intent(self, tenant_id: str, symbol: str) -> Optional[dict]:
        """Dashboard-writes/bot-reads request to cancel a strategy's live order."""
        return self._get_scope(tenant_id, symbol, "cancel_intent")

    def clear_cancel_intent(self, tenant_id: str, symbol: str) -> None:
        data = self._load()
        block = data.get(tenant_id, {}).get(symbol, {})
        if "cancel_intent" in block:
            del block["cancel_intent"]
            self._save(data)

    def get_state_patch(self, tenant_id: str, symbol: str) -> Optional[dict]:
        """Adam 2026-07-15: diag-writes/bot-reads pending state correction.

        Solves the diag-vs-live race: diag script writes state → live bot's
        in-memory state is stale → next _save_state clobbers our write.

        Patch shape:
            {"sleeves": {"<sid>": {"realized_pnl": 14.55,
                                    "recent_cycle_pnls_append": 15.10}},
             "reason": "…", "ts": …}

        Bot consumes at start of each step() BEFORE any tick logic runs,
        applies via _apply_state_patch, then clears. Fields ending in
        _append are appended to the target list (bounded); others are set."""
        return self._get_scope(tenant_id, symbol, "state_patch")

    def put_state_patch(self, tenant_id: str, symbol: str, patch: dict) -> None:
        self._put_scope(tenant_id, symbol, "state_patch", patch)

    def clear_state_patch(self, tenant_id: str, symbol: str) -> None:
        data = self._load()
        block = data.get(tenant_id, {}).get(symbol, {})
        if "state_patch" in block:
            del block["state_patch"]
            self._save(data)

    def list_symbols(self, tenant_id: str) -> list[str]:
        return sorted((self._load().get(tenant_id) or {}).keys())

    def list_tenants(self) -> list[str]:
        return sorted(self._load().keys())


class InMemoryStateStore(JsonFileStateStore):
    """Zero-disk variant of JsonFileStateStore. Inherits all scoped methods
    (get_/put_/clear_ config/state/snapshot/intent/etc) by overriding just
    _load and _save to use a Python dict.

    Purpose: backtests + parameter tuning call trader.step() thousands of
    times, and each step calls self.store.put_state → _save → os.fsync.
    Under JsonFileStateStore that's ~10ms/step. A single 30-day 5-min
    backtest × 12 walk-forward folds = 100k+ fsyncs = 15-30 minutes just
    waiting on disk. This store makes those calls near-free.

    NOT SAFE for anything that expects durability. Only use for ephemeral
    computations (backtest, walk-forward eval, grid tuning) where the
    result is what matters, not the intermediate state file.
    """

    def __init__(self):
        self._mem: dict = {}

    def _load(self) -> dict:
        return self._mem

    def _save(self, data: dict) -> None:
        self._mem = data


class RedisJsonStore:
    """Redis-backed store that holds the entire state blob under one key.

    Mirrors JsonFileStateStore semantics exactly: every operation is a
    read-modify-write of the whole JSON tree. Fine for our scale (single blob
    stays well under 1MB). Cross-process concurrent writes have the same
    last-writer-wins semantics as the file backend.

    Use for multi-service deploys where several processes need to see the same
    state (e.g. Render workers + dashboard).
    """

    def __init__(self, url: str, key: str = "silver-swing:store"):
        import redis  # local import so tests without redis don't fail on import
        self._r = redis.Redis.from_url(url, decode_responses=True)
        self._key = key

    def _load(self) -> dict:
        raw = self._r.get(self._key)
        return json.loads(raw) if raw else {}

    # [crew:#1] Concurrency-safe read-modify-write via Redis WATCH/MULTI.
    # PROBLEM this fixes: the bot rewrites its `state` scope ~1x/sec while the
    # dashboard writes the kill switch / portfolio halt into the SAME blob key.
    # The old plain load->mutate->SET meant the bot's save (built from a blob it
    # loaded a moment earlier) could silently ERASE a kill switch the dashboard
    # set in between — your panic-stop reverts and the bot keeps trading.
    # WATCH the key: if any other writer changes it between our read and our
    # SET, the transaction aborts and we retry with fresh data, so the other
    # writer's change (e.g. the kill switch) is preserved rather than clobbered.
    _RMW_MAX_RETRIES = 50

    def _mutate(self, fn) -> None:
        """Atomically load the blob, apply fn(data) to mutate it in place, and
        persist — retrying if a concurrent writer touches the key mid-flight."""
        import redis  # local import mirrors __init__; keeps redis-less tests importable
        for _ in range(self._RMW_MAX_RETRIES):
            with self._r.pipeline() as pipe:
                try:
                    pipe.watch(self._key)
                    raw = pipe.get(self._key)
                    data = json.loads(raw) if raw else {}
                    fn(data)
                    pipe.multi()
                    pipe.set(self._key, json.dumps(data, sort_keys=True))
                    pipe.execute()
                    return
                except redis.exceptions.WatchError:
                    continue  # someone else wrote; reload and retry
        # Extreme sustained contention: fall back to a best-effort direct write
        # rather than silently dropping the update.
        raw = self._r.get(self._key)
        data = json.loads(raw) if raw else {}
        fn(data)
        self._r.set(self._key, json.dumps(data, sort_keys=True))

    def _save(self, data: dict) -> None:
        # Retained for API compatibility. Prefer _mutate() for any concurrent
        # path — a bare SET can still clobber a concurrent writer.
        self._r.set(self._key, json.dumps(data, sort_keys=True))

    def _get_scope(self, tenant_id, symbol, scope):
        return self._load().get(tenant_id, {}).get(symbol, {}).get(scope)

    def _put_scope(self, tenant_id, symbol, scope, value):
        self._mutate(lambda data: data.setdefault(tenant_id, {}).setdefault(symbol, {}).__setitem__(scope, value))

    def get_config(self, tenant_id, symbol): return self._get_scope(tenant_id, symbol, "config")
    def put_config(self, tenant_id, symbol, config): self._put_scope(tenant_id, symbol, "config", config)
    def get_state(self, tenant_id, symbol): return self._get_scope(tenant_id, symbol, "state")
    def put_state(self, tenant_id, symbol, state): self._put_scope(tenant_id, symbol, "state", state)
    def get_snapshot(self, tenant_id, symbol): return self._get_scope(tenant_id, symbol, "snapshot")
    def put_snapshot(self, tenant_id, symbol, snapshot): self._put_scope(tenant_id, symbol, "snapshot", snapshot)
    def get_paper_state(self, tenant_id, symbol): return self._get_scope(tenant_id, symbol, "paper_state")
    def put_paper_state(self, tenant_id, symbol, state): self._put_scope(tenant_id, symbol, "paper_state", state)
    def clear_paper_state(self, tenant_id, symbol): self._clear_scope(tenant_id, symbol, "paper_state")
    def get_intent(self, tenant_id, symbol): return self._get_scope(tenant_id, symbol, "intent")
    def put_intent(self, tenant_id, symbol, intent): self._put_scope(tenant_id, symbol, "intent", intent)

    def _clear_scope(self, tenant_id, symbol, scope):
        def _apply(data):
            block = data.get(tenant_id, {}).get(symbol, {})
            if scope in block:
                del block[scope]
        self._mutate(_apply)  # [crew:#1] atomic RMW, see _mutate above

    def clear_intent(self, tenant_id, symbol): self._clear_scope(tenant_id, symbol, "intent")
    def get_resume_intent(self, tenant_id, symbol): return self._get_scope(tenant_id, symbol, "resume_intent")
    def put_resume_intent(self, tenant_id, symbol, intent): self._put_scope(tenant_id, symbol, "resume_intent", intent)
    def clear_resume_intent(self, tenant_id, symbol): self._clear_scope(tenant_id, symbol, "resume_intent")
    def get_reset_intent(self, tenant_id, symbol): return self._get_scope(tenant_id, symbol, "reset_intent")
    def put_reset_intent(self, tenant_id, symbol, intent): self._put_scope(tenant_id, symbol, "reset_intent", intent)
    def clear_reset_intent(self, tenant_id, symbol): self._clear_scope(tenant_id, symbol, "reset_intent")
    def get_cancel_intent(self, tenant_id, symbol): return self._get_scope(tenant_id, symbol, "cancel_intent")
    def clear_cancel_intent(self, tenant_id, symbol): self._clear_scope(tenant_id, symbol, "cancel_intent")
    def get_state_patch(self, tenant_id, symbol): return self._get_scope(tenant_id, symbol, "state_patch")
    def put_state_patch(self, tenant_id, symbol, patch): self._put_scope(tenant_id, symbol, "state_patch", patch)
    def clear_state_patch(self, tenant_id, symbol): self._clear_scope(tenant_id, symbol, "state_patch")

    def list_symbols(self, tenant_id):
        return sorted((self._load().get(tenant_id) or {}).keys())

    def list_tenants(self):
        return sorted(self._load().keys())


def make_store(data_dir: str):
    """Pick JsonFileStateStore or RedisJsonStore based on REDIS_URL env var."""
    url = os.getenv("REDIS_URL")
    if url:
        return RedisJsonStore(url)
    return JsonFileStateStore(f"{data_dir}/store.json")
