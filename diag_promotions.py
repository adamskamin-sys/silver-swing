#!/usr/bin/env python3
"""Two-in-one diagnostic:
  1. Verify Redis has the promoted sell_px/buy_px/trail_distance for
     XLP-20DEC30-CDE, PT-28SEP26-CDE, SLR-27AUG26-CDE (today's promotions).
     Compares actual → expected. If actual != expected, the promotion did
     not stick and the problem is in the store, not the dashboard.
  2. Show recent CU-27AUG26-CDE trade activity from the event log:
     cycle_completed (regular sell) + sleeve_stop_loss_triggered (stop-out).

Usage:
    SWING_TENANT=adam-live python3 diag_promotions.py
"""

from __future__ import annotations

import os
import sys


PROMOTED = [
    # (symbol, sleeve_key, expected_sell, expected_buy, expected_trail)
    ("XLP-20DEC30-CDE", "sell_px", 0.18925, 0.18853, 0.0075),
    ("PT-28SEP26-CDE", "sell_px", 1641.875, 1638.125, 0.75),
    ("SLR-27AUG26-CDE", "sell_px", 60.0675, 59.7125, 0.24375),
]


def _pick_tenant(store) -> str:
    env = os.getenv("SWING_TENANT")
    if env:
        return env
    live = [t for t in (store.list_tenants() or []) if t.endswith("-live")]
    return live[0] if live else "adam"


def main() -> int:
    data_dir = os.getenv("SWING_DATA_DIR", "data")
    from state_store import make_store
    store = make_store(data_dir)
    tenant = _pick_tenant(store)
    print(f"[diag] tenant={tenant!r}")
    print()

    # 1. Verify Redis has the promoted values
    print("=" * 78)
    print("PART 1 — Redis config check (are promotions actually in the store?)")
    print("=" * 78)
    ok_count = 0
    for sym, _, exp_sell, exp_buy, exp_trail in PROMOTED:
        cfg = store.get_config(tenant, sym)
        if not cfg:
            print(f"  {sym}: NO CONFIG FOUND")
            continue
        sleeves = cfg.get("sleeves") or []
        if not sleeves:
            print(f"  {sym}: NO SLEEVES in config")
            continue
        # Assume Model B is the first sleeve (matches promotion diffs)
        s = sleeves[0]
        got_sell = s.get("sell_px")
        got_buy = s.get("buy_px")
        got_trail = s.get("trail_distance")
        match_sell = got_sell == exp_sell
        match_buy = got_buy == exp_buy
        match_trail = got_trail == exp_trail
        all_match = match_sell and match_buy and match_trail
        flag = "✅ PROMOTED" if all_match else "❌ MISMATCH"
        print(f"\n  {sym}: {flag}")
        print(f"    sleeve[0] id={s.get('id')} name={s.get('name')!r}")
        print(f"    sell_px: got={got_sell} expected={exp_sell} {'✓' if match_sell else 'MISMATCH'}")
        print(f"    buy_px:  got={got_buy} expected={exp_buy} {'✓' if match_buy else 'MISMATCH'}")
        print(f"    trail:   got={got_trail} expected={exp_trail} {'✓' if match_trail else 'MISMATCH'}")
        if all_match:
            ok_count += 1

    print(f"\nPART 1 result: {ok_count}/{len(PROMOTED)} promotions confirmed in Redis")
    if ok_count == len(PROMOTED):
        print("→ Values ARE in Redis. If dashboard still shows old numbers, problem is")
        print("  dashboard-side: browser cache OR the dashboard service hasn't restarted.")
        print("  Fix: hard-refresh (Cmd-Shift-R) + Manual Deploy the dashboard service.")
    else:
        print("→ Some promotions did NOT land in Redis. Values may have been overwritten")
        print("  by the auto-apply expert tile if the Edit modal was opened + saved.")

    # 2. Recent CU trade events
    print()
    print("=" * 78)
    print("PART 2 — CU-27AUG26-CDE recent trade events")
    print("=" * 78)
    from safety import make_trade_log
    try:
        log = make_trade_log(data_dir)
        events = log.tail(10000)
    except Exception as e:
        print(f"  could not read trade log: {type(e).__name__}: {e}")
        return 1

    cu_events = [e for e in events
                 if str(e.get("tenant")) == tenant
                 and str(e.get("symbol")) == "CU-27AUG26-CDE"]
    print(f"  Total CU events in current 10k window: {len(cu_events)}")

    # Filter to cycle completions + stop-loss events
    interesting = [e for e in cu_events
                   if str(e.get("event_type")) in (
                       "cycle_completed", "sleeve_cycle_completed",
                       "stop_loss_triggered", "sleeve_stop_loss_triggered",
                       "order_filled", "sleeve_order_filled",
                       "manual_market_order", "manual_limit_order",
                   )]
    print(f"  CU trade-related events (fills / cycles / stop-loss): {len(interesting)}")

    if not interesting:
        print("\n  No cycle_completed or stop-loss events for CU in the recent window.")
        print("  If Coinbase shows position=0 for CU now but earlier showed qty=1, the")
        print("  fill was manual or fell outside this 10k window — check Coinbase's own")
        print("  order history for the timestamp + fill price.")
        return 0

    print("\n  Most recent 5 CU trade events (newest last):")
    for e in interesting[-5:]:
        et = e.get("event_type")
        ts = e.get("ts")
        gross = e.get("gross")
        fill = e.get("average_filled_price") or e.get("fill_price")
        sleeve = e.get("sleeve_name") or e.get("sleeve_id") or "primary"
        line = f"    {et:32s} sleeve={sleeve!s:20s}"
        if fill is not None:
            line += f" fill=${fill}"
        if gross is not None:
            line += f" realized_delta=${gross:+.2f}"
        print(line)

    # Sum gross across the recent CU events
    total = sum(float(e.get("gross") or 0) for e in cu_events)
    print(f"\n  Sum of gross across ALL {len(cu_events)} CU events in this window: ${total:+.2f}")
    print("  (This is realized in the CURRENT 10k window only — older cycles are trimmed.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
