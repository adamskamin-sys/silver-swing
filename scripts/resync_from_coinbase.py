"""Resync every live product's contract_size, tick_size, and fees against
Coinbase directly — and recompute sleeve buy_px/sell_px so each sleeve's
implied $/swing net matches the user's target. Dry-run by default.

Why: dashboard modal computes spread = (target_net + fees) / (qty × contract_size).
If the stored contract_size was wrong when the modal ran (e.g., defaulted to
silver's 50 for a product where the real spec is different), the resulting
sleeve holds a spread that's too tight — every cycle nets a fraction of what
the user asked for. This script re-anchors around the truth.

Usage:
    # Dry-run — no writes, just report the drift and what would change:
    python3 scripts/resync_from_coinbase.py
    python3 scripts/resync_from_coinbase.py --tenant adam-live
    python3 scripts/resync_from_coinbase.py --target-net 2

    # Actually write:
    python3 scripts/resync_from_coinbase.py --apply
    python3 scripts/resync_from_coinbase.py --apply --target-net 2

Notes:
    - Contract_size and fees are pulled from Coinbase authoritatively.
      Anything stored that disagrees is overwritten.
    - Sleeve spread rewrite preserves the CURRENT midpoint (so the sleeve
      keeps anchoring around whatever price it was tracking) but widens
      or narrows the spread to actually produce --target-net after LIVE fees.
    - Sleeves named 'Custom' are skipped so hand-tuned setups keep values.
    - Skips coin products (BTC-USD, ETH-USD, etc.) — they have no futures spec.
"""

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from state_store import make_store  # noqa: E402


def fetch_live_spec(symbol: str) -> dict | None:
    """Return {contract_size, tick_size, fee_per_fill_buy, fee_per_fill_sell,
    fee_per_contract_roundtrip} pulled live, or None if Coinbase rejects the
    product (e.g., non-futures symbol).
    """
    try:
        from broker import BrokerConfig, CoinbaseBroker  # noqa: F401
    except Exception as e:
        print(f"broker import failed: {e}", file=sys.stderr)
        return None
    try:
        broker = CoinbaseBroker(BrokerConfig(product_id=symbol))
        spec = broker.contract_spec()
        out = {
            "contract_size": float(spec.get("contract_size") or 0),
            "tick_size": float(spec.get("tick_size") or 0),
        }

        # Preview both sides so we get real per-fill fees.
        def _preview(side: str) -> float:
            try:
                if side == "BUY":
                    r = broker.client.preview_limit_order_gtc_buy(
                        product_id=symbol, base_size="1", limit_price="0.001",
                    )
                else:
                    r = broker.client.preview_limit_order_gtc_sell(
                        product_id=symbol, base_size="1", limit_price="999999.99",
                    )
                pd = r.to_dict() if hasattr(r, "to_dict") else r
                return float(pd.get("commission_total") or 0.0)
            except Exception:
                return 0.0

        buy_fee = _preview("BUY")
        sell_fee = _preview("SELL")
        if buy_fee <= 0 and sell_fee > 0:
            buy_fee = sell_fee
        if sell_fee <= 0 and buy_fee > 0:
            sell_fee = buy_fee
        out["fee_per_fill_buy"] = round(buy_fee, 4)
        out["fee_per_fill_sell"] = round(sell_fee, 4)
        out["fee_per_contract_roundtrip"] = round(buy_fee + sell_fee, 4)
        return out
    except Exception as e:
        print(f"  Coinbase spec fetch failed for {symbol}: {type(e).__name__}: {e}")
        return None


def implied_net(sell_px: float, buy_px: float, contract_size: float,
                qty: int, rt_fee: float) -> float:
    return (sell_px - buy_px) * contract_size * qty - rt_fee * qty


