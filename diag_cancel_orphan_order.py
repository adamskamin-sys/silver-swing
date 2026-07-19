"""Cancel a specific orphan open order after safety checks.

Adam 2026-07-19: diag_find_orphan_order.py found two orphan SELLs
that would take the account SHORT after the primary sell closes:
  - CHN-19DEC30-CDE  8f6ad22c-...  SELL 1 @ $3014.9
  - NER-20DEC30-CDE  832979ad-...  SELL 1 @ $1.8597

An orphan is defined as: Coinbase has this order OPEN, but no sleeve
on any tenant references its oid in live_order_id or resting_stop_oid.

Safety:
  - REFUSES to cancel if the oid IS referenced by a sleeve (not orphan)
  - REFUSES to cancel if Coinbase reports status != OPEN
  - Dry-run by default; --apply required to actually cancel

Usage:
    python3 diag_cancel_orphan_order.py <PRODUCT_ID> <OID>
    python3 diag_cancel_orphan_order.py <PRODUCT_ID> <OID> --apply
"""
from __future__ import annotations
import os
import sys


def main() -> None:
    if len(sys.argv) < 3:
        print("USAGE: python3 diag_cancel_orphan_order.py <PRODUCT_ID> <OID> [--apply]")
        return
    product_id = sys.argv[1]
    oid = sys.argv[2]
    apply = "--apply" in sys.argv

    print("=" * 78)
    print(f"CANCEL ORPHAN{'  (APPLYING)' if apply else '  (dry-run)'} — "
          f"{product_id} / {oid}")
    print("=" * 78)

    # 1. Confirm the oid is truly orphaned (not referenced by any sleeve)
    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    raw = store._load()
    referenced_by: list[str] = []
    for tenant, tdata in raw.items():
        if not isinstance(tdata, dict):
            continue
        for symbol, entry in tdata.items():
            if not isinstance(entry, dict):
                continue
            state = entry.get("state") or {}
            for sid, ss in (state.get("sleeves") or {}).items():
                if not isinstance(ss, dict):
                    continue
                if ss.get("live_order_id") == oid:
                    referenced_by.append(f"{tenant}/{symbol}/{sid} live_order_id")
                if ss.get("resting_stop_oid") == oid:
                    referenced_by.append(f"{tenant}/{symbol}/{sid} resting_stop_oid")
            if state.get("live_order_id") == oid:
                referenced_by.append(f"{tenant}/{symbol} primary live_order_id")

    if referenced_by:
        print(f"\n✗ REFUSING: oid {oid} IS referenced by a sleeve:")
        for r in referenced_by:
            print(f"    - {r}")
        print(f"\nThis is not an orphan. Do not cancel from this script.")
        return
    print(f"\n✓ oid {oid} not referenced by any sleeve — genuine orphan")

    # 2. Confirm Coinbase-side status
    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id=product_id))
    try:
        st = b.order_status(oid)
    except Exception as e:
        print(f"\n✗ order_status({oid}) failed: {e}")
        return
    status = (st or {}).get("status")
    print(f"\nCoinbase order_status({oid}):")
    for k in ("status", "side", "average_filled_price", "filled_qty",
              "product_id", "cancel_message"):
        print(f"  {k}: {(st or {}).get(k)}")

    if status != "OPEN":
        print(f"\n✗ Status is {status}, not OPEN — nothing to cancel.")
        return

    if not apply:
        print(f"\n(dry-run — pass --apply to cancel)")
        return

    # 3. Cancel
    try:
        b.cancel(oid)
        print(f"\n✓ CANCELED. Verify via Coinbase Orders page.")
    except Exception as e:
        print(f"\n✗ cancel({oid}) failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
