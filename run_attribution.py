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


def _load_events(log_path: str, tail: int) -> list[dict]:
    """Read the last `tail` events from a jsonl trade log. Missing file → []."""
    if not os.path.exists(log_path):
        return []
    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            # 4KB per event is generous; grab a chunk that will cover `tail`
            # events, then split. Small files: read the whole thing.
            chunk = min(size, max(64 * 1024, tail * 512))
            f.seek(max(0, size - chunk))
            data = f.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    events: list[dict] = []
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    return events[-tail:]


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


def _find_log_path(data_dir: str, tenant: str, symbol: str) -> str:
    """The bot writes per-(tenant, symbol) event logs. Filename convention lives
    in safety.TradeLog; the plumbing here mirrors what the live loop uses."""
    # Common patterns tried in order — first hit wins.
    candidates = [
        os.path.join(data_dir, f"{tenant}_{symbol}.jsonl"),
        os.path.join(data_dir, "logs", f"{tenant}_{symbol}.jsonl"),
        os.path.join(data_dir, tenant, f"{symbol}.jsonl"),
        os.path.join(data_dir, "events", f"{tenant}_{symbol}.jsonl"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[0]  # nonexistent — caller handles missing gracefully


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

    print(f"Attribution sweep: {len(symbols)} product(s), tail={tail} events each")
    print("=" * 100)

    table: list[dict] = []
    for i, sym in enumerate(symbols, 1):
        path = _find_log_path(data_dir, tenant, sym)
        events = _load_events(path, tail)
        if not events:
            print(f"[{i}/{len(symbols)}] {sym}: no events at {path}")
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
