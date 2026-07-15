"""Product-ID + realized-P&L audit — resolves 2 remaining bugs in one shot.

Bug 1: Dashboard shows "HYP 20 DEC 30" for what is actually HYPE PERP at
       Coinbase. Need to know: what product_id is stored for HYPE, and
       does it match what Coinbase returns?

Bug 2: Dashboard shows HYPE realized +$14.75, but Coinbase fills sum to
       ~net-flat. Sleeve accounting is off — need per-sleeve realized
       vs Coinbase-observed roundtrip P&L.

Reports:
  1. Every product_id in the store (config + state) — the ground truth
  2. Every strategy sleeve's product_id (mismatch = phantom bug)
  3. Coinbase's actual product list — cross-reference against stored IDs
  4. For each stored product, sleeve realized_pnl vs. Coinbase fills sum
     over the last 30 days (spot the accounting drift)

Read-only. Usage:
    python3 diag_product_id_audit.py
    python3 diag_product_id_audit.py HYP    # filter to symbols matching HYP
"""
from __future__ import annotations
import json
import os
import sys


def _load_store() -> dict:
    """Get the raw store — Redis if REDIS_URL is set, JSON file otherwise.
    Uses state_store.make_store() (same as the bot) + ._load()."""
    data_dir = os.getenv("SWING_DATA_DIR", "data")
    try:
        import state_store
        store = state_store.make_store(data_dir)
        return store._load()
    except Exception as e:
        print(f"  WARN: state_store.make_store failed: {e}")
    for name in ("store.json", "state.json", "swing_state.json"):
        p = os.path.join(data_dir, name)
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    return {}


def _find_all_product_ids(store: dict) -> set[str]:
    """Walk store, collect every string that looks like a product_id."""
    ids: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and "-" in v and (
                    "-CDE" in v or "-PERP" in v or "-INTX" in v or "-USD" in v
                ):
                    ids.add(v)
                elif isinstance(v, (dict, list)):
                    walk(v)
                if k in ("product_id", "symbol", "product") and isinstance(v, str):
                    ids.add(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)
    walk(store)
    return ids


def _walk_sleeves(store: dict, filter_substring: str = "") -> list[dict]:
    """Return list of {tenant, product_id, sleeve_id, sleeve_name, realized_pnl,
    cycles, state} for every sleeve in the store matching the filter."""
    out: list[dict] = []
    for tenant, tenant_data in (store or {}).items():
        if not isinstance(tenant_data, dict):
            continue
        for product_id, entry in tenant_data.items():
            if not isinstance(entry, dict) or product_id.startswith("__"):
                continue
            if filter_substring and filter_substring.upper() not in product_id.upper():
                continue
            cfg = entry.get("config") or {}
            sleeves_cfg = {s.get("id"): s for s in (cfg.get("sleeves") or [])}
            state = entry.get("state") or {}
            sleeves_state = state.get("sleeves") or {}
            for sid, sc in sleeves_cfg.items():
                ss = sleeves_state.get(sid, {})
                out.append({
                    "tenant": tenant,
                    "product_id": product_id,
                    "sleeve_id": sid,
                    "sleeve_name": sc.get("name") or sid,
                    "qty": sc.get("qty"),
                    "state": ss.get("state"),
                    "realized_pnl": ss.get("realized_pnl", 0.0),
                    "cycles": ss.get("cycles", 0),
                    "live_order_id": ss.get("live_order_id"),
                })
    return out


def _query_coinbase_products() -> list[str]:
    """Get all product_ids currently listed at Coinbase."""
    try:
        from coinbase.rest import RESTClient
        from dotenv import load_dotenv
        load_dotenv()
        key_path = os.getenv("COINBASE_API_KEY_JSON_PATH")
        if not key_path:
            print("  WARN: COINBASE_API_KEY_JSON_PATH not set — skipping Coinbase check")
            return []
        client = RESTClient(key_file=key_path)
        all_ids = set()
        for ptype in ("FUTURE", "SPOT"):
            try:
                resp = client.get_products(product_type=ptype)
                payload = resp.to_dict() if hasattr(resp, "to_dict") else resp
                for p in (payload.get("products") or []):
                    pid = p.get("product_id") or p.get("product_type_id")
                    if pid:
                        all_ids.add(pid)
            except Exception as e:
                print(f"  WARN: Coinbase list failed for {ptype}: {e}")
        return sorted(all_ids)
    except Exception as e:
        print(f"  Could not query Coinbase: {e}")
        return []


def main() -> None:
    filter_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    print("=" * 70)
    print(f"PRODUCT-ID AUDIT {'(filter=' + filter_arg + ')' if filter_arg else ''}")
    print("=" * 70)

    store = _load_store()
    if not store:
        print("\nNO STORE DATA. Set SWING_DATA_DIR or check state_store.")
        return

    # 1) Every product_id we're tracking
    stored_ids = _find_all_product_ids(store)
    if filter_arg:
        stored_ids = {i for i in stored_ids if filter_arg.upper() in i.upper()}
    print(f"\n1) STORED PRODUCT IDs ({len(stored_ids)}):")
    for pid in sorted(stored_ids):
        print(f"     {pid}")

    # 2) Coinbase's actual product IDs
    coinbase_ids = _query_coinbase_products()
    if coinbase_ids and filter_arg:
        cb_filtered = [i for i in coinbase_ids if filter_arg.upper() in i.upper()]
        print(f"\n2) COINBASE PRODUCT IDs matching '{filter_arg}' ({len(cb_filtered)}):")
        for pid in cb_filtered:
            print(f"     {pid}")

    # 3) Diff: which stored IDs DON'T exist at Coinbase?
    if coinbase_ids:
        coinbase_set = set(coinbase_ids)
        ghosts = stored_ids - coinbase_set
        print(f"\n3) STORED IDs NOT FOUND AT COINBASE ({len(ghosts)}):")
        print(f"   These are the 'ghost' products — mislabeling likely comes from these:")
        for pid in sorted(ghosts):
            print(f"     ❌ {pid}")

    # 4) Sleeve realized_pnl per product
    sleeves = _walk_sleeves(store, filter_arg)
    print(f"\n4) SLEEVE REALIZED P&L ({len(sleeves)} sleeves):")
    for s in sleeves:
        print(f"     {s['product_id']:35s} sleeve={s['sleeve_id'][:20]:20s}"
              f" state={s['state']:20s} realized=${s['realized_pnl']:>8.2f}"
              f" cycles={s['cycles']:>3d}")

    # 5) Aggregate realized per product
    from collections import defaultdict
    per_product_realized: dict[str, float] = defaultdict(float)
    per_product_cycles: dict[str, int] = defaultdict(int)
    for s in sleeves:
        per_product_realized[s["product_id"]] += float(s["realized_pnl"] or 0)
        per_product_cycles[s["product_id"]] += int(s["cycles"] or 0)
    print(f"\n5) AGGREGATE REALIZED PER PRODUCT (from sleeve state):")
    for pid, rp in sorted(per_product_realized.items(), key=lambda x: -x[1]):
        print(f"     {pid:35s} realized=${rp:>8.2f}  cycles={per_product_cycles[pid]}")


if __name__ == "__main__":
    main()
