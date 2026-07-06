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

    def list_symbols(self, tenant_id: str) -> list[str]:
        return sorted((self._load().get(tenant_id) or {}).keys())

    def list_tenants(self) -> list[str]:
        return sorted(self._load().keys())
