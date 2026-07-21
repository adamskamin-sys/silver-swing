"""Today's realized $ per product + fee-drag + fe07f8c guard verification.

Adam 2026-07-20 (Images #366/#367): fill history shows same-second SELL
duplicates on HYPE / MAG7C / ONDO before 14:19 CT. All predate the
broker-authoritative guard shipped in fe07f8c at 14:19 CT (19:19 UTC).
Adam wants proof:

  A. Realized $ per product for today (from Coinbase fills, not sleeve
     state) using FIFO matching (earliest unmatched BUY closes each SELL).
     Uses per-product contract_size to convert price delta → dollars.
  B. Fee-drag as % of gross realized. High % = churn is eating the edge.
  C. Any SELL fill AFTER 2026-07-20 19:19 UTC that has a sibling SELL
     on the same product within 60 seconds. Zero = the guard is holding.

Read-only. Run:  python3 diag_today_realized_and_guard.py
"""
from __future__ import annotations
import os
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any


GUARD_DEPLOY_UTC = datetime(2026, 7, 20, 19, 19, 0, tzinfo=timezone.utc)
TODAY_START_UTC = datetime(2026, 7, 20, 0, 0, 0, tzinfo=timezone.utc)
SIBLING_WINDOW_SEC = 60


def _dump(o: Any) -> dict:
    if hasattr(o, "to_dict"):
        try:
            return o.to_dict()
        except Exception:
            pass
    if isinstance(o, dict):
        return o
    return {}


def _parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _money(x: float, nd: int = 2) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.{nd}f}"


