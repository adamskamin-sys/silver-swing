"""Audit configured vs actual fee_per_contract_roundtrip per product.

Adam 2026-07-20: fees feel too high. Root suspect: swing_leg.py:69 defaults
fee_per_contract_roundtrip=4.68 (empirical SLR/silver rate), applied to
every product regardless of type. Perps + small futures have real fees
of ~$0.42-$0.61/side ($0.84-$1.22 roundtrip) — the config overstates by
up to 10× on ETH PERP.

Consequences of overcounting:
  - Fee-floor clamp requires unrealistic sell_px lift → take-profit rarely
    fires → cycles close via stop_loss → net-loss cycles cascade
  - Realized P&L display double-charges → dashboard understates true P&L
  - Scanner ranking / expert consensus / $/day all skewed

For every product with a config in Redis, print:
  configured fee_per_contract_roundtrip
  actual roundtrip from Coinbase (avg of last 5+ fills per product)
  discrepancy multiplier
  recommended calibrated value

Read-only. Run:  python3 diag_fee_configured_vs_actual.py
"""
from __future__ import annotations
import os
import json
from collections import defaultdict


def _get(o, k):
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
    print("=" * 92)
    print("FEE CONFIGURED vs ACTUAL AUDIT")
    print("=" * 92)

    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if not url:
        print("\n✗ REDIS_URL not set")
        return
    r = redis.Redis.from_url(url, decode_responses=True)
    store = json.loads(r.get("silver-swing:store") or "{}")
    tenant = next((t for t in store if t.endswith("-live")), None)
    if not tenant:
        print(f"\n✗ no live tenant in store")
        return
    print(f"\n  tenant: {tenant}")

    # ---- Configured fees per product ----------------------------------
    configured = {}
    for pid, blk in store[tenant].items():
        if pid.startswith("__"):
            continue
        cfg = (blk or {}).get("config") or {}
        fee = cfg.get("fee_per_contract_roundtrip")
        if fee is not None:
            configured[pid] = float(fee)
    print(f"\n  {len(configured)} products have configured fee_per_contract_roundtrip")

    # ---- Actual fees from Coinbase last 250 fills ---------------------
    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id="BTC-USD"))
    print(f"\n  fetching recent Coinbase fills...")
    try:
        resp = b.client.list_orders(order_status="FILLED", limit=250)
        orders = _get(resp, "orders") or []
    except Exception as e:
        print(f"  ✗ list_orders failed: {e}")
        return

    # Group by product_id, track fee + qty
    per_product = defaultdict(list)
    for o in orders:
        pid = _get(o, "product_id")
        if not pid:
            continue
        try:
            fee = float(_get(o, "total_fees") or 0)
            qty = float(_get(o, "filled_size") or 0)
        except Exception:
            continue
        if fee <= 0 or qty <= 0:
            continue
        per_product[pid].append({"fee": fee, "qty": qty})

    # ---- Compare ------------------------------------------------------
    print("\n" + "-" * 92)
    print(f"  {'PID':<24} {'cfg_rt':>10} {'actual_rt':>10} {'over×':>8} "
          f"{'n_fills':>7}  status")
    print("-" * 92)
    critical = []
    all_pids = sorted(set(list(configured.keys()) + list(per_product.keys())))
    for pid in all_pids:
        cfg_rt = configured.get(pid)
        fills = per_product.get(pid, [])
        if not fills or len(fills) < 2:
            # not enough data to compare
            n = len(fills)
            cfg_str = f"${cfg_rt:.2f}" if cfg_rt is not None else "-"
            print(f"  {pid:<24} {cfg_str:>10} {'?':>10} {'?':>8} "
                  f"{n:>7}  (need ≥2 fills)")
            continue
        # Avg per-side fee per contract
        avg_fee_per_side = sum(f["fee"] / max(1, f["qty"]) for f in fills) / len(fills)
        actual_rt = avg_fee_per_side * 2
        cfg_str = f"${cfg_rt:.2f}" if cfg_rt is not None else "none"
        act_str = f"${actual_rt:.2f}"
        over = (cfg_rt / actual_rt) if (cfg_rt and actual_rt > 0) else None
        over_str = f"{over:.1f}×" if over else "-"
        status = ""
        if over is None:
            status = "no cfg"
        elif over >= 2.0:
            status = f"🚨 OVER by {over:.1f}× — clamp too tight, TP rarely fires"
            critical.append((pid, cfg_rt, actual_rt, over))
        elif over <= 0.5:
            status = f"⚠ UNDER by {1/over:.1f}× — clamp too loose, net-loss risk"
            critical.append((pid, cfg_rt, actual_rt, over))
        elif over > 1.2 or over < 0.8:
            status = f"drift {over:.2f}×"
        else:
            status = "✓ close enough"
        print(f"  {pid:<24} {cfg_str:>10} {act_str:>10} {over_str:>8} "
              f"{len(fills):>7}  {status}")

    print("-" * 92)
    if critical:
        print(f"\n  🚨 {len(critical)} products with fee misconfiguration:")
        for pid, cfg_rt, actual_rt, over in critical:
            print(f"       {pid}: cfg ${cfg_rt:.2f} → actual ${actual_rt:.2f} "
                  f"(recalibrate to ${actual_rt:.3f})")
        print("\n  Impact: bot's fee-floor clamp for these products requires")
        print("  ~{}× the real profit margin to fire a take-profit. Cycles that".format(
            max(o for _, _, _, o in critical if o >= 2)))
        print("  should exit green instead sit through stop-loss triggers.")
    else:
        print("\n  ✓ all products within 2× of actual fees.")


if __name__ == "__main__":
    main()
