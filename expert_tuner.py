"""Per-product parameter tuning from real historical data (Layer 2).

Layer 1 (expert_params.py) picks ATR multipliers from published trader
literature — Turtle 2N, Van Tharp 1R, Le Beau chandelier, Kaufman-adjusted
crypto. That gives you defaults that are semantically correct per asset
class, but not empirically validated on YOUR specific product.

Layer 2 (this module) grid-searches those multipliers against each product's
last 30 days of 5-min candles and picks the multiplier that maximized
risk-adjusted return. Silver may prefer 2.0×ATR trail because it's a
smooth continuous mover; oil may prefer 2.5×ATR because its noise around
inventory-report windows chops out 2.0× trails. This module finds that
per product, not by assumption.

Grid searched: trail_x_atr in {1.5, 2.0, 2.5, 3.0}. Stop_x_atr is held at
the literature value because tuning it against history overfits to the
absence of black-swan crashes in the window (survivorship bias — a stop
that "would have" made more money on this window may have been sitting
in the middle of tomorrow's flash crash).

Scoring: net profit ÷ max drawdown ($). Higher is better. Draws smoothly
scaled with return, penalizes chains that made $X while briefly drawing
down 5×$X.

Cached under __tuned_params__ symbol on the live tenant. Refreshed daily.
Falls back to Layer 1 literature multipliers if tuning hasn't run or
fails for this product.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Optional


TRAIL_X_ATR_GRID = [1.5, 2.0, 2.5, 3.0]


def tune_product(coinbase_client, product_id: str, days: int = 30) -> Optional[dict]:
    """Grid-search the best trail_x_atr multiplier for `product_id` against
    the last `days` days of 5-min candles.

    Returns:
      {
        "product_id": ...,
        "tuned_at": epoch_seconds,
        "atr": float,
        "trail_x_atr": chosen multiplier,
        "grid": [{"trail_x_atr": m, "return": $, "max_dd": $, "score": r} ...],
        "days": tested window,
      }
      or None if we couldn't get enough data.
    """
    from backtest import fetch_candles, run_backtest
    from paper_broker import PaperConfig
    from expert_params import compute_atr, asset_class_of, multipliers_for

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    try:
        candles = fetch_candles(coinbase_client, product_id, start, end, granularity="FIVE_MINUTE")
    except Exception as e:
        return {"product_id": product_id, "error": f"fetch_candles: {type(e).__name__}: {e}"}
    if len(candles) < 200:
        return {"product_id": product_id, "error": f"insufficient candles ({len(candles)} < 200)"}

    atr = compute_atr(candles, period=14)
    if atr <= 0:
        return {"product_id": product_id, "error": "atr computed as 0"}

    # Contract spec — we need real tick/size to score correctly.
    try:
        spec = _dump(coinbase_client.get_product(product_id))
    except Exception:
        spec = {}
    tick_size = float(spec.get("price_increment") or 0.005)
    details = spec.get("future_product_details") or {}
    contract_size = float(details.get("contract_size") or 50)

    paper_cfg = PaperConfig(
        product_id=product_id,
        contract_size=contract_size, tick_size=tick_size,
        fee_per_fill=2.34, margin_per_contract=275.0,
        starting_balance=100_000.0,
    )

    # Backtest each multiplier candidate.
    mid_close = candles[len(candles) // 2].close
    grid = []
    for trail_mult in TRAIL_X_ATR_GRID:
        try:
            cfg = _cfg_for_grid(mid_close, atr, trail_mult, contract_size)
            store, trader_factory = _make_trader_factory(cfg, product_id, mid_close)
            result = run_backtest(trader_factory, paper_cfg, candles)
            ret = result.total_return
            mdd = max(result.max_drawdown, 1.0)  # avoid div0
            score = ret / mdd
            grid.append({
                "trail_x_atr": trail_mult,
                "return": round(ret, 2),
                "max_dd": round(result.max_drawdown, 2),
                "cycles": result.cycles,
                "score": round(score, 4),
            })
        except Exception as e:
            grid.append({"trail_x_atr": trail_mult, "error": f"{type(e).__name__}: {e}"})

    ok_runs = [g for g in grid if "error" not in g and g.get("cycles", 0) > 0]
    if not ok_runs:
        return {
            "product_id": product_id, "atr": atr,
            "error": "no viable grid run",
            "grid": grid,
        }

    best = max(ok_runs, key=lambda g: g["score"])
    return {
        "product_id": product_id,
        "tuned_at": time.time(),
        "atr": atr,
        "days": days,
        "trail_x_atr": best["trail_x_atr"],
        "grid": grid,
        "chosen_score": best["score"],
        "asset_class": asset_class_of(product_id),
        "literature_multipliers": multipliers_for(product_id),
    }


def _cfg_for_grid(mid_price: float, atr: float, trail_mult: float, contract_size: float) -> dict:
    """Construct a swing-config for a single grid run. Buy and sell centered
    on mid_price with a spread of 2×ATR — realistic for the window."""
    spread = 2.0 * atr
    sell = round(mid_price + spread / 2, 3)
    buy = round(mid_price - spread / 2, 3)
    return {
        "core_qty": 0, "swing_qty": 2, "max_swing_qty": 5,
        "sell_px": sell, "buy_px": buy, "contract_size": contract_size,
        "margin_per_contract": 275.0, "scale_up_buffer_mult": 1.5,
        "fee_per_contract_roundtrip": 4.68,
        "abort_below": mid_price - 20 * atr, "abort_above": mid_price + 20 * atr,
        "fee_sanity_multiplier": 2.0,
        "exit_mode": "trailing_stop",
        "trail_trigger": sell,
        "trail_distance": round(trail_mult * atr, 4),
        "reanchor_threshold": round(atr, 4),
    }


def _make_trader_factory(cfg: dict, tenant_symbol: str, seed_price: float):
    """Return (store, factory) for a single grid run. Uses an in-memory
    ephemeral state store so grid iterations don't leak state to each other
    AND so each trader.step() doesn't pay a disk-fsync tax. Without this,
    a 30-day 5-min walk-forward = 100k+ fsync calls = 15+ min just waiting
    on disk. With InMemoryStateStore the same run completes in seconds."""
    import os
    from state_store import InMemoryStateStore
    from safety import TradeLog
    from swing_leg import SwingTrader
    tenant = "tuner"
    store = InMemoryStateStore()
    store.put_config(tenant, tenant_symbol, cfg)
    log = TradeLog(os.path.join(os.getenv("SWING_DATA_DIR", "/tmp"),
                                f"tune_{tenant_symbol.replace('-', '_')}_{int(time.time()*1000)}.jsonl"))

    def factory(broker):
        # Seed a position so the strategy has something to swing on.
        from paper_broker import Lot, PaperPosition
        import uuid
        seed_qty = int(cfg.get("swing_qty") or 0)
        if seed_qty > 0:
            broker.position = PaperPosition(
                product_id=broker.cfg.product_id, qty=seed_qty, avg_entry=seed_price)
            broker.lots = [Lot(
                id=f"lot-tune-{uuid.uuid4()}",
                qty=seed_qty, entry_price=seed_price, entry_ts=time.time(),
                source="tuner", strategy_id=None,
            )]
        return SwingTrader(broker, store, tenant, tenant_symbol, trade_log=log)

    return store, factory


def _dump(obj):
    if obj is None:
        return {}
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, dict):
        return obj
    return {}


def tune_products(coinbase_client, product_ids: list[str], days: int = 30) -> dict[str, dict]:
    """Run tune_product() for every product. Returns {product_id: tuning_result}.
    Slow — expect ~5-15 seconds per product for the grid search. Meant to be
    called from a daily background scheduler, not in the main tick loop.
    """
    out = {}
    for pid in product_ids:
        result = tune_product(coinbase_client, pid, days=days)
        if result:
            out[pid] = result
    return out