def rebalanced_prices(midpoint: float, target_net: float, contract_size: float,
                      qty: int, rt_fee: float) -> tuple[float, float]:
    """Return (buy_px, sell_px) that produce target_net after rt fees, anchored
    on midpoint. Preserves the sleeve's current anchor point; only rewrites the
    spread width.
    """
    gross_needed = target_net + rt_fee * qty
    spread = gross_needed / (qty * contract_size)
    sell_px = midpoint + spread / 2
    buy_px = midpoint - spread / 2
    return round(buy_px, 4), round(sell_px, 4)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="Actually write. Default is dry-run.")
    ap.add_argument("--tenant", default="adam-live",
                    help="Tenant to resync (default: adam-live).")
    ap.add_argument("--target-net", type=float, default=2.0,
                    help="Target $ net per swing per sleeve (default: 2.0).")
    ap.add_argument("--data-dir", default="./data",
                    help="Local data dir (used when REDIS_URL not set).")
    args = ap.parse_args()

    store = make_store(args.data_dir)
    backend = "Redis" if os.getenv("REDIS_URL") else f"JSON ({args.data_dir}/store.json)"
    print(f"Backend: {backend}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"Tenant: {args.tenant}")
    print(f"Target net / swing / sleeve: ${args.target_net}")
    print("=" * 90)

    symbols = [s for s in store.list_symbols(args.tenant) if not s.startswith("__")]
    if not symbols:
        print(f"No products for tenant {args.tenant!r}.")
        return 1

    stat_products = 0
    stat_spec_updated = 0
    stat_sleeves_rewritten = 0

    for symbol in sorted(symbols):
        cfg = store.get_config(args.tenant, symbol) or {}
        if not cfg:
            continue
        # Skip coin products — no futures spec available.
        if not any(x in symbol for x in ("-CDE", "-CFE", "-USD-FUT")) and "-USD" in symbol:
            continue

        stat_products += 1
        print(f"\n{symbol}")
        stored_size = float(cfg.get("contract_size") or 0)
        stored_rt = float(cfg.get("fee_per_contract_roundtrip") or 0)
        stored_buy_fee = float(cfg.get("fee_per_fill_buy") or 0)
        stored_sell_fee = float(cfg.get("fee_per_fill_sell") or 0)

        live = fetch_live_spec(symbol)
        if not live:
            print(f"  live spec unavailable — skipping")
            continue

        # Report spec drift.
        drift_size = live["contract_size"] != stored_size
        drift_rt = abs(live["fee_per_contract_roundtrip"] - stored_rt) > 0.01
        print(f"  contract_size stored={stored_size}  live={live['contract_size']}  {'← DRIFT' if drift_size else 'ok'}")
        print(f"  buy_fee        stored=${stored_buy_fee:.4f}  live=${live['fee_per_fill_buy']:.4f}")
        print(f"  sell_fee       stored=${stored_sell_fee:.4f}  live=${live['fee_per_fill_sell']:.4f}")
        print(f"  rt_fee         stored=${stored_rt:.4f}  live=${live['fee_per_contract_roundtrip']:.4f}  {'← DRIFT' if drift_rt else 'ok'}")

        cfg_dirty = False
        if drift_size or drift_rt or live["fee_per_fill_buy"] != stored_buy_fee or live["fee_per_fill_sell"] != stored_sell_fee:
            cfg["contract_size"] = live["contract_size"]
            cfg["tick_size"] = live["tick_size"] or cfg.get("tick_size")
            cfg["fee_per_fill_buy"] = live["fee_per_fill_buy"]
            cfg["fee_per_fill_sell"] = live["fee_per_fill_sell"]
            cfg["fee_per_contract_roundtrip"] = live["fee_per_contract_roundtrip"]
            cfg_dirty = True
            if args.apply:
                stat_spec_updated += 1

        # Per-sleeve spread reconciliation.
        sleeves = cfg.get("sleeves") or []
        new_sleeves = []
        sleeves_changed = False
        for sl in sleeves:
            new_sl = dict(sl)
            name = sl.get("name") or sl.get("id")
            if str(name).lower().startswith("custom"):
                print(f"  sleeve {name}: skipped (Custom)")
                new_sleeves.append(new_sl)
                continue
            qty = int(sl.get("qty") or 0)
            buy_px = float(sl.get("buy_px") or 0)
            sell_px = float(sl.get("sell_px") or 0)
            if qty <= 0 or buy_px <= 0 or sell_px <= 0:
                new_sleeves.append(new_sl)
                continue

            live_net = implied_net(sell_px, buy_px, live["contract_size"], qty,
                                   live["fee_per_contract_roundtrip"])
            midpoint = (buy_px + sell_px) / 2
            new_buy, new_sell = rebalanced_prices(
                midpoint, args.target_net, live["contract_size"], qty,
                live["fee_per_contract_roundtrip"])
            new_net = implied_net(new_sell, new_buy, live["contract_size"], qty,
                                  live["fee_per_contract_roundtrip"])

            drift_price = abs(new_buy - buy_px) > 0.0001 or abs(new_sell - sell_px) > 0.0001
            marker = "← REWRITE" if drift_price else "ok"
            print(f"  sleeve {name} qty={qty}: "
                  f"buy=${buy_px:.4f} sell=${sell_px:.4f} → net=${live_net:.2f}  |  "
                  f"target ${args.target_net}: buy=${new_buy:.4f} sell=${new_sell:.4f} → net=${new_net:.2f}  {marker}")
            if drift_price:
                new_sl["buy_px"] = new_buy
                new_sl["sell_px"] = new_sell
                sleeves_changed = True
                if args.apply:
                    stat_sleeves_rewritten += 1
            new_sleeves.append(new_sl)

        if args.apply and (cfg_dirty or sleeves_changed):
            cfg["sleeves"] = new_sleeves
            store.put_config(args.tenant, symbol, cfg)
            print(f"  → written")

    print()
    print("=" * 90)
    print(f"Products scanned: {stat_products}")
    print(f"Spec updates: {stat_spec_updated}")
    print(f"Sleeve rewrites: {stat_sleeves_rewritten}")
    if not args.apply:
        print("\nDRY RUN — re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
