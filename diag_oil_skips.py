"""Diagnostic — why is a sleeve not re-entering?

Usage:  python3 diag_oil_skips.py [SYMBOL]
Default SYMBOL = OIL-20JUL26-CDE.

Reads recent trade events (Redis on Render, trades.jsonl locally) and prints
the count of entry-blocked events (arm_skipped_*, arm_refused, fee_gate_halt,
*_halt) plus the last 10 skips with reason. That tells you which gate has
been blocking the rebuy.
"""
from __future__ import annotations
import os
import sys
from collections import Counter

from safety import make_trade_log


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "OIL-20JUL26-CDE"
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = [e for e in log.tail(5000) if e.get("symbol") == symbol]

    def is_block(e: dict) -> bool:
        et = e.get("event_type", "")
        return ("skip" in et.lower()
                or "refused" in et
                or "halt" in et
                or "gate" in et)

    blocked = [e for e in events if is_block(e)]
    print(f"{symbol}: {len(events)} events, {len(blocked)} entry-blocked")

    if not blocked:
        print("  (no entry-block events — check state, not gates)")
        return

    print("\nCounts by event_type:")
    for et, n in Counter(e.get("event_type") for e in blocked).most_common():
        print(f"  {n:4d}  {et}")

    print("\nLast 15 skips:")
    for e in blocked[-15:]:
        et = e.get("event_type", "")
        sn = (e.get("sleeve_name") or "?")[:38]
        reason = e.get("reason", "-")
        extra = ""
        for k in ("mark", "buy_px", "sell_px", "vpin", "book_imbalance"):
            if e.get(k) is not None:
                extra += f" {k}={e[k]}"
        print(f"  {et:38s} {sn:38s} reason={reason}{extra}")


if __name__ == "__main__":
    main()
