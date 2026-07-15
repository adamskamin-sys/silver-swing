"""Recent ZEC events via the proper trade-log interface (Redis-backed).

Adam 2026-07-15: grep on JSONL returned nothing because the log lives
in Redis in production. This uses safety.make_trade_log which reads
whichever backend is configured, so it works in Render + local.

Read-only. Usage:
    python3 diag_zec_recent.py           # last 60 min
    python3 diag_zec_recent.py 120       # last 120 min
    python3 diag_zec_recent.py 60 HYP    # switch product filter
"""
from __future__ import annotations
import os
import sys
import time


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "?"


def main() -> None:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    filter_arg = sys.argv[2].upper() if len(sys.argv) > 2 else "ZEC"

    print("=" * 78)
    print(f"RECENT EVENTS — last {minutes:.0f}min matching '{filter_arg}'")
    print("=" * 78)

    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    except Exception as e:
        print(f"trade log load failed: {e}")
        return

    cutoff = time.time() - (minutes * 60)
    events = [e for e in log.events()
              if isinstance(e, dict) and float(e.get("ts", 0) or 0) >= cutoff]
    # Filter by symbol OR sleeve_id-containing OR event_type-containing the filter
    matches = []
    for e in events:
        sym = str(e.get("symbol") or "")
        if filter_arg in sym.upper():
            matches.append(e)
    matches.sort(key=lambda e: float(e.get("ts") or 0))

    if not matches:
        print(f"\nNo events matching {filter_arg} in the last {minutes}min.")
        return

    print(f"\n{len(matches)} events:\n")
    for e in matches:
        ts = _fmt_ts(e.get("ts"))
        et = e.get("event_type", "?")
        # Interesting fields to show
        details = []
        for k in ("side", "leg", "action", "price", "average_filled_price",
                  "limit_price", "stop_price", "target_px", "from_px", "to_px",
                  "stage", "old_buy_px", "new_buy_px", "reason", "why", "error",
                  "sleeve_id", "sleeve_name", "qty", "filled_qty", "oid",
                  "order_id", "old_order_id", "new_order_id",
                  "adopted_avg", "own_avg_entry", "position_qty",
                  "unclaimed_qty", "claimed_by_others",
                  "drift_pct", "will_skip", "current_buy_px",
                  "would_action", "would_new_buy_px", "mode", "cycles",
                  "realized_pnl"):
            if k in e and e[k] not in (None, "", 0, 0.0):
                v = e[k]
                if isinstance(v, float):
                    v = f"{v:.4f}"
                details.append(f"{k}={v}")
        detail_s = " ".join(details)
        print(f"   {ts}  {et:45s}  {detail_s[:180]}")


if __name__ == "__main__":
    main()
