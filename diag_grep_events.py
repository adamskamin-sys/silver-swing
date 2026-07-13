"""Diagnostic — grep recent trade-log events for red-flag patterns.

Usage:  python3 diag_grep_events.py [PATTERN] [--tail N] [--symbol SYMBOL]
Default PATTERN mirrors Adam's grep:
  expert_params_drift|__tuned_params__|tuned_at|HALT|kill.?switch|
  portfolio.?risk|reconcile|fee_gate_preview_failed|partial fill

Runs against Redis on Render (via make_trade_log) so you see prod events,
not the paper-tenant local trades.jsonl copy.
"""
from __future__ import annotations
import json
import os
import re
import sys
from collections import Counter

from safety import make_trade_log


DEFAULT_PATTERN = (r"expert_params_drift|__tuned_params__|tuned_at|HALT|"
                   r"kill.?switch|portfolio.?risk|reconcile|"
                   r"fee_gate_preview_failed|partial fill")


def main() -> None:
    args = sys.argv[1:]
    pattern = DEFAULT_PATTERN
    tail = 2000
    symbol_filter = None
    positional = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--tail" and i + 1 < len(args):
            tail = int(args[i + 1]); i += 2; continue
        if a == "--symbol" and i + 1 < len(args):
            symbol_filter = args[i + 1]; i += 2; continue
        positional.append(a); i += 1
    if positional:
        pattern = positional[0]

    rx = re.compile(pattern, re.IGNORECASE)
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = log.tail(tail)
    if symbol_filter:
        events = [e for e in events
                  if symbol_filter.upper() in (e.get("symbol") or "").upper()]

    matches = [e for e in events if rx.search(json.dumps(e, default=str))]
    print(f"{len(events)} events scanned, {len(matches)} match /{pattern}/")

    if not matches:
        return

    by_type = Counter(e.get("event_type") for e in matches)
    print("\nBy event_type:")
    for et, n in by_type.most_common():
        print(f"  {n:5d}  {et}")

    by_symbol = Counter((e.get("symbol") or "-") for e in matches)
    print("\nBy symbol:")
    for sym, n in by_symbol.most_common(15):
        print(f"  {n:5d}  {sym}")

    print("\nLast 20 matching events (full JSON):")
    for e in matches[-20:]:
        print(json.dumps(e, default=str))


if __name__ == "__main__":
    main()
