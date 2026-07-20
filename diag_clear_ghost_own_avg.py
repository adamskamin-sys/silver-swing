"""Clear a stale own_avg_entry on a sleeve whose bot state claims HELD
but Coinbase reports position=0.

Adam 2026-07-19: XLP scan-mrqn4az1 sits in ARMED_SELL with
own_avg_entry=0.18775 and stop_loss_enabled=True, but Coinbase confirms
position size=0. The own_avg is a ghost from a prior cycle. Left as-is,
the sleeve will refuse to arm a BUY (thinks it holds) and refuse to arm
a stop (no real position to stop).

Fix: transition state ARMED_SELL → ARMED_BUY, clear own_avg_entry,
clear resting_stop_oid, clear live_order_id, stamp armed_buy_since_ts.
Next tick the bot will arm a fresh BUY order at sc.buy_px.

Read-only by default. Usage:
    python3 diag_clear_ghost_own_avg.py XLP-20DEC30-CDE scan-mrqn4az1
    python3 diag_clear_ghost_own_avg.py XLP-20DEC30-CDE scan-mrqn4az1 --apply

Refuses to apply if Coinbase reports a real position (protects against
zeroing out a legit holding).
"""
from __future__ import annotations
import os
import sys
import time


def main() -> None:
    if len(sys.argv) < 3:
        print("USAGE: python3 diag_clear_ghost_own_avg.py <PRODUCT_ID> <SLEEVE_ID> [--apply]")
        return
    product_id = sys.argv[1]
    sleeve_id = sys.argv[2]
    apply = "--apply" in sys.argv

    print("=" * 78)
    print(f"CLEAR GHOST own_avg{'  (APPLYING)' if apply else '  (dry-run)'} — {product_id} / {sleeve_id}")
    print("=" * 78)

    # Ground truth: Coinbase position for this product
    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id=product_id))
    try:
        cb_qty = int(b.position_qty())
    except Exception as e:
        print(f"✗ position_qty() failed: {e} — refusing to apply blind")
        return
    print(f"\nCoinbase {product_id} position: {cb_qty}")
    # Load state early so we can check per-sleeve claims before refusing.
    import state_store as _ss_early
    _pre_store = _ss_early.make_store(os.getenv("SWING_DATA_DIR", "data"))
    _pre_raw = _pre_store._load()
    _pre_target_ss = None
    for _t, _td in (_pre_raw or {}).items():
        if not isinstance(_td, dict):
            continue
        _entry = _td.get(product_id)
        if not isinstance(_entry, dict):
            continue
        _sleeves = (_entry.get("state") or {}).get("sleeves") or {}
        _ss2 = _sleeves.get(sleeve_id)
        if _ss2:
            _pre_target_ss = _ss2
            break
    _pre_state = str((_pre_target_ss or {}).get("state") or "").upper()
    _pre_halted = _pre_state == "HALTED"
    _pre_own_avg = (_pre_target_ss or {}).get("own_avg_entry")
    if cb_qty != 0:
        # Multi-sleeve carve-out (2026-07-20): if THIS sleeve is HALTED
        # (needs manual clearance) or claims nothing (own_avg=None), it
        # doesn't own the position — a sibling sleeve does. Safe to clear.
        # Only refuse if THIS sleeve legitimately claims the position AND
        # isn't in a halt requiring intervention.
        if _pre_halted:
            print(f"  → Coinbase position {cb_qty} confirmed, but THIS sleeve is "
                  f"HALTED (needs manual clearance). Position belongs to sibling "
                  f"sleeve(s). Proceeding with clear.")
        elif _pre_own_avg in (None, 0, 0.0):
            print(f"  → Coinbase position {cb_qty} confirmed, but THIS sleeve "
                  f"claims nothing (own_avg=None). Position belongs to sibling. "
                  f"Proceeding with clear.")
        else:
            print(f"✗ REFUSING: Coinbase reports real position of {cb_qty} "
                  f"contracts AND this sleeve claims own_avg=${_pre_own_avg}.")
            print(f"  own_avg is NOT a ghost — do not clear.")
            return

    # Load state
    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    raw = store._load()

    target_tenant = None
    target_state = None
    target_ss = None
    for tenant, tenant_data in raw.items():
        if not isinstance(tenant_data, dict):
            continue
        entry = tenant_data.get(product_id)
        if not isinstance(entry, dict):
            continue
        state = entry.get("state") or {}
        sleeves_state = state.get("sleeves") or {}
        ss = sleeves_state.get(sleeve_id)
        if ss:
            target_tenant = tenant
            target_state = state
            target_ss = ss
            break

    if target_ss is None:
        print(f"\n✗ Sleeve {sleeve_id} not found on {product_id} in any tenant.")
        return

    print(f"\nSleeve BEFORE: tenant={target_tenant}")
    print(f"  state={target_ss.get('state')}")
    print(f"  own_avg_entry={target_ss.get('own_avg_entry')}")
    print(f"  resting_stop_oid={target_ss.get('resting_stop_oid')}")
    print(f"  live_order_id={target_ss.get('live_order_id')}")
    print(f"  halt_reason={target_ss.get('halt_reason')}")
    print(f"  cycles={target_ss.get('cycles')}  realized_pnl=${target_ss.get('realized_pnl', 0)}")

    # Two ghost shapes we clear:
    #   (a) own_avg set but Coinbase position=0 (classic ghost)
    #   (b) own_avg=None but sleeve HALTED with a dead resting_stop_oid
    #       — Resume alone won't stick because the credit path re-fires
    #         against the stale oid and re-halts.
    is_halted_with_dead_stop = (
        target_ss.get("state") == "HALTED"
        and target_ss.get("resting_stop_oid")
    )
    if target_ss.get("own_avg_entry") is None and not is_halted_with_dead_stop:
        print(f"\n✓ Nothing to clear — own_avg_entry is None and no HALTED+stop_oid to reset.")
        return

    # Preview the transition
    print(f"\nWOULD:")
    print(f"  state {target_ss.get('state')} → ARMED_BUY")
    print(f"  own_avg_entry {target_ss.get('own_avg_entry')} → None")
    print(f"  resting_stop_oid {target_ss.get('resting_stop_oid')} → None")
    print(f"  live_order_id {target_ss.get('live_order_id')} → None")
    if target_ss.get("halt_reason"):
        print(f"  halt_reason {target_ss.get('halt_reason')} → None (was: preserved as _prev_halt_reason)")
    print(f"  armed_buy_since_ts → now")
    print(f"  cycles / realized_pnl: unchanged")

    if not apply:
        print(f"\n(dry-run — pass --apply to execute)")
        return

    # Apply
    if target_ss.get("halt_reason"):
        target_ss["_prev_halt_reason"] = target_ss.get("halt_reason")
    target_ss["state"] = "ARMED_BUY"
    target_ss["own_avg_entry"] = None
    target_ss["resting_stop_oid"] = None
    target_ss["resting_stop_px"] = None
    target_ss["resting_stop_stage"] = None
    target_ss["live_order_id"] = None
    target_ss["halt_reason"] = None
    target_ss["armed_buy_since_ts"] = time.time()

    # Persist
    sleeves_state = target_state.get("sleeves") or {}
    sleeves_state[sleeve_id] = target_ss
    target_state["sleeves"] = sleeves_state
    store.put_state(target_tenant, product_id, target_state)
    print(f"\n✓ APPLIED. Sleeve {sleeve_id} ghost cleared.")
    print(f"  With reload-on-tick deployed (commit 83dd31b), the next bot")
    print(f"  tick will read this from Redis and re-arm a fresh BUY.")


if __name__ == "__main__":
    main()
