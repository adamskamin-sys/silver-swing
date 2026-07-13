#!/usr/bin/env python3
"""Run the champion-challenger evaluator on REAL candles. Local only — needs
your Coinbase credentials + your live config. READ-ONLY: never trades or edits.

Usage (from the repo root):
    # sweep every tracked product on the tenant
    python3 run_champion_challenger.py --days 30

    # or scope to one symbol
    SWING_SYMBOL=SLR-27AUG26-CDE python3 run_champion_challenger.py --days 30

What it does per symbol:
  - reads the LIVE config as the "champion"
  - builds two trail-distance challenger variants (tighter / wider)
  - fetches the last N days of 5-min candles
  - evaluates every config OUT-OF-SAMPLE via walk-forward
  - prints a conservative promotion recommendation (usually: keep champion)

Note: this assumes SwingConfig keys match expert_tuner._cfg_for_grid
(trail_distance, contract_size, margin_per_contract, ...). If a key differs on
your setup, adjust the two spots marked [ADJUST].
"""

import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone


def _evaluate_one(symbol: str, champion: dict, coinbase, days: int):
    """Walk-forward CC for a single symbol. Returns (ok, report_or_msg)."""
    from backtest import fetch_candles, run_backtest
    from paper_broker import PaperConfig
    from expert_tuner import _make_trader_factory
    from expert_params import compute_atr
    import champion_challenger as cc

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    try:
        candles = fetch_candles(coinbase.client, symbol, start, end, granularity="FIVE_MINUTE")
    except Exception as e:
        return False, f"fetch_candles error: {type(e).__name__}: {e}"
    if len(candles) < 200:
        return False, f"only {len(candles)} candles (need >= 200)"
    atr = compute_atr(candles, 14)

    spec = {}
    try:
        spec = coinbase.contract_spec() or {}
    except Exception:
        pass
    tick = float(champion.get("tick_size") or spec.get("price_increment") or 0.005)
    contract_size = float(champion.get("contract_size") or 50)
    paper_cfg = PaperConfig(
        product_id=symbol,
        contract_size=contract_size,
        tick_size=tick,
        fee_per_fill=float(champion.get("fee_per_contract_roundtrip", 4.68)) / 2.0,
        margin_per_contract=float(champion.get("margin_per_contract", 275.0)),
        starting_balance=100_000.0,
        slippage_ticks=1.0,
    )

    # Adam 2026-07-13: variants must touch BOTH the top-level primary
    # SwingConfig fields AND every sleeve's fields — sleeves are where the
    # real trading happens on Adam's live setup, and they carry their own
    # sell_px / buy_px / trail_distance. Only scaling the primary yielded
    # identical backtest metrics across all configs. Scale both bands
    # (sell/buy) and trail — one variant hits every exit_mode.
    seed_price = candles[len(candles) // 2].close

    def _scale_node(node: dict, mult: float) -> None:
        # sell/buy band around the mid                             [ADJUST]
        sell = node.get("sell_px"); buy = node.get("buy_px")
        if sell is not None and buy is not None:
            s = float(sell); b = float(buy)
            if s > b:
                mid = (s + b) / 2.0
                half = ((s - b) / 2.0) * mult
                node["sell_px"] = round(mid + half, 6)
                node["buy_px"] = round(mid - half, 6)
        # trailing-stop distance                                    [ADJUST]
        td = node.get("trail_distance")
        if td is not None:
            node["trail_distance"] = round(float(td) * mult, 6)

    def _variant(mult: float) -> dict:
        c = copy.deepcopy(champion)
        _scale_node(c, mult)
        for s in (c.get("sleeves") or []):
            if isinstance(s, dict):
                _scale_node(s, mult)
        return c

    configs = {
        "champion": champion,
        "tighter": _variant(0.75),
        "wider": _variant(1.25),
    }

    def run_fn(cfg, cs):
        _, factory = _make_trader_factory(cfg, symbol, seed_price)
        return run_backtest(factory, paper_cfg, cs)

    try:
        report = cc.evaluate_challengers(
            candles, configs, run_fn,
            champion="champion", n_splits=4, embargo=5, min_edge_pct=10.0,
        )
    except Exception as e:
        return False, f"evaluate_challengers error: {type(e).__name__}: {e}"
    report["_candles"] = len(candles)
    report["_atr"] = round(atr, 6)
    return True, report


def _list_tracked_symbols(store, tenant: str) -> list[str]:
    """Every symbol in the tenant with a real config (not meta keys). Includes
    futures + perps + anything the user tracks."""
    out = []
    try:
        symbols = store.list_symbols(tenant) or []
    except Exception:
        symbols = []
    for sym in symbols:
        if sym.startswith("__"):
            continue
        cfg = store.get_config(tenant, sym) or {}
        if cfg.get("product_id") or cfg.get("contract_size"):
            out.append(sym)
    return sorted(set(out))


def main() -> int:
    days = 30
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])

    tenant = os.getenv("SWING_TENANT", "adam")
    single_symbol = os.getenv("SWING_SYMBOL")
    data_dir = os.getenv("SWING_DATA_DIR", "data")

    from state_store import make_store
    from broker import BrokerConfig, CoinbaseBroker

    store = make_store(data_dir)

    # Adam 2026-07-13: "every tracked product and every future product."
    # Either the single-symbol override or a sweep across the tenant.
    if single_symbol:
        symbols = [single_symbol]
    else:
        symbols = _list_tracked_symbols(store, tenant)
        if not symbols:
            print(f"No tracked products for tenant {tenant!r}. "
                  f"Set SWING_SYMBOL=<product> to test a specific one.")
            return 1

    print(f"Champion-challenger sweep: {len(symbols)} symbol(s), {days}d each")
    print("=" * 72)

    summary = []
    for i, symbol in enumerate(symbols, 1):
        champion = store.get_config(tenant, symbol)
        if not champion:
            print(f"\n[{i}/{len(symbols)}] {symbol}: no config; skip")
            summary.append({"symbol": symbol, "status": "no_config"})
            continue
        print(f"\n[{i}/{len(symbols)}] {symbol} — fetching {days}d candles ...")
        try:
            coinbase = CoinbaseBroker(BrokerConfig(product_id=symbol))
        except Exception as e:
            print(f"  broker init failed: {type(e).__name__}: {e}")
            summary.append({"symbol": symbol, "status": "broker_error", "error": str(e)})
            continue
        ok, report = _evaluate_one(symbol, champion, coinbase, days)
        if not ok:
            print(f"  {report}")
            summary.append({"symbol": symbol, "status": "error", "error": report})
            continue
        print(f"  {report['_candles']} candles, ATR(14) = {report['_atr']}")
        print(json.dumps(
            {k: v for k, v in report.items() if not k.startswith("_")},
            indent=2, default=str,
        ))
        print(f"  >>> {report.get('note')}")
        summary.append({
            "symbol": symbol,
            "status": "ok",
            "recommend_promote": report.get("recommend_promote"),
            "champion_oos_mean_return": report.get("champion_metrics", {}).get("oos_mean_return"),
        })

    print("\n" + "=" * 72)
    print("SWEEP SUMMARY")
    print("=" * 72)
    for row in summary:
        print(json.dumps(row, default=str))
    promotable = [r["symbol"] for r in summary if r.get("recommend_promote")]
    print(f"\nPromotable: {promotable if promotable else 'none — keep all champions'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
