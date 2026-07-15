"""Dump raw sleeve state for a product — see what auto-recovery discovery sees.

Adam 2026-07-15: auto-recovery finds only AVE as silent, even though
fleet-health shows 9 products with ARMED?=yes. My discovery logic uses
same check but only picks up AVE. Something's different — this diag
dumps raw state.sleeves for a product so we can compare.

Read-only. Usage:
    python3 diag_sleeve_state_dump.py                           # all products
    python3 diag_sleeve_state_dump.py HYF-31JUL26-CDE           # one product
"""
from __future__ import annotations
import os
import sys
import json


def main() -> None:
    product_filter = sys.argv[1] if len(sys.argv) > 1 else None
    tenant = "adam-live"

    print("=" * 100)
    print(f"RAW SLEEVE STATE DUMP — tenant={tenant}"
          + (f"  product={product_filter}" if product_filter else ""))
    print("=" * 100)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))

    for tid in store.list_tenants():
        if tid != tenant:
            continue
        for sym in store.list_symbols(tid):
            if sym.startswith("__"):
                continue
            if product_filter and sym != product_filter:
                continue
            st = store.get_state(tid, sym) or {}
            sleeves = st.get("sleeves") or {}
            print(f"\n─── {sym} ───")
            print(f"  top-level state.swing_qty: {st.get('swing_qty')}")
            print(f"  top-level state.state:     {st.get('state')}")
            print(f"  sleeves count:             {len(sleeves)}")
            if not sleeves:
                print(f"  (no sleeves)")
                continue
            for sid, ss in sleeves.items():
                sstate = ss.get('state')
                is_armed = str(sstate or "") in ("ARMED_BUY", "ARMED_SELL")
                marker = " ⬅ ARMED" if is_armed else ""
                print(f"    · sleeve={sid}")
                print(f"      state={sstate!r}{marker}")
                print(f"      cycles={ss.get('cycles')}  realized_pnl={ss.get('realized_pnl')}")
                print(f"      live_order_id={ss.get('live_order_id')}")
                print(f"      own_avg_entry={ss.get('own_avg_entry')}")
                print(f"      halt_reason={ss.get('halt_reason')}")
                print(f"      armed_buy_since_ts={ss.get('armed_buy_since_ts')}")
    print("=" * 100)


if __name__ == "__main__":
    main()
