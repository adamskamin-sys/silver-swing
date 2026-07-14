"""Fix the 2026-07-14 SLR re-arm bug: adam-live primary state stuck at
swing_qty=2 despite config.swing_qty=0. Every tick, the primary state
machine (ARMED_SELL, live_order_id cleared after cancellation) re-arms
`sell 2 @ $65.25`.

This script:
  1. Reads adam-live/SLR-27AUG26-CDE STATE
  2. Shows the current values you're about to change
  3. If --confirm: sets state.swing_qty=0, state.state=HALTED,
     state.live_order_id=None, adds halt_reason for the audit trail.

Effect:
  * Primary strategy for SLR on adam-live stops re-arming.
  * Sleeves (if any) keep working — they're independent of the primary.
  * The currently-OPEN Coinbase order (live_order_id=396c24c4...) is
    NOT cancelled by this script. You must cancel it manually on
    Coinbase after running this. Otherwise it can still fill.

Usage:
    python3 diag_fix_slr_primary.py            # PREVIEW ONLY
    python3 diag_fix_slr_primary.py --confirm  # WRITE
"""
from __future__ import annotations
import argparse
import json
import os
import time

from state_store import make_store


TENANT = "adam-live"
SYMBOL = "SLR-27AUG26-CDE"
NEW_HALT_REASON = ("manual fix 2026-07-14: config.swing_qty=0 but "
                   "state.swing_qty=2 caused unwanted re-arm at $65.25 "
                   "sell every tick after cancellation. Primary halted "
                   "until manual review.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true",
                    help="actually write the fix (default: preview only)")
    args = ap.parse_args()

    store = make_store(os.getenv("SWING_DATA_DIR", "data"))
    state = store.get_state(TENANT, SYMBOL) or {}

    print(f"BEFORE ({TENANT}/{SYMBOL}):")
    keep = ["state", "swing_qty", "live_order_id", "halt_reason",
            "filled_qty", "reserved_margin", "trail_armed",
            "trail_high_water_price"]
    for k in keep:
        print(f"  {k:<26} = {state.get(k)!r}")

    if not args.confirm:
        print("\nPREVIEW only. To WRITE, re-run with --confirm.")
        print("\nProposed change:")
        print("  state                     = 'HALTED'")
        print("  swing_qty                 = 0")
        print("  live_order_id             = None")
        print("  halt_reason               = (see script)")
        print("\nAfter fix:")
        print("  * Primary stops re-arming SLR sells on adam-live.")
        print("  * Sleeves keep working.")
        print("  * You MUST manually cancel the open $65.25 sell on Coinbase.")
        return

    new_state = dict(state)
    new_state["state"] = "HALTED"
    new_state["swing_qty"] = 0
    new_state["live_order_id"] = None
    new_state["halt_reason"] = NEW_HALT_REASON
    new_state["_manual_fix_ts"] = time.time()
    store.put_state(TENANT, SYMBOL, new_state)

    print(f"\nAFTER ({TENANT}/{SYMBOL}):")
    for k in keep:
        print(f"  {k:<26} = {new_state.get(k)!r}")
    print(f"\n  → Wrote. Primary is now HALTED on {TENANT}/{SYMBOL}.")
    print(f"  → NEXT: cancel the open $65.25 sell on Coinbase manually.")
    print(f"     (live_order_id was {state.get('live_order_id')})")


if __name__ == "__main__":
    main()
