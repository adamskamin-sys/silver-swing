"""Print CHN-19DEC30-CDE sleeve IDs + current state.

Read-only. Used to identify the sleeve_id to pass to
diag_force_credit_cycle.py after the 2026-07-19 22:04:29 trail-breach
market sell that fired before the credit-tracking fix (commit 3eee50c).
"""
from __future__ import annotations
import os


def main() -> None:
    tenant = "adam-live"
    product_id = "CHN-19DEC30-CDE"
    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    state = store.get_state(tenant, product_id) or {}
    sleeves = state.get("sleeves") or {}
    if not sleeves:
        print(f"No sleeves in state for {tenant}/{product_id}")
        return
    print(f"{tenant}/{product_id} — {len(sleeves)} sleeve(s):\n")
    for sid, ss in sleeves.items():
        print(f"  sleeve_id:        {sid}")
        print(f"    name:           {ss.get('name')}")
        print(f"    state:          {ss.get('state')}")
        print(f"    own_avg_entry:  {ss.get('own_avg_entry')}")
        print(f"    live_order_id:  {ss.get('live_order_id')}")
        print(f"    resting_stop:   {ss.get('resting_stop_oid')}")
        print(f"    cycles:         {ss.get('cycles', 0)}")
        print(f"    realized_pnl:   {ss.get('realized_pnl', 0)}")
        print(f"    halt_reason:    {ss.get('halt_reason')}")
        print()
    print("If any sleeve above shows state=ARMED_SELL/WAITING_FOR_SELL")
    print("with own_avg_entry set BUT live_order_id + resting_stop_oid")
    print("both None, that's the orphan. To credit + recover:")
    print()
    print("  python3 diag_force_credit_cycle.py CHN-19DEC30-CDE \\")
    print("    <sleeve_id_above> 3106.10 3074.40 1 --apply")


if __name__ == "__main__":
    main()
