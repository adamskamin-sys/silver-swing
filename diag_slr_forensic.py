"""Forensic dump of SLR-27AUG26-CDE sleeve state + recent stop events.

Purpose: definitively answer WHY the ratchet isn't executing. Rather
than more Claude guessing, this reads the actual persisted state
and the actual trade log, so we can see:

  1. What's in Redis right now for each SLR sleeve
     (trail_armed, trail_high_water_price, resting_stop_oid/_px/_stage,
     own_avg_entry, state, live_order_id)
  2. Whether the ratchet EVER fired (search trade log for
     resting_stop_ratcheted events with product_id=SLR)
  3. Whether any place/cancel failures were recorded
  4. Whether trail_breach_market_sell fired but was blocked

If we see NO ratchet events in the log AND HWM is high in state,
then _maintain_resting_stop is being called but not entering the
ratchet branch. If we see ratchet_place_failed events, Coinbase
is rejecting the place. If we see NO events at all, the sleeve
step isn't running _maintain_resting_stop for these sleeves.

Read-only. Prints only. Usage:

    python3 diag_slr_forensic.py
"""
from __future__ import annotations
import json
import os


def main() -> None:
    tenant = "adam-live"
    product_id = "SLR-27AUG26-CDE"

    print("=" * 78)
    print(f"SLR FORENSIC — {tenant}/{product_id}")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    state = store.get_state(tenant, product_id) or {}
    sleeves = state.get("sleeves") or {}

    print(f"\n[1/3] SLEEVE STATE (from Redis, right now):")
    print(f"Total sleeves: {len(sleeves)}")
    for sid, ss in sleeves.items():
        print(f"\n  sleeve {sid} (name: {ss.get('name')}):")
        print(f"    state:                    {ss.get('state')}")
        print(f"    own_avg_entry:            {ss.get('own_avg_entry')}")
        print(f"    sell_entry_avg:           {ss.get('sell_entry_avg')}")
        print(f"    live_order_id:            {ss.get('live_order_id')}")
        print(f"    resting_stop_oid:         {ss.get('resting_stop_oid')}")
        print(f"    resting_stop_px:          {ss.get('resting_stop_px')}")
        print(f"    resting_stop_stage:       {ss.get('resting_stop_stage')}")
        print(f"    trail_armed:              {ss.get('trail_armed')}")
        print(f"    trail_high_water_price:   {ss.get('trail_high_water_price')}")
        print(f"    stop_loss_hwm:            {ss.get('stop_loss_hwm')}")
        print(f"    cycles:                   {ss.get('cycles', 0)}")
        print(f"    realized_pnl:             {ss.get('realized_pnl', 0)}")

    print(f"\n[2/3] SLEEVE CONFIGS (from Redis):")
    config = store.get_config(tenant, product_id) or {}
    for k in ("trail_distance", "sell_px", "buy_px", "stop_loss_px",
              "resting_stop_enabled", "stop_loss_enabled",
              "tick_size", "contract_size", "fee_per_contract_roundtrip"):
        if k in config:
            print(f"  config.{k}: {config[k]}")
    # Sleeve-level configs (each sleeve has its own trail_distance etc)
    sleeves_cfg = config.get("sleeves") or []
    if not sleeves_cfg:
        # Try alternate location
        sleeves_cfg = store.get_sleeves(tenant, product_id) if hasattr(store, "get_sleeves") else []
    for scfg in sleeves_cfg:
        if isinstance(scfg, dict):
            sid = scfg.get("id")
            print(f"\n  sleeve_cfg {sid} (name: {scfg.get('name')}):")
            for k in ("trail_distance", "sell_px", "buy_px", "stop_loss_px",
                      "resting_stop_enabled", "stop_loss_enabled",
                      "exit_mode", "qty", "trail_activation_px"):
                if k in scfg:
                    print(f"    {k}: {scfg[k]}")

    print(f"\n[3/3] RECENT SLR TRADE LOG EVENTS (last 200):")
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        events = log.tail(1000) if hasattr(log, "tail") else []
        slr_events = [e for e in events if str(e.get("symbol") or e.get("product_id") or "") == product_id]
        # Filter to stop-related events
        relevant_kinds = {
            "resting_stop_placed", "resting_stop_ratcheted",
            "resting_stop_ratchet_cancel_failed", "resting_stop_ratchet_place_failed",
            "resting_stop_place_failed", "resting_stop_cleared",
            "resting_stop_skipped_above_mark", "resting_stop_external_cancel_cleared",
            "resting_stop_cancel_failed", "resting_stop_status_check_failed",
            "resting_stop_credit_via_pos_zero_race_fix",
            "trail_breach_market_sell", "trail_breach_market_sell_failed",
            "trail_breach_cancel_failed",
            "sleeve_stop_loss_triggered", "sleeve_stop_loss_sell_failed",
            "expert_trail_applied", "expert_trail_error",
        }
        relevant = [e for e in slr_events if e.get("event_type") in relevant_kinds]
        print(f"Total SLR events in last 1000: {len(slr_events)}")
        print(f"Stop-related SLR events: {len(relevant)}")
        for e in relevant[-30:]:  # last 30 relevant
            ts = e.get("ts", 0)
            kind = e.get("event_type")
            sid = e.get("sleeve_id", "?")
            severity = e.get("severity", "info")
            print(f"  [{ts}] {kind} sleeve={sid} severity={severity}")
            # Show useful details for select event types
            for k in ("target_px", "from_px", "to_px", "stage", "error",
                      "hwm", "trail_distance", "last_price", "oid"):
                if k in e:
                    print(f"        {k}: {e[k]}")
    except Exception as e:
        print(f"Trade log read failed: {e}")


if __name__ == "__main__":
    main()
