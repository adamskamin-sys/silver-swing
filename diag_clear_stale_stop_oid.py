"""Force-clear a stale resting_stop_oid from bot state so the bot re-
places a fresh stop on next tick.

Adam 2026-07-19 XLM 31 JUL 26 incident: sleeve state has
resting_stop_oid=f2aabf2c... but Coinbase has ZERO open orders on
XLM. Position is 1 LONG at $0.18828, UNPROTECTED (feedback_ratchet_
stop_never_gap violation). The bot's self-heal path
(_maybe_credit_resting_stop_fill → order_status → CANCELLED → clear)
isn't firing for some reason.

Fix: strip the stop_oid from bot state (also clears resting_stop_px
+ resting_stop_stage). On next tick, _maintain_resting_stop sees
oid=None + held position + stop_loss_enabled → places fresh stop.

Safety:
  - REFUSES if oid IS present on Coinbase as OPEN (would clobber a
    valid stop)
  - REFUSES if bot position=0 (nothing to protect)
  - Preserves halt_reason and every other sleeve field

Usage:
    python3 diag_clear_stale_stop_oid.py XLM-31JUL26-CDE scan-mrqqcnu1
    python3 diag_clear_stale_stop_oid.py XLM-31JUL26-CDE scan-mrqqcnu1 --apply
"""
from __future__ import annotations
import os
import sys


def main() -> None:
    if len(sys.argv) < 3:
        print("USAGE: python3 diag_clear_stale_stop_oid.py <PRODUCT_ID> <SLEEVE_ID> [--apply]")
        return
    product_id = sys.argv[1]
    sleeve_id = sys.argv[2]
    apply = "--apply" in sys.argv

    print("=" * 78)
    print(f"CLEAR STALE STOP_OID{'  (APPLYING)' if apply else '  (dry-run)'} — "
          f"{product_id} / {sleeve_id}")
    print("=" * 78)

    # Load bot state
    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    raw = store._load()

    target_tenant = None
    target_state = None
    target_ss = None
    for tenant, tdata in raw.items():
        if not isinstance(tdata, dict):
            continue
        entry = tdata.get(product_id)
        if not isinstance(entry, dict):
            continue
        state = entry.get("state") or {}
        ss = (state.get("sleeves") or {}).get(sleeve_id)
        if ss:
            target_tenant = tenant
            target_state = state
            target_ss = ss
            break

    if target_ss is None:
        print(f"\n✗ Sleeve {sleeve_id} not found on {product_id}.")
        return

    stale_oid = target_ss.get("resting_stop_oid")
    if not stale_oid:
        print(f"\n✓ Nothing to clear — resting_stop_oid is already None.")
        return

    print(f"\nSleeve BEFORE:")
    print(f"  tenant={target_tenant}")
    print(f"  state={target_ss.get('state')}  own_avg={target_ss.get('own_avg_entry')}")
    print(f"  resting_stop_oid={stale_oid}")
    print(f"  resting_stop_px={target_ss.get('resting_stop_px')}")
    print(f"  resting_stop_stage={target_ss.get('resting_stop_stage')}")

    # Safety: verify position on Coinbase
    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id=product_id))
    try:
        pos = int(b.position_qty())
    except Exception as e:
        print(f"\n✗ position_qty() failed: {e} — refusing")
        return
    print(f"\nCoinbase position: {pos}")
    if pos == 0:
        print(f"✗ REFUSING: no held position. If bot state claims to hold, "
              f"use diag_clear_ghost_own_avg.py instead.")
        return

    # Safety: verify oid is actually stale (not open)
    try:
        st = b.order_status(stale_oid)
    except Exception:
        st = None
    status = (st or {}).get("status") if st else "UNKNOWN"
    print(f"Coinbase order_status({stale_oid}): {status}")
    if status == "OPEN":
        print(f"✗ REFUSING: oid is OPEN on Coinbase. Not stale — do NOT clear.")
        return

    if not apply:
        print(f"\nWOULD:")
        print(f"  resting_stop_oid {stale_oid} → None")
        print(f"  resting_stop_px → None")
        print(f"  resting_stop_stage → None")
        print(f"  (bot's next tick will place a fresh stop via _maintain_resting_stop)")
        print(f"\n(dry-run — pass --apply to execute)")
        return

    # Apply
    target_ss["resting_stop_oid"] = None
    target_ss["resting_stop_px"] = None
    target_ss["resting_stop_stage"] = None
    sleeves = target_state.get("sleeves") or {}
    sleeves[sleeve_id] = target_ss
    target_state["sleeves"] = sleeves
    store.put_state(target_tenant, product_id, target_state)
    print(f"\n✓ APPLIED. Stale stop_oid cleared.")
    print(f"  With reload-on-tick deployed (commit 83dd31b), the next bot tick")
    print(f"  will read this and re-place a fresh resting stop within ~1s.")


if __name__ == "__main__":
    main()
