"""Emergency diag for the 2026-07-14 SLR re-arm bug.

Adam has 1 SLVR contract, sleeve wants to sell 1 at $60.017 — but the
bot keeps placing SELL 2 at $65.25 orders that Adam cancels and they
come back. Something has stale swing_qty=2 + sell_px=65.25 for SLR
in some tenant's primary config.

This dumps the PRIMARY config + state for SLR-27AUG26-CDE across every
tenant, so we can see exactly which tenant's config has the stale
values and which bot service is reading it.

Read-only. No writes."""
from __future__ import annotations
import json
import os

from state_store import make_store


def _dump(label, obj):
    print(f"  {label}:")
    if not obj:
        print("    (empty)")
        return
    # Skip the huge `sleeves` list — this diag is about the PRIMARY only.
    if isinstance(obj, dict):
        for k in sorted(obj):
            if k == "sleeves":
                v = obj[k]
                if isinstance(v, list):
                    print(f"    sleeves: (list of {len(v)} entries — hidden, see diag_sleeve_state.py)")
                elif isinstance(v, dict):
                    print(f"    sleeves: (dict of {len(v)} entries — hidden, see diag_sleeve_state.py)")
                continue
            print(f"    {k}: {obj[k]!r}")
    else:
        print(f"    {obj!r}")


def main() -> None:
    store = make_store(os.getenv("SWING_DATA_DIR", "data"))
    try:
        tenants = store.list_tenants() if hasattr(store, "list_tenants") else \
                  ["adam-live", "adam-paper", "adam-lab", "adam-live-live"]
    except Exception:
        tenants = ["adam-live", "adam-paper", "adam-lab", "adam-live-live"]

    print(f"Checking primary config + state for SLR-27AUG26-CDE across tenants: {tenants}\n")

    for tenant in tenants:
        try:
            cfg = store.get_config(tenant, "SLR-27AUG26-CDE") or {}
            state = store.get_state(tenant, "SLR-27AUG26-CDE") or {}
        except Exception as e:
            print(f"=== {tenant} === (error: {e})\n")
            continue

        # Skip tenants where SLR doesn't exist
        if not cfg and not state:
            continue

        print(f"=== {tenant} · SLR-27AUG26-CDE ===")
        _dump("CONFIG (primary swing settings)", cfg)
        _dump("STATE (runtime)", state)
        print()

    print("Notes:")
    print("  * PRIMARY swing_qty > 0 with sell_px set = the primary bot")
    print("    will re-arm a sell order every tick when ARMED_SELL and")
    print("    no live_order_id — even if a sleeve exists on the same")
    print("    product. Cancelling the order externally does not clear")
    print("    the config; only editing the config does.")
    print("  * To neutralize the primary (let sleeves manage it):")
    print("    set swing_qty=0 on THAT tenant's SLR config.")


if __name__ == "__main__":
    main()
