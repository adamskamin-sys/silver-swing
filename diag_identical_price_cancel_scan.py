"""Scan Coinbase cancelled-order history for identical-price cancel-replace
loops across ALL products.

Adam 2026-07-22: NEAR screenshot showed 16 CANCELLED BUY LIMITs at $2.0100
in a 10-min window. The trade_log has already rotated past that window,
so we can't trace it from events. Instead, query Coinbase's own history
(never rotates) and find the pattern anywhere else on the account.

Groups cancelled orders by (product_id, side, price, size) — any group
with >= 3 members at identical price is a cancel-replace loop signature.
"""
import os
from collections import defaultdict


def main():
    from broker import BrokerConfig, CoinbaseBroker
    # Seed broker with the primary symbol so we can call list_orders
    # without a specific product filter (or query per-product later).
    seed = os.getenv("SWING_SYMBOL", "SLR-27AUG26-CDE")
    b = CoinbaseBroker(BrokerConfig(product_id=seed))

    try:
        resp = b.client.list_orders(order_status=["CANCELLED"], limit=250)
        raw = resp.to_dict() if hasattr(resp, "to_dict") else resp
    except Exception as e:
        print(f"list_orders failed: {e}")
        return

    orders = raw.get("orders") or []
    print(f"pulled {len(orders)} CANCELLED orders from Coinbase")

    # Extract (product, side, price, size) per order + created_time for sort
    parsed = []
    for o in orders:
        pid = o.get("product_id") or ""
        side = str(o.get("side") or "").upper()
        oid = o.get("order_id") or ""
        created = str(o.get("created_time") or "")[:19]
        cfg = o.get("order_configuration") or {}
        # Extract price + size from whichever config shape it is
        price = None
        size = None
        kind = ""
        for k, v in (cfg.items() if isinstance(cfg, dict) else []):
            kind = k
            if isinstance(v, dict):
                _p = (v.get("limit_price") or v.get("stop_price")
                      or v.get("price"))
                _s = v.get("base_size")
                if _p:
                    try:
                        price = float(_p)
                    except (TypeError, ValueError):
                        pass
                if _s:
                    try:
                        size = float(_s)
                    except (TypeError, ValueError):
                        pass
                break
        parsed.append({
            "pid": pid, "side": side, "price": price, "size": size,
            "kind": kind, "oid": oid, "created": created,
        })

    # Group by (pid, side, price)
    groups = defaultdict(list)
    for o in parsed:
        if o["price"] is None or o["size"] is None:
            continue
        # Round price to 6 decimals to group near-identical
        key = (o["pid"], o["side"], round(o["price"], 8), o["size"])
        groups[key].append(o)

    # Sort groups by size (loop severity)
    ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))

    print("\n[IDENTICAL-PRICE CANCEL GROUPS] (>=3 = loop signature)")
    found = 0
    for (pid, side, price, size), items in ranked:
        if len(items) < 3:
            continue
        found += 1
        items.sort(key=lambda o: o["created"])
        first = items[0]["created"]
        last = items[-1]["created"]
        print(f"\n  {pid}  {side}  {size}@{price}  ({first} - {last})")
        print(f"    {len(items)} identical cancels")
        # Show first, middle, last 3 examples
        _show = items[:3] + (items[-3:] if len(items) > 6 else [])
        for o in _show[:6]:
            print(f"    {o['created']}  oid={o['oid'][:20]}  kind={o['kind']}")

    if not found:
        print("  no identical-price cancel groups >=3 found (clean)")


if __name__ == "__main__":
    main()
