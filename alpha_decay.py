"""Live edge-health / alpha-decay monitor (crew).

Every edge decays as the market adapts or the regime it was fit to passes. This
tracks the LIVE realized edge (from cycle_completed events) against the edge the
backtest promised, and flags when the live edge has drifted below its confidence
band. It's the live counterpart to champion_challenger (which is offline):
champion-challenger asks "is there something better?"; this asks "is what I'm
running still real?"

Read-only. Uses backtest_integrity's Probabilistic Sharpe Ratio for the
significance test.
"""

from __future__ import annotations

from statistics import mean, pstdev
from typing import Optional

import backtest_integrity as bi


def cycle_pnls(events, window: Optional[int] = None) -> list[float]:
    """Per-cycle realized P&L from cycle_completed events (newest last)."""
    g = [float(e.get("gross") or 0) for e in events
         if str(e.get("event_type")) == "cycle_completed"]
    return g[-window:] if window else g


def edge_health(live_pnls: list[float],
                backtest_expectancy: Optional[float] = None,
                backtest_sharpe: Optional[float] = None,
                min_samples: int = 20) -> dict:
    """Grade the live edge.

    live_pnls: per-cycle realized P&L, live.
    backtest_expectancy: mean per-cycle P&L the backtest promised (optional).
    backtest_sharpe: per-cycle Sharpe the backtest promised (optional benchmark).

    Verdict:
      HEALTHY  — live edge consistent with (or above) the backtest promise
      DECAYING — positive but significantly below the backtest promise
      DEAD     — live expectancy <= 0 with enough samples (you're donating)
      UNKNOWN  — not enough live cycles yet
    """
    n = len(live_pnls)
    if n < min_samples:
        return {"verdict": "UNKNOWN", "n": n,
                "note": f"need >= {min_samples} cycles, have {n}"}

    live_mean = mean(live_pnls)
    live_sr = bi.sharpe_ratio(live_pnls)
    # PSR that the true (live) Sharpe beats the backtest Sharpe benchmark.
    psr_vs_backtest = None
    if backtest_sharpe is not None:
        psr_vs_backtest = round(bi.probabilistic_sharpe_ratio(live_sr, n, backtest_sharpe), 3)

    if live_mean <= 0:
        verdict = "DEAD"
    elif backtest_expectancy is not None and live_mean < 0.5 * backtest_expectancy:
        verdict = "DECAYING"
    elif psr_vs_backtest is not None and psr_vs_backtest < 0.10:
        # very unlikely the live edge still matches the backtest bar
        verdict = "DECAYING"
    else:
        verdict = "HEALTHY"

    decay_pct = None
    if backtest_expectancy and backtest_expectancy != 0:
        decay_pct = round((1.0 - live_mean / backtest_expectancy) * 100, 1)

    return {
        "verdict": verdict,
        "n": n,
        "live_expectancy": round(live_mean, 4),
        "live_sharpe": round(live_sr, 4),
        "backtest_expectancy": backtest_expectancy,
        "backtest_sharpe": backtest_sharpe,
        "expectancy_decay_pct": decay_pct,
        "psr_vs_backtest": psr_vs_backtest,
        "advice": {
            "HEALTHY": "Live edge still matches the promise — keep running.",
            "DECAYING": "Live edge is materially below backtest — investigate (regime, crowding, execution via TCA) before scaling; consider pulling size.",
            "DEAD": "No positive live edge — you are donating. Halt this config and re-evaluate.",
        }[verdict],
    }


def run_edge_health(trade_log, backtest_expectancy=None, backtest_sharpe=None,
                    window: int = 100, tail: int = 3000) -> dict:
    """Convenience: pull recent cycles from the trade log and grade the edge."""
    try:
        events = list(trade_log.tail(tail)) if hasattr(trade_log, "tail") else list(trade_log)
    except Exception:
        events = []
    return edge_health(cycle_pnls(events, window), backtest_expectancy, backtest_sharpe)
