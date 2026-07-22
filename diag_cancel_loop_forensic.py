"""Cancel-loop forensic for the CURRENT looping products.

Adam 2026-07-22: after Phase A + broker freeze, still seeing
trail_breach_limit_sell_failed on CHN, HYF, META every 10-20s.
This shows the exact event sequence per product so we can trace
which code path is thrashing.

Also dumps sleeve state (own_avg, stop_loss_px, sell_px,
resting_stop_oid, resting_profit_limit_oid) and current Coinbase
open orders for each looping product.
"""
import os
import json
import time
from collections import Counter, defaultdict


def _fmt_ts(ts):
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "??:??:??"


def main():
    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = log.tail(3000)
    now = time.time()

    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    r = redis.Redis.from_url(url, decode_responses=True) if url else None
    store = json.loads((r.get("silver-swing:store") if r else "{}") or "{}")
    tbody = store.get("adam-live") or {}

    # Find products with trail_breach_limit_sell_failed in last hour
    cutoff = now - 3600
    trail_breach_by_pid = Counter()
    for e in events:
        if e.get("event_type") == "trail_breach_limit_sell_failed":
            if float(e.get("ts") or 0) >= cutoff:
                trail_breach_by_pid[e.get("symbol")] += 1

    if not trail_breach_by_pid:
        print("no trail_breach_limit_sell_failed events in last hour")
        return

    print("=" * 96)
    print("CANCEL-LOOP FORENSIC")
    print("=" * 96)
    print(f"\nTrail-breach failures in last hour by product:")
    for pid, n in trail_breach_by_pid.most_common():
        print(f"  {n:>4}  {pid}")

    for pid, _n in trail_breach_by_pid.most_common(5):
        print("\n" + "=" * 96)
        print(f"PRODUCT: {pid}")
        print("=" * 96)

        # Sleeve state
        block = tbody.get(pid) or {}
        state = block.get("state") or {}
        cfg = block.get("config") or {}
        sleeves_cfg = {s.get("id"): s for s in (cfg.get("sleeves") or [])}
        sleeves_state = state.get("sleeves") or {}

        print(f"\n[SLEEVE STATE]")
        for sid, ss in sleeves_state.items():
            sc = sleeves_cfg.get(sid) or {}
            if not ss.get("own_avg_entry"):
                continue
            print(f"  {sid}:")
            print(f"    own_avg_entry:            {ss.get('own_avg_entry')}")
            print(f"    state:                    {ss.get('state')}")
            print(f"    sell_px (cfg):            {sc.get('sell_px')}")
            print(f"    stop_loss_px (cfg):       {sc.get('stop_loss_px')}")
            print(f"    stop_loss_enabled:        {sc.get('stop_loss_enabled')}")
            print(f"    resting_stop_oid:         {str(ss.get('resting_stop_oid') or '')[:20]}")
            print(f"    resting_stop_px:          {ss.get('resting_stop_px')}")
            print(f"    resting_stop_stage:       {ss.get('resting_stop_stage')}")
            print(f"    resting_profit_limit_oid: {str(ss.get('resting_profit_limit_oid') or '')[:20]}")
            print(f"    resting_profit_limit_px:  {ss.get('resting_profit_limit_px')}")
            print(f"    live_order_id:            {str(ss.get('live_order_id') or '')[:20]}")
            print(f"    trail_high_water_price:   {ss.get('trail_high_water_price')}")
            print(f"    trail_armed:              {ss.get('trail_armed')}")

        # Current mark from snapshot
        snap = json.loads((r.get(f"silver-swing:snapshot:adam-live:{pid}") if r else "{}") or "{}")
        mark = snap.get("last_mark") or snap.get("mark")
        print(f"\n[MARK] {mark}")

        # Coinbase open orders
        print(f"\n[COINBASE OPEN ORDERS]")
        try:
            from broker import BrokerConfig, CoinbaseBroker
            b = CoinbaseBroker(BrokerConfig(product_id=pid))
            resp = b.client.list_orders(product_id=pid,
                                         order_status="OPEN", limit=50)
            _r = resp.to_dict() if hasattr(resp, "to_dict") else resp
            orders = (_r or {}).get("orders") or []
            for o in orders:
                side = str(o.get("side") or "")
                oid = str(o.get("order_id") or "")[:20]
                cfg_o = o.get("order_configuration") or {}
                kind = ""
                px = ""
                stop_px = ""
                sz = ""
                for k, v in (cfg_o.items() if isinstance(cfg_o, dict) else []):
                    kind = k
                    if isinstance(v, dict):
                        px = v.get("limit_price") or ""
                        stop_px = v.get("stop_price") or ""
                        sz = v.get("base_size") or ""
                        break
                ct = str(o.get("created_time") or "")[:19]
                print(f"  {side:<4}  {kind:<32}  size={sz:<6}  px={px:<10}  "
                      f"stop={stop_px:<10}  oid={oid}  {ct}")
            if not orders:
                print("  (no open orders)")
        except Exception as e:
            print(f"  ✗ broker query failed: {e}")

        # Event timeline
        pid_events = [e for e in events
                      if e.get("symbol") == pid
                      and float(e.get("ts") or 0) >= now - 900]  # last 15 min
        pid_events.sort(key=lambda e: float(e.get("ts") or 0))

        # Event type counts
        by_type = Counter(e.get("event_type") for e in pid_events)
        print(f"\n[EVENT TYPES last 15min] ({len(pid_events)} total)")
        for et, n in by_type.most_common(15):
            print(f"  {n:>4}  {et}")

        # Timeline of key events (last 30)
        interesting = {
            "trail_breach_limit_sell",
            "trail_breach_limit_sell_failed",
            "trail_breach_cancel_failed",
            "profit_lock_limit_placed",
            "profit_lock_limit_place_failed",
            "profit_lock_limit_cancelled_for_reprice",
            "profit_lock_limit_external_cancel_cleared",
            "profit_lock_limit_adopted_from_broker",
            "resting_stop_placed",
            "resting_stop_place_failed",
            "resting_stop_cancelled_excess_over_position",
            "resting_stop_wrong_price_cancelled",
            "resting_stop_adopted_from_broker",
            "phase_a_migration_stale_limit_cancelled",
            "bracket_stop_cancelled_on_profit_fill",
            "sleeve_cycle_completed",
        }
        timeline = [e for e in pid_events if e.get("event_type") in interesting]
        print(f"\n[TIMELINE — last 30 significant events]")
        for e in timeline[-30:]:
            ts = _fmt_ts(e.get("ts"))
            et = e.get("event_type") or ""
            sid = e.get("sleeve_id") or ""
            px = (e.get("target_px") or e.get("sell_px")
                  or e.get("limit_px") or e.get("stop_px")
                  or e.get("cancelled_stop_px") or "")
            oid = str(e.get("oid") or e.get("cancelled_oid")
                      or e.get("old_oid") or "")[:12]
            print(f"  {ts}  {et:<45}  {sid:<15}  px={px}  oid={oid}")

        # cancel/replace rate
        breach = [e for e in pid_events
                  if e.get("event_type") in ("trail_breach_limit_sell",
                                              "trail_breach_limit_sell_failed")]
        if len(breach) >= 2:
            gaps = []
            for i in range(1, len(breach)):
                dt = float(breach[i].get("ts") or 0) - float(breach[i-1].get("ts") or 0)
                if dt > 0:
                    gaps.append(dt)
            if gaps:
                avg = sum(gaps) / len(gaps)
                print(f"\n[LOOP CADENCE] {len(breach)} trail_breach events, "
                      f"avg gap {avg:.1f}s")


if __name__ == "__main__":
    main()
