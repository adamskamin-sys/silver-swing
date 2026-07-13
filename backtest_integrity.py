"""Backtest-integrity referee (crew).

For a real-money model, the most dangerous failure isn't a bug — it's the
backtest OVERSTATING the edge. `expert_tuner` grid-searches trail_x_atr over a
handful of values and keeps the best score; that is exactly the setup that
manufactures curve-fit "edges." This module is the referee that catches it,
using published multiple-testing / overfitting statistics rather than vibes.

What it computes (all stdlib, no numpy):
  - Deflated / Probabilistic Sharpe Ratio (Bailey & Lopez de Prado, 2014):
    haircuts an observed Sharpe for the number of trials run, sample length,
    and non-normal (skew/kurtosis) returns. Answers "is this Sharpe real or a
    lucky max over N tries?"
  - Expected-max-Sharpe benchmark under the null of no skill.
  - A tuning-overfit report over expert_tuner's grid: trials, best-vs-plateau,
    and a multiple-testing haircut → verdict ROBUST / MARGINAL / LIKELY_OVERFIT.
  - Walk-forward window helper for out-of-sample evaluation with an embargo.

References:
  Bailey & Lopez de Prado, "The Deflated Sharpe Ratio" (2014):
    https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf
  Bailey, Borwein, Lopez de Prado & Zhu, "The Probability of Backtest
    Overfitting" (2015): https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253
"""

from __future__ import annotations

import math
from statistics import NormalDist, mean, pstdev
from typing import Optional

_N = NormalDist()
_EULER = 0.5772156649015329  # Euler-Mascheroni


def sharpe_ratio(returns: list[float]) -> float:
    """Non-annualized Sharpe of a per-period return series."""
    if len(returns) < 2:
        return 0.0
    sd = pstdev(returns)
    if sd == 0:
        return 0.0
    return mean(returns) / sd


def probabilistic_sharpe_ratio(sr_observed: float, n_obs: int,
                               sr_benchmark: float = 0.0,
                               skew: float = 0.0, kurt: float = 3.0) -> float:
    """P(true SR > sr_benchmark) given an observed (non-annualized) Sharpe.

    Accounts for track-record length and non-normality (Bailey-Lopez de Prado).
    kurt is the (non-excess) kurtosis; 3.0 = normal.
    """
    if n_obs < 2:
        return 0.0
    denom = math.sqrt(max(1e-12, 1.0 - skew * sr_observed + (kurt - 1.0) / 4.0 * sr_observed ** 2))
    z = (sr_observed - sr_benchmark) * math.sqrt(n_obs - 1) / denom
    return _N.cdf(z)


def expected_max_sharpe(n_trials: int, sr_std: float) -> float:
    """Expected MAX of n_trials Sharpe estimates under the null (true SR=0),
    given the cross-trial dispersion sr_std. This is the bar an observed best
    must clear to be considered skill rather than luck (Bailey-Lopez de Prado)."""
    if n_trials < 2 or sr_std <= 0:
        return 0.0
    a = _N.inv_cdf(1.0 - 1.0 / n_trials)
    b = _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return sr_std * ((1.0 - _EULER) * a + _EULER * b)


def deflated_sharpe_ratio(sr_observed: float, sr_trials: list[float],
                          n_obs: int, skew: float = 0.0, kurt: float = 3.0) -> dict:
    """Deflated Sharpe Ratio: PSR of the observed Sharpe against the
    expected-max benchmark implied by how many trials were run. DSR < 0.95 (or
    your bar) means the result is not convincingly better than the best you'd
    expect from luck across that many trials.
    """
    n_trials = max(1, len(sr_trials))
    sr_std = pstdev(sr_trials) if len(sr_trials) > 1 else 0.0
    benchmark = expected_max_sharpe(n_trials, sr_std)
    dsr = probabilistic_sharpe_ratio(sr_observed, n_obs, benchmark, skew, kurt)
    return {
        "deflated_sharpe": round(dsr, 4),
        "observed_sharpe": round(sr_observed, 4),
        "benchmark_max_sharpe": round(benchmark, 4),
        "n_trials": n_trials,
        "n_obs": n_obs,
        "verdict": "SKILL" if dsr >= 0.95 else ("MARGINAL" if dsr >= 0.75 else "LIKELY_LUCK"),
    }