def main() -> None:
    print("=" * 96)
    print("TODAY'S REALIZED P&L + fe07f8c GUARD VERIFICATION")
    print(f"Guard deploy:  {GUARD_DEPLOY_UTC.isoformat()}  (2026-07-20 14:19 CT)")
    print(f"Day start:     {TODAY_START_UTC.isoformat()}")
    print("=" * 96)

    from broker import BrokerConfig, CoinbaseBroker

    # Use a bare CoinbaseBroker to reach the raw client.
    try:
        b = CoinbaseBroker(BrokerConfig(product_id="BTC-USD"))
    except Exception as e:
        print(f"\n✗ CoinbaseBroker init failed: {e}")
        return

    # ---- 1. Pull all fills for today -----------------------------------
    print(f"\n[1] Fetching fills since {TODAY_START_UTC.isoformat()}...")
    all_fills: list[dict] = []
    cursor = None
    page = 0
    while True:
        page += 1
        try:
            kwargs = {
                "start_sequence_timestamp": TODAY_START_UTC.isoformat(),
                "limit": 250,
            }
            if cursor:
                kwargs["cursor"] = cursor
            resp = _dump(b.client.list_fills(**kwargs))
        except Exception as e:
            print(f"    ✗ list_fills page {page} failed: {e}")
            break
        page_fills = resp.get("fills") or []
        all_fills.extend(page_fills)
        cursor = resp.get("cursor") or ""
        if not cursor or not page_fills:
            break
        if page > 20:
            print(f"    ⚠ >20 pages, stopping")
            break
    print(f"    got {len(all_fills)} fills across {page} page(s)")

    # Filter to today (defense; the API param should already do this)
    fills_today: list[dict] = []
    for f in all_fills:
        ts = _parse_ts(f.get("trade_time") or "")
        if ts is None or ts < TODAY_START_UTC:
            continue
        fills_today.append(f)
    print(f"    {len(fills_today)} fills on 2026-07-20 UTC")

    if not fills_today:
        print("\n(no fills today)")
        return

    # ---- 2. Group by product + fetch contract sizes --------------------
    by_product: dict[str, list[dict]] = defaultdict(list)
    for f in fills_today:
        pid = str(f.get("product_id") or "")
        if pid:
            by_product[pid].append(f)

    # Sort each product chronologically for FIFO
    for pid, fs in by_product.items():
        fs.sort(key=lambda x: str(x.get("trade_time") or ""))

    contract_sizes: dict[str, float] = {}
    for pid in by_product.keys():
        try:
            pb = CoinbaseBroker(BrokerConfig(product_id=pid))
            spec = pb.contract_spec() if hasattr(pb, "contract_spec") else None
            cs = float((spec or {}).get("contract_size") or 0)
            contract_sizes[pid] = cs if cs > 0 else 1.0
        except Exception:
            contract_sizes[pid] = 1.0

    # ---- 3. FIFO realized P&L per product ------------------------------
    print(f"\n[2] Per-product realized P&L (FIFO — earliest unmatched BUY "
          f"closes each SELL)")
    print("-" * 96)
    print(f"{'PRODUCT':<24} {'cs':>6} {'buys':>5} {'sells':>5}  "
          f"{'gross':>12} {'fees':>10} {'net':>12} {'fee%':>6} "
          f"{'unmatched':>12}")
    print("-" * 96)

    totals = {"gross": 0.0, "fees": 0.0, "net": 0.0}
    per_product_summary: list[dict] = []

    for pid, fs in sorted(by_product.items()):
        cs = contract_sizes.get(pid, 1.0)
        buy_q: deque = deque()  # (qty_remaining, price, fee_per_unit)
        realized_gross = 0.0
        realized_fees = 0.0
        buy_cnt = sell_cnt = 0
        for f in fs:
            side = str(f.get("side") or "").upper()
            qty = int(float(f.get("size") or 0))
            px = float(f.get("price") or 0)
            fee = float(f.get("commission") or 0)
            if qty <= 0:
                continue
            fee_per_unit = fee / qty if qty > 0 else 0
            if side == "BUY":
                buy_cnt += 1
                buy_q.append([qty, px, fee_per_unit])
                # Full BUY fee is a realized cost when matched (below).
            elif side == "SELL":
                sell_cnt += 1
                remaining = qty
                while remaining > 0 and buy_q:
                    lot = buy_q[0]
                    take = min(lot[0], remaining)
                    delta_px = (px - lot[1])
                    realized_gross += delta_px * cs * take
                    # Buy fee attributable to matched portion + sell fee
                    realized_fees += lot[2] * take
                    realized_fees += fee_per_unit * take
                    lot[0] -= take
                    remaining -= take
                    if lot[0] <= 0:
                        buy_q.popleft()
                # If SELL exceeded open BUYs, that's a SHORT — attribute
                # excess as unrealized loss context; we don't compute it.

        unmatched_qty = sum(int(l[0]) for l in buy_q)
        unmatched_val = sum(int(l[0]) * float(l[1]) * cs for l in buy_q)
        net = realized_gross - realized_fees
        fee_pct = (realized_fees / abs(realized_gross) * 100.0) \
            if realized_gross else 0.0
        totals["gross"] += realized_gross
        totals["fees"] += realized_fees
        totals["net"] += net
        per_product_summary.append({
            "pid": pid, "cs": cs, "buys": buy_cnt, "sells": sell_cnt,
            "gross": realized_gross, "fees": realized_fees, "net": net,
            "fee_pct": fee_pct,
            "unmatched_qty": unmatched_qty, "unmatched_val": unmatched_val,
        })
        print(f"{pid[:24]:<24} {cs:>6.0f} {buy_cnt:>5} {sell_cnt:>5}  "
              f"{_money(realized_gross):>12} {_money(realized_fees):>10} "
              f"{_money(net):>12} {fee_pct:>5.1f}% "
              f"{unmatched_qty:>4}c ({_money(unmatched_val, 0):>8})")

    print("-" * 96)
    tot_fee_pct = (totals["fees"] / abs(totals["gross"]) * 100.0
                   if totals["gross"] else 0.0)
    print(f"{'TOTAL':<24} {'':>6} {'':>5} {'':>5}  "
          f"{_money(totals['gross']):>12} {_money(totals['fees']):>10} "
          f"{_money(totals['net']):>12} {tot_fee_pct:>5.1f}%")

    # ---- 4. Worst churn (highest fee%) --------------------------------
    print(f"\n[3] Fee-drag ranking (worst first — high % = churn eats edge)")
    print("-" * 96)
    churn = sorted(per_product_summary, key=lambda x: -x["fee_pct"])
    for p in churn[:10]:
        if p["gross"] == 0 and p["fees"] == 0:
            continue
        print(f"  {p['pid']:<24}  gross {_money(p['gross']):>10}  "
              f"fees {_money(p['fees']):>8}  net {_money(p['net']):>10}  "
              f"fee%={p['fee_pct']:>5.1f}   ({p['buys']} buys, "
              f"{p['sells']} sells)")

    # ---- 5. Guard verification: post-19:19 UTC sibling SELLs -----------
    print(f"\n[4] fe07f8c guard verification — SELL siblings within "
          f"{SIBLING_WINDOW_SEC}s")
    print(f"    (any AFTER {GUARD_DEPLOY_UTC.isoformat()} is a guard leak)")
    print("-" * 96)

    all_sells = []
    for f in fills_today:
        if str(f.get("side") or "").upper() != "SELL":
            continue
        ts = _parse_ts(f.get("trade_time") or "")
        if ts is None:
            continue
        all_sells.append({
            "pid": str(f.get("product_id") or ""),
            "ts": ts,
            "oid": str(f.get("order_id") or ""),
            "size": int(float(f.get("size") or 0)),
            "px": float(f.get("price") or 0),
        })
    all_sells.sort(key=lambda x: (x["pid"], x["ts"]))

    pre_guard_pairs = []
    post_guard_pairs = []
    by_pid_sells: dict[str, list[dict]] = defaultdict(list)
    for s in all_sells:
        by_pid_sells[s["pid"]].append(s)

    for pid, sells in by_pid_sells.items():
        for i, s in enumerate(sells):
            for j in range(i + 1, len(sells)):
                s2 = sells[j]
                gap = (s2["ts"] - s["ts"]).total_seconds()
                if gap > SIBLING_WINDOW_SEC:
                    break
                # Different order_id = actual sibling fires, not
                # a partial-fill of one order.
                if s["oid"] == s2["oid"]:
                    continue
                pair = (pid, s, s2, gap)
                if s["ts"] >= GUARD_DEPLOY_UTC:
                    post_guard_pairs.append(pair)
                else:
                    pre_guard_pairs.append(pair)

    print(f"\n  PRE-guard SELL sibling pairs (before 19:19 UTC): "
          f"{len(pre_guard_pairs)}")
    for pid, s, s2, gap in pre_guard_pairs[:20]:
        print(f"    {pid:<22}  {s['ts'].strftime('%H:%M:%S')} → "
              f"{s2['ts'].strftime('%H:%M:%S')}  gap={gap:>4.0f}s  "
              f"px1=${s['px']:.4f}  px2=${s2['px']:.4f}  "
              f"oids={s['oid'][:8]}/{s2['oid'][:8]}")
    if len(pre_guard_pairs) > 20:
        print(f"    ... {len(pre_guard_pairs) - 20} more")

    print(f"\n  POST-guard SELL sibling pairs (after 19:19 UTC): "
          f"{len(post_guard_pairs)}")
    if post_guard_pairs:
        print(f"    🚨 GUARD LEAKING — investigate each below:")
        for pid, s, s2, gap in post_guard_pairs:
            print(f"    {pid:<22}  {s['ts'].strftime('%H:%M:%S')} → "
                  f"{s2['ts'].strftime('%H:%M:%S')}  gap={gap:>4.0f}s  "
                  f"px1=${s['px']:.4f}  px2=${s2['px']:.4f}  "
                  f"oids={s['oid'][:8]}/{s2['oid'][:8]}")
    else:
        print(f"    ✓ ZERO — every post-guard SELL is a solo fire")

    # ---- 6. Summary verdict -------------------------------------------
    print("\n" + "=" * 96)
    print("SUMMARY")
    print("=" * 96)
    print(f"  Today gross:    {_money(totals['gross'])}")
    print(f"  Today fees:     {_money(totals['fees'])}  "
          f"({tot_fee_pct:.1f}% of gross)")
    print(f"  Today net:      {_money(totals['net'])}")
    print(f"  Pre-guard dup SELLs:   {len(pre_guard_pairs):>4}")
    print(f"  Post-guard dup SELLs:  {len(post_guard_pairs):>4}  "
          f"{'← should be 0' if len(post_guard_pairs) == 0 else '← 🚨 LEAK'}")


if __name__ == "__main__":
    main()
