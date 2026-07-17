"""Dump EVERY trade-log event for a symbol/sleeve — no filter.

Adam 2026-07-17: diag_symbol_price_trace filtered out the event that
actually SET XLP buy_px to $0.00055. We need to see everything for
that sleeve without pre-selection to identify the culprit.

Read-only. Prints every event whose `symbol` OR `sleeve_id` matches
the argument. Truncates long payload fields but keeps everything else.

Usage:
    python3 diag_sleeve_all_events.py XLP-20DEC30-CDE
    python3 diag_sleeve_all_events.py scan-mro2eavr       # by sleeve_id
    python3 diag_sleeve_all_events.py XLP --hours 8
    python3 diag_sleeve_all_events.py XLP --grep buy_px   # only events whose payload contains 'buy_px'
"""
from __future__ import annotations
import argparse
import os
import sys
import time


TENANT = "adam-live"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("needle")
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--grep", default=None,
                    help="only show events whose event_type or payload contains this string")
    ap.add_argument("--data-dir", default=os.getenv("SWING_DATA_DIR", "data"))
    args = ap.parse_args()

    n = args.needle
    n_up = n.upper().replace("-", "").replace("_", "")

    print("=" * 90)
    print(f"ALL EVENTS matching {n!r} — last {args.hours}h"
          + (f" (grep={args.grep!r})" if args.grep else ""))
    print("=" * 90)

    cutoff = time.time() - args.hours * 3600.0
    events = []
    try:
        from safety import make_trade_log
        log = make_trade_log(args.data_dir)
        for e in log.events():
            try:
                if float(e.get("ts") or 0) < cutoff:
                    continue
            except (ValueError, TypeError):
                continue
            # Fuzzy match on symbol or sleeve_id
            sym = str(e.get("symbol") or "")
            sid = str(e.get("sleeve_id") or "")
            if (n_up not in sym.upper().replace("-", "").replace("_", "")
                    and n_up not in sid.upper().replace("-", "").replace("_", "")):
                continue
            if args.grep:
                etype = str(e.get("event_type") or "")
                if args.grep in etype:
                    events.append(e)
                    continue
                # Check payload
                try:
                    import json
                    if args.grep in json.dumps(e, default=str):
                        events.append(e)
                        continue
                except Exception:
                    pass
                continue
            events.append(e)
    except Exception as e:
        print(f"✗ trade log read failed: {type(e).__name__}: {e}")
        sys.exit(1)

    events.sort(key=lambda e: float(e.get("ts") or 0))

    if not events:
        print(f"\n(no events matched)")
        return

    print(f"\n{len(events)} events:\n")
    for e in events:
        ts = float(e.get("ts") or 0)
        age_min = (time.time() - ts) / 60.0
        etype = e.get("event_type") or "?"
        sym = e.get("symbol") or "-"
        sid = e.get("sleeve_id") or "-"
        # Payload = everything else, truncated
        payload = {k: v for k, v in e.items()
                    if k not in ("ts", "event_type", "symbol", "sleeve_id")}
        print(f"  {age_min:6.1f}min ago  [{sym}] {etype}"
              + (f"  sleeve={sid}" if sid != "-" else ""))
        for k, v in payload.items():
            s = str(v)
            if len(s) > 140:
                s = s[:137] + "..."
            print(f"        {k} = {s}")
    print()
    print("=" * 90)


if __name__ == "__main__":
    main()
