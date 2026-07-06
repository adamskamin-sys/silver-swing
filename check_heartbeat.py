"""
check_heartbeat.py — dead-man's-switch watcher (spec §9B).

Runs periodically (Render cron, launchd, whatever) and alerts if the bot
hasn't updated its heartbeat within the stale threshold. Idempotent: safe
to invoke every N minutes.

The bot writes `last_heartbeat_ts` to state on every save (see swing_leg.py
_save_state). If that timestamp gets stale, the bot either crashed silently,
lost network, or the process died — the ONE class of failure a running bot
can't self-report.

Config via env vars:
  SWING_TENANT              (default "adam")
  SWING_SYMBOL              (default "SLR-27AUG26-CDE")
  SWING_DATA_DIR            (default "data")
  SWING_HEARTBEAT_STALE_S   seconds after which we alert (default 120)

Exit codes:
  0  heartbeat fresh
  1  heartbeat stale (alert sent)
  2  no state at all (never started, or wrong tenant/symbol)
"""

from __future__ import annotations

import os
import sys
import time

from dotenv import load_dotenv

from alerting import Priority, default_notifier
from state_store import JsonFileStateStore


def check(store, tenant: str, symbol: str, stale_seconds: float, notifier) -> int:
    state = store.get_state(tenant, symbol)
    if state is None:
        notifier.send(
            f"heartbeat: no state for {tenant}/{symbol}",
            "The bot's state has never been written. Either it never started, "
            "or SWING_TENANT/SWING_SYMBOL is wrong.",
            Priority.WARN,
        )
        return 2

    last = float(state.get("last_heartbeat_ts") or 0)
    age = time.time() - last
    if age > stale_seconds:
        notifier.send(
            f"HEARTBEAT STALE: {tenant}/{symbol}",
            f"Last save {age:.0f}s ago (threshold {stale_seconds:.0f}s). "
            f"The bot process may be dead. State snapshot:\n"
            f"  state={state.get('state')}, live_order_id={state.get('live_order_id')}, "
            f"cycles={state.get('cycles')}",
            Priority.CRIT,
        )
        return 1
    return 0


def main() -> int:
    load_dotenv()
    tenant = os.getenv("SWING_TENANT", "adam")
    symbol = os.getenv("SWING_SYMBOL", "SLR-27AUG26-CDE")
    data_dir = os.getenv("SWING_DATA_DIR", "data")
    stale = float(os.getenv("SWING_HEARTBEAT_STALE_S", "120"))
    store = JsonFileStateStore(f"{data_dir}/store.json")
    notifier = default_notifier()
    return check(store, tenant, symbol, stale, notifier)


if __name__ == "__main__":
    sys.exit(main())
