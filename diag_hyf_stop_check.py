"""Why is HYF stop-loss NOT PLACED despite valid config?

Adam 2026-07-20: HYF 31 JUL 26 shows Position 1 LONG @ $62.19, sleeve
config has stop_loss_enabled=true + stop_loss_px=$61.99, state
WAITING_FOR_SELL — but chip shows STOP LOSS: NOT PLACED. §3.6 violation.

Enumerates every reason placement might be blocked:
  1. Strategy halted? (my step() early-returns)
  2. Track alive? (dead track = no _maintain_resting_stop tick)
  3. Sleeve state ARMED_SELL? (needed for stop placement)
  4. Existing open SELLs on Coinbase? (would trigger broker-authoritative
     refusal in commit fe07f8c)
  5. resting_stop_oid tracked in sleeve state? (already placed?)
  6. Recent events showing placement attempts + refusals

Read-only. Run:  python3 diag_hyf_stop_check.py
"""
from __future__ import annotations
import os
import json
import time
from collections import Counter


PID = "HYF-31JUL26-CDE"


def _dump(o):
    if hasattr(o, "to_dict"):
        return o.to_dict()
    if isinstance(o, dict):
        return o
    return {}


def main() -> None:
    print("=" * 90)
    print(f"HYF STOP-PLACEMENT AUDIT — {PID}")
    print("=" * 90)

    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if not url:
        print("\n✗ REDIS_URL not set")
        return
    r = redis.Redis.from_url(url, decode_responses=True)
    store = json.loads(r.get("silver-swing:store") or "{}")
    tenant = next((t for t in store if t.endswith("-live")
                   and PID in (store.get(t) or {})), None)
    if not tenant:
        print(f"\n✗ {PID} not in any live tenant")
        return
    block = store[tenant][PID]
    cfg = block.get("config") or {}
    state = block.get("state") or {}
    sleeves_cfg = cfg.get("sleeves") or []
    sleeves_state = state.get("sleeves") or {}
    print(f"\n  tenant: {tenant}")

    # ---- 1. Strategy state ---------------------------------------------
    print("\n1. STRATEGY STATE")
    print(f"    state:       {state.get('state', 'unknown')}")
    print(f"    halt_reason: {state.get('halt_reason', 'none')}")
    if state.get("state") == "HALTED":
        print(f"    🚨 STRATEGY HALTED — _maintain_resting_stop never runs")

    # ---- 2. Track alive? -----------------------------------------------
    print("\n2. TRACK HEARTBEAT")
    hb_raw = store[tenant].get("__track_heartbeat__") or {}
    hb = hb_raw.get("config") or hb_raw
    tracks = hb.get("tracks") or {}
    t = tracks.get(PID) or {}
    last_ok = float(t.get("last_step_ok_ts") or 0)
    if last_ok > 0:
        age = int(time.time() - last_ok)
        alive = age < 600
        print(f"    last_step_ok_age: {age}s   {'✓ alive' if alive else '🚨 DEAD'}")
        print(f"    tick_count: {t.get('tick_count', 0)}")
    else:
        print(f"    🚨 NO HEARTBEAT ENTRY — track has never stepped this session")

    # ---- 3. Sleeve config + state --------------------------------------
    print("\n3. SLEEVE CONFIG + STATE")
    for sc in sleeves_cfg:
        sid = sc.get("id")
        ss = sleeves_state.get(sid) or {}
        st = ss.get("state") or "?"
        own = ss.get("own_avg_entry")
        rst_oid = ss.get("resting_stop_oid")
        rst_px = ss.get("resting_stop_px")
        rst_stage = ss.get("resting_stop_stage")
        sl_enabled = sc.get("stop_loss_enabled")
        sl_px = sc.get("stop_loss_px")
        rst_enabled = sc.get("resting_stop_enabled", True)
        print(f"    • {sid}  qty={sc.get('qty')}  state={st}")
        print(f"        own_avg_entry:    {own}")
        print(f"        stop_loss_enabled: {sl_enabled}")
        print(f"        stop_loss_px:      {sl_px}")
        print(f"        resting_stop_enabled: {rst_enabled}")
        print(f"        resting_stop_oid:  {rst_oid}")
        print(f"        resting_stop_px:   {rst_px}")
        print(f"        resting_stop_stage: {rst_stage}")
        if st != "ARMED_SELL":
            print(f"        ⚠ state is {st}, not ARMED_SELL — placement path may skip")
        if not sl_enabled:
            print(f"        ⚠ stop_loss_enabled=False — no stop will be placed")
        if not sl_px or float(sl_px) <= 0:
            print(f"        ⚠ stop_loss_px is 0 — expert fallback needed")
        if rst_oid:
            print(f"        ✓ resting_stop_oid IS tracked — verify still open on Coinbase")

    # ---- 4. Coinbase truth: position + open orders ---------------------
    print("\n4. COINBASE TRUTH")
    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id=PID))
    try:
        positions = _dump(b.client.list_futures_positions()).get("positions") or []
        pos = next((p for p in positions if p.get("product_id") == PID), None)
        if pos:
            side = str(pos.get("side") or "").upper()
            qty = int(float(pos.get("number_of_contracts") or 0))
            avg = float(pos.get("avg_entry_price") or 0)
            print(f"    position:    {side} {qty} @ ${avg}")
        else:
            print(f"    position:    FLAT (not in list_futures_positions)")
    except Exception as e:
        print(f"    ✗ list_futures_positions failed: {e}")

    try:
        _resp = b.client.list_orders(product_id=PID, order_status="OPEN", limit=50)
        orders = _dump(_resp).get("orders") or []
        buys = [o for o in orders if str(o.get("side") or "").upper() == "BUY"]
        sells = [o for o in orders if str(o.get("side") or "").upper() == "SELL"]
        print(f"    open orders: {len(orders)}  (BUYs {len(buys)}, SELLs {len(sells)})")
        for o in sells:
            oid = str(o.get("order_id") or "")[:20]
            cfg2 = o.get("order_configuration") or {}
            px_show = "?"
            qty_show = "?"
            for v in cfg2.values() if isinstance(cfg2, dict) else []:
                if isinstance(v, dict):
                    px_show = v.get("limit_price") or v.get("stop_price") or px_show
                    qty_show = v.get("base_size") or v.get("size") or qty_show
                    if px_show != "?" and qty_show != "?":
                        break
            print(f"      SELL  {oid}...  px={px_show}  qty={qty_show}  "
                  f"created={o.get('created_time')}")
        if sells:
            print(f"    ⚠ existing SELLs on Coinbase — broker-authoritative")
            print(f"      guard (fe07f8c) may refuse to place a new stop.")
    except Exception as e:
        print(f"    ✗ list_orders failed: {e}")

    # ---- 5. Resume intent ----------------------------------------------
    print("\n5. RESUME INTENT")
    ri = block.get("resume_intent")
    if ri:
        print(f"    ⚠ resume_intent pending in Redis: {ri}")
        print(f"      If track dead, intent sits unread. Commit eb6652f flags")
        print(f"      products with pending resume_intent as should_track_critical.")
    else:
        print(f"    no pending resume_intent")

    # ---- 6. Recent events ----------------------------------------------
    print("\n6. RECENT HYF EVENTS (last 40 relevant)")
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        events = log.tail(5000)
        hyf_events = [e for e in events if (e.get("symbol") or "") == PID]
        keywords = ("resting_stop", "stop_loss", "sleeve_", "expert_stop",
                    "track_", "spawn", "zombie", "halt", "resume")
        relevant = [e for e in hyf_events
                    if any(k in (e.get("event_type") or "") for k in keywords)]
        print(f"    {len(hyf_events)} HYF events total, {len(relevant)} relevant")
        for e in relevant[-40:]:
            ts_ago = int(time.time() - float(e.get("ts") or 0))
            et = e.get("event_type") or ""
            reason = (e.get("reason") or "")[:80]
            sev = e.get("severity") or ""
            print(f"    {ts_ago:>6}s  [{sev:>8}]  {et:<50}  {reason}")
        cnt = Counter(e.get("event_type") for e in relevant)
        print(f"\n    top event types:")
        for et, n in cnt.most_common(10):
            print(f"      {n:5d}  {et}")
    except Exception as e:
        print(f"    ✗ trade log read failed: {e}")


if __name__ == "__main__":
    main()
