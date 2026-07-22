"""Find what internal product_id the bot uses for XRP-PERP-CDE display.

Adam 2026-07-22: Coinbase order screenshot showed "XRP PERP CDE 500"
with cancels at 10:17-10:26. diag_xrp_stop_loop returned zero events.
Means either the internal product_id isn't "XRP-*" (Coinbase display
name ≠ internal id, like OIL/NOL alias) OR the bot didn't originate
the cancels.

This diag:
  1. Lists every product in adam-live store
  2. Highlights any that could plausibly be XRP (contains XRP, RIPPLE, etc.)
  3. Queries Coinbase directly for the product spec by scanning open orders
     to find the actual product_id used in fills
"""
import os
import json


def main():
    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if not url:
        print("REDIS_URL not set")
        return
    r = redis.Redis.from_url(url, decode_responses=True)
    store = json.loads(r.get("silver-swing:store") or "{}")
    tbody = store.get("adam-live") or {}

    print("=" * 80)
    print("All products bot is tracking in adam-live:")
    print("=" * 80)
    products = sorted([p for p in tbody.keys()
                       if not p.startswith("__") and isinstance(tbody.get(p), dict)])
    for pid in products:
        block = tbody[pid]
        state = block.get("state") or {}
        sleeves = state.get("sleeves") or {}
        held = sum(1 for ss in sleeves.values()
                    if ss.get("state") == "ARMED_SELL" and ss.get("own_avg_entry"))
        flag = "★" if any(k in pid.upper() for k in ("XRP", "RIP", "XLP")) else " "
        print(f"  {flag}  {pid}  ({len(sleeves)} sleeves, {held} held)")

    print("\n" + "=" * 80)
    print("Coinbase open orders — find the XRP product_id from live orders:")
    print("=" * 80)
    from broker import BrokerConfig, CoinbaseBroker
    # Use any known product to seed the broker (we call list_orders globally)
    seed = products[0] if products else "SLR-27AUG26-CDE"
    b = CoinbaseBroker(BrokerConfig(product_id=seed))
    try:
        resp = b.client.list_orders(order_status="OPEN", limit=100)
        raw = resp.to_dict() if hasattr(resp, "to_dict") else resp
    except Exception as e:
        print(f"list_orders failed: {e}")
        return

    orders = raw.get("orders") or []
    # Also filter to XRP/Ripple candidates
    pids_seen = set()
    xrp_matches = []
    for o in orders:
        pid = o.get("product_id") or ""
        pids_seen.add(pid)
        if any(k in pid.upper() for k in ("XRP", "RIP", "XLP")):
            xrp_matches.append(o)

    print(f"\nOpen product_ids on Coinbase (n={len(pids_seen)}):")
    for p in sorted(pids_seen):
        flag = "★" if any(k in p.upper() for k in ("XRP", "RIP", "XLP")) else " "
        in_bot = "IN BOT" if p in products else "NOT IN BOT"
        print(f"  {flag}  {p}  [{in_bot}]")

    print(f"\nOrders matching XRP/RIP/XLP (n={len(xrp_matches)}):")
    for o in xrp_matches:
        pid = o.get("product_id")
        side = o.get("side")
        cfg = o.get("order_configuration") or {}
        kind = list(cfg.keys())[0] if cfg else ""
        _shape = list(cfg.values())[0] if cfg else {}
        px = _shape.get("limit_price") or _shape.get("stop_price") if isinstance(_shape, dict) else ""
        sz = _shape.get("base_size") if isinstance(_shape, dict) else ""
        oid = o.get("order_id", "")
        print(f"  {pid:<24}  {side:<5}  {kind:<32}  size={sz}  px={px}  oid={oid[:20]}")

    # Also check recent CANCELLED orders for XRP
    print("\n" + "=" * 80)
    print("Recent CANCELLED orders matching XRP/RIP/XLP:")
    print("=" * 80)
    try:
        resp2 = b.client.list_orders(order_status=["CANCELLED"], limit=250)
        raw2 = resp2.to_dict() if hasattr(resp2, "to_dict") else resp2
        cancels = raw2.get("orders") or []
    except Exception as e:
        print(f"cancelled list failed: {e}")
        return

    cnc_matches = [o for o in cancels
                    if any(k in str(o.get("product_id") or "").upper()
                           for k in ("XRP", "RIP", "XLP"))]
    print(f"\n{len(cnc_matches)} XRP-shaped cancels in last 250")
    for o in cnc_matches[:20]:
        pid = o.get("product_id")
        side = o.get("side")
        ct = str(o.get("created_time") or "")[:19]
        cfg = o.get("order_configuration") or {}
        kind = list(cfg.keys())[0] if cfg else ""
        _shape = list(cfg.values())[0] if cfg else {}
        px = _shape.get("limit_price") or _shape.get("stop_price") if isinstance(_shape, dict) else ""
        oid = str(o.get("order_id", ""))[:20]
        print(f"  {ct}  {pid:<24}  {side}  {kind:<32}  px={px}  oid={oid}")


if __name__ == "__main__":
    main()
