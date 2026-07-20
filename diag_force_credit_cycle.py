"""Force-credit a missed sleeve cycle AND recover the halted sleeve — atomic.

Adam 2026-07-15: `_credit_stop_fill` halts when own_avg_entry is None at
credit time. HYPE 2026-07-15 07:37:47 sell $68.32 vs 03:06:35 buy $66.81
hit this: cycle counted, +$15.10 profit NOT recorded, sleeve HALTED.

Adam 2026-07-20: prior version of this script only credited the profit
via state_patch — did NOT clear the halt, did NOT reset cycle-scoped
state (own_avg_entry, resting_stop_oid, trail_armed, etc). So after
running the diag, the sleeve stayed HALTED and the operator had to
run diag_clear_ghost_own_avg separately. Now atomic:

  1. Compute profit = (fill − own_avg) × contract_size × qty − half_fee
  2. Increment realized_pnl + cycles + recent_cycle_pnls
  3. Clear own_avg_entry (position was sold)
  4. Clear resting_stop_oid / resting_stop_px / resting_stop_stage
  5. Reset cycle-scoped state (trail_armed, trail_high_water_price,
     hybrid_sell_triggered_ts, buy_trail_armed, buy_trail_low_water,
     stop_loss_hwm)
  6. Clear halt_reason
  7. State → ARMED_BUY, armed_buy_since_ts = now
  8. Write via put_state (bot's reload-on-tick from commit 83dd31b
     picks it up on next tick)

Read-only by default. Usage:

    python3 diag_force_credit_cycle.py PRODUCT_ID SLEEVE_ID FILL_PRICE OWN_AVG [QTY]
    python3 diag_force_credit_cycle.py PRODUCT_ID SLEEVE_ID FILL_PRICE OWN_AVG [QTY] --apply

Example (HYPE 2026-07-15 missed credit + halt clear):
    python3 diag_force_credit_cycle.py HYP-20DEC30-CDE smrluux5w 68.32 66.81 1 --apply
"""
from __future__ import annotations
import os
import sys
import time


