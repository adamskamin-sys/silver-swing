"""Force-credit a missed sleeve cycle's realized P&L.

Adam 2026-07-15: `_maybe_credit_resting_stop_fill` skips crediting if
own_avg_entry is None at credit time — computes profit=0 and just
increments the cycle counter. HYPE cycle 1 (2026-07-15 07:37:47 sell
$68.32 vs 03:06:35 buy $66.81) hit this: cycle counted, +$15.10 profit
NOT recorded in the sleeve's realized_pnl.

This script backfills. Read the exact prices from Coinbase Fills, feed
them in, script computes profit = (fill - own_avg) × contract_size ×
qty and adds to sleeve.realized_pnl. Records a `force_credit_backfill`
event so the audit trail shows a manual correction.

Read-only by default. Usage:

    python3 diag_force_credit_cycle.py PRODUCT_ID SLEEVE_ID FILL_PRICE OWN_AVG [QTY]
    python3 diag_force_credit_cycle.py PRODUCT_ID SLEEVE_ID FILL_PRICE OWN_AVG [QTY] --apply

Example (HYPE 2026-07-15 missed credit):
    python3 diag_force_credit_cycle.py HYP-20DEC30-CDE smrluux5w 68.32 66.81 1
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
    print(f"FORCE-CREDIT CYCLE {'(APPLY)' if apply else '(dry-run)'} "
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

    # Get contract_size from Coinbase
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

    profit = (fill_price - own_avg) * qty * contract_size
    new_realized = float(ss.get("realized_pnl", 0) or 0) + profit
    old_cycles = int(ss.get("cycles", 0) or 0)

    print(f"\nCURRENT sleeve state:")
    print(f"  cycles:       {old_cycles}")
    print(f"  realized_pnl: ${ss.get('realized_pnl', 0):.2f}")
    print(f"  own_avg_entry:{ss.get('own_avg_entry')}")
    print(f"  state:        {ss.get('state')}")

    print(f"\nCOMPUTED profit:")
    print(f"  fill_price:   ${fill_price}")
    print(f"  own_avg:      ${own_avg}")
    print(f"  qty:          {qty}")
    print(f"  contract_size:{contract_size}")
    print(f"  profit:       ({fill_price} - {own_avg}) × {qty} × {contract_size} = ${profit:.2f}")

    print(f"\nAFTER credit (proposed):")
    print(f"  realized_pnl: ${ss.get('realized_pnl', 0):.2f} → ${new_realized:.2f}")
    print(f"  cycles:       {old_cycles} (unchanged — cycle counter was already incremented)")

    if not apply:
        print("\n(dry-run — pass --apply to persist)")
        return

    # Apply
    ss["realized_pnl"] = new_realized
    # Also append to recent_cycle_pnls if the field exists (used for
    # loss-streak detection + TCA display)
    try:
        recent = list(ss.get("recent_cycle_pnls") or [])
        recent.append(profit)
        if len(recent) > 20:
            recent = recent[-20:]
        ss["recent_cycle_pnls"] = recent
    except Exception:
        pass
    sleeves_state[sleeve_id] = ss
    state["sleeves"] = sleeves_state
    store.put_state(tenant, product_id, state)
    # Log the backfill event so the audit trail shows it
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        log.record(
            "force_credit_backfill",
            tenant=tenant, symbol=product_id, sleeve_id=sleeve_id,
            fill_price=fill_price, own_avg_entry=own_avg,
            qty=qty, contract_size=contract_size,
            profit=round(profit, 2),
            new_realized_pnl=round(new_realized, 2),
            reason="manual correction — original credit ran with own_avg_entry=None",
            severity="info",
        )
    except Exception as e:
        print(f"\n(note: trade log record failed: {e})")

    print(f"\n✓ APPLIED. Sleeve {sleeve_id} realized_pnl now ${new_realized:.2f}")
    print("  Refresh the dashboard — REALIZED column should reflect the new number.")


if __name__ == "__main__":
    main()
