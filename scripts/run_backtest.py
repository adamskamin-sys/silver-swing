#!/usr/bin/env python3
"""
scripts/run_backtest.py — CLI backtest runner invoked by the dashboard.

Reads a JSON request on stdin, prints a JSON result on stdout. Structured for
subprocess spawn from the Node dashboard so the JS side never touches Coinbase.

Request shape:
{
  "tenant": "adam",
  "symbol": "SLR-27AUG26-CDE",
  "days": 30,
  "granularity": "FIVE_MINUTE",
  "mode": "single" | "compare_all",
  "starting_balance": 100000.0     # optional, default 100k
}

Result shape (single mode):
{
  "ok": true,
  "result": { starting_balance, final_equity, total_return, total_return_pct,
              realized_pnl, unrealized_pnl, fees_paid, max_drawdown,
              max_drawdown_pct, cycles, fills, halted, halt_reason }
}

Result shape (compare_all mode):
{
  "ok": true,
  "results": [
    { "strategy": "fixed_limit", ...same fields as single result },
    { "strategy": "trailing_stop", ...same fields }
  ]
}

On failure: {"ok": false, "error": "..."}
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone


def main() -> int:
    try:
        req = json.load(sys.stdin)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"bad request: {e}"}))
        return 1

    tenant = req.get("tenant") or "adam"
    symbol = req.get("symbol") or "SLR-27AUG26-CDE"
    days = int(req.get("days") or 30)
    granularity = req.get("granularity") or "FIVE_MINUTE"
    mode = req.get("mode") or "single"
    starting_balance = float(req.get("starting_balance") or 100_000.0)

    try:
        # Late imports so a missing dep only affects backtest runs, not the
        # rest of the server.
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

        # Fresh store per backtest so runs don't cross-contaminate
        store_path = os.path.join(os.getenv("SWING_DATA_DIR", "data"),
                                  f"bt_{tenant}_{symbol}_{int(end.timestamp())}.json")
        store = JsonFileStateStore(store_path)
        cfg = _default_config()
        store.put_config(tenant, symbol, cfg)
        log = TradeLog(store_path.replace(".json", ".jsonl"))

        def factory(broker, exit_mode=None):
            if exit_mode:
                cfg2 = dict(cfg)
                cfg2["exit_mode"] = exit_mode
                # trailing needs trigger + distance to validate; add defaults if missing
                cfg2.setdefault("trail_trigger", cfg2["sell_px"])
                cfg2.setdefault("trail_distance", 0.20)
                store.put_config(tenant, symbol, cfg2)
            return SwingTrader(broker, store, tenant, symbol, trade_log=log)

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
            print(json.dumps({"ok": True, "results": results}))
        else:
            r = run_backtest(factory, paper_cfg, candles)
            print(json.dumps({"ok": True, "result": _result_to_dict(r)}))

        # Cleanup the throwaway backtest artifacts
        for p in (store_path, store_path.replace(".json", ".jsonl")):
            try: os.remove(p)
            except OSError: pass

        return 0
    except Exception as e:
        print(json.dumps({
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "trace": traceback.format_exc(),
        }))
        return 1


def _default_config():
    return {
        "core_qty": 10, "swing_qty": 2, "max_swing_qty": 5,
        "sell_px": 65.0, "buy_px": 63.0, "contract_size": 50,
        "margin_per_contract": 275.0, "scale_up_buffer_mult": 1.5,
        "fee_per_contract_roundtrip": 4.68,
        "abort_below": 60.0, "abort_above": 70.0,
        "fee_sanity_multiplier": 2.0, "exit_mode": "fixed_limit",
    }


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
