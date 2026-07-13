"""Diagnostic — dump sleeve state for a product, across all tenants.

Usage:  python3 diag_sleeve_state.py [SYMBOL_SUBSTRING]
Default = "OIL".

Walks every tenant scope in the state store, finds symbol keys containing
the substring (case-insensitive), and prints the state for each sleeve
plus the primary. Answers: which tenant / symbol key / state is each sleeve in?
"""
from __future__ import annotations
import json
import os
import sys

from state_store import make_store


TENANTS_TO_CHECK = ("adam-live", "adam", "adam-paper")


def _iter_scoped(store, tenant: str, needle: str):
    """Yield (symbol_key, state_dict) for symbols matching needle in tenant."""
    # Try both list-based APIs first; fall back to a probe of the common suffixes.
    listed = None
    for method_name in ("list_symbols", "keys", "list_scopes"):
        m = getattr(store, method_name, None)
        if callable(m):
            try:
                listed = m(tenant)
                break
            except Exception:
                pass
    if listed is None:
        # Can't enumerate — probe common OIL contract codes.
        probes = [
            f"{needle}-20JUL26-CDE", f"{needle}-27JUL26-CDE",
            f"{needle}-20AUG26-CDE", f"{needle}-20SEP26-CDE",
        ]
        for k in probes:
            st = store.get_state(tenant, k) or {}
            if st:
                yield k, st
        return
    for k in listed:
        if needle.upper() in str(k).upper():
            st = store.get_state(tenant, k) or {}
            if st:
                yield k, st


def main() -> None:
    needle = sys.argv[1] if len(sys.argv) > 1 else "OIL"
    store = make_store(os.getenv("SWING_DATA_DIR", "data"))

    found_any = False
    for tenant in TENANTS_TO_CHECK:
        for sym_key, st in _iter_scoped(store, tenant, needle):
            found_any = True
            print(f"\n=== tenant={tenant}  symbol_key={sym_key} ===")
            sleeves = st.get("sleeves") or []
            print(f"primary state: {st.get('state','-')}  "
                  f"live_order_id: {st.get('live_order_id','-')}  "
                  f"swing_qty: {st.get('swing_qty','-')}  "
                  f"position_avg: {st.get('position_avg','-')}")
            print(f"{len(sleeves)} sleeve(s):")
            for i, s in enumerate(sleeves):
                keep = {
                    "name": s.get("name"),
                    "state": s.get("state"),
                    "qty": s.get("qty"),
                    "sell_px": s.get("sell_px"),
                    "buy_px": s.get("buy_px"),
                    "own_avg_entry": s.get("own_avg_entry"),
                    "cycles": s.get("cycles"),
                    "trail_high_water_price": s.get("trail_high_water_price"),
                    "stop_loss_px": s.get("stop_loss_px"),
                    "stop_loss_enabled": s.get("stop_loss_enabled"),
                    "live_order_id": s.get("live_order_id"),
                    "last_reanchor_ts": s.get("last_reanchor_ts"),
                    "exit_mode": s.get("exit_mode"),
                    "entry_trend_filter": s.get("entry_trend_filter"),
                }
                print(f"  [{i}] {json.dumps(keep, indent=2, default=str)}")

    if not found_any:
        print(f"No sleeves match '{needle}' in tenants {TENANTS_TO_CHECK}.")
        # Fallback — enumerate whatever we can.
        for tenant in TENANTS_TO_CHECK:
            for method_name in ("list_symbols", "keys", "list_scopes"):
                m = getattr(store, method_name, None)
                if callable(m):
                    try:
                        listed = m(tenant)
                        print(f"\n{tenant} known symbols ({len(listed)}):")
                        for k in sorted(listed):
                            print(f"  {k}")
                        break
                    except Exception as e:
                        print(f"  ({method_name} failed: {e})")


if __name__ == "__main__":
    main()
