#!/usr/bin/env python3
"""P&L attribution across every tracked product in the live tenant.

Reads the on-disk trade log (safety.TradeLog jsonl) — no Coinbase auth,
no backtest. Fast. Read-only.

Answers: "which sleeve/source has actually earned or lost money on each
product?" Highlights net losers as pruning candidates and prints the
busiest gates so you can see which guards are doing the most work.

Usage:
    # sweep every product on the live tenant (auto-picks *-live)
    python3 run_attribution.py

    # scope to one product
    python3 run_attribution.py --symbol PT-28SEP26-CDE

    # cap events read per product (default 5000)
    python3 run_attribution.py --tail 2000
"""

from __future__ import annotations

import json
import os
import sys
from typing import Iterable


def _load_all_events(data_dir: str, tail_total: int) -> list[dict]:
    """Read the last `tail_total` events from the unified trade log. On Render
    (REDIS_URL set) this reads from Redis; locally it reads trades.jsonl.
    Adam's setup on Render 2026-07-13: RedisTradeLog on key silver-swing:trades.
    Earlier version tried per-(tenant,symbol) files which don't exist under
    this store."""
    from safety import make_trade_log
    log = make_trade_log(data_dir)
    try:
        return log.tail(tail_total)
    except Exception:
        return []


def _filter_events(events: list[dict], tenant: str, symbol: str) -> list[dict]:
    """Subset events for one (tenant, symbol). safety.TradeLog stores every
    event across every product in one log; swing_leg._record injects tenant +
    symbol on every entry."""
    out = []
    for e in events:
        if str(e.get("tenant")) != tenant:
            continue
        if str(e.get("symbol")) != symbol:
            continue
        out.append(e)
    return out


def _pick_tenant(store) -> str:
    env = os.getenv("SWING_TENANT")
    if env:
        return env
    tenants = list(store.list_tenants() or [])
    live = [t for t in tenants if t.endswith("-live")]
    return (live[0] if live else "adam")


def _list_tracked_symbols(store, tenant: str) -> list[str]:
    try:
        raw = store.list_symbols(tenant) or []
    except Exception:
        raw = []
    return sorted({s for s in raw if not s.startswith("__")})


def main() -> int:
    tail = 5000
    if "--tail" in sys.argv:
        tail = int(sys.argv[sys.argv.index("--tail") + 1])
    single = None
    if "--symbol" in sys.argv:
        single = sys.argv[sys.argv.index("--symbol") + 1]

    data_dir = os.getenv("SWING_DATA_DIR", "data")

    from state_store import make_store
    import attribution as attr

    store = make_store(data_dir)
    tenant = _pick_tenant(store)
    print(f"[diag] tenant: {tenant!r}")

    symbols = [single] if single else _list_tracked_symbols(store, tenant)
    if not symbols:
        print(f"No products for tenant {tenant!r}.")
        return 1

    # One big log read up front; filter per-symbol below. Cheap because Redis
    # LRANGE is O(N) once, then we slice in memory. Beats N queries.
    print(f"Loading trade log (up to {tail * len(symbols)} events) ...")
    all_events = _load_all_events(data_dir, tail * len(symbols))
    print(f"[diag] loaded {len(all_events)} events")
    print(f"Attribution sweep: {len(symbols)} product(s), tail={tail} events each")
    print("=" * 100)

    table: list[dict] = []
    for i, sym in enumerate(symbols, 1):
        events = _filter_events(all_events, tenant, sym)[-tail:]
        if not events:
            print(f"[{i}/{len(symbols)}] {sym}: no events for this (tenant, symbol)")
            table.append({"symbol": sym, "status": "no_events"})
            continue
        pnl = attr.attribute_pnl(events)
        gates = attr.gate_activity(events)
        print(f"\n[{i}/{len(symbols)}] {sym} — {len(events)} events, "
              f"total realized ${pnl['total_realized']:+.2f}")
        for src in pnl["ranked"][:6]:
            b = pnl["by_source"][src]
            print(f"  {src:<28} realized ${b['realized']:+8.2f}  "
                  f"trades={b['trades']:>3}  win%={b['win_rate']:.2f}  "
                  f"avg_win=${b['avg_win']:+7.2f}  avg_loss=${b['avg_loss']:+7.2f}  "
                  f"expectancy=${b['expectancy']:+7.2f}")
        if pnl["net_losers"]:
            print(f"  net-losers (prune candidates): {pnl['net_losers']}")
        if gates["total_blocks"]:
            top = list(gates["by_gate"].items())[:3]
            top_s = ", ".join(f"{g}={n}" for g, n in top)
            print(f"  gates: {gates['total_blocks']} blocks — top: {top_s}")
        table.append({
            "symbol": sym, "status": "ok",
            "total_realized": pnl["total_realized"],
            "top_contributor": pnl["top_contributor"],
            "net_losers": pnl["net_losers"],
        })

    # Aggregate summary
    print("\n" + "=" * 100)
    print("ATTRIBUTION SUMMARY")
    print("=" * 100)
    ranked = sorted(
        [r for r in table if r.get("status") == "ok"],
        key=lambda r: r.get("total_realized") or 0.0, reverse=True,
    )
    header = f"{'SYMBOL':<24} {'TOTAL REALIZED':>16} {'TOP SOURCE':<28} {'NET LOSERS'}"
    print(header)
    print("-" * len(header))
    for row in ranked:
        losers = ", ".join(row.get("net_losers") or []) or "—"
        print(f"{row['symbol']:<24} ${(row.get('total_realized') or 0):>+14.2f}  "
              f"{(row.get('top_contributor') or '—'):<28} {losers}")
    missing = [r["symbol"] for r in table if r.get("status") != "ok"]
    if missing:
        print(f"\nNo events for: {missing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
