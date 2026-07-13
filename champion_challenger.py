"""Champion-challenger evaluator (crew).

Safe continuous improvement for a live money system: run candidate strategy
configs ("challengers") against the current live config ("champion") over the
SAME historical candles, judged OUT-OF-SAMPLE via walk-forward, and only ever
recommend promoting a challenger when it genuinely and robustly beats the
champion. Never auto-promotes; it produces a recommendation a human approves.

Design: strategy plumbing is INJECTED. You pass a `run_fn(cfg, candles) ->
result` where result has `.total_return`, `.max_drawdown`, and `.equity_curve`
(a list of points with `.equity`) — i.e. a backtest.BacktestResult. That keeps
this module decoupled and unit-testable; wire it to the real engine with
expert_tuner._make_trader_factory + backtest.run_backtest.

Promotion rules (a challenger is PROMOTABLE only if ALL hold):
  1. Beats the champion's mean OOS return by >= min_edge_pct (a real margin,
     not noise).
  2. Is at least as ROBUST — its worst single OOS fold is not worse than the
     champion's worst fold (no "great on average, catastrophic once").
  3. Positive mean OOS return (an edge, not just "less bad").
Anything else stays with the incumbent. Ties go to the champion (do not churn a
live system for noise).
"""

from __future__ import annotations

from statistics import mean, pstdev
from typing import Callable, Optional

import backtest_integrity as bi


def _returns_from_curve(curve) -> list[float]:
    eqs = []
    for p in curve or []:
        e = getattr(p, "equity", None)
        if e is None and isinstance(p, dict):
            e = p.get("equity")
        if e is not None:
            eqs.append(float(e))
    rets = []
    for i in range(1, len(eqs)):
        prev = eqs[i - 1]
        if prev != 0:
            rets.append((eqs[i] - prev) / abs(prev))
    return rets


def _fold_metrics(result) -> dict:
    ret = float(getattr(result, "total_return", 0.0))
    mdd = float(getattr(result, "max_drawdown", 0.0))
    curve = getattr(result, "equity_curve", None)
    sr = bi.sharpe_ratio(_returns_from_curve(curve))
    return {"return": ret, "max_dd": mdd, "sharpe": sr}


def evaluate_challengers(
    candles: list,
    configs: dict,                      # {name: cfg}
    run_fn: Callable,                   # run_fn(cfg, candles_slice) -> BacktestResult-like
    champion: str,
    n_splits: int = 4,
    embargo: int = 0,
    min_edge_pct: float = 10.0,
) -> dict:
    """Walk-forward evaluate every config out-of-sample; return a report with a
    single, conservative promotion recommendation (or None)."""
    folds = bi.walk_forward_windows(len(candles), n_splits=n_splits, embargo=embargo)
    if not folds:
        return {"error": "not enough candles for walk-forward", "recommend_promote": None}
    if champion not in configs:
        return {"error": f"champion {champion!r} not in configs", "recommend_promote": None}

    # Per config: OOS metrics across folds.
    per: dict[str, dict] = {}
    for name, cfg in configs.items():
        fold_rets, fold_srs = [], []
        for f in folds:
            s, e = f["test"]
            try:
                res = run_fn(cfg, candles[s:e])
                m = _fold_metrics(res)
            except Exception as ex:
                m = {"return": 0.0, "max_dd": 0.0, "sharpe": 0.0, "error": str(ex)}
            fold_rets.append(m["return"])
            fold_srs.append(m["sharpe"])
        per[name] = {
            "oos_mean_return": round(mean(fold_rets), 2),
            "oos_worst_fold": round(min(fold_rets), 2),
            "oos_mean_sharpe": round(mean(fold_srs), 4),
            "oos_return_std": round(pstdev(fold_rets) if len(fold_rets) > 1 else 0.0, 2),
            "folds": len(folds),
        }

    champ = per[champion]
    ranked = sorted(
        ((n, m) for n, m in per.items() if n != champion),
        key=lambda kv: kv[1]["oos_mean_return"], reverse=True,
    )

    recommendation = None
    reasons = {}
    for name, m in ranked:
        edge_pct = _pct_improvement(m["oos_mean_return"], champ["oos_mean_return"])
        beats_margin = edge_pct is not None and edge_pct >= min_edge_pct
        as_robust = m["oos_worst_fold"] >= champ["oos_worst_fold"]
        has_edge = m["oos_mean_return"] > 0
        reasons[name] = {
            "edge_vs_champion_pct": edge_pct,
            "beats_by_margin": beats_margin,
            "at_least_as_robust": as_robust,
            "positive_oos_edge": has_edge,
            "promotable": bool(beats_margin and as_robust and has_edge),
        }
        if reasons[name]["promotable"] and recommendation is None:
            recommendation = name

    return {
        "champion": champion,
        "champion_metrics": champ,
        "challengers": per,
        "assessment": reasons,
        "recommend_promote": recommendation,
        "note": ("Promote " + recommendation + " — beats champion OOS by a robust margin."
                 if recommendation else
                 "Keep the champion — no challenger robustly beat it out-of-sample."),
    }


def _pct_improvement(challenger: float, champ: float) -> Optional[float]:
    """% improvement of challenger over champion, robust near zero/negative."""
    if champ > 0:
        return round((challenger - champ) / champ * 100.0, 1)
    # champion made ~0 or lost money: any positive challenger is a large,
    # meaningful improvement; express as an absolute-dollar-based large number.
    if challenger > champ:
        return 999.0
    return -999.0
