#!/usr/bin/env python3
"""Go-live gauntlet runner.

Wraps go_live_check.gauntlet() in a CLI that a human can invoke before
promoting any parameter change to live money. Chains three checks:
  1. OVERFIT   — fragile tuning-winner detection (skipped without --grid)
  2. TAIL      — stress_test.stress_report on the candidate
  3. OOS EDGE  — candidate vs live champion, walk-forward

Verdict is one of NO-GO / GO-HOLD / GO-PROMOTE. Read-only.

Usage:
    # gauntlet a candidate config on SLR: compare against live champion
    python3 run_go_live_check.py --symbol SLR-27AUG26-CDE \\
        --candidate /path/to/candidate.json --days 30

If --candidate is omitted, uses a "0.75x band" variant of the current live
config as the candidate — same recipe the CC runner uses. Handy for a
quick sanity gauntlet.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone


def _scale_node(node: dict, mult: float) -> None:
    sell = node.get("sell_px"); buy = node.get("buy_px")
    if sell is not None and buy is not None:
        s = float(sell); b = float(buy)
        if s > b:
            mid = (s + b) / 2.0
            half = ((s - b) / 2.0) * mult
            node["sell_px"] = round(mid + half, 6)
            node["buy_px"] = round(mid - half, 6)
    td = node.get("trail_distance")
    if td is not None:
        node["trail_distance"] = round(float(td) * mult, 6)


def _variant(champion: dict, mult: float) -> dict:
    c = copy.deepcopy(champion)
    _scale_node(c, mult)
    for s in (c.get("sleeves") or []):
        if isinstance(s, dict):
            _scale_node(s, mult)
    return c


def _pick_tenant(store) -> str:
    env = os.getenv("SWING_TENANT")
    if env:
        return env
    tenants = list(store.list_tenants() or [])
    live = [t for t in tenants if t.endswith("-live")]
    return (live[0] if live else "adam")


def main() -> int:
    days = 30
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])

    symbol = None
    if "--symbol" in sys.argv:
        symbol = sys.argv[sys.argv.index("--symbol") + 1]
    if not symbol:
        print("ERROR: --symbol is required.")
        print("Usage: python3 run_go_live_check.py --symbol <PRODUCT> [--candidate <cfg.json>] [--days 30]")
        return 2

    candidate_path = None
    if "--candidate" in sys.argv:
        candidate_path = sys.argv[sys.argv.index("--candidate") + 1]

    data_dir = os.getenv("SWING_DATA_DIR", "data")

    from state_store import make_store
    from broker import BrokerConfig, CoinbaseBroker
    from backtest import fetch_candles, run_backtest
    from sim_broker import SimConfig as PaperConfig  # WS3: sim_broker replaces paper_broker
    from expert_tuner import _make_trader_factory
    from expert_params import compute_atr
    import go_live_check as glc

    store = make_store(data_dir)
    tenant = _pick_tenant(store)
    print(f"[diag] tenant: {tenant!r}, symbol: {symbol!r}")

    champion = store.get_config(tenant, symbol)
    if not champion:
        print(f"No live config for {tenant}/{symbol}.")
        return 1

    if candidate_path:
        try:
            with open(candidate_path) as f:
                candidate = json.load(f)
            print(f"[diag] candidate loaded from {candidate_path}")
        except Exception as e:
            print(f"failed to read {candidate_path}: {type(e).__name__}: {e}")
            return 1
    else:
        candidate = _variant(champion, 0.75)
        print(f"[diag] no --candidate given; using default 0.75x-band variant")

    # Fetch candles once
    coinbase = CoinbaseBroker(BrokerConfig(product_id=symbol))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    print(f"[diag] fetching {days}d candles for {symbol} ...")
    try:
        candles = fetch_candles(coinbase.client, symbol, start, end, granularity="FIVE_MINUTE")
    except Exception as e:
        print(f"fetch_candles error: {type(e).__name__}: {e}")
        return 1
    if len(candles) < 200:
        print(f"only {len(candles)} candles (need >= 200)")
        return 1
    atr = compute_atr(candles, 14)
    print(f"[diag] {len(candles)} candles, ATR(14) = {atr:.6f}")

    spec = {}
    try:
        spec = coinbase.contract_spec() or {}
    except Exception:
        pass
    tick = float(champion.get("tick_size") or spec.get("price_increment") or 0.005)
    contract_size = float(champion.get("contract_size") or spec.get("contract_size") or 50)
    paper_cfg = PaperConfig(
        product_id=symbol, contract_size=contract_size, tick_size=tick,
        fee_per_fill=float(champion.get("fee_per_contract_roundtrip", 4.68)) / 2.0,
        margin_per_contract=float(champion.get("margin_per_contract", 275.0)),
        starting_balance=100_000.0, slippage_ticks=1.0,
    )
    seed_price = candles[len(candles) // 2].close

    def run_fn(cfg, cs):
        _, factory = _make_trader_factory(cfg, symbol, seed_price)
        return run_backtest(factory, paper_cfg, cs)

    print("\nRunning gauntlet (overfit → tail → OOS edge) ...\n")
    result = glc.gauntlet(
        candidate_cfg=candidate, champion_cfg=champion,
        candles=candles, run_fn=run_fn,
        tuning_grid=None,   # skip overfit check unless a grid is supplied
    )

    print("=" * 88)
    print(f"VERDICT: {result['verdict']}")
    print("=" * 88)
    print(result["summary"])
    if result.get("blockers"):
        print("\nBLOCKERS:")
        for b in result["blockers"]:
            print(f"  - {b}")
    print("\nCHECKS:")
    print(json.dumps(result.get("checks") or {}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
