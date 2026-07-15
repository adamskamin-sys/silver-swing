"""Clear the SLR-27AUG26-CDE halt on adam-live and queue a resume intent.

The safety_halt "reason: unspecified" alert has been firing repeatedly
because SLR's sleeve state is HALTED. This script queues a resume_intent
in the store; the bot picks it up on the next tick and clears the halt.

Usage (Render silver-swing-bot-live shell):
    python3 diag_resume_slr.py           # preview only
    python3 diag_resume_slr.py --confirm # actually queue the resume

Safe: only writes resume_intent (not the state itself). Bot's own resume
logic decides what to do with the intent — same code path as the
dashboard's Resume button.
"""
from __future__ import annotations
import argparse
import os
import sys
import time

from state_store import make_store


TENANT = "adam-live"
SYMBOL = "SLR-27AUG26-CDE"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true",
                    help="actually write the resume_intent (default: preview)")
    args = ap.parse_args()

    store = make_store(os.getenv("SWING_DATA_DIR", "data"))
    state = store.get_state(TENANT, SYMBOL) or {}
    current_state = str(state.get("state") or "?").upper()
    halt_reason = state.get("halt_reason") or "(none)"

    print("=" * 60)
    print(f"BEFORE ({TENANT}/{SYMBOL}):")
    print(f"  state         = {current_state}")
    print(f"  halt_reason   = {halt_reason!r}")
    print(f"  swing_qty     = {state.get('swing_qty', '?')}")
    print(f"  live_order_id = {state.get('live_order_id') or '(none)'}")
    print()

    if current_state != "HALTED":
        print(f"NOTE: state is not HALTED ({current_state}). Resume may be a no-op.")
        print()

    print("PROPOSED ACTION:")
    print(f"  Queue resume_intent scope on {TENANT}/{SYMBOL}.")
    print("  Bot picks it up on next tick and calls its own resume logic.")
    print("  Same code path as the dashboard's Resume button.")
    print()

    if not args.confirm:
        print("PREVIEW only. Add --confirm to write.")
        return

    resume_intent = {
        "requested_ts": time.time(),
        "requested_by": "diag_resume_slr",
        "previous_reason": halt_reason,
    }
    # Use put_state on a synthetic scope? No — the resume_intent is its own
    # scope. Write via store's generic put mechanism. Check API.
    try:
        # Try dedicated method first
        store.put_resume_intent(TENANT, SYMBOL, resume_intent)
    except AttributeError:
        # Fall back to direct scope write via internal helper if needed
        print("ERROR: store.put_resume_intent not available on this backend.")
        sys.exit(2)

    print(f"AFTER: resume_intent queued on {TENANT}/{SYMBOL}.")
    print()
    print("Watch the bot logs for one of:")
    print("  * sleeve_resume_success — halt cleared, state restored")
    print("  * sleeve_resume_failed  — halt persists, check logs for reason")
    print()
    print("If the halt persists after 60 seconds, the sleeve might be")
    print("halted for a REAL reason (auth failure, margin breach, etc.)")
    print("— check bot logs for the underlying error.")


if __name__ == "__main__":
    main()
