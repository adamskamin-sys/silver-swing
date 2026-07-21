"""Show recent order-cancel events + WHY the bot cancelled each one.

Adam 2026-07-21: Coinbase order history shows a big cancel burst
across 6 products at 14:30:04-14:30:12 CT, and same prices being
cancelled repeatedly (ETH Buy $1,873.5 x4, ZCASH Sell $562.15 x3).
Something is stuck in a cancel-replace loop.
"""
import os
import time
from collections import Counter


def main():
    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = log.tail(1000)

    # Event types that indicate a cancel action
    cancel_types = [et for et in set(e.get("event_type") for e in events)
                    if et and ("cancel" in et.lower() or "cancelled" in et.lower())]
    print(f"Cancel event types seen (last 1000 events):")
    for et in sorted(cancel_types):
        cnt = sum(1 for e in events if e.get("event_type") == et)
        print(f"  {cnt:>4}  {et}")

    print()
    print("=" * 90)
    print("MOST RECENT 30 CANCEL EVENTS  (chronological)")
    print("=" * 90)
    cancel_events = [e for e in events if any(
        k in (e.get("event_type") or "") for k in
        ("cancel", "reprice"))]
    for e in cancel_events[-30:]:
        ts = float(e.get("ts") or 0)
        age = int(time.time() - ts)
        et = e.get("event_type") or ""
        sym = e.get("symbol") or ""
        sid = e.get("sleeve_id") or ""
        reason = str(e.get("reason") or "")[:100]
        print(f"{age:>5}s  {et:<50}  {sym:<20}  {sid}")
        if reason:
            print(f"        reason: {reason}")
        # Show additional fields when present
        for k in ("cancelled_oid", "old_px", "new_sell_px", "stale_px",
                  "correct_sell_px", "old_oid", "reload_px", "buy_px"):
            v = e.get(k)
            if v is not None:
                print(f"        {k}: {v}")
        print()

    print("=" * 90)
    print("CANCEL COUNTS PER PRODUCT (last 1000 events)")
    print("=" * 90)
    cnt_by_pid = Counter(e.get("symbol") for e in cancel_events)
    for pid, n in cnt_by_pid.most_common():
        print(f"  {n:>4}  {pid}")


if __name__ == "__main__":
    main()
