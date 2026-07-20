"""Emergency — is XLP currently NET SHORT? (§3.8 violation check).

Adam 2026-07-20 07:07: XLP fills timeline showed 3 BUYs vs 7 SELLs over
~9 min (07:02:32→07:06:19), most with double-fire pairs at same second.
If starting position was <4 long, XLP is now net short and the 17 open
resting SELL orders on the portfolio will keep piling shorts.

Reads Coinbase truth via list_futures_positions + list_orders. Prints:
  1. Current XLP position side + qty
  2. Open resting orders (BUY vs SELL breakdown by qty)
  3. If SHORT: recommended BUY qty to flatten
  4. Sleeve state (own_avg per sleeve) vs broker truth

Read-only. Does NOT place orders. Use paper trade or manual Coinbase UI
if the recommendation shows a flatten needed.

Usage:  python3 diag_xlp_short_check.py
"""
from __future__ import annotations
import os
import json


PID = "XLP-20DEC30-CDE"


def _dump(o, k):
    if hasattr(o, k):
        return getattr(o, k)
    if isinstance(o, dict):
        return o.get(k)
    return None


def _to_dict(x):
    if hasattr(x, "to_dict"):
        return x.to_dict()
    if isinstance(x, dict):
        return x
    return {}


def main() -> None:
    print("=" * 78)
    print(f"XLP SHORT-CHECK — {PID}")
    print("=" * 78)

    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id=PID))

    # ---- Position side + qty -------------------------------------------
    positions = _to_dict(b.client.list_futures_positions()).get("positions") or []
    pos = next((p for p in positions if _dump(p, "product_id") == PID), None)
    if not pos:
        print(f"\n  Coinbase: {PID} not in list_futures_positions")
        print(f"  → position is FLAT (0 contracts)")
        pos_side = "FLAT"
        pos_qty = 0
        avg_entry = 0.0
    else:
        pos_side = str(_dump(pos, "side") or "").upper()
        pos_qty = int(float(_dump(pos, "number_of_contracts") or 0))
        avg_entry = float(_dump(pos, "avg_entry_price") or 0)
        print(f"\n  Coinbase truth:  side={pos_side}  qty={pos_qty}  avg=${avg_entry}")

    # ---- Open orders on XLP --------------------------------------------
    print(f"\n  Open orders on {PID}:")
    try:
        resp = b.client.list_orders(product_id=PID, order_status="OPEN", limit=100)
        orders = _dump(resp, "orders") or []
    except Exception as e:
        print(f"  ✗ list_orders failed: {e}")
        orders = []

    buys = [o for o in orders if str(_dump(o, "side") or "").upper() == "BUY"]
    sells = [o for o in orders if str(_dump(o, "side") or "").upper() == "SELL"]
    print(f"    total open: {len(orders)}  BUYs: {len(buys)}  SELLs: {len(sells)}")

    def _qty(o):
        cfg = _to_dict(_dump(o, "order_configuration"))
        for k in cfg.values() if isinstance(cfg, dict) else []:
            if isinstance(k, dict):
                q = k.get("base_size") or k.get("size") or k.get("quote_size")
                if q:
                    try:
                        return int(float(q))
                    except Exception:
                        pass
        return 1  # fallback

    buy_qty_total = sum(_qty(o) for o in buys)
    sell_qty_total = sum(_qty(o) for o in sells)
    print(f"    open BUY qty total:  {buy_qty_total}")
    print(f"    open SELL qty total: {sell_qty_total}")
    if sells:
        print(f"\n    open SELL orders (first 10):")
        for o in sells[:10]:
            oid = str(_dump(o, "order_id") or "")[:20]
            cfg = _to_dict(_dump(o, "order_configuration"))
            px = "?"
            for k in cfg.values() if isinstance(cfg, dict) else []:
                if isinstance(k, dict):
                    px = k.get("limit_price") or k.get("stop_price") or px
                    if px != "?":
                        break
            created = _dump(o, "created_time") or "?"
            print(f"      {oid}...  px={px}  created={created}")

    # ---- Short-risk math -----------------------------------------------
    print(f"\n  Short-risk analysis:")
    net_after_all_sells_fire = pos_qty - sell_qty_total + buy_qty_total
    if pos_side == "SHORT":
        print(f"  🚨 ALREADY SHORT: side=SHORT qty={pos_qty}")
        print(f"  → §3.8 VIOLATION. Flatten by BUYing {pos_qty} contract(s) on Coinbase UI.")
    elif net_after_all_sells_fire < 0:
        gap = abs(net_after_all_sells_fire)
        print(f"  ⚠ SHORT-RISK: if all {sell_qty_total} open SELLs fire and only {buy_qty_total} BUYs fire, net = {net_after_all_sells_fire}")
        print(f"  → Would go NET SHORT by {gap} contract(s). Cancel {gap} SELL orders to prevent.")
    else:
        print(f"  ✓ SAFE: current qty {pos_qty}, open sells {sell_qty_total}, open buys {buy_qty_total}")
        print(f"    worst-case net = {net_after_all_sells_fire} (still long)")

    # ---- Sleeve state vs broker ----------------------------------------
    print(f"\n  Sleeve state:")
    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if not url:
        print(f"  ✗ REDIS_URL not set — skipping sleeve state check")
        return
    r = redis.Redis.from_url(url, decode_responses=True)
    store = json.loads(r.get("silver-swing:store") or "{}")
    tenant = next((t for t in store if t.endswith("-live")
                   and PID in (store.get(t) or {})), None)
    if not tenant:
        print(f"  ✗ {PID} not in any live tenant")
        return
    block = store[tenant][PID]
    sleeves_cfg = (block.get("config") or {}).get("sleeves") or []
    sleeves_state = (block.get("state") or {}).get("sleeves") or {}
    print(f"    tenant: {tenant}")
    print(f"    sleeves configured: {len(sleeves_cfg)}")
    sleeve_claim_total = 0
    for sc in sleeves_cfg:
        sid = sc.get("id")
        ss = sleeves_state.get(sid) or {}
        st = ss.get("state") or "?"
        own = ss.get("own_avg_entry")
        qty = sc.get("qty") or 0
        rst_oid = ss.get("resting_stop_oid")
        rst_px = ss.get("resting_stop_px")
        if st == "ARMED_SELL":
            sleeve_claim_total += int(qty)
        print(f"      • {sid}  qty={qty}  state={st}  own_avg={own}  "
              f"rst_stop_oid={rst_oid}  rst_px={rst_px}")
    print(f"\n    ARMED_SELL claim total: {sleeve_claim_total}  broker qty: {pos_qty}")
    if sleeve_claim_total > pos_qty:
        gap = sleeve_claim_total - pos_qty
        print(f"    👻 GHOST SLEEVES detected: {gap} contract(s) claimed by sleeves but not on Coinbase")
        print(f"    → Need auto-heal to flip ghost sleeves back to ARMED_BUY.")
    elif sleeve_claim_total < pos_qty:
        gap = pos_qty - sleeve_claim_total
        print(f"    UNCLAIMED broker qty: {gap} — will be adopted by reconciler next tick.")
    else:
        print(f"    ✓ sleeve claim matches broker qty exactly.")


if __name__ == "__main__":
    main()