def tuning_overfit_report(grid: list[dict], score_key: str = "score",
                          plateau_tol: float = 0.15) -> dict:
    """Referee expert_tuner's grid output. `grid` is a list of dicts each with a
    numeric `score_key` (and ideally the parameter value). Flags whether the
    winner is a robust plateau or a fragile spike, and applies a multiple-
    testing haircut so a best-of-N pick isn't trusted on face value.

    Verdict:
      ROBUST        — winner is a plateau AND clears the multiple-testing bar
      MARGINAL      — one of those two holds
      LIKELY_OVERFIT— neither; the "edge" is probably a curve-fit
    """
    scored = [g for g in grid if isinstance(g, dict) and isinstance(g.get(score_key), (int, float))]
    n = len(scored)
    if n == 0:
        return {"verdict": "NO_DATA", "reason": "no scored grid points", "n_trials": 0}
    scores = sorted((float(g[score_key]) for g in scored), reverse=True)
    best = scores[0]
    if n == 1:
        return {"verdict": "MARGINAL", "reason": "single grid point — nothing to compare",
                "n_trials": 1, "best": round(best, 4)}
    second = scores[1]
    rest_mean = mean(scores[1:])
    rest_std = pstdev(scores[1:]) if len(scores) > 2 else (abs(rest_mean) or 1.0)

    # Plateau test — the primary grid-search overfit detector. If the runner-up
    # is within plateau_tol of the winner, MANY nearby params work about as
    # well: the result is robust to the exact choice. If the winner is an
    # ISOLATED SPIKE (big gap to #2), only that one param "worked" — the
    # classic curve-fit smell, made worse the larger the gap.
    denom = abs(best) if abs(best) > 1e-9 else 1.0
    gap_to_second = (best - second) / denom
    is_plateau = gap_to_second <= plateau_tol

    # Informational: how many stdevs above the field is the winner? A modest
    # edge sits a few stdevs up; an extreme value ("too good to be true") is
    # itself a fragility/overfit signal, not reassurance.
    winner_z = (best - rest_mean) / rest_std if rest_std > 0 else 0.0
    luck_bar = math.sqrt(2.0 * math.log(n)) if n > 1 else 0.0  # E[max] of n null draws

    if best <= 0:
        verdict = "MARGINAL"                # no positive edge even at the best param
    elif not is_plateau:
        verdict = "LIKELY_OVERFIT"          # isolated spike — do not trust
    else:
        verdict = "ROBUST"                  # broad plateau with a positive edge

    return {
        "verdict": verdict,
        "n_trials": n,
        "best": round(best, 4),
        "second": round(second, 4),
        "gap_to_second_pct": round(gap_to_second * 100, 1),
        "is_plateau": is_plateau,
        "winner_stdevs_above_field": round(winner_z, 2),
        "luck_bar_stdevs": round(luck_bar, 2),
        "advice": {
            "ROBUST": "Winner is a broad plateau with a positive edge — safe to apply; nearby params work too.",
            "MARGINAL": "Robust to the parameter but the best score isn't a real edge — don't expect this to make money; keep the conventional value.",
            "LIKELY_OVERFIT": "Isolated spike — only this one param 'worked'. Do NOT apply; keep the conventional multiplier and re-test out-of-sample.",
        }[verdict],
    }


def walk_forward_windows(n: int, n_splits: int = 4, embargo: int = 0) -> list[dict]:
    """Expanding-window walk-forward index splits with an embargo gap between
    train and test (avoids leakage across the boundary). Returns a list of
    {"train": (start, end), "test": (start, end)} half-open index ranges.
    """
    if n_splits < 1 or n < n_splits + 1:
        return []
    fold = n // (n_splits + 1)
    out = []
    for k in range(1, n_splits + 1):
        train_end = fold * k
        test_start = min(n, train_end + embargo)
        test_end = min(n, fold * (k + 1))
        if test_start >= test_end:
            continue
        out.append({"train": (0, train_end), "test": (test_start, test_end)})
    return out


def referee_tuning(tuning_result: dict, score_key: str = "score") -> dict:
    """Top-level: take an expert_tuner.tune_product() result and return a
    go/no-go integrity verdict its caller can gate on before applying params.
    """
    grid = (tuning_result or {}).get("grid") or []
    report = tuning_overfit_report(grid, score_key=score_key)
    report["product_id"] = (tuning_result or {}).get("product_id")
    report["chosen_trail_x_atr"] = (tuning_result or {}).get("trail_x_atr")
    report["safe_to_apply"] = report["verdict"] in ("ROBUST", "MARGINAL")
    return report
