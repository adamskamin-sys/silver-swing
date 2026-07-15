"""Current snapshot of every sleeve: state, position, resting orders.

Adam 2026-07-15: after a batch of shipping, want a single view of
"what's every sleeve doing right now?" without grepping the trade log.
Reeval only runs on ARMED_BUY sleeves, so if the fleet is quiet, it's
usually because most sleeves are in ARMED_SELL (holding). This confirms.

For each sleeve, prints:
  - Product ID
  - Sleeve name + id
  - State (ARMED_BUY / ARMED_SELL / HALTED)
  - live_order_id (the resting Coinbase order if any)
  - resting_stop_oid (the ratchet-stop's Coinbase order if any)
  - Coinbase position_qty for that product
  - Configured buy_px / sell_px / stop_loss_px / trail_distance
  - Sleeve realized_pnl + cycles
  - Interpretation: what this sleeve is currently doing

Read-only. Usage:
    python3 diag_sleeve_snapshot.py
    python3 diag_sleeve_snapshot.py BIT    # filter to symbols containing BIT
"""
from __future__ import annotations
import os
import sys


def _load_store() -> dict:
    data_dir = os.getenv("SWING_DATA_DIR", "data")
    try:
        import state_store
        return state_store.make_store(data_dir)._load()
    except Exception as e:
        print(f"  WARN: state_store failed: {e}")
        return {}


def _get_position(product_id: str) -> int | None:
    """Try to get current Coinbase position for the product. Returns None
    if credentials unavailable or query fails."""
    try:
        from broker import BrokerConfig, CoinbaseBroker
        b = CoinbaseBroker(BrokerConfig(product_id=product_id))
        return int(b.position_qty() or 0)
    except Exception:
        return None


def main() -> None:
    filter_arg = sys.argv[1].upper() if len(sys.argv) > 1 else ""
    print("=" * 78)
    hdr = "SLEEVE SNAPSHOT"
    if filter_arg:
        hdr += f" (filter={filter_arg})"
    print(hdr)
    print("=" * 78)

    store = _load_store()
    if not store:
        print("\nNO STORE.")
        return

    total_sleeves = 0
    armed_buy = 0
    armed_sell = 0
    halted = 0

    for tenant, tenant_data in store.items():
        if not isinstance(tenant_data, dict):
            continue
        for product_id, entry in tenant_data.items():
            if not isinstance(entry, dict) or product_id.startswith("__"):
                continue
            if filter_arg and filter_arg not in product_id.upper():
                continue
            cfg = entry.get("config") or {}
            state = entry.get("state") or {}
            sleeves_cfg = {s.get("id"): s for s in (cfg.get("sleeves") or [])}
            sleeves_state = state.get("sleeves") or {}
            if not sleeves_cfg:
                continue

            # Product-level position (one Coinbase call per product)
            pos = _get_position(product_id)
            pos_s = f"pos={pos}" if pos is not None else "pos=?"

            print(f"\n── {product_id}   tenant={tenant}   {pos_s}")
            for sid, sc in sleeves_cfg.items():
                total_sleeves += 1
                ss = sleeves_state.get(sid, {}) or {}
                sleeve_state = ss.get("state", "?")
                if sleeve_state == "ARMED_BUY":
                    armed_buy += 1
                elif sleeve_state == "ARMED_SELL":
                    armed_sell += 1
                elif sleeve_state == "HALTED":
                    halted += 1

                live_oid = ss.get("live_order_id")
                rest_oid = ss.get("resting_stop_oid")
                rest_px = ss.get("resting_stop_px")
                rest_stage = ss.get("resting_stop_stage")
                cycles = ss.get("cycles", 0)
                realized = ss.get("realized_pnl", 0.0)
                own_avg = ss.get("own_avg_entry")

                buy_px = sc.get("buy_px")
                sell_px = sc.get("sell_px")
                stop_px = sc.get("stop_loss_px", 0)
                trail_dist = sc.get("trail_distance", 0)
                qty = sc.get("qty", 1)

                # Interpretation
                interp = []
                if sleeve_state == "ARMED_BUY" and live_oid:
                    interp.append(f"waiting for buy fill @ ${buy_px}")
                elif sleeve_state == "ARMED_BUY" and not live_oid:
                    interp.append(f"⚠ ARMED_BUY but NO live_order (ghost — should self-heal next tick)")
                elif sleeve_state == "ARMED_SELL" and live_oid:
                    interp.append(f"holding, waiting for sell fill @ ${sell_px}")
                elif sleeve_state == "ARMED_SELL":
                    interp.append(f"holding, no live sell order")
                elif sleeve_state == "HALTED":
                    interp.append(f"HALTED — reason: {ss.get('halt_reason', '?')}")

                if rest_oid:
                    interp.append(f"✓ ratchet-stop live @ ${rest_px} stage={rest_stage}")

                print(f"    {sc.get('name', sid)[:35]:35s} id={sid[:12]}")
                print(f"       state={sleeve_state:12s} qty={qty}  cycles={cycles}  realized=${realized:.2f}")
                print(f"       buy=${buy_px}  sell=${sell_px}  stop=${stop_px}  trail_dist=${trail_dist}")
                if own_avg:
                    print(f"       own_avg_entry=${own_avg}")
                if live_oid:
                    print(f"       live_order_id={str(live_oid)[:20]}...")
                if rest_oid:
                    print(f"       resting_stop_oid={str(rest_oid)[:20]}...  px=${rest_px}  stage={rest_stage}")
                for line in interp:
                    print(f"       → {line}")

    print(f"\nSUMMARY: {total_sleeves} sleeves total")
    print(f"   ARMED_BUY  (waiting to buy):        {armed_buy}")
    print(f"   ARMED_SELL (holding, waiting sell): {armed_sell}")
    print(f"   HALTED:                              {halted}")
    print(f"   Reeval only runs on ARMED_BUY sleeves ({armed_buy} candidates).")


if __name__ == "__main__":
    main()
