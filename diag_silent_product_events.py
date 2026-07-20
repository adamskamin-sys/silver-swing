"""Dump the last N events for the silent products (any event_type)
so we can see what's actually happening — or not happening — that's
keeping XLP + ZEC from ticking.

Adam 2026-07-19: diag_throttle_check showed 0 feed_stale, 0
reconnect, 0 step_failure for ZEC — yet ZEC has been silent 1h.
Either the silence has no logged cause (bot doesn't know it's stale)
or the events use names my earlier grep missed. This dumps EVERY
event for the named products so we can see the real story.

Read-only. Usage:

    python3 diag_silent_product_events.py                       # XLP + ZEC last 40
    python3 diag_silent_product_events.py SYM1 SYM2 ...         # any pids, last 40
    python3 diag_silent_product_events.py --limit=80            # bump event count
"""
from __future__ import annotations
import os
import sys
import time
from collections import Counter


def main() -> None:
    limit = 40
    args = []
    for a in sys.argv[1:]:
        if a.startswith("--limit="):
            try: limit = int(a.split("=", 1)[1])
            except ValueError: pass
        elif not a.startswith("--"):
            args.append(a.upper())
    if not args:
        args = ["XLP-20DEC30-CDE", "ZEC-20DEC30-CDE"]

    print("=" * 78)
    print(f"SILENT-PRODUCT EVENT DUMP — {', '.join(args)} (last {limit} each)")
    print("=" * 78)

    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = log.tail(20000) if hasattr(log, "tail") else []

    now = time.time()
    for pid in args:
        pid_events = [e for e in events
                      if isinstance(e, dict)
                      and (e.get("symbol") == pid
                           or e.get("product_id") == pid)]
        pid_events.sort(key=lambda e: float(e.get("ts") or 0))

        print(f"\n{'=' * 78}")
        print(f"{pid} — total events in log: {len(pid_events)}")
        print(f"{'=' * 78}")
        if not pid_events:
            print(f"  ✗ ZERO events. Track was NEVER created, OR silent from boot.")
            continue

        # Age of newest event
        newest = pid_events[-1]
        newest_age = int(now - float(newest.get("ts", 0)))
        print(f"  newest event: {newest_age}s ago  ({newest.get('event_type')})")

        # Frequency of event types
        types = Counter(str(e.get("event_type") or "?") for e in pid_events)
        print(f"\n  event_type frequency (top 15):")
        for et, cnt in types.most_common(15):
            print(f"    {cnt:>5d}  {et}")

        # Last N events with details
        print(f"\n  last {limit} events (oldest → newest):")
        for e in pid_events[-limit:]:
            ts_age = int(now - float(e.get("ts", 0)))
            et = e.get("event_type") or "?"
            sev = e.get("severity", "info")
            sid = e.get("sleeve_id") or ""
            # Interesting extras — dump anything that isn't the boilerplate
            extras = {k: v for k, v in e.items()
                      if k not in ("ts", "event_type", "symbol", "product_id",
                                    "tenant", "severity", "sleeve_id", "sleeve_name")}
            extras_str = ""
            if extras:
                # Truncate long values
                extras_str = " " + " ".join(
                    f"{k}={str(v)[:80]}" for k, v in list(extras.items())[:6])
            print(f"    [{ts_age:>6d}s ago] {sev[:4]:4s} {et:38s} sleeve={sid[:14]:14s}"
                  f"{extras_str}")


if __name__ == "__main__":
    main()
