"""Find MISSED FILLS — the trigger was touched but the order didn't fill.

Adam 2026-07-15: "the triggers were touched and nothing happened"

Two suspected causes:
  1) Cancel-replace race: reeval cancels the resting BUY and re-places
     within ~200ms. If price wicks down during that window, no order is
     on the book → touch is missed → no fill.
  2) Order never actually made it to Coinbase (place failed silently).

This script:
  - For each sleeve in ARMED_BUY (waiting to fill):
      - Reads the last N minutes of trade-log events for cancel/place actions
      - Fetches recent 1-min candles from Coinbase for the same window
      - For each cancel-replace pair, checks if low ≤ buy_px happened
        DURING the coverage gap (between cancel event and next place event)
  - Reports:
      * Per sleeve: number of cancel-replace pairs, number of touches DURING
        coverage gaps, number of touches when we WERE covered but didn't fill
      * The specific timestamps + prices so you can verify against Coinbase

Read-only. Usage:
    python3 diag_missed_fills.py             # last 12 hours
    python3 diag_missed_fills.py 24          # last 24 hours
    python3 diag_missed_fills.py 6 BIT       # last 6 hours, only BIT products
"""
from __future__ import annotations
import json
import os
import sys
import time
from collections import defaultdict


def _now() -> float:
    return time.time()


def _load_events(hours: float) -> list[dict]:
    """Load trade-log events from the last N hours."""
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        cutoff = _now() - (hours * 3600)
        events = [e for e in log.events() if isinstance(e, dict)
                  and float(e.get("ts", 0) or 0) >= cutoff]
        return events
    except Exception as e:
        print(f"  WARN: make_trade_log failed: {e}")
    return []


def _fetch_candles(product_id: str, hours: float) -> list[dict]:
    """Get 1-min candles from Coinbase for the last N hours.
    Returns [{start, low, high, open, close, volume}]. Empty on failure."""
    try:
        from coinbase.rest import RESTClient
        from dotenv import load_dotenv
        load_dotenv()
        key_path = os.getenv("COINBASE_API_KEY_JSON_PATH")
        if not key_path:
            return []
        client = RESTClient(key_file=key_path)
        end = int(_now())
        start = end - int(hours * 3600)
        # Coinbase caps at 300 candles per request → chunk if hours > 5
        candles: list[dict] = []
        chunk_secs = 5 * 3600  # 300 min = 5h
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + chunk_secs, end)
            try:
                resp = client.get_candles(
                    product_id=product_id,
                    start=str(cursor), end=str(chunk_end),
                    granularity="ONE_MINUTE",
                )
                payload = resp.to_dict() if hasattr(resp, "to_dict") else resp
                for c in (payload.get("candles") or []):
                    try:
                        candles.append({
                            "start": int(float(c.get("start") or 0)),
                            "low": float(c.get("low") or 0),
                            "high": float(c.get("high") or 0),
                            "open": float(c.get("open") or 0),
                            "close": float(c.get("close") or 0),
                        })
                    except Exception:
                        pass
            except Exception as e:
                print(f"     Coinbase candles failed [{product_id} {cursor}]: {e}")
                break
            cursor = chunk_end
        candles.sort(key=lambda x: x["start"])
        return candles
    except Exception as e:
        print(f"     Could not query Coinbase for {product_id}: {e}")
        return []


def _fmt_ts(ts: float | int | None) -> str:
    if not ts:
        return "?"
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "?"


