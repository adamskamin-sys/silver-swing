"""Diagnose why CRYPTO section is empty in the scanner.

Adam 2026-07-20: after b413327 (SPOT filter fix), 963430e (fetch both
product types), 8a19cc8 (per-class top_n), CRYPTO section STILL empty.
This diag isolates each failure point.

Reports:
  1. Does coinbase_client.get_products(product_type='SPOT') return
     anything? Count + first 5 product_ids + first product's raw shape.
  2. What's the product_type field value on each returned SPOT product?
     (If Coinbase's SDK doesn't set it consistently, our stamping might
     fail to distinguish downstream.)
  3. Do SPOT products pass compute_ranking? Count survivors.
  4. Do SPOT products pass classify_asset_class as 'crypto'?
  5. What's in the current __scanner__ Redis snapshot (top / top_crypto
     / top_derivative)?

Read-only. Usage:
    python3 diag_scanner_spot_check.py
"""
from __future__ import annotations
import os
import json


def main() -> None:
    print("=" * 78)
    print("SCANNER SPOT PIPELINE DIAG")
    print("=" * 78)

    # ---- 1. Raw Coinbase SPOT fetch --------------------------------------
    print("\n[1/5] Raw Coinbase get_products(product_type='SPOT')")
    print("-" * 78)
    try:
        from broker import BrokerConfig, CoinbaseBroker
        b = CoinbaseBroker(BrokerConfig(product_id="BTC-USD"))
        resp = b.client.get_products(product_type="SPOT")
        payload = resp.to_dict() if hasattr(resp, "to_dict") else resp
        products = payload.get("products") or []
        print(f"  Returned {len(products)} SPOT products")
        if products:
            print(f"  First 10 product_ids:")
            for p in products[:10]:
                pid = p.get("product_id") if isinstance(p, dict) else "?"
                pt = p.get("product_type") if isinstance(p, dict) else "?"
                price = p.get("price") if isinstance(p, dict) else "?"
                print(f"    {pid} product_type={pt!r} price={price}")
            print(f"\n  Full field list on first product:")
            first = products[0]
            if isinstance(first, dict):
                for k in sorted(first.keys()):
                    v = first[k]
                    if isinstance(v, (str, int, float, bool, type(None))):
                        print(f"    {k}: {v}")
                    else:
                        print(f"    {k}: <{type(v).__name__}>")
    except Exception as e:
        print(f"  ✗ FAILED: {type(e).__name__}: {e}")
        products = []

    # ---- 2. product_type stamping ----------------------------------------
    print(f"\n[2/5] product_type field distribution on {len(products)} SPOT products")
    print("-" * 78)
    pt_counts = {}
    for p in products:
        if isinstance(p, dict):
            pt = str(p.get("product_type") or "NONE")
            pt_counts[pt] = pt_counts.get(pt, 0) + 1
    for pt, n in sorted(pt_counts.items(), key=lambda x: -x[1]):
        print(f"  {pt}: {n}")

    # ---- 3. compute_ranking survival -------------------------------------
    print(f"\n[3/5] compute_ranking survival test (SPOT only)")
    print("-" * 78)
    from scanner import compute_ranking, classify_asset_class
    # Stamp product_type as the scanner does
    for p in products:
        if isinstance(p, dict):
            p["product_type"] = "SPOT"
    ranked = compute_ranking(products, top_n=50)
    print(f"  {len(ranked)} products survived compute_ranking (out of {len(products)})")
    if ranked:
        print(f"  First 5 survivors:")
        for r in ranked[:5]:
            print(f"    {r['product_id']}: price=${r['price']}, "
                  f"vol_pct={r['vol_pct']}%, "
                  f"product_type={r.get('product_type')}")

    # ---- 4. classify_asset_class check -----------------------------------
    print(f"\n[4/5] classify_asset_class → 'crypto' check")
    print("-" * 78)
    if ranked:
        crypto_count = 0
        for r in ranked:
            cls = classify_asset_class(r.get("product_id") or "",
                                        product_type=r.get("product_type") or "")
            if cls == "crypto":
                crypto_count += 1
        print(f"  {crypto_count}/{len(ranked)} classified as 'crypto'")
        # Show a couple examples that DIDN'T classify as crypto
        non_crypto = [r for r in ranked
                       if classify_asset_class(r.get("product_id") or "",
                                                product_type=r.get("product_type") or "") != "crypto"]
        if non_crypto:
            print(f"  ⚠ {len(non_crypto)} did NOT classify as crypto — first 5:")
            for r in non_crypto[:5]:
                cls = classify_asset_class(r.get("product_id") or "",
                                            product_type=r.get("product_type") or "")
                print(f"    {r['product_id']} product_type={r.get('product_type')!r} → {cls}")

    # ---- 5. Current Redis snapshot ---------------------------------------
    print(f"\n[5/5] Current __scanner__ Redis snapshot")
    print("-" * 78)
    try:
        import redis
        url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
        if not url:
            print(f"  REDIS_URL not set — skipping")
        else:
            r = redis.Redis.from_url(url, decode_responses=True)
            raw = r.get("silver-swing:scanner")
            if not raw:
                print(f"  Redis key silver-swing:scanner is empty (scanner never ran?)")
            else:
                data = json.loads(raw)
                gen = data.get("generated_at", 0)
                import time
                age = int(time.time() - gen) if gen else -1
                print(f"  Snapshot age: {age}s")
                top = data.get("top") or []
                top_crypto = data.get("top_crypto") or []
                top_deriv = data.get("top_derivative") or []
                print(f"  top (combined): {len(top)} entries")
                print(f"  top_crypto:     {len(top_crypto)} entries")
                print(f"  top_derivative: {len(top_deriv)} entries")
                if top:
                    print(f"\n  First 3 of combined top:")
                    for e in top[:3]:
                        print(f"    {e.get('product_id')} product_type={e.get('product_type')!r} "
                              f"asset_class={e.get('asset_class')!r}")
    except Exception as e:
        print(f"  ✗ Redis check failed: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
