"""ETP cancel-loop forensic — every event touching ETP in last 2h.

Adam 2026-07-22: cancel-loop on ETH PERP CDE 0.1 at $1873.5 continues
5+ hours after Phase A kill switch was set. Something else is placing.
This diag pulls EVERY event that mentions ETP in the last 2h + all
Coinbase orders on that product, so we can trace who's placing/cancelling.
"""
import os, time, json
from collections import Counter


def main():
    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = log.tail(20000)

    now = time.time()
    cutoff = now - 7200  # 2h

    etp = [e for e in events
           if (e.get("symbol") or "") == "ETP-20DEC30-CDE"
           and float(e.get("ts") or 0) >= cutoff]

    print(f"ETP events in last 2h: {len(etp)}")
    print()

    # Event type breakdown
    by_type = Counter(e.get("event_type") for e in etp)
    print("BY EVENT TYPE:")
    for et, n in by_type.most_common():
        print(f"  {n:>5}  {et}")

    # Placement events (any that call place_limit / place_stop_limit)
    place_types = [
        "profit_lock_limit_placed",
        "resting_stop_placed",
        "reentry_reeval_replaced",
        "sleeve_reanchored",
        "sleeve_auto_refresh",
        "sleeve_ghost_armed",
        "sleeve_orphan_position_adopted",
        "trail_breach_limit_sell",
        "sleeve_arm",
    ]
    places = [e for e in etp if e.get("event_type") in place_types]
    print(f"\nPLACEMENT EVENTS: {len(places)}")
    for e in places[-30:]:
        age = int(now - float(e.get("ts") or 0))
        print(f"  {age:>5}s  {e.get('event_type'):<40}  "
              f"buy_px={e.get('buy_px') or e.get('new_buy_px')}  "
              f"oid={str(e.get('oid') or e.get('cancelled_oid') or '')[:12]}")

    # Cancel events
    cancel_types = [
        "profit_lock_limit_cancelled_for_reprice",
        "profit_lock_limit_external_cancel_cleared",
        "resting_stop_external_cancel_cleared",
        "resting_stop_cancelled_excess_over_position",
        "resting_stop_wrong_price_cancelled",
        "phase_a_migration_stale_limit_cancelled",
        "broker_excess_sell_cancelled",
        "trail_breach_cancel_failed",
        "bracket_stop_cancelled_on_profit_fill",
    ]
    cancels = [e for e in etp if e.get("event_type") in cancel_types
               or "cancel" in (e.get("event_type") or "").lower()]
    print(f"\nCANCEL EVENTS: {len(cancels)}")
    for e in cancels[-15:]:
        age = int(now - float(e.get("ts") or 0))
        print(f"  {age:>5}s  {e.get('event_type'):<45}  "
              f"oid={str(e.get('cancelled_oid') or e.get('oid') or '')[:12]}")
        reason = str(e.get("reason") or "")[:80]
        if reason:
            print(f"          reason: {reason}")

    # Reeval decisions
    reevals = [e for e in etp if e.get("event_type") == "reentry_reeval_decision"]
    print(f"\nREENTRY_REEVAL_DECISION: {len(reevals)}")
    if reevals:
        actions = Counter(e.get("action") for e in reevals)
        print(f"  action counts: {dict(actions)}")
        for e in reevals[-5:]:
            age = int(now - float(e.get("ts") or 0))
            print(f"  {age:>5}s  action={e.get('action')}  "
                  f"old={e.get('old_buy_px')}  new={e.get('new_buy_px')}  "
                  f"mode={e.get('mode')}")
            why = str(e.get("why") or "")[:100]
            if why:
                print(f"          why: {why}")

    # Expert reentry decisions
    experts = [e for e in etp if e.get("event_type") == "expert_reentry_decision"]
    print(f"\nEXPERT_REENTRY_DECISION: {len(experts)}")
    if experts:
        actions = Counter(e.get("action") for e in experts)
        print(f"  action counts: {dict(actions)}")

    # Coinbase current open orders on ETP
    print("\n" + "=" * 90)
    print("COINBASE OPEN ORDERS (current):")
    try:
        from broker import BrokerConfig, CoinbaseBroker
        b = CoinbaseBroker(BrokerConfig(product_id="ETP-20DEC30-CDE"))
        resp = b.client.list_orders(product_id="ETP-20DEC30-CDE",
                                     order_status="OPEN", limit=50)
        _r = resp.to_dict() if hasattr(resp, "to_dict") else resp
        orders = (_r or {}).get("orders") or []
        for o in orders:
            side = str(o.get("side") or "")
            oid = str(o.get("order_id") or "")[:20]
            cfg = o.get("order_configuration") or {}
            px = ""
            for k, v in (cfg.items() if isinstance(cfg, dict) else []):
                if isinstance(v, dict):
                    px = v.get("limit_price") or v.get("stop_price") or ""
                    break
            ct = str(o.get("created_time") or "")[:19]
            print(f"  {side:<5}  {oid}...  px={px}  created={ct}")
        if not orders:
            print("  (no open orders)")
    except Exception as e:
        print(f"  ✗ broker query failed: {e}")


if __name__ == "__main__":
    main()
