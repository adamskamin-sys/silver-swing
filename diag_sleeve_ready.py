"""Why doesn't sleeve X have a Coinbase order right now?

Adam 2026-07-15: dashboard shows N ARMED_BUY sleeves but only SOME
have a resting BUY on Coinbase. The mode alone doesn't explain it
(hybrid sleeves DO rest limit buys on the BUY leg — only their SELL
leg is trigger-based).

This diag inspects each ARMED_BUY sleeve without a CB order and
scans the trade log for the LAST arm_skipped / cascade_hold /
knife_gate / trend_hold / reeval / drift event on that sleeve.
Reports the specific reason the arm didn't fire.

Read-only. Usage:
    python3 diag_sleeve_ready.py                   # all sleeves
    python3 diag_sleeve_ready.py PRODUCT_ID        # one product
"""
from __future__ import annotations
import os
import sys
import time


BLOCK_EVENT_KEYWORDS = [
    "sleeve_arm_skipped",       # any arm_skipped_* variant
    "cascade_reentry_hold",
    "entry_velocity_hold",
    "cascade_reentry_error",
    "sleeve_time_reanchor",
    "sleeve_vol_reanchor",
    "reentry_reeval_replace_skipped_below_drift",
    "reentry_reeval_decision",
    "reentry_reeval_replaced",
    "sleeve_reanchored",
    "sleeve_stop_loss_triggered",
    # Adam 2026-07-15: extra block events discovered while hunting the
    # CU/HYF/ZEC "no CB order + no events" silent bug.
    "buy_trail_waiting_for_dip",
    "buy_trail_waiting_for_bounce",
    "buy_trail_armed",
    "buy_trail_bounce_confirmed",
    "sleeve_trend_gate_blocked",
    "sleeve_ms_pause",
    "sleeve_ms_size_taper",
    "velocity_gate_error",
    "sleeve_order_cleared",
    "sleeve_credited_partial_before_clear",
]


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


def _find_last_blocker(events, sleeve_id, product_id, since_secs=86400):
    """Walk events (already filtered to this product+sleeve) newest-first
    and return the first event whose event_type is a known blocker.

    Adam 2026-07-15: default 24h window (was 10min). Sleeves can be armed
    for hours before hitting a state where they need to be diagnosed —
    HYF armed 6h, ZEC 7h. A 10min window missed the block reasons entirely."""
    cutoff = time.time() - since_secs
    for e in reversed(events):  # newest last in log; reverse for newest first
        ts = float(e.get("ts") or 0)
        if ts < cutoff:
            break
        if e.get("sleeve_id") != sleeve_id:
            continue
        et = str(e.get("event_type") or "")
        for kw in BLOCK_EVENT_KEYWORDS:
            if kw in et:
                return e
    return None


