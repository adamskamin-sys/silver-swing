"""Ralph Vince optimal-f sizing overlay (crew).

References
----------
Vince, Ralph. *The Handbook of Portfolio Mathematics: Formulas for
Optimal Allocation & Leverage*. Wiley, 2007.
    - Ch. 2 "Optimal f" — the fraction of capital that maximises TWR
      (Terminal Wealth Relative) for a given trade distribution.
    - Ch. 3 "The Geometric Average Trade" — expected geometric growth.

Vince, Ralph. *The Leverage Space Trading Model: Reconciling Portfolio
Management Strategies and Economic Theory*. Wiley, 2009.
    - Optimal f generalized to multi-asset, risk-of-ruin-constrained.

Purpose
-------
Cap the qty on any single re-entry so no one trade breaches an acceptable
risk-of-ruin probability. Sits BETWEEN sleeve.qty (the strategy's declared
size) and the actual arm — takes the lower of "strategy size" and "Vince's
optimal-f cap".

We do NOT replace Kelly (kelly.py already provides Kelly-fraction dynamic
sizing based on realised edge). Vince's optimal-f is a stricter overlay
for asymmetric distributions where Kelly can overbet — the two agree in
the symmetric limit and Vince binds tighter when the loss distribution
has fat tails.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence


# -- Optimal f from a trade P&L series (Vince 2007 Ch. 2) ------------------

def optimal_f(pnl_series: Sequence[float], resolution: int = 200) -> Optional[dict]:
    """Grid-search the fraction f ∈ (0, 1] that maximises TWR.
    TWR(f) = ∏ (1 + f * (−trade_i / worst_loss))
    where worst_loss = |min(pnl_series)|.

    Returns dict with optimal_f, twr_at_optimum, geometric_mean_hpr.
    None if series has no losing trades (Vince's optimal-f is undefined
    when there's no worst-loss reference)."""
    if not pnl_series:
        return None
    losses = [x for x in pnl_series if x < 0]
    if not losses:
        return None
    worst = abs(min(losses))
    if worst <= 0:
        return None

    best_f = 0.0
    best_twr = 1.0
    for i in range(1, resolution + 1):
        f = i / resolution
        twr = 1.0
        for x in pnl_series:
            hpr = 1.0 + f * (float(x) / worst)
            if hpr <= 0:
                twr = 0.0
                break
            twr *= hpr
        if twr > best_twr:
            best_twr = twr
            best_f = f

    n = len(pnl_series)
    geom = best_twr ** (1.0 / n) if best_twr > 0 and n > 0 else 0.0
    return {
        "optimal_f": round(best_f, 4),
        "twr_at_optimum": round(best_twr, 6),
        "geometric_mean_hpr": round(geom, 6),
        "worst_loss": round(worst, 6),
        "n_trades": n,
        "citation": "Vince 2007 HPM Ch. 2, 3",
    }


# -- Position-sizing conversion (Vince 2007 Ch. 3, HPR to contracts) -------

def contracts_at_optimal_f(equity: float, worst_loss_per_contract: float,
                           opt_f: float) -> int:
    """Convert Vince's optimal-f into a discrete contract count.

    Vince (2007 Ch. 3 eq. 3-8):
        contracts = int(equity × f / worst_loss_per_contract)

    equity                      — account equity in $
    worst_loss_per_contract     — largest historical loss per 1 contract (positive $)
    opt_f                       — optimal fraction (from optimal_f() above)
    """
    if worst_loss_per_contract <= 0 or opt_f <= 0 or equity <= 0:
        return 0
    return max(0, int(equity * opt_f / worst_loss_per_contract))


# -- Risk-of-ruin cap (Vince 2009 leverage-space) --------------------------

def ruin_probability(edge: float, win_rate: float,
                     bankroll_units: int = 20) -> float:
    """Classical gambler's ruin, adapted per Vince 2009 for a bounded
    unit-bet strategy. Answers 'what's P(ruin) if I bet fraction f=1
    of a bankroll unit each trade, given this win rate & edge?'

    We use this to reject qty proposals whose implied risk of ruin
    exceeds ~5% (Vince's canonical threshold).

    edge          — mean R per trade (units of worst_loss)
    win_rate      — historical fraction of winning trades
    bankroll_units — number of worst_loss units in the account
    """
    if not (0 < win_rate < 1):
        return 1.0
    # Bernoulli approximation of ruin probability with edge:
    #   P(ruin) = ((1 − p) / p) ** bankroll   when edge favors us (p > 0.5)
    p = win_rate
    q = 1.0 - p
    if p <= q:
        return 1.0
    try:
        return (q / p) ** bankroll_units
    except (OverflowError, ValueError):
        return 1.0


# -- Sizing overlay used by the swing_leg re-entry path --------------------

def cap_reentry_qty(strategy_qty: int, pnl_series: Sequence[float],
                    account_equity: float, worst_loss_per_contract: float,
                    max_ruin_prob: float = 0.05) -> dict:
    """Cap a strategy-proposed qty by Vince's optimal-f + risk-of-ruin gate.

    strategy_qty              — sleeve.qty (what the strategy wants)
    pnl_series                — recent per-cycle P&L (from trade log)
    account_equity            — account $ equity (portfolio snapshot)
    worst_loss_per_contract   — largest observed 1-contract loss
    max_ruin_prob             — reject if P(ruin) > this (default 5%)

    Returns dict with capped_qty, plus diagnostics for logging.
    Always returns a qty <= strategy_qty (never up-sizes)."""
    if strategy_qty <= 0:
        return {"capped_qty": 0, "reason": "strategy qty is 0"}
    if not pnl_series or worst_loss_per_contract <= 0 or account_equity <= 0:
        # No data to reason about — pass strategy qty through unchanged.
        return {"capped_qty": strategy_qty,
                "reason": "insufficient data — no cap applied",
                "vince": None}

    opt = optimal_f(pnl_series)
    if opt is None:
        return {"capped_qty": strategy_qty,
                "reason": "no losing trades in history — optimal-f undefined",
                "vince": None}
    vince_qty = contracts_at_optimal_f(account_equity,
                                       worst_loss_per_contract,
                                       opt["optimal_f"])
    capped = min(strategy_qty, vince_qty) if vince_qty > 0 else strategy_qty

    # Ruin gate — even if capped qty is low, check the resulting ruin prob.
    wins = [x for x in pnl_series if x > 0]
    win_rate = (len(wins) / len(pnl_series)) if pnl_series else 0.0
    mean_pnl = sum(pnl_series) / len(pnl_series)
    edge = mean_pnl / opt["worst_loss"] if opt["worst_loss"] > 0 else 0.0
    bankroll_units = max(1, int(account_equity / opt["worst_loss"])) if opt["worst_loss"] > 0 else 1
    ruin = ruin_probability(edge, win_rate, bankroll_units=bankroll_units)
    if ruin > max_ruin_prob:
        # Aggressive cap: halve the capped qty when ruin risk is high.
        capped = max(0, capped // 2)
    return {
        "capped_qty": capped,
        "strategy_qty": strategy_qty,
        "vince_optimal_qty": vince_qty,
        "vince": opt,
        "win_rate": round(win_rate, 3),
        "edge_R": round(edge, 3),
        "bankroll_units": bankroll_units,
        "ruin_prob": round(ruin, 4),
        "ruin_gate_tripped": ruin > max_ruin_prob,
        "reason": ("ruin gate tripped — halved" if ruin > max_ruin_prob
                   else "optimal-f cap applied"),
    }
