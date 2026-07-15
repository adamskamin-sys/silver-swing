"""Which tenant does live_runner ACTUALLY read from?

Adam 2026-07-15: diag_discovery_replay found all 10 armed products
under tenant 'adam-live'. But live_runner's auto-recovery only
detects AVE. Hypothesis: live_runner is reading from a DIFFERENT
tenant scope (like the phantom 'adam-live-live' from a past bug),
where only AVE exists.

Lists all tenants + shows which products have state under each.

Read-only. Usage:
    python3 diag_tenant_scan.py
"""
from __future__ import annotations
import os


def main() -> None:
    print("=" * 100)
    print(f"TENANT SCAN — enumerate all tenants + product counts")
    print("=" * 100)
    print(f"\nSWING_TENANT env var: {os.getenv('SWING_TENANT', '(not set)')}")
    print(f"SWING_SYMBOL env var: {os.getenv('SWING_SYMBOL', '(not set)')}")
    print(f"SWING_DATA_DIR env: {os.getenv('SWING_DATA_DIR', '(not set)')}")

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))

    tenants = store.list_tenants()
    print(f"\nTotal tenants in store: {len(tenants)}")
    for t in sorted(tenants):
        symbols = list(store.list_symbols(t))
        armed_products = []
        for sym in symbols:
            if sym.startswith("__"):
                continue
            try:
                st = store.get_state(t, sym) or {}
                sleeves = st.get("sleeves") or {}
                for ss in sleeves.values():
                    sstate = str(ss.get("state") or "")
                    if sstate in ("ARMED_BUY", "ARMED_SELL"):
                        armed_products.append(sym)
                        break
            except Exception:
                pass
        print(f"\n  {t!r}: {len(symbols)} symbols, {len(armed_products)} with ARMED sleeves")
        if armed_products:
            for ap in armed_products:
                print(f"      · {ap}")
    print("=" * 100)


if __name__ == "__main__":
    main()
