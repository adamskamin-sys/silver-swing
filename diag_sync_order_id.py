"""Manually write a live order ID to a sleeve's state.

Emergency use only: when place_limit succeeded at Coinbase but the sleeve
state wasn't updated (e.g., diag_force_arm_missing_orders returned early
due to a bug), the bot doesn't know about the order and could place a
duplicate. This script writes the order_id you already have back into
the sleeve state so the bot picks up where it should have.

Usage (Render silver-swing-bot-live shell):
    python3 diag_sync_order_id.py <PRODUCT_ID> <SLEEVE_ID> <ORDER_ID>
    python3 diag_sync_order_id.py <PRODUCT_ID> <SLEEVE_ID> --clear

Example (ZEC case, 2026-07-15):
    python3 diag_sync_order_id.py ZEC-20DEC30-CDE smrinj04e 0d127834-2999-4f60-977f-4aa4dfe95e7f

--clear mode: null out the sleeve's live_order_id (use if you synced
the wrong id — e.g., an order that already filled and shouldn't be
polled anymore).

Safety:
  * Refuses to overwrite an existing non-empty live_order_id (prevents
    accidental loss of tracking on a different in-flight order).
  * Refuses if the sleeve isn't in ARMED_BUY or ARMED_SELL state.
  * Prints before/after for audit.
"""
from __future__ import annotations
import os
import sys
import time

from state_store import make_store


TENANT = "adam-live"


def main() -> None:
    if len(sys.argv) < 4:
        print("Usage: python3 diag_sync_order_id.py <PRODUCT_ID> <SLEEVE_ID> <ORDER_ID_OR_--clear>")
        sys.exit(1)
    product_id = sys.argv[1]
    sleeve_id = sys.argv[2]
    third_arg = sys.argv[3]
    clear_mode = (third_arg == "--clear")
    order_id = None if clear_mode else third_arg

    store = make_store(os.getenv("SWING_DATA_DIR", "data"))
    state = store.get_state(TENANT, product_id) or {}
    sleeves_st = state.get("sleeves") or {}

    if sleeve_id not in sleeves_st:
        print(f"ERROR: sleeve {sleeve_id} not found on {TENANT}/{product_id}")
        print(f"Available sleeves: {list(sleeves_st.keys())}")
        sys.exit(2)

    ss = sleeves_st[sleeve_id]
    current_oid = ss.get("live_order_id")
    current_state = str(ss.get("state", "?")).upper()

    print(f"BEFORE ({TENANT}/{product_id}/{sleeve_id}):")
    print(f"  state         = {current_state}")
    print(f"  live_order_id = {current_oid or '(none)'}")
    print()

    # CLEAR mode — null out live_order_id (recovery for wrong-id syncs)
    if clear_mode:
        if not current_oid:
            print("Nothing to clear — live_order_id already empty.")
            return
        print(f"CLEARING live_order_id={current_oid}...")
        ss["live_order_id"] = None
        ss["_manual_clear_ts"] = time.time()
        ss["_manual_clear_note"] = "cleared via diag_sync_order_id.py --clear"
        state["sleeves"] = sleeves_st
        store.put_state(TENANT, product_id, state)
        after = store.get_state(TENANT, product_id) or {}
        after_ss = (after.get("sleeves") or {}).get(sleeve_id, {})
        print()
        print(f"AFTER ({TENANT}/{product_id}/{sleeve_id}):")
        print(f"  state         = {after_ss.get('state')}")
        print(f"  live_order_id = {after_ss.get('live_order_id') or '(none)'}")
        print()
        print("Bot's next tick will proceed as if no order is outstanding.")
        print("If the sleeve is ARMED_SELL and holding a position, it will")
        print("attempt to place a new SELL order via the normal arm path.")
        return

    # WRITE mode — safety checks
    if current_oid and str(current_oid).strip():
        print(f"REFUSING: sleeve already has live_order_id={current_oid}.")
        print("Cancel that order first (or verify it's the same one you're syncing)")
        print("and only overwrite via manual state edit if you're sure.")
        sys.exit(3)
    if current_state not in ("ARMED_BUY", "ARMED_SELL"):
        print(f"REFUSING: sleeve state is {current_state}, not ARMED_BUY/ARMED_SELL.")
        print("Order ID only meaningful when sleeve is armed.")
        sys.exit(3)

    print(f"WRITING order_id={order_id} to sleeve state...")
    ss["live_order_id"] = order_id
    ss["_manual_sync_ts"] = time.time()
    ss["_manual_sync_note"] = "synced via diag_sync_order_id.py"
    state["sleeves"] = sleeves_st
    store.put_state(TENANT, product_id, state)

    # Verify
    after = store.get_state(TENANT, product_id) or {}
    after_ss = (after.get("sleeves") or {}).get(sleeve_id, {})
    print()
    print(f"AFTER ({TENANT}/{product_id}/{sleeve_id}):")
    print(f"  state         = {after_ss.get('state')}")
    print(f"  live_order_id = {after_ss.get('live_order_id')}")
    print()
    print("Bot's next tick will poll the order status via order_id and pick up")
    print("the fill when it lands. Watch trade log for sleeve_on_fill event.")


if __name__ == "__main__":
    main()