def main() -> None:
    if len(sys.argv) < 5:
        print("USAGE: python3 diag_force_credit_cycle.py PRODUCT_ID SLEEVE_ID "
              "FILL_PRICE OWN_AVG [QTY] [--apply]")
        return
    product_id = sys.argv[1]
    sleeve_id = sys.argv[2]
    fill_price = float(sys.argv[3])
    own_avg = float(sys.argv[4])
    qty = int(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] != "--apply" else 1
    apply = "--apply" in sys.argv
    tenant = "adam-live"

    print("=" * 78)
    print(f"FORCE-CREDIT + HALT-RECOVER {'(APPLY)' if apply else '(dry-run)'} "
          f"— {tenant}/{product_id}/{sleeve_id}")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    state = store.get_state(tenant, product_id) or {}
    sleeves_state = state.get("sleeves") or {}
    ss = sleeves_state.get(sleeve_id)
    if ss is None:
        print(f"\n✗ sleeve {sleeve_id} not in state.sleeves for {product_id}")
        print(f"  Existing sleeve ids: {list(sleeves_state.keys())}")
        return

    # Contract spec + fee
    try:
        from broker import BrokerConfig, CoinbaseBroker
        b = CoinbaseBroker(BrokerConfig(product_id=product_id))
        spec = b.contract_spec()
        contract_size = float(spec.get("contract_size") or 0)
    except Exception as e:
        print(f"\n✗ contract_spec failed: {e}")
        return
    if contract_size <= 0:
        print(f"\n✗ contract_size = {contract_size}, can't compute profit")
        return

    # Fee from persisted config (best-effort)
    fee_per_rt = 0.0
    try:
        cfg = store.get_config(tenant, product_id) or {}
        fee_per_rt = float(cfg.get("fee_per_contract_roundtrip") or 0)
    except Exception:
        pass
    half_fee = (fee_per_rt / 2.0) * qty if fee_per_rt > 0 else 0.0

    gross = (fill_price - own_avg) * qty * contract_size
    profit = gross - half_fee
    new_realized = float(ss.get("realized_pnl", 0) or 0) + profit
    old_cycles = int(ss.get("cycles", 0) or 0)
    new_cycles = old_cycles + 1

    print(f"\nCURRENT sleeve state:")
    print(f"  state:            {ss.get('state')}")
    print(f"  halt_reason:      {ss.get('halt_reason')}")
    print(f"  cycles:           {old_cycles}")
    print(f"  realized_pnl:     ${ss.get('realized_pnl', 0):.2f}")
    print(f"  own_avg_entry:    {ss.get('own_avg_entry')}")
    print(f"  resting_stop_oid: {ss.get('resting_stop_oid')}")
    print(f"  trail_armed:      {ss.get('trail_armed')}")

    print(f"\nCOMPUTED profit:")
    print(f"  fill_price:       ${fill_price}")
    print(f"  own_avg:          ${own_avg}")
    print(f"  qty:              {qty}")
    print(f"  contract_size:    {contract_size}")
    print(f"  gross:            ({fill_price} − {own_avg}) × {qty} × {contract_size} = ${gross:.2f}")
    print(f"  half_fee:         (${fee_per_rt}/2) × {qty} = ${half_fee:.2f}")
    print(f"  profit:           ${gross:.2f} − ${half_fee:.2f} = ${profit:.2f}")

    print(f"\nATOMIC RECOVERY (proposed):")
    print(f"  realized_pnl:     ${ss.get('realized_pnl', 0):.2f} → ${new_realized:.2f}")
    print(f"  cycles:           {old_cycles} → {new_cycles}")
    print(f"  state:            {ss.get('state')} → ARMED_BUY")
    print(f"  halt_reason:      {ss.get('halt_reason')} → None")
    print(f"  own_avg_entry:    {ss.get('own_avg_entry')} → None")
    print(f"  resting_stop_oid: {ss.get('resting_stop_oid')} → None")
    print(f"  trail_armed:      {ss.get('trail_armed')} → False")
    print(f"  trail_hwm:        {ss.get('trail_high_water_price')} → 0.0")
    print(f"  hybrid_trig_ts:   {ss.get('hybrid_sell_triggered_ts')} → None")
    print(f"  armed_buy_since:  → now")

    if not apply:
        print("\n(dry-run — pass --apply to persist)")
        return

    # Snapshot the halt reason before clearing (audit trail)
    prev_halt_reason = ss.get("halt_reason")

    # Apply all state changes
    ss["realized_pnl"] = round(new_realized, 6)
    ss["cycles"] = new_cycles
    ss["state"] = "ARMED_BUY"
    if prev_halt_reason:
        ss["_prev_halt_reason"] = prev_halt_reason
    ss["halt_reason"] = None
    ss["own_avg_entry"] = None
    ss["sell_entry_avg"] = None
    ss["resting_stop_oid"] = None
    ss["resting_stop_px"] = None
    ss["resting_stop_stage"] = None
    ss["live_order_id"] = None
    ss["trail_armed"] = False
    ss["trail_high_water_price"] = 0.0
    ss["hybrid_sell_triggered_ts"] = None
    ss["buy_trail_armed"] = False
    ss["buy_trail_low_water"] = 0.0
    ss["stop_loss_hwm"] = None
    ss["consecutive_stops"] = 0
    ss["armed_buy_since_ts"] = time.time()
    # Append this cycle's pnl to recent_cycle_pnls (bounded to 20)
    recent = list(ss.get("recent_cycle_pnls") or [])
    recent.append(round(profit, 6))
    if len(recent) > 20:
        recent = recent[-20:]
    ss["recent_cycle_pnls"] = recent
    ss["last_sell_qty"] = qty
    ss["last_sell_fill_price"] = fill_price
    ss["last_cycle_realized"] = round(new_realized, 6)

    # Persist via put_state — bot's _reload_sleeves_from_redis (commit 83dd31b)
    # will pick up the change on next tick.
    sleeves_state[sleeve_id] = ss
    state["sleeves"] = sleeves_state
    store.put_state(tenant, product_id, state)

    # Trade log for audit trail
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        log.record(
            "force_credit_backfill_atomic",
            tenant=tenant, symbol=product_id, sleeve_id=sleeve_id,
            fill_price=fill_price, own_avg_entry=own_avg,
            qty=qty, contract_size=contract_size,
            gross=round(gross, 2), half_fee=round(half_fee, 2),
            profit=round(profit, 2),
            new_realized_pnl=round(new_realized, 2),
            new_cycles=new_cycles,
            prev_halt_reason=prev_halt_reason,
            reason="atomic force-credit + halt clear + cycle reset",
            severity="info",
        )
    except Exception as e:
        print(f"\n(note: trade log record failed: {e})")

    print(f"\n✓ APPLIED. Sleeve {sleeve_id} fully recovered.")
    print(f"  realized_pnl: ${new_realized:.2f}   cycles: {new_cycles}   state: ARMED_BUY")
    print(f"  Bot will pick up on next tick (reload-on-tick, commit 83dd31b).")
    print(f"  Refresh dashboard in ~5-10s.")


if __name__ == "__main__":
    main()
