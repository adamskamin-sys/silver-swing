"""Remove sleeves from a product's state (stops auto-recovery chasing them).

Adam 2026-07-15: AVE, HYF, NGS have feed-lifecycle issues inside
live_runner that make their Tracks spawn but immediately die. Rather
than dig into the feed bug now, retire the sleeves so auto-recovery
stops trying to spawn them (nothing 'armed' = nothing to track).

The SLEEVE STATE gets removed. Top-level state/config preserved.
Trade log records sleeve_retired_via_diag events for audit.

Also writes a retirement-ledger entry that stops the bot from re-arming the
product for --cooldown-hours (default 24h). Closes the PT/HYP/SLR ghost
class where retiring the sleeve did not prevent immediate re-inflation
from config on the next tick. See retirement_ledger.py.

Read-only by default. Pass --apply to delete.

Usage:
    python3 diag_retire_sleeves.py PRODUCT_ID SLEEVE_ID
    python3 diag_retire_sleeves.py PRODUCT_ID SLEEVE_ID --apply
    python3 diag_retire_sleeves.py PRODUCT_ID all --apply       # all sleeves on product
    python3 diag_retire_sleeves.py PRODUCT_ID all --apply --cooldown-hours 48
"""
from __future__ import annotations
import os
import sys
import time


def main() -> None:
    if len(sys.argv) < 3:
        print("USAGE: python3 diag_retire_sleeves.py PRODUCT_ID SLEEVE_ID|all "
              "[--apply] [--cooldown-hours N]")
        return
    product_id = sys.argv[1]
    target = sys.argv[2]
    apply = "--apply" in sys.argv
    cooldown_hours = 5.0 / 60.0  # 5 minutes default; matches retirement_ledger
    if "--cooldown-hours" in sys.argv:
        try:
            cooldown_hours = float(sys.argv[sys.argv.index("--cooldown-hours") + 1])
        except (IndexError, ValueError):
            print("✗ --cooldown-hours needs a numeric value")
            return
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

    # Record in retirement ledger — bot refuses to re-create sleeve state
    # for this product until cooldown expires, even if config still lists
    # the sleeve. See retirement_ledger.py.
    try:
        import retirement_ledger
        for sid in to_remove:
            retirement_ledger.record_retirement(
                store, tenant, product_id, sid,
                reason=f"manual retire via diag_retire_sleeves.py",
                cooldown_hours=cooldown_hours,
            )
    except Exception as _rle:
        print(f"  ⚠ retirement_ledger write failed: {type(_rle).__name__}: {_rle}")
        print(f"    (state was cleared but bot may re-create from config)")

    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        for sid in to_remove:
            log.record(
                "sleeve_retired_via_diag",
                tenant=tenant, symbol=product_id, sleeve_id=sid,
                cooldown_hours=cooldown_hours,
                severity="info",
                reason="manual retirement via diag_retire_sleeves.py",
                ts=int(time.time()),
            )
    except Exception:
        pass

    if cooldown_hours >= 1:
        cd_str = f"{cooldown_hours:.1f}h"
    else:
        cd_str = f"{int(cooldown_hours * 60)}m"
    print(f"\n✓ REMOVED {len(to_remove)} sleeve(s) from {product_id}")
    print(f"✓ Retirement cooldown: {cd_str} — bot will refuse to re-create")
    print(f"  sleeve state on this product until cooldown expires.")
    print(f"  Override early with: python3 diag_clear_retirement.py {product_id} --apply")
    print("=" * 90)


if __name__ == "__main__":
    main()
