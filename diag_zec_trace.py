"""Trace all ZEC (or any specified product) activity in the trade log.

Shows:
  1. Total event types + counts for the product (activity level)
  2. Non-primary track events (create / evict / fail — did the track come up?)
  3. Any sleeve_arm_failed, place_failed, error, or PRECISION events
  4. Most recent 10 events regardless of type

Usage (Render silver-swing-bot-live shell):
    python3 diag_zec_trace.py                     # defaults to ZEC-20DEC30-CDE
    python3 diag_zec_trace.py XLP-20DEC30-CDE     # any product
"""
from __future__ import annotations
import os
import sys


DEFAULT_PRODUCT = "ZEC-20DEC30-CDE"


def main() -> None:
    product = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PRODUCT
    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = list(log.events())[-5000:]

    hits = [e for e in events if product in str(e)]
    print("=" * 70)
    print(f"TRACE for {product} — last 5000 events, {len(hits)} mention it")
    print("=" * 70)

    if not hits:
        print()
        print("NO EVENTS mention this product in the last 5000 trade log entries.")
        print("Either:")
        print("  * The sleeve isn't being ticked (main loop skipping it)")
        print("  * The non-primary track failed to come up (check bot logs)")
        print("  * Event logging is broken for this product")
        return

    # ---- 1) Event type counts
    counts = {}
    for e in hits:
        et = str(e.get("event_type", "?"))
        counts[et] = counts.get(et, 0) + 1
    print()
    print("EVENT TYPE COUNTS:")
    for et, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {n:5d}  {et}")

    # ---- 2) Non-primary track events
    np_events = [e for e in hits
                 if "non-primary" in str(e).lower() or "non_primary" in str(e).lower()]
    print()
    print(f"NON-PRIMARY TRACK EVENTS ({len(np_events)}):")
    if np_events:
        for e in np_events[-10:]:
            print(f"  {e.get('ts', '?')} {e.get('event_type')} "
                  f"reason={e.get('reason') or e.get('error') or ''}")
    else:
        print("  (none — non-primary track may not be logging or wasn't created)")

    # ---- 3) Fail / error / precision events
    fail_events = [e for e in hits
                   if any(needle in str(e).lower()
                          for needle in ("fail", "error", "precision", "invalid"))]
    print()
    print(f"FAIL / ERROR EVENTS ({len(fail_events)}):")
    if fail_events:
        for e in fail_events[-15:]:
            msg = e.get("error") or e.get("reason") or e.get("message") or ""
            print(f"  {e.get('ts', '?')} {e.get('event_type')}: {str(msg)[:100]}")
    else:
        print("  (none)")

    # ---- 4) Most recent 15 events regardless of type
    print()
    print(f"MOST RECENT 15 EVENTS FOR {product}:")
    for e in hits[-15:]:
        details = {}
        for key in ("side", "qty", "price", "buy_px", "sell_px", "reason", "error"):
            if e.get(key) is not None:
                details[key] = e.get(key)
        detail_str = ", ".join(f"{k}={v}" for k, v in details.items())[:80]
        print(f"  {e.get('ts', '?')} {e.get('event_type', '?'):<40} {detail_str}")


if __name__ == "__main__":
    main()
