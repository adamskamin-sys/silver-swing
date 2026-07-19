"""Trace what happened to a specific sleeve id — was it retired, evicted,
scanner-rotated, or is it still around?

Adam 2026-07-19: XLP scan-mrr27ttp was ARMED_BUY with cycles=1 and
$0.75 realized, then vanished from config between two diag runs.
This diag pulls every trace of a sleeve id: retirement ledger, trade
log events, current cfg/state on every tenant.

Usage: python3 diag_sleeve_history.py <PRODUCT_ID> <SLEEVE_ID>
       python3 diag_sleeve_history.py XLP-20DEC30-CDE scan-mrr27ttp
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timezone


def _fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return str(ts)


def main() -> None:
    if len(sys.argv) < 3:
        print("USAGE: python3 diag_sleeve_history.py <PRODUCT_ID> <SLEEVE_ID>")
        return
    product_id = sys.argv[1]
    sleeve_id = sys.argv[2]
    print("=" * 78)
    print(f"HISTORY — {product_id} / {sleeve_id}")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    raw = store._load()

    # 1. Current state / cfg on every tenant
    print(f"\n--- Current cfg / state ---")
    hits = 0
    for tenant, tdata in raw.items():
        if not isinstance(tdata, dict):
            continue
        entry = tdata.get(product_id)
        if not isinstance(entry, dict):
            continue
        cfg = entry.get("config") or {}
        state = entry.get("state") or {}
        sleeves_cfg = [s.get("id") for s in (cfg.get("sleeves") or [])]
        sleeves_state = state.get("sleeves") or {}
        in_cfg = sleeve_id in sleeves_cfg
        in_state = sleeve_id in sleeves_state
        if in_cfg or in_state:
            hits += 1
            print(f"  tenant={tenant}")
            print(f"    in cfg: {in_cfg}")
            print(f"    in state: {in_state}")
            if in_state:
                ss = sleeves_state[sleeve_id]
                print(f"      state={ss.get('state')}  cycles={ss.get('cycles')}  "
                      f"realized=${ss.get('realized_pnl', 0)}")
                print(f"      halt_reason={ss.get('halt_reason')}")
    if hits == 0:
        print(f"  ✗ Not present in cfg or state on any tenant.")

    # 2. Retirement ledger entries
    print(f"\n--- Retirement ledger entries ---")
    try:
        import retirement_ledger as _rl
        found_ret = 0
        for tenant in raw.keys():
            data = _rl._load(store, tenant)
            for e in data.get("entries") or []:
                if e.get("product_id") == product_id or e.get("sleeve_id") == sleeve_id:
                    found_ret += 1
                    print(f"  tenant={tenant}")
                    print(f"    product={e.get('product_id')}  sleeve={e.get('sleeve_id')}")
                    print(f"    retired_at={_fmt_ts(e.get('retired_at'))}  "
                          f"cooldown_hours={e.get('cooldown_hours')}")
                    print(f"    reason={e.get('reason')}")
        if found_ret == 0:
            print(f"  ✗ No retirement ledger entries.")
    except Exception as e:
        print(f"  (retirement_ledger read failed: {e})")

    # 3. Trade log grep — sleeve events tagged with sleeve_id or product_id
    print(f"\n--- Trade log events (last 200 mentioning this sleeve/product) ---")
    trade_log_path = os.path.join(os.getenv("SWING_DATA_DIR", "data"),
                                    "trades.jsonl")
    if not os.path.exists(trade_log_path):
        print(f"  (no trade log at {trade_log_path})")
        return

    events: list[dict] = []
    try:
        with open(trade_log_path) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                if sleeve_id not in line and product_id not in line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                events.append(ev)
    except Exception as e:
        print(f"  (trade log read failed: {e})")
        return

    if not events:
        print(f"  ✗ No events reference {sleeve_id} or {product_id}.")
        return

    # Show most recent 30
    events = events[-30:]
    for ev in events:
        ts = ev.get("ts") or ev.get("timestamp")
        typ = ev.get("event_type") or ev.get("event") or "?"
        sid_field = ev.get("sleeve_id") or ""
        marker = "  ← THIS SLEEVE" if sid_field == sleeve_id else ""
        print(f"  {_fmt_ts(ts)}  {typ}  sid={sid_field}{marker}")


if __name__ == "__main__":
    main()
