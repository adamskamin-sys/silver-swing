"""Why is HIGH-USD's profit_lock_limit_place_failed firing every tick?

Same pattern META hit earlier (INSUFFICIENT_FUND on spot). My self-heal
in _maintain_and_credit_profit_lock_limit should cancel the blocker
LIMIT SELL when INSUFFICIENT_FUND fires — but audit shows it's still
looping. This diag finds out why.

Prints:
  1. Last 5 profit_lock_limit_place_failed events for HIGH-USD, full error
  2. Whether profit_lock_limit_blocker_cancelled_spot fired since (self-heal)
  3. Current sleeve state (all oids, own_avg, sell_px)
  4. All open SELLs on HIGH-USD via broker
  5. Actual position vs sleeve-sum (ghost-detect)
  6. Simulates place_limit at sell_px with include_pending=False for raw error
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
    print("HIGH-USD profit-lock failure trace")
    print("=" * 90)

    fail_events = [e for e in events
                   if e.get("event_type") == "profit_lock_limit_place_failed"
                   and e.get("symbol") == "HIGH-USD"]
    heal_events = [e for e in events
                   if e.get("event_type") == "profit_lock_limit_blocker_cancelled_spot"
                   and e.get("symbol") == "HIGH-USD"]
    heal_fail = [e for e in events
                 if e.get("event_type") == "profit_lock_limit_blocker_cancel_failed_spot"
                 and e.get("symbol") == "HIGH-USD"]

    print(f"\nLast hour on HIGH-USD:")
    print(f"  profit_lock_limit_place_failed:              {len(fail_events)}")
    print(f"  profit_lock_limit_blocker_cancelled_spot:    {len(heal_events)}")
    print(f"  profit_lock_limit_blocker_cancel_failed_spot:{len(heal_fail)}")

    if fail_events:
        print("\n[LAST 3 FAILURES with full error]")
        for e in fail_events[-3:]:
            age = int(now - float(e.get("ts") or 0))
            print(f"  {age}s ago  sleeve={e.get('sleeve_id')}  sell_px={e.get('sell_px')}")
            print(f"           error: {e.get('error')}")

    if heal_events:
        print(f"\n[LAST SELF-HEAL FIRE]")
        e = heal_events[-1]
        age = int(now - float(e.get("ts") or 0))
        print(f"  {age}s ago  cancelled_oid={e.get('cancelled_oid')}")
        print(f"           blocker_px={e.get('blocker_px')}  size={e.get('blocker_size')}")
    else:
        print("\n⚠ self-heal has NEVER fired for HIGH-USD in the log window")

    if heal_fail:
        print(f"\n[SELF-HEAL FAILURES]")
        for e in heal_fail[-3:]:
            age = int(now - float(e.get("ts") or 0))
            print(f"  {age}s  oid={e.get('oid')}  error={e.get('error')}")

    print("\n" + "=" * 90)
    print("Sleeve state")
    print("=" * 90)

    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if not url:
        print("REDIS_URL not set")
        return
    r = redis.Redis.from_url(url, decode_responses=True)
    store = json.loads(r.get("silver-swing:store") or "{}")
    block = (store.get("adam-live") or {}).get("HIGH-USD") or {}
    state = block.get("state") or {}
    cfg = block.get("config") or {}

    print(f"\nproduct:  contract_size={cfg.get('contract_size')} tick={cfg.get('tick_size')}")
    print(f"primary:  state={state.get('state')} swing_qty={state.get('swing_qty')}")

    sleeve_sum_qty = 0
    for sid, ss in (state.get("sleeves") or {}).items():
        sc = next((s for s in (cfg.get("sleeves") or [])
                   if s.get("id") == sid), {})
        oe = ss.get("own_avg_entry")
        st = ss.get("state")
        print(f"\nsleeve {sid}  state={st}  own_avg={oe}")
        print(f"  cfg qty={sc.get('qty')}  buy_px={sc.get('buy_px')}  "
              f"sell_px={sc.get('sell_px')}  stop_loss_px={sc.get('stop_loss_px')}")
        for k in ("live_order_id", "resting_stop_oid",
                  "resting_profit_limit_oid",
                  "resting_stop_stage"):
            print(f"  {k}: {ss.get(k)}")
        if st == "ARMED_SELL" and oe:
            sleeve_sum_qty += int(sc.get("qty") or 0)

    print("\n" + "=" * 90)
    print("Broker state")
    print("=" * 90)

    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id="HIGH-USD"))

    try:
        # Spot position via spot_position_qty
        if hasattr(b, "_spot_position_qty"):
            pq = b._spot_position_qty()
        else:
            pq = b.position_qty()
        print(f"\nbroker position_qty (base units): {pq}")
        print(f"sleeve-sum bot-expected qty:      {sleeve_sum_qty}")
        if pq is not None and sleeve_sum_qty > 0:
            _diff = float(pq) - sleeve_sum_qty
            if _diff != 0:
                print(f"⚠ MISMATCH: broker has {_diff:+g} more/fewer units than sleeves expect")
            else:
                print(f"✓ match")
    except Exception as _e:
        print(f"  position query error: {_e}")

    try:
        resp = b.client.list_orders(product_id="HIGH-USD",
                                     order_status="OPEN", limit=50)
        _r = resp.to_dict() if hasattr(resp, "to_dict") else resp
        orders = (_r or {}).get("orders") or []
        print(f"\nopen orders on HIGH-USD: {len(orders)}")
        _open_sell_qty = 0
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
            side = str(o.get("side") or "")
            if side == "SELL":
                try:
                    _open_sell_qty += float(sz or 0)
                except Exception:
                    pass
            print(f"  {side:<5} {kind:<30} size={sz:<10} px={px:<12} "
                  f"oid={str(o.get('order_id'))[:20]}")
        print(f"\ntotal OPEN SELL qty on Coinbase: {_open_sell_qty}")
    except Exception as _e:
        print(f"  list_orders error: {_e}")

    # Try a dry place at each sleeve's sell_px
    print(f"\n[SIMULATE profit-lock place]")
    for sc in (cfg.get("sleeves") or []):
        sid = sc.get("id")
        s_st = (state.get("sleeves") or {}).get(sid) or {}
        if s_st.get("state") != "ARMED_SELL" or not s_st.get("own_avg_entry"):
            continue
        qty = int(sc.get("qty") or 0)
        sell_px = float(sc.get("sell_px") or 0)
        if qty <= 0 or sell_px <= 0:
            continue
        print(f"\n  sleeve {sid}: try place_limit SELL {qty} @ {sell_px}")
        try:
            oid = b.place_limit("SELL", qty, sell_px, include_pending=False)
            print(f"    ✓ succeeded: oid={oid}")
            b.cancel(oid)
            print(f"    ✓ cancelled test order")
        except Exception as _pe:
            print(f"    ✗ failed: {_pe}")


if __name__ == "__main__":
    main()