def main() -> None:
    product_filter = sys.argv[1] if len(sys.argv) > 1 else None
    tenant = "adam-live"

    print("=" * 130)
    print(f"SLEEVE READINESS — real reasons per ARMED_BUY sleeve"
          + (f"  product={product_filter}" if product_filter else ""))
    print("=" * 130)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))

    # Live open orders from Coinbase (single API call, all products)
    live_orders_by_pid: dict[str, list] = {}
    try:
        from broker import CoinbaseBroker, BrokerConfig
        b = CoinbaseBroker(BrokerConfig(product_id="BIT-31JUL26-CDE"))
        for o in b.list_open_orders():
            live_orders_by_pid.setdefault(o.get("symbol"), []).append(o)
    except Exception as e:
        print(f"\n⚠ broker.list_open_orders failed: {e}")

    # Pre-load trade log events, filter by symbol as we walk.
    # 24h window — sleeves can be armed for hours before hitting the
    # state the operator wants to diagnose (HYF armed 6h, ZEC 7h).
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        cutoff = time.time() - 86400
        recent_events_by_symbol: dict[str, list] = {}
        for e in log.events():
            if not isinstance(e, dict):
                continue
            ts = float(e.get("ts") or 0)
            if ts < cutoff:
                continue
            sym = str(e.get("symbol") or "")
            if not sym:
                continue
            recent_events_by_symbol.setdefault(sym, []).append(e)
    except Exception as e:
        print(f"\n⚠ trade log load failed: {e}")
        recent_events_by_symbol = {}

    print(f"\n{'PRODUCT':22} {'SLEEVE':14} {'STATE':11} {'BUY $':>10} {'MARK':>10} "
          f"{'CB BUY?':8} {'REAL REASON':60}")
    print("-" * 130)

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
            # Get current mark from portfolio snapshot
            mark = 0
            try:
                pf = store.get_state(tid, "__portfolio__") or {}
                snap = pf.get(sym) or {}
                mark = float(snap.get("last_mark") or 0)
            except Exception:
                pass
            for sc in sleeves_cfg:
                sid = sc.get("id", "?")
                ss = sleeves_state.get(sid, {})
                state_val = ss.get("state") or "ARMED_SELL"
                buy_px = sc.get("buy_px", 0)
                pid_orders = live_orders_by_pid.get(sym, [])
                live_buys = [o for o in pid_orders if o.get("side") == "BUY"]
                cb_flag = "yes" if live_buys else "no"
                # Reason resolution
                if state_val != "ARMED_BUY":
                    reason = f"state={state_val} (not waiting to buy)"
                elif live_buys:
                    reason = f"HAS resting limit @ ${live_buys[0].get('price')}"
                elif ss.get("live_order_id"):
                    reason = (f"has live_order_id={str(ss.get('live_order_id'))[:8]}… "
                              f"but not visible in list_open_orders (may just filled/cancelled)")
                elif ss.get("reentry_pending"):
                    reason = "reentry_pending (post-stop wait for vol contraction)"
                else:
                    # Scan trade log for the LAST blocker event
                    blocker = _find_last_blocker(
                        recent_events_by_symbol.get(sym, []), sid, sym)
                    if blocker:
                        et = blocker.get("event_type", "?")
                        why = (blocker.get("reason")
                               or blocker.get("phase")
                               or blocker.get("action")
                               or blocker.get("kind")
                               or "")
                        age = _fmt_age(blocker.get("ts"))
                        reason = f"[{age} ago] {et}: {why}"[:60]
                    else:
                        # Adam 2026-07-15: differentiate "no BLOCK events"
                        # from "no ANY events." The latter means the Track
                        # is dead (evicted / never spawned) — check any-
                        # event count for this product in the 24h window.
                        total_events = len(recent_events_by_symbol.get(sym, []))
                        armed_since = ss.get("armed_buy_since_ts")
                        if armed_since:
                            age_s = int(time.time() - float(armed_since))
                            if age_s < 30:
                                reason = f"just armed {age_s}s ago — waiting for next tick"
                            elif total_events == 0:
                                reason = (f"⚠ TRACK DEAD — 0 events for this product "
                                          f"in 24h. Track was evicted or never spawned. "
                                          f"Check live_runner logs.")
                            elif age_s < 300:
                                reason = (f"armed {age_s}s ago, no blocker event visible "
                                          f"(saw {total_events} other events on this product)")
                            else:
                                # Show top event types so we can see WHAT
                                # the tick loop is doing even if none matched
                                # our block-event list. That's the fingerprint
                                # of the silent path.
                                from collections import Counter
                                ev_types = Counter()
                                for _e in recent_events_by_symbol.get(sym, []):
                                    if _e.get("sleeve_id") == sid or not _e.get("sleeve_id"):
                                        ev_types[_e.get("event_type", "?")] += 1
                                top = ev_types.most_common(4)
                                top_str = ", ".join(f"{k}={v}" for k, v in top)
                                reason = (f"armed {_fmt_age(armed_since)} ago, "
                                          f"{total_events} events / TOP TYPES: {top_str}")
                        else:
                            reason = "no armed_buy_since_ts — never fully armed"
                print(f"{sym:22} {sid:14} {state_val:11} ${buy_px:>9} ${mark:>9.4f} "
                      f"{cb_flag:8} {reason:60}")

    print("=" * 130)
    print("\nInterpretation:")
    print("  'HAS resting limit @ $X' — expected + working, buy sitting on the book")
    print("  '[Xm ago] sleeve_arm_skipped_*' — recently gated by named check")
    print("  'reentry_pending' — expected quiet after a stop-out")
    print("  'NO recent block events' — potential silent bug worth digging into")


if __name__ == "__main__":
    main()
