"""One-time corrector for CHN sleeve state after auto-heal fake credits.

Background: commit 31fc108 (reverted by a7675c4) shipped a broken
auto-heal that credited a new cycle every tick because it didn't
persist state properly. Each tick reloaded pre-auto-heal state from
Redis, saw own_avg still set + no live_order_id, and fired another
fake credit. CHN went from CYCLES=0 to CYCLES=15+ and REALIZED
inflated to +$555 in ~5 minutes.

Actual truth (verified in Adam's Coinbase orders):
  1 market sell fired 2026-07-19 22:04:29 at $3,106.10
  Buy avg was $3,074.40
  Gross profit: ($3,106.10 - $3,074.40) × 1 × contract_size
  Minus half-fee for the sell side

This corrector:
  1. Reads current CHN sleeve state
  2. Resets cycles to (current − fake_count), where fake_count is
     (current − 1). Result: cycles = 1 (the one real sell).
  3. Recomputes realized_pnl from scratch: previous_realized (before
     the fake credits) + real_profit_for_the_one_sell.
  4. Clears cycle-scoped state (own_avg_entry, resting_stop_oid,
     trail flags, etc) so the sleeve advances cleanly to ARMED_BUY.
  5. Trims recent_cycle_pnls to just the one real entry.

Read-only by default. Usage:

    python3 diag_correct_chn_fake_credits.py                # dry-run
    python3 diag_correct_chn_fake_credits.py --apply        # persist

The math is transparent — dry-run prints every proposed change with
before/after so you can eyeball it before applying.
"""
from __future__ import annotations
import os
import sys
import time


REAL_MARKET_SELL_PRICE = 3106.10   # from Coinbase orders 22:04:29 fill
KNOWN_BUY_PRICE = 3074.40           # Scanner 02:04 sleeve own_avg pre-sell
REAL_SELL_QTY = 1


