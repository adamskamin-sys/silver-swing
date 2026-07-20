"""Diagnose why XLP-20DEC30-CDE (XLM 31) shows STOP LOSS: NOT PLACED.

Adam 2026-07-20: after commit c680651 that extended _maintain_resting_stop's
breach-exit branch to hard_bottom stages, XLM 31 still displays NOT PLACED.
Possibilities:
  1. Deploy hasn't landed on Render yet — check running commit vs origin.
  2. Sleeve state has a stale live_order_id (idempotency guard returns early).
  3. Track not ticking (XLP-20DEC30-CDE not spawned or feed dead).
  4. resting_stop_oid points at a real live stop that dashboard just isn't
     surfacing (state cache).

Read-only. Usage:
    python3 diag_xlm31_stop_state.py
"""
from __future__ import annotations
import json
import os
import time


PID = "XLP-20DEC30-CDE"


def _dump(obj):
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj if isinstance(obj, dict) else {}


def main() -> None:
    print("=" * 78)
    print(f"XLM 31 ({PID}) STOP DIAGNOSTIC")
    print("=" * 78)

    # ---- 1. Running commit (verify fix deployed) -------------------------
    print("\n[1/6] Running commit on this instance")
    print("-" * 78)
    for env_key in ("RENDER_GIT_COMMIT", "GIT_COMMIT", "GIT_SHA"):
        v = os.environ.get(env_key)
        if v:
            print(f"  {env_key}: {v}")
            break
    else:
        print(f"  (no *_COMMIT env — try 'git rev-parse HEAD' if this is a shell session)")
    print(f"\n  Expected commit for XLM fix: c680651 (2026-07-20)")

    # ---- 2. Sleeve state from Redis -------------------------------------
    print("\n[2/6] Sleeve state (Redis)")
    print("-" * 78)
    try:
        import redis
        url = (os.environ.get("REDIS_URL")
               or os.environ.get("REDIS_INTERNAL_URL"))
        if not url:
            print(f"  REDIS_URL not set — cannot read sleeve state")
            return
        r = redis.Redis.from_url(url, decode_responses=True)
        store_raw = r.get("silver-swing:store")
        store = json.loads(store_raw) if store_raw else {}
        # Find live tenant that has XLP
        live_tenants = [k for k in store.keys() if k.endswith("-live")]
        xlp_state = None
        xlp_cfg = None
        xlp_tenant = None
        for lt in live_tenants:
            block = (store.get(lt) or {}).get(PID) or {}
            if block:
                xlp_state = block.get("state") or {}
                xlp_cfg = block.get("config") or {}
                xlp_tenant = lt
                break
        if not xlp_state:
            print(f"  ✗ {PID} not found in any live tenant")
            return
        print(f"  tenant: {xlp_tenant}")
        sleeves = xlp_state.get("sleeves") or {}
        print(f"  sleeves ({len(sleeves)}):")
        for sid, ss in sleeves.items():
            print(f"\n  sleeve id: {sid}")
            for k in ("state", "own_avg_entry", "qty",
                      "trail_armed", "trail_high_water_price",
                      "resting_stop_oid", "resting_stop_px", "resting_stop_stage",
                      "live_order_id", "realized_pnl", "cycles"):
                v = ss.get(k)
                if v is not None:
                    print(f"    {k}: {v}")
        # Config values that matter for _maintain_resting_stop
        print(f"\n  config values that drive stop placement:")
        cfg_sleeves = xlp_cfg.get("sleeves") or []
        for sc in cfg_sleeves:
            print(f"    sleeve {sc.get('id')}:")
            for k in ("stop_loss_enabled", "stop_loss_px", "sell_px",
                      "buy_px", "trail_distance", "trail_activation_px",
                      "resting_stop_enabled", "qty"):
                v = sc.get(k)
                if v is not None:
                    print(f"      {k}: {v}")
    except Exception as e:
        print(f"  ✗ Redis read failed: {type(e).__name__}: {e}")
        return

    # ---- 3. Live Coinbase state for XLP ---------------------------------
    print(f"\n[3/6] Live Coinbase state")
    print("-" * 78)
    try:
        from broker import BrokerConfig, CoinbaseBroker
        b = CoinbaseBroker(BrokerConfig(product_id=PID))
        # Position
        pos = b.position_qty()
        print(f"  position_qty(): {pos}")
        # Open orders
        resp = _dump(b.client.list_orders(product_id=PID,
                                          order_status=["OPEN", "PENDING"]))
        orders = resp.get("orders") or []
        if not orders:
            print(f"  no open orders on Coinbase")
        else:
            print(f"  {len(orders)} open orders:")
            for o in orders:
                side = o.get("side")
                oid = o.get("order_id")
                otype = o.get("order_type")
                cfg_block = o.get("order_configuration") or {}
                print(f"    {side} oid={oid} type={otype}")
                print(f"      config: {json.dumps(cfg_block, default=str)[:250]}")
        # Live product info
        pd = _dump(b.client.get_product(PID))
        print(f"  current price: ${pd.get('price')}")
        print(f"  best_bid: ${pd.get('best_bid_price')}, best_ask: ${pd.get('best_ask_price')}")
    except Exception as e:
        print(f"  ✗ Coinbase probe failed: {type(e).__name__}: {e}")

    # ---- 4. Status of any tracked live_order_id --------------------------
    print(f"\n[4/6] Status probe on tracked live_order_id (idempotency guard)")
    print("-" * 78)
    try:
        for sid, ss in sleeves.items():
            loid = ss.get("live_order_id")
            if not loid:
                print(f"  sleeve {sid}: live_order_id=None (guard won't fire)")
                continue
            try:
                st = b.order_status(loid)
                print(f"  sleeve {sid} live_order_id={loid}")
                print(f"    status: {st.get('status')}, raw: {st.get('raw_status')}")
                print(f"    filled_qty: {st.get('filled_qty')}")
                open_states = ("OPEN", "PENDING", "QUEUED")
                if st.get("status") in open_states:
                    print(f"    ⚠ FIX WOULD SKIP: idempotency guard treats this as open → return early")
                else:
                    print(f"    ✓ status={st.get('status')} — fix would proceed past guard")
            except Exception as _e:
                print(f"  sleeve {sid} status probe raised: {_e}")
                print(f"    ⚠ FIX WOULD SKIP: exception fallback treats as _open_status=True → return early")
    except Exception as e:
        print(f"  ✗ probe failed: {type(e).__name__}: {e}")

    # ---- 5. Is XLP being ticked? -----------------------------------------
    print(f"\n[5/6] Recent tick evidence in trade log")
    print("-" * 78)
    try:
        raw_events = r.lrange("silver-swing:trade_log", 0, 2000) or []
        events = []
        for line in raw_events:
            try:
                events.append(json.loads(line))
            except Exception:
                continue
        events.reverse()
        now = time.time()
        cutoff = now - 900  # last 15 min
        xlp_events = [e for e in events
                      if float(e.get("ts") or 0) > cutoff
                      and (e.get("product_id") == PID
                           or e.get("symbol") == PID)]
        if not xlp_events:
            print(f"  ✗ NO events for {PID} in last 15 min — Track may be silent / not ticking")
        else:
            print(f"  {len(xlp_events)} events in last 15 min:")
            for e in xlp_events[-15:]:
                ts = float(e.get("ts") or 0)
                rel = int(now - ts)
                sev = e.get("severity") or ""
                sev_mark = "🚨" if sev == "critical" else "⚠" if sev == "warn" else " "
                etype = e.get("event_type") or "?"
                reason = e.get("reason") or ""
                print(f"    {sev_mark} {rel:>4}s ago  {etype}  {reason[:80]}")
    except Exception as e:
        print(f"  ✗ log scan failed: {type(e).__name__}: {e}")

    # ---- 6. Recommendation ------------------------------------------------
    print(f"\n[6/6] What to check next")
    print("-" * 78)
    print("  If commit is < c680651: wait 60s for Render deploy + re-run")
    print("  If live_order_id is set AND status OPEN: cancel via Coinbase UI")
    print("      (or diag_cancel_orphan_order.py) to unblock the fix branch")
    print("  If no XLP events in last 15 min: track is silent — investigate")
    print("      _non_primary_tracks eviction / feed status")
    print("  If XLP events present but no resting_stop_placed / trail_breach_")
    print("      limit_sell: the fix branch isn't being reached — trace stage")


if __name__ == "__main__":
    main()
