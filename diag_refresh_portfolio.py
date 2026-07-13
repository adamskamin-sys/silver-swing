"""Force a fresh __portfolio__ snapshot pull from Coinbase, right now (crew).

Solves the "sleeve editor says position 0 but Coinbase shows N" bug: the
sleeve-capacity validator reads position count from the cached
__portfolio__.config.derivatives, and if that scope is stale (bot halted,
refresh loop errored, new position not yet synced) the validator
rejects perfectly-valid sleeve edits with "exceeds available 0".

Usage:  python3 diag_refresh_portfolio.py [TENANT]
Default TENANT = adam-live.

Reports before/after: how many derivative rows the cache had vs now, and
lists any product Coinbase reports that the cache did not previously
carry (the OIL/NOL that surprised us this morning).

Safe: read from Coinbase + overwrite the __portfolio__ scope only. No
orders, no config changes.
"""
from __future__ import annotations
import os
import sys

from state_store import make_store
import main as _main  # exposes refresh_portfolio_snapshot


def _rows_by_pid(cfg: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for d in (cfg.get("derivatives") or []):
        pid = d.get("product_id")
        if pid:
            out[pid] = d
    return out


def run() -> None:
    tenant = sys.argv[1] if len(sys.argv) > 1 else "adam-live"
    store = make_store(os.getenv("SWING_DATA_DIR", "data"))

    before_cfg = store.get_config(tenant, "__portfolio__") or {}
    before_rows = _rows_by_pid(before_cfg)
    print(f"BEFORE {tenant}/__portfolio__: {len(before_rows)} derivative rows")
    for pid, row in sorted(before_rows.items()):
        print(f"  {pid:<24} qty={row.get('qty')}  avg={row.get('avg_entry')}  mark={row.get('mark')}")

    n = _main.refresh_portfolio_snapshot(store, tenant)
    print(f"\nrefresh_portfolio_snapshot → {n} derivative positions synced")

    after_cfg = store.get_config(tenant, "__portfolio__") or {}
    after_rows = _rows_by_pid(after_cfg)
    print(f"\nAFTER {tenant}/__portfolio__: {len(after_rows)} derivative rows")
    for pid, row in sorted(after_rows.items()):
        marker = " ← NEW" if pid not in before_rows else ""
        print(f"  {pid:<24} qty={row.get('qty')}  avg={row.get('avg_entry')}"
              f"  mark={row.get('mark')}{marker}")

    added = set(after_rows) - set(before_rows)
    removed = set(before_rows) - set(after_rows)
    if added:
        print(f"\nADDED to the cache: {sorted(added)}")
    if removed:
        print(f"REMOVED from the cache: {sorted(removed)}")
    if not added and not removed:
        print("\nNo product_id set changes — cache was up to date on which products, "
              "just refreshed marks/qtys.")


if __name__ == "__main__":
    run()
