"""Clear retirement-ledger entries for a product so it can be re-armed.

Adam 2026-07-18: `diag_retire_sleeves.py` now writes a cooldown to the
retirement ledger. Bot refuses to create new sleeve state on the product
until cooldown expires. This diag removes those entries so the product
is eligible again immediately.

Use when you retire a sleeve as a temporary block (e.g. rate-limit
recovery) and want to bring the product back sooner than the default
24h cooldown.

Read-only by default. Pass --apply to actually clear.

Usage:
    python3 diag_clear_retirement.py PRODUCT_ID
    python3 diag_clear_retirement.py PRODUCT_ID --apply
    python3 diag_clear_retirement.py --list                # show all active retirements
"""
from __future__ import annotations
import os
import sys
import time


TENANT = "adam-live"


def main() -> None:
    if len(sys.argv) < 2:
        print("USAGE: python3 diag_clear_retirement.py PRODUCT_ID [--apply]")
        print("       python3 diag_clear_retirement.py --list")
        return

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))

    import retirement_ledger

    if sys.argv[1] == "--list":
        active = retirement_ledger.list_active(store, TENANT)
        print("=" * 90)
        print(f"ACTIVE RETIREMENT LEDGER ENTRIES — tenant={TENANT}")
        print("=" * 90)
        if not active:
            print("  (none — no products in cooldown)")
            return
        now = time.time()
        for e in active:
            retired_at = float(e.get("retired_at") or 0)
            cd_h = float(e.get("cooldown_hours") or 0)
            expires_at = retired_at + cd_h * 3600.0
            remaining = expires_at - now
            print(f"  · {e.get('product_id'):<28}  sleeve={e.get('sleeve_id')}")
            print(f"      retired {int((now - retired_at)/60)}m ago, cooldown {cd_h:.1f}h, "
                  f"{remaining/3600:.1f}h remaining")
            print(f"      reason: {e.get('reason')}")
        return

    product_id = sys.argv[1]
    apply = "--apply" in sys.argv

    active = retirement_ledger.list_active(store, TENANT)
    matching = [e for e in active if e.get("product_id") == product_id]

    print("=" * 90)
    print(f"CLEAR RETIREMENT {'(APPLY)' if apply else '(dry-run)'} — "
          f"{TENANT}/{product_id}")
    print("=" * 90)

    if not matching:
        print(f"\n· {product_id} has no active retirement entries — nothing to clear")
        return

    now = time.time()
    print(f"\nActive entries to clear:")
    for e in matching:
        retired_at = float(e.get("retired_at") or 0)
        cd_h = float(e.get("cooldown_hours") or 0)
        remaining = retired_at + cd_h * 3600.0 - now
        print(f"  · sleeve={e.get('sleeve_id')}  retired {int((now - retired_at)/60)}m ago  "
              f"cooldown {cd_h:.1f}h  {remaining/3600:.1f}h remaining")
        print(f"    reason: {e.get('reason')}")

    if not apply:
        print(f"\n(dry-run — pass --apply to clear)")
        return

    removed = retirement_ledger.clear_product(store, TENANT, product_id)
    print(f"\n✓ CLEARED {removed} entry(ies) for {product_id}")
    print(f"  Bot will now allow new sleeve state for this product on the next tick.")
    print(f"  (Config must still list the sleeve — this only removes the cooldown block.)")

    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        log.record(
            "retirement_ledger_cleared_via_diag",
            tenant=TENANT, symbol=product_id,
            entries_removed=removed,
            severity="info",
            reason="manual clear via diag_clear_retirement.py",
        )
    except Exception:
        pass

    print("=" * 90)


if __name__ == "__main__":
    main()
