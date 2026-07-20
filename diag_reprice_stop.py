"""Manually reprice a sleeve's resting stop-limit UP to protect profit.

Adam 2026-07-20: safety hatch while commit 6ae8e96 (HWM-update fix in
_maintain_resting_stop) is deploying. If a held sleeve's exchange stop
is stuck below where the bot's trail SHOULD be — because HWM never
advanced on the resting-stop path — this script cancels the stale stop
and places a fresh one at the specified higher price.

Guardrails:
  - Refuses to LOWER the stop (protect_profit_above_all).
  - Refuses to place a stop AT or ABOVE current mark (would fire immediately).
  - Refuses if the position is 0 on Coinbase (no thing to protect).
  - Read-only by default. Pass --apply to execute.

Usage:
    python3 diag_reprice_stop.py <PRODUCT_ID> <NEW_STOP_PRICE> [--apply]
    python3 diag_reprice_stop.py CHN-19DEC30-CDE 3110       # dry-run
    python3 diag_reprice_stop.py CHN-19DEC30-CDE 3110 --apply

The limit price is auto-set to stop_price - 2 ticks so the fill catches
the trigger without slippage risk.
"""
from __future__ import annotations
import os
import sys
import time


def main() -> None:
    if len(sys.argv) < 3:
        print("USAGE: python3 diag_reprice_stop.py <PRODUCT_ID> <NEW_STOP_PRICE> [--apply]")
        return
    product_id = sys.argv[1]
    try:
        new_stop_px = float(sys.argv[2])
    except ValueError:
        print(f"✗ NEW_STOP_PRICE must be a number, got {sys.argv[2]!r}")
        return
    apply = "--apply" in sys.argv

    print("=" * 78)
    print(f"REPRICE RESTING STOP{'  (APPLYING)' if apply else '  (dry-run)'} — {product_id} → ${new_stop_px}")
    print("=" * 78)

    # Ground truth: Coinbase position
    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id=product_id))
    try:
        cb_qty = int(b.position_qty())
    except Exception as e:
        print(f"✗ position_qty() failed: {e}")
        return
    print(f"\nCoinbase {product_id} position: {cb_qty}")
    if cb_qty <= 0:
        print(f"✗ REFUSING: no long position to protect (qty={cb_qty}).")
        return

    # Current mark
    try:
        mark = float(b.mark_price())
    except Exception:
        try:
            mark = float(b.best_bid())
        except Exception:
            mark = 0.0
    print(f"Current mark: ${mark}")
    if mark > 0 and new_stop_px >= mark:
        print(f"✗ REFUSING: new stop ${new_stop_px} >= current mark ${mark}. "
              f"Would fire immediately as a limit sell.")
        return

    # Contract spec (tick + size)
    try:
        spec = b.contract_spec() or {}
        tick = float(spec.get("tick_size", 0.01) or 0.01)
    except Exception:
        tick = 0.01
    limit_px = new_stop_px - 2 * tick
    print(f"Tick size: ${tick} → limit price: ${limit_px}")

    # Load state to find the sleeve holding this position
    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    raw = store._load()
    target_tenant = None
    target_state = None
    target_sleeve_id = None
    target_ss = None
    for tenant, tenant_data in (raw or {}).items():
        if not isinstance(tenant_data, dict):
            continue
        entry = tenant_data.get(product_id)
        if not isinstance(entry, dict):
            continue
        state = entry.get("state") or {}
        sleeves_state = state.get("sleeves") or {}
        for sid, ss in sleeves_state.items():
            # Pick the sleeve with a real resting_stop_oid (or own_avg_entry set)
            if ss.get("resting_stop_oid") or ss.get("own_avg_entry"):
                target_tenant = tenant
                target_state = state
                target_sleeve_id = sid
                target_ss = ss
                break
        if target_ss:
            break

    if target_ss is None:
        print(f"\n✗ No sleeve on {product_id} claims a resting stop or own_avg. "
              f"Nothing to reprice — position may be un-managed.")
        return

    print(f"\nSleeve: tenant={target_tenant}  id={target_sleeve_id}")
    print(f"  state={target_ss.get('state')}")
    print(f"  own_avg_entry={target_ss.get('own_avg_entry')}")
    old_oid = target_ss.get("resting_stop_oid")
    old_stop_px = target_ss.get("resting_stop_px") or 0
    print(f"  resting_stop_oid={old_oid}")
    print(f"  resting_stop_px=${old_stop_px}")

    # Protect-profit guardrail: never LOWER an existing stop.
    if old_stop_px and float(old_stop_px) > 0 and new_stop_px < float(old_stop_px):
        print(f"\n✗ REFUSING: new stop ${new_stop_px} < existing ${old_stop_px}. "
              f"This script only ratchets UP (protect_profit_above_all).")
        return

    print(f"\nWOULD:")
    if old_oid:
        print(f"  1. Cancel existing resting stop {old_oid} @ ${old_stop_px}")
    print(f"  2. Place fresh STOP_LIMIT SELL qty={cb_qty} stop=${new_stop_px} limit=${limit_px}")
    print(f"  3. Update sleeve state: resting_stop_oid, resting_stop_px, resting_stop_stage='manual_reprice'")

    if not apply:
        print(f"\n(dry-run — pass --apply to execute)")
        return

    # 1. Cancel old
    if old_oid:
        try:
            b.cancel(old_oid)
            print(f"\n✓ Cancelled old stop {old_oid}")
        except Exception as e:
            print(f"⚠ Cancel failed (may already be gone): {e}")
    # Brief pause to let cancel settle before place (avoids no-short guard
    # false-positive if include_pending were true).
    time.sleep(0.5)

    # 2. Place new
    try:
        new_oid = b.place_stop_limit("SELL", cb_qty, new_stop_px, limit_px)
    except Exception as e:
        print(f"\n✗ PLACE FAILED: {e}")
        print(f"  Old stop already cancelled — your position is UNPROTECTED until "
              f"the bot places a fresh one on next tick.")
        return
    print(f"\n✓ Placed new stop: oid={new_oid} stop=${new_stop_px} limit=${limit_px}")

    # 3. Update sleeve state
    target_ss["resting_stop_oid"] = new_oid
    target_ss["resting_stop_px"] = new_stop_px
    target_ss["resting_stop_stage"] = "manual_reprice"
    sleeves_state = target_state.get("sleeves") or {}
    sleeves_state[target_sleeve_id] = target_ss
    target_state["sleeves"] = sleeves_state
    store.put_state(target_tenant, product_id, target_state)
    print(f"\n✓ Sleeve state updated (tenant={target_tenant}, sleeve={target_sleeve_id})")
    print(f"  With reload-on-tick (commit 83dd31b), the bot will pick this up "
          f"on its next tick and continue ratcheting UP from ${new_stop_px}.")


if __name__ == "__main__":
    main()
