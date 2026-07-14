"""Fix the 2026-07-14 SLR re-arm bug: adam-live primary state stuck at
swing_qty=2 despite config.swing_qty=0. Every tick, the primary state
machine (ARMED_SELL, live_order_id cleared after cancellation) re-arms
`sell 2 @ $65.25`.

⚠️  BOT MUST BE SUSPENDED BEFORE RUNNING WITH --confirm  ⚠️

The bot has authoritative in-memory state that OVERWRITES Redis writes
on every tick. If you run this while the bot is running, the bot's
next tick undoes your write within seconds. Symptom you'll see: state
looks fixed for a moment, then re-arms.

CORRECT SEQUENCE:
  1. Render dashboard → silver-swing-bot-live → Suspend Service
  2. Wait ~10s for the pod to actually stop (status shows "Suspended")
  3. python3 diag_fix_slr_primary.py --confirm
  4. Cancel the open Coinbase order (live_order_id printed at end)
  5. Render dashboard → Resume Service
  6. New pod loads the fixed state from Redis, keeps it.

If you skip step 1, your write gets clobbered.

This script:
  1. Reads adam-live/SLR-27AUG26-CDE STATE
  2. Checks last_heartbeat_ts — if fresh (<30s old), warns bot is
     likely running and REFUSES --confirm unless --force is set.
  3. Shows the current values you're about to change
  4. If --confirm (+ safe heartbeat): sets state.swing_qty=0,
     state.state=HALTED, state.live_order_id=None, adds halt_reason.

Effect:
  * Primary strategy for SLR on adam-live stops re-arming.
  * Sleeves (if any) keep working — they're independent of the primary.
  * The currently-OPEN Coinbase order is NOT cancelled by this script.
    You must cancel it manually on Coinbase after running this.

Usage:
    python3 diag_fix_slr_primary.py            # PREVIEW ONLY
    python3 diag_fix_slr_primary.py --confirm  # WRITE (refuses if bot alive)
    python3 diag_fix_slr_primary.py --confirm --force  # WRITE anyway
"""
from __future__ import annotations
import argparse
import json
import os
import sys
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
    ap.add_argument("--force", action="store_true",
                    help="allow --confirm even if bot appears alive (dangerous)")
    args = ap.parse_args()

    store = make_store(os.getenv("SWING_DATA_DIR", "data"))
    state = store.get_state(TENANT, SYMBOL) or {}

    # Safety: refuse --confirm if bot is actively managing this scope.
    # Running bot has in-memory state that overwrites Redis writes on every
    # tick. Fix has to run WHILE BOT IS SUSPENDED. Two-signal guard —
    # either signal being alive => bot alive => refuse.
    if args.confirm and not args.force:
        refuse_reasons = []
        # Signal 1: live_order_id set = bot has active order = alive
        loid = state.get("live_order_id")
        if loid:
            refuse_reasons.append(
                f"state.live_order_id = {loid!r} (bot has an active order — "
                f"means bot is running and managing this scope)"
            )
        # Signal 2: fresh heartbeat (kept as belt-and-suspenders)
        hb = state.get("last_heartbeat_ts")
        if hb is not None:
            try:
                age = time.time() - float(hb)
                if age < 60.0:  # loosened from 30 → 60 (bot may write less often)
                    refuse_reasons.append(
                        f"last_heartbeat_ts was {age:.1f}s ago (threshold 60s)"
                    )
            except (TypeError, ValueError):
                pass
        if refuse_reasons:
            print("REFUSING --confirm:")
            for r in refuse_reasons:
                print(f"  * {r}")
            print()
            print("The bot's in-memory state will overwrite this write within seconds")
            print("if it's running. CORRECT SEQUENCE:")
            print("  1. Render → silver-swing-bot-live → Suspend Service")
            print("  2. Wait ~15s for the pod to fully stop")
            print("  3. Re-run: python3 diag_fix_slr_primary.py --confirm")
            print("  4. Cancel the open Coinbase order")
            print("  5. Render → Resume Service")
            print()
            print("(If you're sure the bot is stopped, add --force to override.)")
            sys.exit(2)

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
