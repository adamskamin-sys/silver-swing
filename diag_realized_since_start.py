"""Total realized P&L since the bot's first-ever cycle event.

Adam 2026-07-15: sum every `cycle_completed` + `sleeve_cycle_completed`
event in the trade log across all tenants + products + sleeves. This
IS the truth even if per-sleeve `realized_pnl` state got reset —
events are append-only.

Read-only.

Usage:
    python3 diag_realized_since_start.py                  # all tenants
    python3 diag_realized_since_start.py adam-live        # one tenant
    python3 diag_realized_since_start.py adam-live 7      # last 7 days
"""
from __future__ import annotations
import os
import sys
import time
from collections import defaultdict


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "?"


def main() -> None:
    tenant_filter = sys.argv[1] if len(sys.argv) > 1 else None
    days_back = float(sys.argv[2]) if len(sys.argv) > 2 else None

    print("=" * 78)
    scope = f"tenant={tenant_filter or 'ALL'}"
    if days_back:
        scope += f"  window={days_back}d"
    else:
        scope += "  window=since-bot-start"
    print(f"REALIZED P&L — {scope}")
    print("=" * 78)

    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    except Exception as e:
        print(f"trade log load failed: {e}")
        return

    cutoff = (time.time() - days_back * 86400) if days_back else 0
    per_product: dict[str, float] = defaultdict(float)
    per_sleeve: dict[str, dict] = {}
    per_tenant: dict[str, float] = defaultdict(float)
    total = 0.0
    total_cycles = 0
    first_ts = None
    last_ts = None
    events_seen = 0

    for e in log.events():
        if not isinstance(e, dict):
            continue
        ts = float(e.get("ts", 0) or 0)
        if ts < cutoff:
            continue
        et = str(e.get("event_type", ""))
        if et not in ("cycle_completed", "sleeve_cycle_completed"):
            continue
        tenant = str(e.get("tenant", ""))
        if tenant_filter and tenant != tenant_filter:
            continue
        pnl = e.get("cycle_pnl")
        if pnl is None:
            # older events might have used net_pnl or gross_pnl
            pnl = e.get("net_pnl") or e.get("realized_delta") or 0
        try:
            pnl = float(pnl)
        except (TypeError, ValueError):
            continue
        if pnl == 0:
            continue
        events_seen += 1
        symbol = str(e.get("symbol") or "?")
        sleeve_id = str(e.get("sleeve_id") or "primary")
        key = f"{symbol}::{sleeve_id}"
        if key not in per_sleeve:
            per_sleeve[key] = {"symbol": symbol, "sleeve_id": sleeve_id,
                               "cycles": 0, "pnl": 0.0, "first_ts": ts, "last_ts": ts}
        per_sleeve[key]["cycles"] += 1
        per_sleeve[key]["pnl"] += pnl
        per_sleeve[key]["last_ts"] = ts
        if ts < per_sleeve[key]["first_ts"]:
            per_sleeve[key]["first_ts"] = ts
        per_product[symbol] += pnl
        per_tenant[tenant] += pnl
        total += pnl
        total_cycles += 1
        if first_ts is None or ts < first_ts:
            first_ts = ts
        if last_ts is None or ts > last_ts:
            last_ts = ts

    if events_seen == 0:
        print("\nNo cycle_completed events found for the given filter.")
        return

    print(f"\nFirst cycle event: {_fmt_ts(first_ts)}")
    print(f"Last  cycle event: {_fmt_ts(last_ts)}")
    print(f"Total events:      {events_seen}")
    print(f"Total cycles:      {total_cycles}")
    print(f"Elapsed days:      {(last_ts - first_ts) / 86400:.2f}")

    print(f"\n{'─' * 78}")
    print(f"BY TENANT")
    print(f"{'─' * 78}")
    for t in sorted(per_tenant.keys(), key=lambda k: -per_tenant[k]):
        sign = "+" if per_tenant[t] >= 0 else "-"
        print(f"  {t:20s}  {sign}${abs(per_tenant[t]):>10.2f}")

    print(f"\n{'─' * 78}")
    print(f"BY PRODUCT (top 20)")
    print(f"{'─' * 78}")
    products_sorted = sorted(per_product.items(), key=lambda x: -abs(x[1]))
    for sym, pnl in products_sorted[:20]:
        sign = "+" if pnl >= 0 else "-"
        print(f"  {sym:25s}  {sign}${abs(pnl):>10.2f}")

    print(f"\n{'─' * 78}")
    print(f"BY SLEEVE (top 20)")
    print(f"{'─' * 78}")
    sleeves_sorted = sorted(per_sleeve.values(), key=lambda x: -abs(x["pnl"]))
    for s in sleeves_sorted[:20]:
        sign = "+" if s["pnl"] >= 0 else "-"
        span = f"{_fmt_ts(s['first_ts'])[:10]} → {_fmt_ts(s['last_ts'])[:10]}"
        print(f"  {s['symbol']:15s} {s['sleeve_id']:20s} "
              f"cycles={s['cycles']:>3d}  {sign}${abs(s['pnl']):>9.2f}  {span}")

    print(f"\n{'═' * 78}")
    sign = "+" if total >= 0 else "-"
    per_day = total / max((last_ts - first_ts) / 86400, 1)
    per_day_sign = "+" if per_day >= 0 else "-"
    print(f"TOTAL REALIZED — {sign}${abs(total):.2f}  ({total_cycles} cycles, "
          f"avg {per_day_sign}${abs(per_day):.2f}/day)")
    print(f"{'═' * 78}")


if __name__ == "__main__":
    main()
