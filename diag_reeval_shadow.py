"""Why is reentry_reeval shadow mode silent?

Prints:
  1. Count of reentry_reeval_* events in the last 500 trade-log entries
     (if 0 across the board, the code path is never being reached).
  2. Every sleeve in ARMED_BUY state with a live_order_id on adam-live
     (the reeval only fires for these — if 0, that's why shadow is silent).
  3. Sleeve price-history depth per sleeve (needs >=30 for reeval to run).

Run on the silver-swing-bot-live Render worker:
    python3 diag_reeval_shadow.py
"""
from __future__ import annotations
import os
import sys

TENANT = "adam-live"


def main() -> None:
    from state_store import make_store
    from safety import make_trade_log

    data_dir = os.getenv("SWING_DATA_DIR", "data")
    store = make_store(data_dir)
    log = make_trade_log(data_dir)

    # ---- 1) reentry_reeval events in recent trade log
    evs = list(log.events())[-500:]
    kinds: dict[str, int] = {}
    for e in evs:
        et = str(e.get("event_type") or "")
        if "reentry_reeval" in et:
            kinds[et] = kinds.get(et, 0) + 1
    print("=" * 60)
    print("reentry_reeval events in last 500 log entries:")
    if kinds:
        for k, v in sorted(kinds.items()):
            print(f"  {k}: {v}")
    else:
        print("  NONE — code path never invoked")
    print()

    # ---- 2) ARMED_BUY sleeves with resting order (reeval trigger condition)
    print("=" * 60)
    print(f"Sleeves in ARMED_BUY with live_order_id (tenant={TENANT}):")
    configs = store.list_configs(TENANT) or {}
    total_sleeves = 0
    armed_with_order = 0
    per_symbol_summary = []
    for sym in configs:
        st = store.get_state(TENANT, sym) or {}
        sleeves = st.get("sleeves") or {}
        sym_total = len(sleeves)
        sym_armed = 0
        for sid, sdata in sleeves.items():
            total_sleeves += 1
            state = str(sdata.get("state", "")).upper()
            oid = sdata.get("live_order_id")
            if state == "ARMED_BUY" and oid:
                armed_with_order += 1
                sym_armed += 1
                print(f"  {sym} sleeve={sid} order={oid}")
        per_symbol_summary.append((sym, sym_armed, sym_total))
    if armed_with_order == 0:
        print("  NONE — nothing for reeval to evaluate")
    print()
    print(f"Totals: {armed_with_order} ARMED_BUY-with-order / "
          f"{total_sleeves} sleeves across {len(configs)} symbols")
    print()

    # ---- 3) Per-symbol sleeve state breakdown
    print("=" * 60)
    print("Per-symbol sleeve state breakdown:")
    for sym in configs:
        st = store.get_state(TENANT, sym) or {}
        sleeves = st.get("sleeves") or {}
        if not sleeves:
            print(f"  {sym}: (no sleeves)")
            continue
        by_state: dict[str, int] = {}
        for sdata in sleeves.values():
            s = str(sdata.get("state", "?")).upper()
            by_state[s] = by_state.get(s, 0) + 1
        state_str = ", ".join(f"{k}={v}" for k, v in sorted(by_state.items()))
        print(f"  {sym}: {state_str}")
    print()

    # ---- 4) Interpretation
    print("=" * 60)
    print("INTERPRETATION:")
    if kinds:
        print("  reentry_reeval IS firing. Check event counts above.")
    elif armed_with_order == 0:
        print("  reentry_reeval is silent because NO sleeves are ARMED_BUY")
        print("  with a resting live_order_id. Nothing to re-evaluate.")
        print("  When you next set a pending buy that stays open, shadow")
        print("  events will start accumulating.")
    else:
        print(f"  {armed_with_order} sleeves ARE eligible but zero events")
        print("  fired. Possible causes: <30 price history entries per")
        print("  sleeve, or silent early-return in _maybe_reeval_pending_arm.")
        print("  Grep for reentry_reeval_error events to check.")


if __name__ == "__main__":
    main()
