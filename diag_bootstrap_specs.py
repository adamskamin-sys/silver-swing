"""Bootstrap Coinbase specs into cfg for a manually-held product.

Adam 2026-07-19: bought SLR-27AUG26-CDE manually outside the bot.
No cfg exists, so _refresh_all_specs (which iterates store.list_symbols)
never seeds contract_size + fees for it. The dashboard modal blocks
preset application because specMissing=true.

This diag calls main._refresh_contract_spec_into_config directly for
one (tenant, product_id), which creates/updates the cfg with fresh
Coinbase contract_size + fee_per_contract_roundtrip. After apply,
reopen the dashboard modal and preset application works normally.

Read-only by default. Usage:
    python3 diag_bootstrap_specs.py SLR-27AUG26-CDE                 # dry-run
    python3 diag_bootstrap_specs.py SLR-27AUG26-CDE --apply         # execute
    python3 diag_bootstrap_specs.py SLR-27AUG26-CDE --apply --tenant adam-live
"""
from __future__ import annotations
import os
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("USAGE: python3 diag_bootstrap_specs.py <PRODUCT_ID> "
              "[--apply] [--tenant TENANT_ID]")
        return
    product_id = sys.argv[1]
    apply = "--apply" in sys.argv
    tenant = "adam-live"
    if "--tenant" in sys.argv:
        try:
            tenant = sys.argv[sys.argv.index("--tenant") + 1]
        except IndexError:
            print("✗ --tenant needs a value")
            return

    print("=" * 78)
    print(f"BOOTSTRAP SPECS{'  (APPLYING)' if apply else '  (dry-run)'} — "
          f"{tenant} / {product_id}")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    existing = store.get_config(tenant, product_id) or {}
    print(f"\nBEFORE: cfg keys = {sorted(existing.keys())}")
    print(f"  contract_size: {existing.get('contract_size')}")
    print(f"  fee_per_contract_roundtrip: {existing.get('fee_per_contract_roundtrip')}")

    # Preview Coinbase spec
    try:
        from broker import BrokerConfig, CoinbaseBroker
        b = CoinbaseBroker(BrokerConfig(product_id=product_id))
        spec = b.contract_spec()
    except Exception as e:
        print(f"\n✗ contract_spec failed: {e} — refusing to bootstrap blind")
        return
    print(f"\nCoinbase contract_spec:")
    for k in ("contract_size", "tick_size", "contract_expiry",
              "intraday_margin_rate", "overnight_margin_rate", "current_price"):
        print(f"  {k}: {spec.get(k)}")

    if not apply:
        print(f"\n(dry-run — pass --apply to invoke _refresh_contract_spec_into_config)")
        print(f"  This will merge contract_size + fees into {tenant}/{product_id} cfg")
        print(f"  and put_config the result. Existing sleeves (if any) preserved.")
        return

    from main import _refresh_contract_spec_into_config
    try:
        _refresh_contract_spec_into_config(store, tenant, product_id)
    except Exception as e:
        print(f"\n✗ _refresh_contract_spec_into_config raised: {type(e).__name__}: {e}")
        return

    # Verify
    after = store.get_config(tenant, product_id) or {}
    print(f"\nAFTER:")
    print(f"  contract_size: {after.get('contract_size')}")
    print(f"  fee_per_contract_roundtrip: {after.get('fee_per_contract_roundtrip')}")
    print(f"  fee_per_fill_buy: {after.get('fee_per_fill_buy')}")
    print(f"  fee_per_fill_sell: {after.get('fee_per_fill_sell')}")
    print(f"  tick_size: {after.get('tick_size')}")

    ok = (isinstance(after.get("contract_size"), (int, float))
          and after["contract_size"] > 0
          and isinstance(after.get("fee_per_contract_roundtrip"), (int, float))
          and after["fee_per_contract_roundtrip"] > 0)
    if ok:
        print(f"\n✓ APPLIED. specMissing will now be False in the dashboard.")
        print(f"  Reopen the {product_id} modal — preset application will work.")
    else:
        print(f"\n⚠ Applied but specs still incomplete. Check the fee preview logs.")


if __name__ == "__main__":
    main()