def main() -> None:
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    filter_arg = sys.argv[2].upper() if len(sys.argv) > 2 else ""

    print("=" * 78)
    hdr = f"MISSED FILL AUDIT — last {hours}h"
    if filter_arg:
        hdr += f" (filter={filter_arg})"
    print(hdr)
    print("=" * 78)

    events = _load_events(hours)
    if not events:
        print("\nNO EVENTS.")
        return

    # ---- 1) Bucket events by (sleeve_id, symbol) --------------------------
    # Interesting event types:
    #   sleeve_order_placed / sleeve_order_replaced / reentry_reeval_replaced
    #   sleeve_order_cancelled / reentry_reeval_cancel_failed
    #   sleeve_order_filled (the good outcome — didn't happen if missed)
    per_sleeve: dict[str, dict] = defaultdict(lambda: {
        "symbol": None, "sleeve_name": None,
        "cancel_events": [],     # (ts, price, oid)
        "place_events": [],       # (ts, price, oid)
        "fill_events": [],        # (ts, price, side)
    })

    for e in events:
        sid = e.get("sleeve_id")
        if not sid:
            continue
        sym = e.get("symbol") or ""
        if filter_arg and filter_arg not in sym.upper():
            continue
        et = e.get("event_type") or ""
        bucket = per_sleeve[sid]
        bucket["symbol"] = bucket["symbol"] or sym
        bucket["sleeve_name"] = bucket["sleeve_name"] or e.get("sleeve_name") or sid
        ts = float(e.get("ts") or 0)
        if not ts:
            continue

        if et in ("sleeve_order_placed", "sleeve_arm_placed"):
            bucket["place_events"].append({
                "ts": ts, "price": e.get("price") or e.get("limit_price"),
                "oid": e.get("order_id") or e.get("oid"),
                "side": (e.get("side") or "").upper(),
            })
        elif et in ("reentry_reeval_replaced",):
            bucket["place_events"].append({
                "ts": ts, "price": e.get("new_buy_px"),
                "oid": e.get("new_order_id"),
                "side": "BUY",
            })
            bucket["cancel_events"].append({
                "ts": ts, "price": e.get("old_buy_px"),
                "oid": e.get("old_order_id"),
            })
        elif et in ("sleeve_order_cancelled",):
            bucket["cancel_events"].append({
                "ts": ts, "price": e.get("price"),
                "oid": e.get("order_id"),
            })
        elif et in ("sleeve_order_filled",):
            leg = (e.get("leg") or "").upper()
            side = "SELL" if "SELL" in leg else ("BUY" if "BUY" in leg else "?")
            bucket["fill_events"].append({
                "ts": ts, "price": e.get("average_filled_price"),
                "side": side,
            })

    if not per_sleeve:
        print("\nNo sleeve events in the window.")
        return

    # ---- 2) Per-sleeve analysis --------------------------------------------
    print(f"\nActive sleeves in window: {len(per_sleeve)}\n")
    total_missed = 0
    total_gaps = 0

    for sid, bucket in per_sleeve.items():
        sym = bucket["symbol"]
        if not sym:
            continue
        cancels = sorted(bucket["cancel_events"], key=lambda x: x["ts"])
        places = sorted(bucket["place_events"], key=lambda x: x["ts"])
        fills = sorted(bucket["fill_events"], key=lambda x: x["ts"])

        print(f"── {sym}  ({bucket['sleeve_name']})  sleeve_id={sid}")
        print(f"     places={len(places)} cancels={len(cancels)} fills={len(fills)}")

        # Compute coverage gaps: from each cancel_ts to the NEXT place_ts
        gaps: list[tuple[float, float, float | None]] = []  # (start, end, buy_px)
        p_idx = 0
        for c in cancels:
            c_ts = c["ts"]
            # find next place strictly after c_ts
            while p_idx < len(places) and places[p_idx]["ts"] <= c_ts:
                p_idx += 1
            next_place = places[p_idx] if p_idx < len(places) else None
            if next_place:
                buy_px = None
                try:
                    buy_px = float(next_place.get("price") or c.get("price") or 0) or None
                except Exception:
                    pass
                gaps.append((c_ts, next_place["ts"], buy_px))
        total_gaps += len(gaps)

        # Fetch candles for this symbol
        candles = _fetch_candles(sym, hours)
        if not candles:
            print(f"     [no candles — Coinbase fetch failed or no data]")
            continue

        # For each gap, check if any candle low ≤ buy_px within the gap window
        missed_in_gap = 0
        for (g_start, g_end, buy_px) in gaps:
            if not buy_px:
                continue
            # Candle timestamps are minute-aligned start
            for c in candles:
                c_start = c["start"]
                c_end = c_start + 60
                # Any overlap between candle window [c_start, c_end] and gap
                # window [g_start, g_end]?
                if c_end < g_start or c_start > g_end:
                    continue
                if c["low"] <= buy_px:
                    missed_in_gap += 1
                    print(f"     ⚠ MISSED IN GAP: {_fmt_ts(c_start)} "
                          f"low=${c['low']} ≤ buy_px=${buy_px}   "
                          f"(gap window {_fmt_ts(g_start)} → {_fmt_ts(g_end)}, "
                          f"{g_end - g_start:.1f}s)")
                    break
        if missed_in_gap:
            total_missed += missed_in_gap
            print(f"     TOTAL MISSED IN GAPS: {missed_in_gap}")

        # ALSO check: did ANY candle low ≤ latest buy_px WITHOUT a fill?
        # (touch outside gap = we WERE covered but didn't fill = different bug)
        latest_buy_px = None
        if places:
            try:
                latest_buy_px = float(places[-1].get("price") or 0) or None
            except Exception:
                pass
        if latest_buy_px:
            touches_outside_gaps = 0
            for c in candles:
                if c["low"] > latest_buy_px:
                    continue
                # was this candle inside a gap?
                c_start = c["start"]
                c_end = c_start + 60
                inside_gap = any(not (c_end < g[0] or c_start > g[1]) for g in gaps)
                if inside_gap:
                    continue
                touches_outside_gaps += 1
            if touches_outside_gaps and not fills:
                print(f"     ⚠ WOULD-FILL TOUCHES OUTSIDE GAPS: {touches_outside_gaps} "
                      f"(candles with low ≤ ${latest_buy_px} while we HAD an order — "
                      f"different bug: maybe post_only rejected, or order was stale)")
        print()

    # ---- 3) Rollup ---------------------------------------------------------
    print(f"\nROLLUP: {total_gaps} cancel-replace coverage gaps, "
          f"{total_missed} definite in-gap missed touches")
    if total_missed > 0:
        print("\n   ROOT CAUSE CONFIRMED: reeval cancel-replace churn is missing fills.")
        print("   FIX: add a min-drift gate to _reeval_cancel_replace so it only")
        print("        replaces when new_buy_px differs from current buy_px by ≥ N%.")
        print("        Or: switch to Coinbase's edit_order primitive (atomic replace,")
        print("        no coverage gap).")
    else:
        print("\n   No in-gap misses found. Different root cause — check for:")
        print("     - post_only rejections in the trade log")
        print("     - orders placed at rounded prices vs actual candle lows")
        print("     - real-time price differing from 1-min candle low")


if __name__ == "__main__":
    main()
