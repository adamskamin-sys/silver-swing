"""Clear a per-product HALT directly in state (bypasses the bot).

Adam 2026-07-15: AVE was retired via diag_retire_sleeves.py — sleeves
are gone but the top-level `state.state == 'HALTED'` remains, so the
dashboard shows a stuck "Strategy halted" banner. Clicking Resume
writes a `resume_intent`, but that only fires when a SwingTrader is
ticking this product. With 0 sleeves, no trader spawns, no consumer.

This diag writes state.state directly. Only safe when no trader is
managing the product (which is the exact case for retired products).

Guards:
  * Refuses --apply if last_heartbeat_ts < 60s or live_order_id set
    (both signal a trader is alive and will clobber the write).
  * --force overrides the guard.

Usage:
    python3 diag_clear_product_halt.py PRODUCT_ID              # preview
    python3 diag_clear_product_halt.py PRODUCT_ID --apply      # write
    python3 diag_clear_product_halt.py PRODUCT_ID --apply --force
"""
from __future__ import annotations
import argparse
import os
import sys
import time

from state_store import make_store


TENANT = "adam-live"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("product_id")
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default: preview)")
    ap.add_argument("--force", action="store_true",
                    help="override liveness guard (dangerous)")
    args = ap.parse_args()

    pid = args.product_id
    store = make_store(os.getenv("SWING_DATA_DIR", "data"))
    state = store.get_state(TENANT, pid) or {}

    print(f"BEFORE ({TENANT}/{pid}):")
    for k in ("state", "halt_reason", "swing_qty", "live_order_id",
              "filled_qty", "last_heartbeat_ts"):
        print(f"  {k:<22} = {state.get(k)!r}")

    if state.get("state") != "HALTED":
        print(f"\n  ✓ Not HALTED — nothing to clear. Current state = "
              f"{state.get('state')!r}")
        return

    # Liveness guard
    if args.apply and not args.force:
        reasons = []
        loid = state.get("live_order_id")
        if loid:
            reasons.append(f"live_order_id={loid!r} — trader has an active order")
        hb = state.get("last_heartbeat_ts")
        if hb is not None:
            try:
                age = time.time() - float(hb)
                if age < 60.0:
                    reasons.append(f"last_heartbeat_ts age={age:.1f}s (threshold 60s)")
            except (TypeError, ValueError):
                pass
        if reasons:
            print("\nREFUSING --apply — signs a trader is running:")
            for r in reasons:
                print(f"  * {r}")
            print("\nA running trader will clobber this write on its next tick.")
            print("Options:")
            print("  1. Suspend the bot on Render, wait 15s, re-run --apply")
            print("  2. Re-run with --force if you're sure no trader manages this product")
            sys.exit(2)

    if not args.apply:
        print(f"\nPREVIEW — proposed change:")
        print(f"  state         = 'ARMED_SELL'")
        print(f"  halt_reason   = None")
        print(f"  live_order_id = None")
        print(f"  filled_qty    = 0")
        print(f"\nRe-run with --apply to write.")
        return

    new_state = dict(state)
    new_state["state"] = "ARMED_SELL"
    new_state["last_halt_reason"] = state.get("halt_reason")
    new_state["halt_reason"] = None
    new_state["live_order_id"] = None
    new_state["filled_qty"] = 0
    new_state["_halt_cleared_via_diag_ts"] = time.time()
    store.put_state(TENANT, pid, new_state)

    print(f"\nAFTER ({TENANT}/{pid}):")
    for k in ("state", "halt_reason", "swing_qty", "live_order_id"):
        print(f"  {k:<22} = {new_state.get(k)!r}")
    print(f"\n  ✓ Halt cleared. Dashboard banner will drop on next refresh.")

    # Audit trail
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        log.record("product_halt_cleared_via_diag",
                   tenant=TENANT, symbol=pid,
                   previous_reason=state.get("halt_reason"),
                   severity="info",
                   reason="cleared via diag_clear_product_halt.py — "
                          "retired product with stuck HALT banner")
    except Exception:
        pass


if __name__ == "__main__":
    main()
