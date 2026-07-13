#!/usr/bin/env python3
"""Run the champion-challenger evaluator on REAL candles. Local only — needs
your Coinbase credentials + your live config. READ-ONLY: never trades or edits.

Usage (from the repo root):
    SWING_SYMBOL=SLR-27AUG26-CDE python3 run_champion_challenger.py --days 30

What it does:
  - reads your LIVE config for the symbol as the "champion"
  - builds two trail-distance challenger variants (tighter / wider)
  - fetches the last N days of 5-min candles
  - evaluates every config OUT-OF-SAMPLE via walk-forward
  - prints a conservative promotion recommendation (usually: keep the champion)

Note: this assumes your SwingConfig keys match expert_tuner._cfg_for_grid
(trail_distance, contract_size, margin_per_contract, ...). If a key differs on
your setup, adjust the two spots marked [ADJUST].
"""

import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone


def main() -> int:
    days = 30
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])

    tenant = os.getenv("SWING_TENANT", "adam")
    symbol = os.getenv("SWING_SYMBOL", "SLR-27AUG26-CDE")
    data_dir = os.getenv("SWING_DATA_DIR", "data")

    from state_store import make_store
    from broker import BrokerConfig, CoinbaseBroker
    from backtest import fetch_candles, run_backtest
    from paper_broker import PaperConfig
    from expert_tuner import _make_trader_factory
    from expert_params import compute_atr
    import champion_challenger as cc

    store = make_store(data_dir)
    champion = store.get_config(tenant, symbol)
    if not champion:
        print(f"No live config found for {tenant}/{symbol}. "
              f"Set SWING_TENANT / SWING_SYMBOL to a configured product.")
        return 1

    coinbase = CoinbaseBroker(BrokerConfig(product_id=symbol))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    print(f"Fetching {days}d of 5-min candles for {symbol} ...")
    candles = fetch_candles(coinbase.client, symbol, start, end, granularity="FIVE_MINUTE")
    if len(candles) < 200:
        print(f"Only {len(candles)} candles — need >= 200 for a walk-forward.")
        return 1
    atr = compute_atr(candles, 14)
    print(f"{len(candles)} candles, ATR(14) = {atr:.6f}")

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
        slippage_ticks=1.0,   # [crew:#5] realistic, not frictionless
    )

    # Champion's implied trail multiple, then tighter/wider challengers. [ADJUST]
    champ_mult = (float(champion.get("trail_distance") or (2.0 * atr)) / atr) if atr else 2.0

    def variant(mult: float) -> dict:
        c = copy.deepcopy(champion)
        c["trail_distance"] = round(atr * mult, 4)   # [ADJUST] if your trail key differs
        return c

    configs = {
        "champion": champion,
        "tighter_trail": variant(champ_mult * 0.75),
        "wider_trail": variant(champ_mult * 1.25),
    }

    seed_price = candles[len(candles) // 2].close

    def run_fn(cfg, cs):
        _, factory = _make_trader_factory(cfg, symbol, seed_price)
        return run_backtest(factory, paper_cfg, cs)

    report = cc.evaluate_challengers(
        candles, configs, run_fn,
        champion="champion", n_splits=4, embargo=5, min_edge_pct=10.0,
    )
    print(json.dumps(report, indent=2, default=str))
    print("\n>>>", report.get("note"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
