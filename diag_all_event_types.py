"""Show every event type + count + most recent age. Sanity-check
whether the trade log is populated and what activity is happening."""
import os, time
from collections import Counter


def main():
    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = log.tail(2000)
    print(f"total events read: {len(events)}")
    if not events:
        print("(trade log is EMPTY — bot may have just restarted, "
              "or store backend changed)")
        return
    now = time.time()
    latest = max(events, key=lambda e: float(e.get("ts") or 0))
    oldest = min(events, key=lambda e: float(e.get("ts") or 0))
    print(f"newest event: {int(now - float(latest.get('ts') or 0))}s ago  "
          f"({latest.get('event_type')} on {latest.get('symbol')})")
    print(f"oldest event: {int(now - float(oldest.get('ts') or 0))}s ago")

    by_type = Counter(e.get("event_type") for e in events)
    print()
    print(f"{'count':<7} {'event_type':<55} newest_age")
    print("-" * 96)
    for et, cnt in by_type.most_common(50):
        et_events = [e for e in events if e.get("event_type") == et]
        if et_events:
            latest_et = max(et_events, key=lambda e: float(e.get("ts") or 0))
            age = int(now - float(latest_et.get("ts") or 0))
            sym = latest_et.get("symbol") or ""
            print(f"{cnt:<7} {(et or '?'):<55} {age:>5}s  ({sym})")

    print()
    print("SYMBOLS seen (last 2000 events):")
    by_sym = Counter(e.get("symbol") for e in events if e.get("symbol"))
    for sym, cnt in by_sym.most_common(30):
        print(f"  {cnt:>4}  {sym}")


if __name__ == "__main__":
    main()
