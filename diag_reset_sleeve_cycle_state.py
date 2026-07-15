"""Clear stale cycle-scoped state for a specific sleeve.

Adam 2026-07-15: bug where resting-stop credit paths didn't reset
trail_armed / trail_high_water_price / stop_loss_hwm / hybrid timeout /
buy_trail state at cycle rollover. Fixed in commit 45fd0ee for FUTURE
cycles, but any sleeve mid-cycle right now still has the carryover.

This diag queues a state_patch that nulls those fields for one sleeve.
Bot picks up on next tick, re-evaluates _maintain_resting_stop cleanly,
and the ratchet display + actual resting stop should align.

Read-only until --apply. Usage:
    python3 diag_reset_sleeve_cycle_state.py PRODUCT_ID SLEEVE_ID
    python3 diag_reset_sleeve_cycle_state.py PRODUCT_ID SLEEVE_ID --apply

Example (HYP after the divergence Adam spotted 2026-07-15):
    python3 diag_reset_sleeve_cycle_state.py HYP-20DEC30-CDE smrluux5w
    python3 diag_reset_sleeve_cycle_state.py HYP-20DEC30-CDE smrluux5w --apply
"""
from __future__ import annotations
import os
import sys
import time


def main() -> None:
    if len(sys.argv) < 3:
        print("USAGE: python3 diag_reset_sleeve_cycle_state.py "
              "PRODUCT_ID SLEEVE_ID [--apply]")
        return
    product_id = sys.argv[1]
    sleeve_id = sys.argv[2]
    apply = "--apply" in sys.argv
    tenant = "adam-live"

    print("=" * 78)
    print(f"RESET CYCLE STATE {'(APPLY)' if apply else '(dry-run)'} "
          f"— {tenant}/{product_id}/{sleeve_id}")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    state = store.get_state(tenant, product_id) or {}
    sleeves_state = state.get("sleeves") or {}
    ss = sleeves_state.get(sleeve_id)
    if ss is None:
        print(f"\n✗ sleeve {sleeve_id} not in state.sleeves for {product_id}")
        print(f"  Existing sleeve ids: {list(sleeves_state.keys())}")
        return

    print(f"\nCURRENT cycle-scoped state (will be cleared):")
    print(f"  trail_armed:              {ss.get('trail_armed')}")
    print(f"  trail_high_water_price:   {ss.get('trail_high_water_price')}")
    print(f"  stop_loss_hwm:            {ss.get('stop_loss_hwm')}")
    print(f"  hybrid_sell_triggered_ts: {ss.get('hybrid_sell_triggered_ts')}")
    print(f"  buy_trail_armed:          {ss.get('buy_trail_armed')}")
    print(f"  buy_trail_low_water:      {ss.get('buy_trail_low_water')}")

    print(f"\nUNCHANGED (position + realized preserved):")
    print(f"  state:               {ss.get('state')}")
    print(f"  own_avg_entry:       {ss.get('own_avg_entry')}")
    print(f"  cycles:              {ss.get('cycles')}")
    print(f"  realized_pnl:        ${ss.get('realized_pnl')}")
    print(f"  resting_stop_oid:    {ss.get('resting_stop_oid')}")
    print(f"  resting_stop_px:     {ss.get('resting_stop_px')}")

    if not apply:
        print("\n(dry-run — pass --apply to queue the state_patch)")
        return

    if not hasattr(store, "put_state_patch"):
        print("\n✗ store lacks put_state_patch — deploy 88d2390+ first.")
        return

    patch = {
        "sleeves": {
            sleeve_id: {
                "trail_armed": False,
                "trail_high_water_price": 0.0,
                "stop_loss_hwm": None,
                "hybrid_sell_triggered_ts": None,
                "buy_trail_armed": False,
                "buy_trail_low_water": 0.0,
            }
        },
        "reason": (f"reset_cycle_state: {product_id}/{sleeve_id} — clearing "
                   f"stale HWM/trail carried over from prior cycle before "
                   f"the 45fd0ee reset fix landed"),
        "ts": int(time.time()),
    }
    store.put_state_patch(tenant, product_id, patch)

    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        log.record(
            "reset_cycle_state_queued",
            tenant=tenant, symbol=product_id, sleeve_id=sleeve_id,
            reason="manual reset via diag_reset_sleeve_cycle_state.py",
            severity="info",
        )
    except Exception as e:
        print(f"\n(note: trade log record failed: {e})")

    print(f"\n✓ QUEUED state_patch for {tenant}/{product_id}.")
    print(f"  Bot will apply on next tick.")
    print(f"  Then _maintain_resting_stop should re-evaluate cleanly:")
    print(f"    - if mark < sell_px goal: keeps existing hard_bottom stop")
    print(f"    - if mark >= sell_px: trail activates from fresh HWM=mark")
    print(f"  Verify with: python3 diag_ratchet_gap.py {product_id}")
    print("=" * 78)


if __name__ == "__main__":
    main()
