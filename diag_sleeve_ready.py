"""Why doesn't sleeve X have a Coinbase order right now?

Adam 2026-07-15: multiple sleeves show ARMED_BUY on the dashboard,
but only SOME have a corresponding OPEN BUY order on Coinbase.
Reasons vary: exit_mode, state, cooldown, expert hold, price history.
This diag categorizes each sleeve so you don't have to click through
7 modals to find out.

Read-only. Usage:
    python3 diag_sleeve_ready.py                   # all sleeves
    python3 diag_sleeve_ready.py PRODUCT_ID        # one product
"""
from __future__ import annotations
import os
import sys
import time


def _fmt_age(ts) -> str:
    try:
        age = int(time.time() - float(ts))
        if age < 60:
            return f"{age}s"
        if age < 3600:
            return f"{age // 60}m"
        return f"{age // 3600}h"
    except Exception:
        return "?"


def main() -> None:
    product_filter = sys.argv[1] if len(sys.argv) > 1 else None
    tenant = "adam-live"

    print("=" * 100)
    print(f"SLEEVE READINESS — why does each sleeve {'not ' if False else ''}have "
          f"an OPEN order on Coinbase?"
          + (f"  product={product_filter}" if product_filter else ""))
    print("=" * 100)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))

    # Fetch live open orders once, group by product_id
    live_orders_by_pid: dict[str, list] = {}
    try:
        from broker import CoinbaseBroker, BrokerConfig
        # Any product works for list_open_orders scope; use SLR as anchor
        b = CoinbaseBroker(BrokerConfig(product_id="SLR-27AUG26-CDE"))
        all_open = b.list_open_orders()
        for o in all_open:
            live_orders_by_pid.setdefault(o.get("symbol"), []).append(o)
    except Exception as e:
        print(f"\n⚠ broker.list_open_orders failed: {e}")
        print(f"  Live Coinbase order data unavailable — showing state-only view.")

    print(f"\n{'PRODUCT':22} {'SLEEVE':12} {'MODE':16} {'STATE':11} "
          f"{'BUY $':>10} {'CB ORDER?':10} {'WHY':50}")
    print("-" * 100)

    for tid in store.list_tenants():
        if tid != tenant:
            continue
        for sym in store.list_symbols(tid):
            if product_filter and sym != product_filter:
                continue
            if sym.startswith("__"):
                continue
            cfg = store.get_config(tid, sym) or {}
            state = store.get_state(tid, sym) or {}
            sleeves_cfg = cfg.get("sleeves") or []
            sleeves_state = state.get("sleeves") or {}
            for sc in sleeves_cfg:
                sid = sc.get("id", "?")
                ss = sleeves_state.get(sid, {})
                mode = sc.get("exit_mode") or "fixed_limit"
                state_val = ss.get("state") or "ARMED_SELL"
                buy_px = sc.get("buy_px", 0)
                # Any live BUY order on this product on Coinbase?
                pid_orders = live_orders_by_pid.get(sym, [])
                live_buys = [o for o in pid_orders if o.get("side") == "BUY"]
                cb_flag = "yes" if live_buys else "no"
                # WHY reasoning
                reasons = []
                if state_val != "ARMED_BUY":
                    reasons.append(f"state={state_val} (not waiting to buy)")
                elif mode in ("hybrid", "trailing_stop"):
                    reasons.append(f"{mode} = trigger mode (no resting order until fire)")
                elif not live_buys:
                    # Should have a resting buy but doesn't
                    live_order_id = ss.get("live_order_id")
                    if live_order_id:
                        reasons.append(f"has live_order_id={live_order_id[:8]}… "
                                       f"but not visible in list_open_orders "
                                       f"(may have just filled/cancelled)")
                    else:
                        # Check timing signals
                        armed_since = ss.get("armed_buy_since_ts")
                        if armed_since:
                            age = int(time.time() - float(armed_since))
                            if age < 30:
                                reasons.append(f"just armed {age}s ago — waiting for tick")
                            else:
                                reasons.append(f"armed {_fmt_age(armed_since)} ago "
                                               f"BUT no live order — check "
                                               f"reeval/skip events")
                        else:
                            reasons.append("no armed_buy_since_ts — never fully armed")
                        # Additional: reentry_pending?
                        if ss.get("reentry_pending"):
                            reasons.append("reentry_pending (post-stop wait)")
                else:
                    reasons.append(f"live BUY on book at ${live_buys[0].get('price')}")
                reason_str = "; ".join(reasons)[:50]
                print(f"{sym:22} {sid:12} {mode:16} {state_val:11} "
                      f"${buy_px:>9} {cb_flag:10} {reason_str:50}")

    print("=" * 100)
    print("\nLegend:")
    print("  MODE fixed_limit / percentage_swing → rests a real BUY on Coinbase")
    print("  MODE hybrid / trailing_stop         → no resting order (trigger mode)")
    print("  If ARMED_BUY + resting mode + no order → check WHY column")


if __name__ == "__main__":
    main()
