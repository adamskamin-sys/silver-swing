"""Check every sleeve config for None / missing critical fields
(buy_px, sell_px, stop_loss_px, contract_size, tick_size).

Reports current state and offers --restore to pull sensible values
from broker.contract_spec + last known Redis values.

    python3 diag_config_drift_check.py           # show state
    python3 diag_config_drift_check.py restore   # attempt restore
"""
import os
import json
import sys
import time


def main():
    action = sys.argv[1].lower() if len(sys.argv) > 1 else "show"

    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if not url:
        print("REDIS_URL not set")
        return
    r = redis.Redis.from_url(url, decode_responses=True)
    store = json.loads(r.get("silver-swing:store") or "{}")

    tenant = "adam-live"
    tbody = store.get(tenant) or {}
    products = sorted([p for p in tbody.keys()
                       if not p.startswith("__") and isinstance(tbody.get(p), dict)])

    print("=" * 96)
    print(f"CONFIG DRIFT CHECK — {tenant}")
    print("=" * 96)

    fixes = []
    for pid in products:
        block = tbody[pid] or {}
        cfg = block.get("config") or {}
        sleeves = cfg.get("sleeves") or []
        cfg_contract_size = cfg.get("contract_size")
        cfg_tick_size = cfg.get("tick_size")
        cfg_fee = cfg.get("fee_per_contract_roundtrip")

        # Top-level product config drift
        product_issues = []
        if cfg_contract_size is None:
            product_issues.append("contract_size=None")
        if cfg_tick_size is None:
            product_issues.append("tick_size=None")
        if cfg_fee is None:
            product_issues.append("fee_per_contract_roundtrip=None")

        # Per-sleeve config drift
        sleeve_issues = []
        for sc in sleeves:
            sid = sc.get("id") or "?"
            missing = []
            for field in ("buy_px", "sell_px", "stop_loss_px"):
                if sc.get(field) is None:
                    missing.append(field)
            if missing:
                sleeve_issues.append(f"    {sid}: {', '.join(missing)}=None")

        if not product_issues and not sleeve_issues:
            continue

        print(f"\n{pid}")
        if product_issues:
            print(f"  product-level: {', '.join(product_issues)}")
        for si in sleeve_issues:
            print(si)
        fixes.append((pid, product_issues, sleeve_issues))

    if not fixes:
        print("\n✓ no drift detected — every sleeve has all required fields")
        return

    if action != "restore":
        print(f"\n{len(fixes)} products with drift")
        print("\nRe-run with 'restore' to attempt automatic fix:")
        print("  python3 diag_config_drift_check.py restore")
        return

    # === RESTORE MODE ===
    print("\n" + "=" * 96)
    print("RESTORE MODE — pulling broker.contract_spec + backing out sensible defaults")
    print("=" * 96)

    from broker import BrokerConfig, CoinbaseBroker

    for pid, product_issues, sleeve_issues in fixes:
        print(f"\n{pid}")
        block = tbody.get(pid) or {}
        cfg = block.get("config") or {}
        try:
            b = CoinbaseBroker(BrokerConfig(product_id=pid))
            spec = b.contract_spec() if hasattr(b, "contract_spec") else {}
        except Exception as e:
            print(f"  ✗ broker.contract_spec failed: {e}")
            continue

        # Restore product-level fields from broker
        if cfg.get("contract_size") is None:
            cs = spec.get("contract_size")
            if cs:
                cfg["contract_size"] = float(cs)
                print(f"  ✓ contract_size <- {cs} (from broker)")
        if cfg.get("tick_size") is None:
            ts = spec.get("tick_size")
            if ts:
                cfg["tick_size"] = float(ts)
                print(f"  ✓ tick_size <- {ts} (from broker)")
        if cfg.get("fee_per_contract_roundtrip") is None:
            # Use conservative default — real value calibrates on next fill
            cfg["fee_per_contract_roundtrip"] = 1.0
            print(f"  ✓ fee_per_contract_roundtrip <- 1.0 (conservative default)")

        # Get current mark for backing out sleeve prices
        try:
            book = _dump(b.client.get_best_bid_ask(product_ids=[pid]))
            pricebooks = book.get("pricebooks") or []
            if pricebooks:
                pb = pricebooks[0]
                bid = float((pb.get("bids") or [{}])[0].get("price") or 0)
                ask = float((pb.get("asks") or [{}])[0].get("price") or 0)
                mark = (bid + ask) / 2.0 if bid and ask else max(bid, ask)
            else:
                mark = 0.0
        except Exception:
            mark = 0.0

        # Restore per-sleeve buy/sell/stop
        for sc in cfg.get("sleeves") or []:
            sid = sc.get("id") or "?"
            if sc.get("buy_px") is None or sc.get("sell_px") is None:
                if mark <= 0:
                    print(f"  ⚠ {sid}: mark unknown, cannot restore prices")
                    continue
                spread = mark * 0.005  # 0.5% as conservative default
                if sc.get("buy_px") is None:
                    sc["buy_px"] = round(mark - spread / 2, 6)
                    print(f"  ✓ {sid} buy_px <- ${sc['buy_px']} (mark - 0.25%)")
                if sc.get("sell_px") is None:
                    sc["sell_px"] = round(mark + spread / 2, 6)
                    print(f"  ✓ {sid} sell_px <- ${sc['sell_px']} (mark + 0.25%)")
            if sc.get("stop_loss_px") is None and sc.get("stop_loss_enabled"):
                if mark > 0:
                    sc["stop_loss_px"] = round(mark * 0.95, 6)
                    print(f"  ✓ {sid} stop_loss_px <- ${sc['stop_loss_px']} (mark - 5%)")

        # Persist
        block["config"] = cfg
        tbody[pid] = block

    store[tenant] = tbody
    r.set("silver-swing:store", json.dumps(store))
    print("\n✓ store persisted to Redis")


def _dump(o):
    if hasattr(o, "to_dict"):
        try:
            return o.to_dict()
        except Exception:
            pass
    if isinstance(o, dict):
        return o
    return {}


if __name__ == "__main__":
    main()
