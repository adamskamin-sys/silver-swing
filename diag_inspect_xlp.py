"""Read-only: dump every XLP-20DEC30-CDE sleeve state across tenants.

Adam 2026-07-19: after --apply reported "No sleeve with resting_stop_oid",
we need to see the actual state to know whether the credit stuck or if
the sleeve got cleaned up some other way.
"""
from __future__ import annotations
import json
import os


def main() -> None:
    product_id = "XLP-20DEC30-CDE"
    print("=" * 78)
    print(f"INSPECT {product_id}")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    raw = store._load()

    hits = 0
    for tenant, tenant_data in raw.items():
        if not isinstance(tenant_data, dict):
            continue
        entry = tenant_data.get(product_id)
        if not isinstance(entry, dict):
            continue
        hits += 1
        print(f"\n--- tenant={tenant} ---")
        cfg = entry.get("config") or {}
        state = entry.get("state") or {}
        sleeves_cfg = cfg.get("sleeves") or []
        sleeves_state = state.get("sleeves") or {}
        print(f"top-level state keys: {list(state.keys())}")
        print(f"top-level state.state={state.get('state')}  swing_qty={state.get('swing_qty')}  filled_qty={state.get('filled_qty')}")
        print(f"top-level realized_pnl=${state.get('realized_pnl', 0)}  cycles={state.get('cycles', 0)}")
        print(f"live_order_id={state.get('live_order_id')}")
        print(f"resting_stop_oid (top-level)={state.get('resting_stop_oid')}")
        print(f"halt_reason (top-level)={state.get('halt_reason')}")
        print(f"\nconfigured sleeves ({len(sleeves_cfg)}):")
        for sc in sleeves_cfg:
            print(f"  cfg id={sc.get('id')}  qty={sc.get('qty')}  enabled={sc.get('enabled')}")
        print(f"\nsleeve states ({len(sleeves_state)}):")
        for sid, ss in sleeves_state.items():
            print(f"  sid={sid}")
            print(f"    state={ss.get('state')}  halt_reason={ss.get('halt_reason')}")
            print(f"    cycles={ss.get('cycles')}  realized_pnl=${ss.get('realized_pnl', 0)}")
            print(f"    own_avg_entry={ss.get('own_avg_entry')}")
            print(f"    resting_stop_oid={ss.get('resting_stop_oid')}")
            print(f"    resting_stop_px={ss.get('resting_stop_px')}  stage={ss.get('resting_stop_stage')}")
            print(f"    live_order_id={ss.get('live_order_id')}")
            print(f"    _prev_halt_reason={ss.get('_prev_halt_reason')}")

    if hits == 0:
        print(f"\nNo {product_id} entries in any tenant.")


if __name__ == "__main__":
    main()
