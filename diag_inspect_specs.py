"""Read-only: inspect what specs (contract_size, fee_per_contract_roundtrip)
are stored on a product's config, and what Coinbase says the real values are.

Adam 2026-07-19: dashboard modal for SLR-27AUG26-CDE says "Coinbase specs
haven't loaded yet." Something is missing contract_size or fees on the
stored config. This diag shows both stored + fresh Coinbase values so we
know whether the periodic _refresh_all_specs is failing OR simply hasn't
run yet.

Usage:  python3 diag_inspect_specs.py SLR-27AUG26-CDE
"""
from __future__ import annotations
import os
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("USAGE: python3 diag_inspect_specs.py <PRODUCT_ID>")
        return
    product_id = sys.argv[1]
    print("=" * 78)
    print(f"SPEC INSPECT — {product_id}")
    print("=" * 78)

    # Coinbase ground truth
    print(f"\nCoinbase contract_spec:")
    try:
        from broker import BrokerConfig, CoinbaseBroker
        b = CoinbaseBroker(BrokerConfig(product_id=product_id))
        spec = b.contract_spec()
        for k, v in (spec or {}).items():
            print(f"  {k}: {v}")
    except Exception as e:
        print(f"  ✗ contract_spec failed: {e}")
        spec = {}

    # Stored config per tenant
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
        cfg = entry.get("config") or {}
        cs = cfg.get("contract_size")
        fee = cfg.get("fee_per_contract_roundtrip")
        print(f"\n--- tenant={tenant} stored config ---")
        print(f"  contract_size: {cs}")
        print(f"  fee_per_contract_roundtrip: {fee}")
        specMissing = not (isinstance(cs, (int, float)) and cs > 0
                          and isinstance(fee, (int, float)) and fee > 0)
        print(f"  specMissing = {specMissing}  (dashboard blocks preset if True)")
        # Sleeve count / heartbeat context
        state = entry.get("state") or {}
        print(f"  sleeves count: {len(cfg.get('sleeves') or [])}")
        print(f"  bot heartbeat present: {bool(state.get('last_heartbeat_ts'))}")

    if hits == 0:
        print(f"\n✗ {product_id} not in any tenant's config.")
        print(f"  The dashboard would show specMissing=True because there's no cfg.")
        print(f"  Fix: attach a strategy or wait for the scanner to pick it up.")


if __name__ == "__main__":
    main()
