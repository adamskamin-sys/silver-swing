"""Trace where a symbol's buy_px/sell_px came from and how it evolved.

Adam 2026-07-17: XLM PERP CDE placed 8 buy orders at $0.00077-$0.00952
while mark was $0.187 on 7/17. Auto-refresh corrected to $0.18489 but
we want to know where the initial ~$0.01 came from.

Read-only. Dumps for the given symbol:
  1. Current state + config (buy_px, sell_px, sleeves, timestamps)
  2. Last N sleeve_arm / order_placed / order_cancelled events
  3. Last N sleeve_auto_refresh / sleeve_reanchored / expert_spread events
  4. Sleeve state history if trade log has sleeve_state_changed events

Usage:
    python3 diag_symbol_price_trace.py XLM-PERP-CDE
    python3 diag_symbol_price_trace.py XLM-PERP-CDE --hours 6
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time


TENANT = "adam-live"

EVENTS_OF_INTEREST = {
    "sleeve_arm", "sleeve_arm_failed",
    "order_placed", "order_cancelled_for_rearm", "cancel_failed",
    "sleeve_auto_refresh", "sleeve_auto_refresh_stale",
    "sleeve_reanchored", "sleeve_reanchor_via_expert",
    "expert_spread_applied", "expert_spread_intra_cycle_decision",
    "expert_spread_primary_applied",
    "sleeve_reeval_cancel_replace",
    "sleeve_reentry_fired", "sleeve_reentry_pending",
    "sleeve_on_fill", "sleeve_cycle_completed",
    "scanner_arm", "arm_as_sleeve",
    "config_migrated", "config_seeded",
    "trail_distance_adapted", "expert_trail_applied",
    "expert_stop_applied",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--data-dir", default=os.getenv("SWING_DATA_DIR", "data"))
    args = ap.parse_args()

    sym = args.symbol
    from state_store import make_store
    store = make_store(args.data_dir)

    # Fuzzy match: if the exact symbol has no state, look for symbols
    # containing the substring on the tenant.
    all_syms = [s for s in store.list_symbols(TENANT) if not s.startswith("__")]
    if sym not in all_syms:
        needle = sym.upper().replace("-", "").replace("_", "")
        candidates = [s for s in all_syms
                       if needle in s.upper().replace("-", "").replace("_", "")]
        if not candidates:
            # Try just the first token (e.g. "XLM" from "XLM-PERP-CDE")
            token = sym.split("-")[0].upper()
            candidates = [s for s in all_syms if token in s.upper()]
        if not candidates:
            print(f"✗ No symbol matching {sym!r} on tenant {TENANT}.")
            print(f"\nAll symbols on {TENANT}:")
            for s in sorted(all_syms):
                print(f"  · {s}")
            sys.exit(2)
        if len(candidates) == 1:
            print(f"Note: exact {sym!r} not found; using {candidates[0]!r}\n")
            sym = candidates[0]
        else:
            print(f"✗ {sym!r} matched {len(candidates)} symbols — be more specific:")
            for c in candidates:
                print(f"  · {c}")
            sys.exit(2)

    print("=" * 90)
    print(f"SYMBOL PRICE TRACE — {sym} — last {args.hours}h")
    print("=" * 90)

    # ---- current state ----------------------------------------------------
    st = store.get_state(TENANT, sym) or {}
    cfg = store.get_config(TENANT, sym) or {}
    print(f"\n[1] CURRENT CONFIG")
    for k in ("buy_px", "sell_px", "swing_qty", "core_qty",
              "abort_below", "abort_above", "contract_size",
              "fee_per_contract_roundtrip", "tick_size",
              "_seeded_by", "_seeded_ts", "_migrated_ts",
              "_auto_seeded", "_auto_seeded_ts", "_auto_seeded_by",
              "_last_expert_refresh_ts"):
        if k in cfg:
            v = cfg[k]
            if k.endswith("_ts") and isinstance(v, (int, float)) and v > 0:
                age = time.time() - float(v)
                v = f"{v} ({age/3600:.2f}h ago)"
            print(f"  {k:<32} = {v!r}")
    sleeves_cfg = cfg.get("sleeves") or []
    print(f"\n  Config sleeves ({len(sleeves_cfg)}):")
    for s in sleeves_cfg:
        print(f"    · id={s.get('id')} name={s.get('name')} "
              f"qty={s.get('qty')} buy_px={s.get('buy_px')} "
              f"sell_px={s.get('sell_px')} stop_loss_px={s.get('stop_loss_px')}")

    print(f"\n[2] CURRENT STATE")
    for k in ("state", "swing_qty", "live_order_id",
              "last_heartbeat_ts", "last_sell_fill_price",
              "armed_buy_since_ts", "last_step_ok_ts"):
        if k in st:
            v = st[k]
            if k.endswith("_ts") and isinstance(v, (int, float)) and v > 0:
                age = time.time() - float(v)
                v = f"{v} ({age/60:.1f} min ago)"
            print(f"  {k:<28} = {v!r}")
    sleeves_st = st.get("sleeves") or {}
    print(f"\n  State sleeves ({len(sleeves_st)}):")
    for sid, ss in sleeves_st.items():
        print(f"    · {sid}: state={ss.get('state')} "
              f"cycles={ss.get('cycles')} "
              f"live_order_id={ss.get('live_order_id')} "
              f"own_avg_entry={ss.get('own_avg_entry')} "
              f"armed_buy_since_ts={ss.get('armed_buy_since_ts')}")

    # ---- trade log events -------------------------------------------------
    cutoff_ts = time.time() - args.hours * 3600.0
    events: list[dict] = []
    try:
        from safety import make_trade_log
        log = make_trade_log(args.data_dir)
        for e in log.events():
            try:
                if float(e.get("ts") or 0) < cutoff_ts:
                    continue
                etype = str(e.get("event_type") or "")
                if etype not in EVENTS_OF_INTEREST:
                    continue
                sym_e = str(e.get("symbol") or "")
                if sym_e and sym_e != sym:
                    continue
                # If no symbol field, include (some events don't carry symbol)
                events.append(e)
            except (ValueError, TypeError):
                pass
    except Exception as e:
        print(f"\n✗ Trade log read failed: {type(e).__name__}: {e}")
        sys.exit(1)

    events.sort(key=lambda e: float(e.get("ts") or 0))

    print(f"\n[3] TRADE LOG EVENTS ({len(events)} relevant, last {args.hours}h)")
    if not events:
        print("  (no events matched)")
    for e in events:
        ts = float(e.get("ts") or 0)
        age = time.time() - ts if ts else 0
        etype = e.get("event_type")
        # Extract key fields depending on event type
        keys_to_show = ["side", "qty", "price", "buy_px", "sell_px",
                        "stop_loss_px", "order_id", "sleeve_id",
                        "new_buy_px", "new_sell_px", "old_buy_px",
                        "old_sell_px", "as_buy_px", "as_sell_px",
                        "as_spread", "reason", "sold_price",
                        "expert_buy_px", "expert_sell_px",
                        "stale_hours", "delta_hours"]
        fields = {k: e[k] for k in keys_to_show if k in e}
        print(f"  {age/60:6.1f}min ago  {etype}")
        if fields:
            for k, v in fields.items():
                if isinstance(v, str) and len(v) > 120:
                    v = v[:117] + "..."
                print(f"      {k}={v}")
    print()
    print("=" * 90)


if __name__ == "__main__":
    main()
