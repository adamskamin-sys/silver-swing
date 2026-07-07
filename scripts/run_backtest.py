#!/usr/bin/env python3
"""
scripts/run_backtest.py — backtest runner.

Two entry paths:
  1. CLI (stdin JSON → stdout JSON) — used locally by the Node dashboard when
     REDIS_URL is not set.
  2. Callable `execute(req: dict) -> dict` — used by the in-process backtest
     worker on the paper service (see backtest_worker.py). Same code path so
     both entry points can't drift.

Request shape:
{
  "tenant": "adam",
  "symbol": "SLR-27AUG26-CDE",
  "days": 30,
  "granularity": "FIVE_MINUTE",
  "mode": "single" | "compare_all",
  "starting_balance": 100000.0,     # optional, default 100k
  "auto_fit": true                  # optional, default true
}

Result shape (single mode):
{
  "ok": true,
  "result": { starting_balance, final_equity, total_return, ... },
  "applied_cfg": { buy_px, sell_px, abort_below, abort_above, auto_fit }
}

Result shape (compare_all mode):
{
  "ok": true,
  "results": [{ "strategy": "fixed_limit", ... }, { "strategy": "trailing_stop", ... }],
  "applied_cfg": { ... }
}

On failure: {"ok": false, "error": "..."}
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone


def execute(req: dict) -> dict:
    """Run a backtest from a request dict. Never raises — returns
    {"ok": false, "error": ...} on failure. Callable from both CLI and the
    in-process backtest worker.
    """
    tenant = req.get("tenant") or "adam"
    symbol = req.get("symbol") or "SLR-27AUG26-CDE"
    days = int(req.get("days") or 30)
    granularity = req.get("granularity") or "FIVE_MINUTE"
    mode = req.get("mode") or "single"
    starting_balance = float(req.get("starting_balance") or 100_000.0)
    # Auto-fit thresholds to the observed window unless the caller explicitly
    # opts out. Answers "did the strategy's mechanics work?" rather than
    # "did the arbitrary $65 target get hit?"
    auto_fit = bool(req.get("auto_fit", True))

    try:
        from backtest import fetch_candles, run_backtest
        from broker import BrokerConfig, CoinbaseBroker
        from paper_broker import PaperConfig
        from safety import TradeLog
        from state_store import JsonFileStateStore
        from swing_leg import SwingTrader

        coinbase = CoinbaseBroker(BrokerConfig(product_id=symbol))
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        candles = fetch_candles(coinbase.client, symbol, start, end, granularity=granularity)

        paper_cfg = PaperConfig(
            product_id=symbol,
            contract_size=50.0, tick_size=0.005,
            fee_per_fill=2.34, margin_per_contract=275.0,
            starting_balance=starting_balance,
        )

        store_path = os.path.join(os.getenv("SWING_DATA_DIR", "data"),
                                  f"bt_{tenant}_{symbol}_{int(end.timestamp())}.json")
        store = JsonFileStateStore(store_path)
        cfg = _default_config()
        if auto_fit and candles:
            _apply_auto_fit(cfg, candles)
        store.put_config(tenant, symbol, cfg)
        log = TradeLog(store_path.replace(".json", ".jsonl"))

        # Seed the paper broker with swing_qty contracts at the first candle's
        # open price. Without this, the strategy starts ARMED_SELL but the paper
        # book is empty, so every arm_sell gets skipped ("insufficient contracts")
        # and the backtest returns all zeros. With a seed, the first sell fires
        # against known basis and the cycle can rotate through the window.
        seed_qty = int(cfg.get("swing_qty") or 0)
        seed_price = float(candles[0].open) if candles else 0.0

        def factory(broker, exit_mode=None):
            if exit_mode:
                cfg2 = dict(cfg)
                cfg2["exit_mode"] = exit_mode
                cfg2.setdefault("trail_trigger", cfg2["sell_px"])
                cfg2.setdefault("trail_distance", 0.20)
                store.put_config(tenant, symbol, cfg2)
            if seed_qty > 0 and seed_price > 0:
                _seed_paper_position(broker, seed_qty, seed_price)
            return SwingTrader(broker, store, tenant, symbol, trade_log=log)

        applied_cfg = {
            "buy_px": cfg["buy_px"],
            "sell_px": cfg["sell_px"],
            "abort_below": cfg["abort_below"],
            "abort_above": cfg["abort_above"],
            "auto_fit": auto_fit,
        }

        try:
            if mode == "compare_all":
                results = []
                for name in ("fixed_limit", "trailing_stop"):
                    try:
                        r = run_backtest(lambda b, n=name: factory(b, n), paper_cfg, candles)
                        d = _result_to_dict(r)
                        d["strategy"] = name
                        results.append(d)
                    except Exception as inner:
                        results.append({"strategy": name, "error": str(inner)})
                return {"ok": True, "results": results, "applied_cfg": applied_cfg}
            else:
                r = run_backtest(factory, paper_cfg, candles)
                return {"ok": True, "result": _result_to_dict(r), "applied_cfg": applied_cfg}
        finally:
            for p in (store_path, store_path.replace(".json", ".jsonl")):
                try: os.remove(p)
                except OSError: pass
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "trace": traceback.format_exc(),
        }


def main() -> int:
    try:
        req = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"bad request: {e}"}))
        return 1
    result = execute(req)
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


def _default_config():
    # core_qty=0 because a backtest doesn't have a "protected core" to worry
    # about — we're evaluating whether the strategy mechanics work in this
    # window, not what a live position would tolerate. Leaving core_qty=10 (as
    # in live config) would cause reconcile() to HALT immediately since the
    # seeded position is only swing_qty, well below 10.
    return {
        "core_qty": 0, "swing_qty": 2, "max_swing_qty": 5,
        "sell_px": 65.0, "buy_px": 63.0, "contract_size": 50,
        "margin_per_contract": 275.0, "scale_up_buffer_mult": 1.5,
        "fee_per_contract_roundtrip": 4.68,
        "abort_below": 60.0, "abort_above": 70.0,
        "fee_sanity_multiplier": 2.0, "exit_mode": "fixed_limit",
    }


def _seed_paper_position(broker, qty: int, price: float) -> None:
    """Give the broker `qty` open contracts at `price` so the ARMED_SELL state
    machine has something to sell in the first candle. Mirrors what a real
    live account looks like at t=0: already long, waiting for the target."""
    from paper_broker import Lot, PaperPosition
    import time as _t, uuid as _uuid
    broker.position = PaperPosition(product_id=broker.cfg.product_id, qty=qty, avg_entry=price)
    broker.lots = [Lot(
        id=f"lot-seed-{_uuid.uuid4()}",
        qty=qty, entry_price=price, entry_ts=_t.time(),
        source="backtest_seed", strategy_id=None,
    )]


def _apply_auto_fit(cfg: dict, candles) -> None:
    """Set buy_px / sell_px / abort_below / abort_above from the observed
    price range of the candles. Rule of thumb: buy at the 25th percentile of
    closes, sell at the 75th, with abort bands at ±10% of range beyond min/max.
    """
    closes = sorted(c.close for c in candles if c.close > 0)
    if not closes:
        return
    lo = closes[0]
    hi = closes[-1]
    p25 = closes[max(0, len(closes) // 4)]
    p75 = closes[min(len(closes) - 1, (3 * len(closes)) // 4)]
    if p75 - p25 < 0.05:
        mid = (hi + lo) / 2
        p25 = mid - 0.10
        p75 = mid + 0.10
    rng = hi - lo
    cfg["buy_px"] = round(p25, 3)
    cfg["sell_px"] = round(p75, 3)
    cfg["abort_below"] = round(lo - 0.1 * rng, 3) if rng > 0 else round(lo * 0.95, 3)
    cfg["abort_above"] = round(hi + 0.1 * rng, 3) if rng > 0 else round(hi * 1.05, 3)


def _result_to_dict(r):
    return {
        "starting_balance": r.starting_balance,
        "final_equity": r.final_equity,
        "total_return": r.total_return,
        "total_return_pct": r.total_return_pct,
        "realized_pnl": r.realized_pnl,
        "unrealized_pnl": r.unrealized_pnl,
        "fees_paid": r.fees_paid,
        "max_drawdown": r.max_drawdown,
        "max_drawdown_pct": r.max_drawdown_pct,
        "cycles": r.cycles,
        "fills": r.fills,
        "halted": r.halted,
        "halt_reason": r.halt_reason,
        "price_min": r.price_min,
        "price_max": r.price_max,
        "price_start": r.price_start,
        "price_end": r.price_end,
        "candle_count": r.candle_count,
    }


if __name__ == "__main__":
    sys.exit(main())
