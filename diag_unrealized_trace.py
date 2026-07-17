"""Trace unrealized P&L discrepancy for a symbol.

Adam 2026-07-17: PT (PLAT) shows +$71 unrealized on dashboard with
qty=0 SIDE=WAITING. Every other WAITING sleeve shows $0. Suspects:
  (a) stale Coinbase portfolio snapshot still reporting PT with qty>0
  (b) unrealized accumulator that didn't reset after position closed
  (c) sleeve.own_avg_entry not cleared on last fill

Read-only. Cross-checks:
  1. __portfolio__ snapshot (stored under CONFIG scope on adam-live)
     — is PT in derivatives? qty? unrealized?
  2. State + sleeve states for the symbol (own_avg_entry, cycles)
  3. Live Coinbase truth: broker.position_qty() + list_futures_positions
  4. Recent sleeve_on_fill / cycle_completed / reconciliation events

Usage:
    python3 diag_unrealized_trace.py PT-28SEP26-CDE
    python3 diag_unrealized_trace.py PT-28SEP26-CDE --hours 6
"""
from __future__ import annotations
import argparse
import os
import sys
import time


TENANT = "adam-live"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--data-dir", default=os.getenv("SWING_DATA_DIR", "data"))
    args = ap.parse_args()

    sym = args.symbol
    print("=" * 90)
    print(f"UNREALIZED TRACE — {sym}")
    print("=" * 90)

    from state_store import make_store
    store = make_store(args.data_dir)

    # ---- 1. __portfolio__ snapshot (stored under CONFIG scope) ------------
    print(f"\n[1] __portfolio__ SNAPSHOT (config scope on {TENANT})")
    pf = store.get_config(TENANT, "__portfolio__") or {}
    if not pf:
        print("  (no __portfolio__ snapshot found)")
    else:
        refresh_ts = pf.get("_refresh_ts")
        if refresh_ts:
            age = time.time() - float(refresh_ts)
            print(f"  _refresh_ts = {refresh_ts} ({age:.0f}s ago)")
        print(f"  _refresh_ok = {pf.get('_refresh_ok')}")
        print(f"  _last_error = {pf.get('_last_error')}")
        derivatives = pf.get("derivatives") or []
        matches = [d for d in derivatives if d.get("product_id") == sym]
        print(f"\n  derivatives count: {len(derivatives)}")
        if not matches:
            print(f"  ✓ {sym} is NOT in derivatives (Coinbase snapshot says no position)")
        for d in matches:
            print(f"  ⚠ {sym} in derivatives:")
            for k in ("side", "qty", "avg_entry", "mark", "unrealized",
                      "liquidation_price"):
                print(f"      {k}={d.get(k)}")

    # ---- 2. State + sleeve states -----------------------------------------
    print(f"\n[2] STATE for {TENANT}/{sym}")
    st = store.get_state(TENANT, sym) or {}
    if not st:
        print(f"  (no state — symbol may have been purged)")
    else:
        for k in ("state", "swing_qty", "cycles", "realized_pnl",
                  "last_sell_fill_price", "last_step_ok_ts"):
            v = st.get(k)
            if k.endswith("_ts") and isinstance(v, (int, float)) and v > 0:
                age = time.time() - float(v)
                v = f"{v} ({age/60:.1f} min ago)"
            print(f"  {k:<24} = {v}")
        sleeves_st = st.get("sleeves") or {}
        print(f"\n  Sleeve states ({len(sleeves_st)}):")
        for sid, ss in sleeves_st.items():
            print(f"    · {sid}: state={ss.get('state')} "
                  f"cycles={ss.get('cycles')} "
                  f"own_avg_entry={ss.get('own_avg_entry')} "
                  f"realized_pnl={ss.get('realized_pnl')} "
                  f"live_order_id={ss.get('live_order_id')}")

    # ---- 3. Live Coinbase truth -------------------------------------------
    print(f"\n[3] LIVE COINBASE — direct position query")
    try:
        from broker import CoinbaseBroker, BrokerConfig
        b = CoinbaseBroker(BrokerConfig(product_id=sym))
        try:
            pq = b.position_qty()
            print(f"  position_qty() = {pq}")
        except Exception as e:
            print(f"  position_qty() failed: {type(e).__name__}: {e}")
        # Full positions dump for context — filter to this symbol
        try:
            from broker import _dump
            resp = _dump(b.client.list_futures_positions()) or {}
            positions = resp.get("positions") or []
            print(f"  list_futures_positions returned {len(positions)} entries")
            for p in positions:
                if p.get("product_id") == sym:
                    print(f"\n  ⚠ Coinbase still shows {sym}:")
                    for k in ("number_of_contracts", "side", "avg_entry_price",
                              "current_price", "unrealized_pnl",
                              "liquidation_price"):
                        print(f"      {k} = {p.get(k)}")
        except Exception as e:
            print(f"  list_futures_positions failed: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"  broker construction failed: {type(e).__name__}: {e}")

    # ---- 4. Recent fill / cycle events ------------------------------------
    print(f"\n[4] RECENT FILL EVENTS ({args.hours}h)")
    cutoff_ts = time.time() - args.hours * 3600.0
    matches = []
    try:
        from safety import make_trade_log
        log = make_trade_log(args.data_dir)
        for e in log.events():
            try:
                if float(e.get("ts") or 0) < cutoff_ts:
                    continue
                if str(e.get("symbol") or "") != sym:
                    continue
                etype = str(e.get("event_type") or "")
                if etype in ("sleeve_on_fill", "sleeve_cycle_completed",
                              "cycle_completed", "sleeve_stop_loss_fired",
                              "sleeve_market_sell_fired", "trail_exit_fired",
                              "reconciliation_position_mismatch",
                              "reconciliation_state_config_drift"):
                    matches.append(e)
            except (ValueError, TypeError):
                pass
    except Exception as e:
        print(f"  trade log read failed: {type(e).__name__}: {e}")

    matches.sort(key=lambda e: float(e.get("ts") or 0))
    if not matches:
        print(f"  (no fill/cycle/mismatch events for {sym})")
    for e in matches:
        ts = float(e.get("ts") or 0)
        age = time.time() - ts if ts else 0
        etype = e.get("event_type")
        keys = ["side", "qty", "price", "fill_price", "realized_pnl",
                "own_avg_entry", "cycles", "own_avg_entry_after",
                "sleeve_id", "reason", "detail"]
        fields = {k: e[k] for k in keys if k in e}
        print(f"  {age/60:6.1f}min ago  {etype}")
        for k, v in fields.items():
            if isinstance(v, str) and len(v) > 120:
                v = v[:117] + "..."
            print(f"      {k}={v}")

    print()
    print("=" * 90)
    print("VERDICT HINTS:")
    print("  - If [1] shows PT in derivatives with qty>0 → Coinbase reports open position")
    print("  - If [3] agrees with [1] → position IS still open on Coinbase (not stale)")
    print("  - If [1] shows no PT but [2] sleeves have own_avg_entry set → local accumulator stale")
    print("  - If [4] shows a recent fill but own_avg_entry not cleared → _sleeve_on_fill bug")
    print("=" * 90)


if __name__ == "__main__":
    main()
