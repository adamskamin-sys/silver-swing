"""Find historical silent-zero credit cycles (pre-c1eba78 HYPE class bug).

Adam 2026-07-15: HYPE lost +$15 to a silent-zero credit — cycles++ fired
with profit=0 because own_avg_entry was None. The fix (c1eba78) prevents
FUTURE bugs, but any missed cycles that happened BEFORE the fix already
lost their profit.

This script scans the trade log for suspect events + sleeves with cycles
> 0 but recent_cycle_pnls showing $0 entries. Reports what to reconcile
via diag_force_credit_cycle.py.

Suspect patterns:
  (A) resting_stop_filled_credited events with profit == 0 (silent-zero)
  (B) resting_stop_filled_credited events with own_avg_entry None/0
  (C) sleeves with recent_cycle_pnls containing [0.0, ...] entries
  (D) sleeves with cycles > 0 but realized_pnl ≈ 0 (all cycles zeroed)

Read-only. Usage:
    python3 diag_find_missed_cycles.py                  # scan all
    python3 diag_find_missed_cycles.py PRODUCT_ID       # one product
    python3 diag_find_missed_cycles.py PRODUCT_ID SLEEVE_ID
"""
from __future__ import annotations
import os
import sys
import time
from collections import defaultdict


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(float(ts)))
    except Exception:
        return "?"


def main() -> None:
    product_filter = sys.argv[1] if len(sys.argv) > 1 else None
    sleeve_filter = sys.argv[2] if len(sys.argv) > 2 else None
    tenant = "adam-live"

    print("=" * 78)
    print(f"MISSED-CYCLE AUDIT — {tenant}"
          + (f"  product={product_filter}" if product_filter else "")
          + (f"  sleeve={sleeve_filter}" if sleeve_filter else ""))
    print("=" * 78)

    # [1] Scan trade log for suspect credit events
    print(f"\n[1] TRADE LOG SCAN — suspect resting_stop_filled_credited events:")
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    except Exception as e:
        print(f"    ✗ trade log load failed: {e}")
        return

    suspect_events = []
    total_credits = 0
    for e in log.events():
        if not isinstance(e, dict):
            continue
        if e.get("event_type") != "resting_stop_filled_credited":
            continue
        sym = str(e.get("symbol") or "")
        sid = str(e.get("sleeve_id") or "")
        if product_filter and sym != product_filter:
            continue
        if sleeve_filter and sid != sleeve_filter:
            continue
        total_credits += 1
        profit = e.get("profit")
        own_avg = e.get("own_avg_entry")
        # Suspect patterns
        is_silent_zero = (profit == 0 or profit == 0.0)
        is_missing_avg = (own_avg is None or own_avg == 0 or own_avg == 0.0)
        if is_silent_zero or is_missing_avg:
            suspect_events.append({
                "ts": e.get("ts"),
                "symbol": sym,
                "sleeve_id": sid,
                "sleeve_name": e.get("sleeve_name"),
                "profit": profit,
                "own_avg": own_avg,
                "fill_price": e.get("fill_price"),
                "filled_qty": e.get("filled_qty"),
                "oid": e.get("oid"),
                "reason": "silent_zero" if is_silent_zero else "missing_own_avg",
            })
    print(f"    total credit events scanned:  {total_credits}")
    print(f"    suspect (zero-profit or missing avg): {len(suspect_events)}")
    if suspect_events:
        print(f"\n    Suspect events:")
        for ev in suspect_events[-30:]:
            print(f"    · {_fmt_ts(ev['ts'])}  {ev['symbol']:22s} sleeve={ev['sleeve_id']}")
            print(f"      profit=${ev['profit']}  own_avg={ev['own_avg']}  "
                  f"fill=${ev['fill_price']}  qty={ev['filled_qty']}  "
                  f"[{ev['reason']}]")
    else:
        print(f"    · none — no historical silent-zero credits found ✓")

    # [2] State-based scan: sleeves with cycles > 0 but suspect realized
    print(f"\n[2] STATE SCAN — sleeves with cycles > 0 but zero-heavy realized:")
    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    suspect_sleeves = []
    for tid in store.list_tenants():
        if tid != tenant:
            continue
        for sym in store.list_symbols(tid):
            if product_filter and sym != product_filter:
                continue
            state = store.get_state(tid, sym) or {}
            sleeves_state = state.get("sleeves") or {}
            for sid, ss in sleeves_state.items():
                if sleeve_filter and sid != sleeve_filter:
                    continue
                cycles = int(ss.get("cycles") or 0)
                realized = float(ss.get("realized_pnl") or 0)
                recent = list(ss.get("recent_cycle_pnls") or [])
                zero_count = sum(1 for p in recent if p == 0 or p == 0.0)
                # Suspect if cycles > 0 AND (realized ≈ 0 OR any zero cycle in recent)
                if cycles > 0 and (abs(realized) < 0.01 or zero_count > 0):
                    suspect_sleeves.append({
                        "symbol": sym,
                        "sleeve_id": sid,
                        "cycles": cycles,
                        "realized_pnl": realized,
                        "recent_zeros": zero_count,
                        "recent": recent,
                        "own_avg_entry": ss.get("own_avg_entry"),
                    })
    print(f"    suspect sleeves: {len(suspect_sleeves)}")
    if suspect_sleeves:
        for s in suspect_sleeves:
            print(f"\n    · {s['symbol']:22s} sleeve={s['sleeve_id']}")
            print(f"      cycles={s['cycles']}  realized=${s['realized_pnl']:.2f}  "
                  f"zero_cycles_in_recent={s['recent_zeros']}/{len(s['recent'])}")
            print(f"      own_avg_entry={s['own_avg_entry']}")
            print(f"      recent_cycle_pnls: {s['recent']}")
    else:
        print(f"    · none — no state-level suspects found ✓")

    # [3] Actionable reconciliation guidance
    print(f"\n[3] TO RECONCILE:")
    if not suspect_events and not suspect_sleeves:
        print(f"    · nothing to reconcile — all cycles account for their profit ✓")
        print("=" * 78)
        return
    print(f"    For each suspect event/sleeve above:")
    print(f"      1. Cross-reference with Coinbase Fills to find the ACTUAL")
    print(f"         buy_price (own_avg) and sell_price (fill_price)")
    print(f"      2. Run diag_force_credit_cycle.py to backfill:")
    print(f"           python3 diag_force_credit_cycle.py \\")
    print(f"             PRODUCT_ID SLEEVE_ID FILL_PRICE OWN_AVG QTY --apply")
    print(f"      3. Bot will pick up the state_patch on next tick.")
    print("=" * 78)


if __name__ == "__main__":
    main()
