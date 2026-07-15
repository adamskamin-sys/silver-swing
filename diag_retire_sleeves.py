"""Remove sleeves from a product's state (stops auto-recovery chasing them).

Adam 2026-07-15: AVE, HYF, NGS have feed-lifecycle issues inside
live_runner that make their Tracks spawn but immediately die. Rather
than dig into the feed bug now, retire the sleeves so auto-recovery
stops trying to spawn them (nothing 'armed' = nothing to track).

The SLEEVE STATE gets removed. Top-level state/config preserved.
Trade log records sleeve_retired_via_diag events for audit.

Read-only by default. Pass --apply to delete.

Usage:
    python3 diag_retire_sleeves.py PRODUCT_ID SLEEVE_ID
    python3 diag_retire_sleeves.py PRODUCT_ID SLEEVE_ID --apply
    python3 diag_retire_sleeves.py PRODUCT_ID all --apply       # all sleeves on product
"""
from __future__ import annotations
import os
import sys
import time


def main() -> None:
    if len(sys.argv) < 3:
        print("USAGE: python3 diag_retire_sleeves.py PRODUCT_ID SLEEVE_ID|all [--apply]")
        return
    product_id = sys.argv[1]
    target = sys.argv[2]
    apply = "--apply" in sys.argv
    tenant = "adam-live"

    print("=" * 90)
    print(f"RETIRE SLEEVES {'(APPLY)' if apply else '(dry-run)'} — "
          f"{tenant}/{product_id}/{target}")
    print("=" * 90)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    state = store.get_state(tenant, product_id) or {}
    sleeves = state.get("sleeves") or {}
    if not sleeves:
        print(f"\n· {product_id} has no sleeves — nothing to retire")
        return

    to_remove = []
    if target == "all":
        to_remove = list(sleeves.keys())
    elif target in sleeves:
        to_remove = [target]
    else:
        print(f"\n✗ sleeve {target} not found on {product_id}")
        print(f"  Existing: {list(sleeves.keys())}")
        return

    print(f"\nSleeves to retire from {product_id}:")
    for sid in to_remove:
        ss = sleeves[sid]
        print(f"  · {sid}")
        print(f"    state={ss.get('state')}  "
              f"cycles={ss.get('cycles')}  "
              f"realized_pnl={ss.get('realized_pnl')}")
        if ss.get("live_order_id"):
            print(f"    ⚠ live_order_id={ss.get('live_order_id')} "
                  f"— cancel this manually on Coinbase first!")

    if not apply:
        print(f"\n(dry-run — pass --apply to remove)")
        return

    # Remove
    for sid in to_remove:
        del sleeves[sid]
    state["sleeves"] = sleeves
    store.put_state(tenant, product_id, state)

    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        for sid in to_remove:
            log.record(
                "sleeve_retired_via_diag",
                tenant=tenant, symbol=product_id, sleeve_id=sid,
                severity="info",
                reason="manual retirement via diag_retire_sleeves.py "
                       "— feed-lifecycle issue prevents ticking",
                ts=int(time.time()),
            )
    except Exception:
        pass

    print(f"\n✓ REMOVED {len(to_remove)} sleeve(s) from {product_id}")
    print(f"  Auto-recovery will stop trying to spawn a Track for this")
    print(f"  product on next cycle (no armed sleeves = nothing to track).")
    print("=" * 90)


if __name__ == "__main__":
    main()
