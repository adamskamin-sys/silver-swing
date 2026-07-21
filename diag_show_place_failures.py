"""Show recent profit_lock_limit_place_failed events with full error text.

Adam 2026-07-21: diag_bot_status shows 17 critical events out of last
50 = Phase A LIMIT placement is failing across the fleet. Need the
actual exception to fix.
"""
import os
import time
from collections import Counter


def main() -> None:
    print("=" * 96)
    print("RECENT LIMIT PLACE FAILURES")
    print("=" * 96)
    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = log.tail(500)

    # Group by event type
    fail_events = [e for e in events if "place_failed" in (e.get("event_type") or "")
                   or "cancel_failed" in (e.get("event_type") or "")]
    print(f"\n  {len(fail_events)} failure events in last 500")
    print(f"  by event_type: {Counter(e.get('event_type') for e in fail_events)}")
    print(f"  by symbol:     {Counter(e.get('symbol') for e in fail_events)}")

    # Show most recent 10 with full error text
    print(f"\n  MOST RECENT 10 (full details):")
    for e in fail_events[-10:]:
        ts = float(e.get("ts") or 0)
        age = int(time.time() - ts)
        print(f"\n  {age:>5}s ago  [{e.get('event_type')}]  {e.get('symbol')}")
        print(f"    sleeve: {e.get('sleeve_id')}")
        for k in ("sell_px", "target_px", "qty", "error", "reason", "oid"):
            v = e.get(k)
            if v is not None:
                print(f"    {k}: {v}")


if __name__ == "__main__":
    main()
