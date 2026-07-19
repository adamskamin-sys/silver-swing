"""Force-credit a filled resting stop back to sleeve state.

Adam 2026-07-15: ratchet-stop fires on Coinbase (verified via Orders
page showing Filled 100% at $568.50) but our sleeve state isn't
advancing — resting_stop_oid still set, cycles/realized stuck, state
still ARMED_SELL with own_avg_entry set. Auto-poller either not deployed
or not firing on this sleeve.

This script:
  1. Reads sleeve state
  2. Queries Coinbase order_status on ss.resting_stop_oid
  3. If FILLED: credits the fill (same logic as _maybe_credit_resting_stop_fill)
  4. Otherwise: reports the actual status so we can debug

Read-only by default. Usage:
    python3 diag_force_credit_resting_stop.py ZEC-20DEC30-CDE               # dry-run
    python3 diag_force_credit_resting_stop.py ZEC-20DEC30-CDE --apply       # execute

    # When own_avg_entry was never recorded (HALTED with "own_avg unknown"),
    # look up the actual buy fill on Coinbase Orders and pass it:
    python3 diag_force_credit_resting_stop.py XLP-20DEC30-CDE --buy-avg 0.18689 --apply
"""
from __future__ import annotations
import os
import sys
import time


def main() -> None:
    if len(sys.argv) < 2:
        print("USAGE: python3 diag_force_credit_resting_stop.py <PRODUCT_ID> "
              "[--apply] [--buy-avg PRICE]")
        return
    product_id = sys.argv[1]
    apply = "--apply" in sys.argv
    manual_buy_avg = None
    if "--buy-avg" in sys.argv:
        try:
            manual_buy_avg = float(sys.argv[sys.argv.index("--buy-avg") + 1])
        except (IndexError, ValueError):
            print("✗ --buy-avg needs a numeric price")
            return
        if manual_buy_avg <= 0:
            print(f"✗ --buy-avg must be > 0, got {manual_buy_avg}")
            return

    print("=" * 78)
    print(f"FORCE CREDIT RESTING STOP{'  (APPLYING)' if apply else '  (dry-run)'} — {product_id}")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    raw = store._load()

    target_tenant = None
    target_sid = None
    target_state = None
    target_cfg = None
    for tenant, tenant_data in raw.items():
        if not isinstance(tenant_data, dict):
            continue
        entry = tenant_data.get(product_id)
        if not isinstance(entry, dict):
            continue
        cfg = entry.get("config") or {}
        state = entry.get("state") or {}
        sleeves_cfg = {s.get("id"): s for s in (cfg.get("sleeves") or [])}
        sleeves_state = state.get("sleeves") or {}
        for sid, sc in sleeves_cfg.items():
            ss = sleeves_state.get(sid, {}) or {}
            if ss.get("resting_stop_oid"):
                target_tenant = tenant
                target_sid = sid
                target_state = ss
                target_cfg = sc

    if not target_state:
        print(f"\nNo sleeve with resting_stop_oid on {product_id}.")
        return

    print(f"\nSleeve: tenant={target_tenant}  id={target_sid}")
    print(f"  state={target_state.get('state')}")
    print(f"  own_avg_entry={target_state.get('own_avg_entry')}")
    print(f"  cycles={target_state.get('cycles', 0)}  realized_pnl=${target_state.get('realized_pnl', 0):.2f}")
    print(f"  resting_stop_oid={target_state.get('resting_stop_oid')}")
    print(f"  resting_stop_px={target_state.get('resting_stop_px')}")
    print(f"  resting_stop_stage={target_state.get('resting_stop_stage')}")

    # Query Coinbase for order status
    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id=product_id))
    oid = target_state["resting_stop_oid"]
    try:
        status_info = b.order_status(oid)
    except Exception as e:
        print(f"\n✗ order_status({oid}) failed: {e}")
        return
    print(f"\nCoinbase order_status({oid}):")
    for k, v in (status_info or {}).items():
        print(f"  {k}: {v}")

    status = (status_info or {}).get("status")
    if status != "FILLED":
        print(f"\n✗ Status is {status}, not FILLED — nothing to credit.")
        if status == "OPEN":
            print("  The stop is still resting on the book. If Coinbase Orders page")
            print("  shows it Filled, there's an SDK/status-mapping issue.")
        return

    fill_price = float(status_info.get("average_filled_price") or 0)
    filled_qty = int(status_info.get("filled_qty") or target_cfg.get("qty") or 1)
    own_avg = float(target_state.get("own_avg_entry") or 0)
    if own_avg <= 0 and manual_buy_avg is not None:
        own_avg = manual_buy_avg
        print(f"\n  ⚠ own_avg_entry was None — using --buy-avg override ${own_avg}")
    if fill_price <= 0 or own_avg <= 0:
        print(f"\n✗ fill_price={fill_price}, own_avg={own_avg} — can't compute profit.")
        if own_avg <= 0:
            print(f"  Look up the actual buy fill for {product_id} on Coinbase Orders,")
            print(f"  then re-run with: --buy-avg <price>")
        return

    # Get contract_size
    try:
        spec = b.contract_spec()
        contract_size = float(spec.get("contract_size") or 1)
    except Exception:
        contract_size = 1.0
    profit = (fill_price - own_avg) * filled_qty * contract_size

    print(f"\nWOULD CREDIT:")
    print(f"  fill_price=${fill_price}  own_avg=${own_avg}  qty={filled_qty}  contract_size={contract_size}")
    print(f"  profit = ({fill_price} - {own_avg}) × {filled_qty} × {contract_size} = ${profit:.2f}")
    print(f"  new_realized_pnl = ${target_state.get('realized_pnl', 0)} + ${profit:.2f} = ${target_state.get('realized_pnl', 0) + profit:.2f}")
    print(f"  new_cycles = {target_state.get('cycles', 0)} + 1 = {target_state.get('cycles', 0) + 1}")
    print(f"  state: {target_state.get('state')} → ARMED_BUY")
    print(f"  own_avg_entry: {own_avg} → None")
    print(f"  resting_stop_oid: {oid} → None")

    if not apply:
        print("\n(dry-run — pass --apply to execute)")
        return

    # Apply
    target_state["realized_pnl"] = float(target_state.get("realized_pnl", 0) or 0) + profit
    target_state["cycles"] = int(target_state.get("cycles", 0) or 0) + 1
    target_state["last_sell_qty"] = filled_qty
    target_state["last_sell_fill_price"] = fill_price
    target_state["own_avg_entry"] = None
    target_state["resting_stop_oid"] = None
    target_state["resting_stop_px"] = None
    target_state["resting_stop_stage"] = None
    target_state["state"] = "ARMED_BUY"
    target_state["armed_buy_since_ts"] = time.time()
    try:
        recent = list(target_state.get("recent_cycle_pnls") or [])
        recent.append(profit)
        if len(recent) > 20:
            recent = recent[-20:]
        target_state["recent_cycle_pnls"] = recent
    except Exception:
        pass

    # Persist
    entry = raw[target_tenant][product_id]
    state = entry.get("state") or {}
    sleeves_state = state.get("sleeves") or {}
    # Clear any HALT reason set by the reconciliation gap so the sleeve can
    # re-arm normally on next tick. Preserved under _prev_halt_reason for audit.
    if target_state.get("state") == "HALTED" or target_state.get("halt_reason"):
        target_state["_prev_halt_reason"] = target_state.get("halt_reason")
        target_state["halt_reason"] = None
    sleeves_state[target_sid] = target_state
    state["sleeves"] = sleeves_state
    store.put_state(target_tenant, product_id, state)
    print(f"\n✓ APPLIED. Sleeve {target_sid} credited with ${profit:.2f} profit.")
    print("  Refresh dashboard — realized should reflect the new number.")
    if manual_buy_avg is not None:
        print(f"  Note: used manual buy_avg ${manual_buy_avg} — verify against Coinbase.")


if __name__ == "__main__":
    main()
