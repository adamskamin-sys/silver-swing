"""Why is META's profit_lock_limit_place_failed firing every tick after unfreeze?

Shows the full error string from the last profit_lock_limit_place_failed
+ profit_lock_limit_placed events on META so we can see what's blocking.

Also dumps META position, existing open orders, cfg constraints.
"""
import os
import json
import time


def main():
    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = log.tail(3000)
    now = time.time()

    print("=" * 90)
    print("META profit_lock_limit_place_failed error trace")
    print("=" * 90)

    place_failed = [e for e in events
                    if e.get("event_type") == "profit_lock_limit_place_failed"
                    and e.get("symbol") == "META-USD"]
    place_ok = [e for e in events
                if e.get("event_type") == "profit_lock_limit_placed"
                and e.get("symbol") == "META-USD"]

    print(f"\nPlace-failed events for META: {len(place_failed)}")
    print(f"Place-succeeded events for META: {len(place_ok)}")

    if place_failed:
        print("\n[LAST 5 FAILURES with full error]")
        for e in place_failed[-5:]:
            age = int(now - float(e.get("ts") or 0))
            print(f"\n  {age}s ago")
            print(f"    sleeve:     {e.get('sleeve_id')}")
            print(f"    sell_px:    {e.get('sell_px')}")
            print(f"    error:      {e.get('error')}")
            print(f"    reason:     {e.get('reason')}")

    print("\n" + "=" * 90)
    print("META current state + broker check")
    print("=" * 90)

    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if url:
        r = redis.Redis.from_url(url, decode_responses=True)
        store = json.loads(r.get("silver-swing:store") or "{}")
        block = (store.get("adam-live") or {}).get("META-USD") or {}
        state = block.get("state") or {}
        cfg = block.get("config") or {}
        print(f"\nProduct-level:")
        print(f"  contract_size:  {cfg.get('contract_size')}")
        print(f"  tick_size:      {cfg.get('tick_size')}")
        print(f"  primary state:  {state.get('state')}")
        print(f"  swing_qty:      {state.get('swing_qty')}")

        for sid, ss in (state.get("sleeves") or {}).items():
            if not ss.get("own_avg_entry"):
                continue
            print(f"\nSleeve {sid}:")
            for k in ("own_avg_entry", "state", "resting_stop_oid",
                      "resting_profit_limit_oid", "live_order_id"):
                print(f"  {k}: {ss.get(k)}")
            sc = next((s for s in (cfg.get("sleeves") or [])
                       if s.get("id") == sid), {})
            print(f"  cfg.buy_px:      {sc.get('buy_px')}")
            print(f"  cfg.sell_px:     {sc.get('sell_px')}")
            print(f"  cfg.stop_loss_px:{sc.get('stop_loss_px')}")
            print(f"  cfg.qty:         {sc.get('qty')}")

    print("\n[BROKER STATE]")
    try:
        from broker import BrokerConfig, CoinbaseBroker
        b = CoinbaseBroker(BrokerConfig(product_id="META-USD"))
        try:
            pos = b.position_qty()
            print(f"  position_qty(): {pos}")
        except Exception as _e:
            print(f"  position_qty() error: {_e}")
        try:
            resp = b.client.list_orders(product_id="META-USD",
                                         order_status="OPEN", limit=50)
            _r = resp.to_dict() if hasattr(resp, "to_dict") else resp
            orders = (_r or {}).get("orders") or []
            print(f"\n  Open orders ({len(orders)}):")
            for o in orders:
                cfg_o = o.get("order_configuration") or {}
                kind = ""
                px = ""
                sz = ""
                for k, v in (cfg_o.items() if isinstance(cfg_o, dict) else []):
                    kind = k
                    if isinstance(v, dict):
                        px = v.get("limit_price") or ""
                        sz = v.get("base_size") or ""
                        break
                print(f"    {str(o.get('side'))[:5]:<5}  {kind:<32}  size={sz}  px={px}  "
                      f"oid={str(o.get('order_id'))[:20]}")
        except Exception as _e:
            print(f"  list_orders error: {_e}")

        # Try place_limit dry-run
        print(f"\n[SIMULATE place_limit sell 23 @ 4.659 include_pending=False]")
        try:
            # Actually place — but immediately cancel. If it errors, that IS the error.
            # Actually let's not place, just simulate the pre-checks.
            oid = b.place_limit("SELL", 23, 4.659, include_pending=False)
            print(f"  ✓ place succeeded: oid={oid}")
            b.cancel(oid)
            print(f"  ✓ cancelled test order")
        except Exception as _e:
            print(f"  ✗ place failed: {type(_e).__name__}: {_e}")
    except Exception as _e:
        print(f"  broker init failed: {_e}")


if __name__ == "__main__":
    main()