def main() -> None:
    apply = "--apply" in sys.argv
    tenant = "adam-live"
    product_id = "CHN-19DEC30-CDE"

    print("=" * 78)
    print(f"CORRECT CHN FAKE CREDITS {'(APPLY)' if apply else '(dry-run)'} "
          f"— {tenant}/{product_id}")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    state = store.get_state(tenant, product_id) or {}
    sleeves = state.get("sleeves") or {}
    if not sleeves:
        print(f"\n✗ No sleeves in state for {tenant}/{product_id}")
        return

    # Find the sleeve — there's typically only one CHN sleeve. If more,
    # we correct the one with the fake-inflated cycles (highest cycles).
    target_sid = None
    target_ss = None
    max_cycles = -1
    for sid, ss in sleeves.items():
        c = int(ss.get("cycles", 0) or 0)
        if c > max_cycles:
            max_cycles = c
            target_sid = sid
            target_ss = ss

    if target_ss is None:
        print(f"\n✗ No sleeve found to correct")
        return

    print(f"\nTARGET sleeve: {target_sid} (name: {target_ss.get('name')})")

    # Compute the real profit from the one Coinbase-verified fill.
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

    fee_per_rt = 0.0
    try:
        cfg = store.get_config(tenant, product_id) or {}
        fee_per_rt = float(cfg.get("fee_per_contract_roundtrip") or 0)
    except Exception:
        pass
    half_fee = (fee_per_rt / 2.0) * REAL_SELL_QTY if fee_per_rt > 0 else 0.0

    gross = (REAL_MARKET_SELL_PRICE - KNOWN_BUY_PRICE) * REAL_SELL_QTY * contract_size
    real_profit = gross - half_fee

    # Estimate previous_realized (before fake credits started). If the
    # sleeve had cycles=0 before the real sell, previous_realized was 0.
    # Adam's earlier screenshot showed CHN with CYCLES=0 pre-sell, so we
    # assume previous_realized = 0.
    ASSUMED_PREVIOUS_REALIZED = 0.0
    correct_realized = ASSUMED_PREVIOUS_REALIZED + real_profit
    correct_cycles = 1  # one real market sell

    current_realized = float(target_ss.get("realized_pnl", 0) or 0)
    current_cycles = int(target_ss.get("cycles", 0) or 0)
    fake_realized_delta = current_realized - correct_realized
    fake_cycles_delta = current_cycles - correct_cycles

    print(f"\nCURRENT sleeve state:")
    print(f"  state:            {target_ss.get('state')}")
    print(f"  cycles:           {current_cycles}")
    print(f"  realized_pnl:     ${current_realized:.2f}")
    print(f"  own_avg_entry:    {target_ss.get('own_avg_entry')}")
    print(f"  live_order_id:    {target_ss.get('live_order_id')}")
    print(f"  resting_stop_oid: {target_ss.get('resting_stop_oid')}")
    print(f"  recent_pnls:      {target_ss.get('recent_cycle_pnls')}")

    print(f"\nCOMPUTED real profit for the 22:04:29 sell:")
    print(f"  fill_price:       ${REAL_MARKET_SELL_PRICE}")
    print(f"  buy_price:        ${KNOWN_BUY_PRICE}")
    print(f"  qty:              {REAL_SELL_QTY}")
    print(f"  contract_size:    {contract_size}")
    print(f"  gross:            ({REAL_MARKET_SELL_PRICE} - {KNOWN_BUY_PRICE}) × {REAL_SELL_QTY} × {contract_size} = ${gross:.2f}")
    print(f"  half_fee:         ${half_fee:.2f}")
    print(f"  real_profit:      ${real_profit:.2f}")

    print(f"\nCORRECTION (proposed):")
    print(f"  cycles:           {current_cycles} → {correct_cycles} "
          f"(removes {fake_cycles_delta} fake credits)")
    print(f"  realized_pnl:     ${current_realized:.2f} → ${correct_realized:.2f} "
          f"(removes ${fake_realized_delta:.2f} fake profit)")
    print(f"  state:            {target_ss.get('state')} → ARMED_BUY")
    print(f"  own_avg_entry:    → None")
    print(f"  live_order_id:    → None")
    print(f"  resting_stop_oid: → None")
    print(f"  trail_armed:      → False")
    print(f"  trail_hwm:        → 0.0")
    print(f"  recent_pnls:      → [${real_profit:.2f}]")

    if not apply:
        print("\n(dry-run — pass --apply to persist)")
        return

    # Apply
    target_ss["cycles"] = correct_cycles
    target_ss["realized_pnl"] = round(correct_realized, 6)
    target_ss["state"] = "ARMED_BUY"
    target_ss["own_avg_entry"] = None
    target_ss["sell_entry_avg"] = None
    target_ss["live_order_id"] = None
    target_ss["resting_stop_oid"] = None
    target_ss["resting_stop_px"] = None
    target_ss["resting_stop_stage"] = None
    target_ss["trail_armed"] = False
    target_ss["trail_high_water_price"] = 0.0
    target_ss["hybrid_sell_triggered_ts"] = None
    target_ss["buy_trail_armed"] = False
    target_ss["buy_trail_low_water"] = 0.0
    target_ss["stop_loss_hwm"] = None
    target_ss["consecutive_stops"] = 0
    target_ss["recent_cycle_pnls"] = [round(real_profit, 6)]
    target_ss["last_sell_qty"] = REAL_SELL_QTY
    target_ss["last_sell_fill_price"] = REAL_MARKET_SELL_PRICE
    target_ss["last_cycle_realized"] = round(correct_realized, 6)
    target_ss["armed_buy_since_ts"] = time.time()
    target_ss["halt_reason"] = None

    sleeves[target_sid] = target_ss
    state["sleeves"] = sleeves
    store.put_state(tenant, product_id, state)

    # Audit log
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        log.record(
            "correct_chn_fake_credits_applied",
            tenant=tenant, symbol=product_id, sleeve_id=target_sid,
            fake_cycles_removed=fake_cycles_delta,
            fake_realized_removed=round(fake_realized_delta, 2),
            correct_cycles=correct_cycles,
            correct_realized=round(correct_realized, 2),
            real_profit=round(real_profit, 2),
            reason=("one-time correction after auto-heal commit 31fc108 "
                    "fabricated cycles + realized_pnl before it was "
                    "reverted by a7675c4"),
            severity="warn",
        )
    except Exception as e:
        print(f"\n(note: trade log record failed: {e})")

    print(f"\n✓ APPLIED. CHN sleeve {target_sid} corrected.")
    print(f"  cycles: {correct_cycles}  realized: ${correct_realized:.2f}  state: ARMED_BUY")
    print(f"  Bot picks up on next tick (reload-on-tick).")


if __name__ == "__main__":
    main()
