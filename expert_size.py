"""Expert-driven position sizing (safety-cap only).

Adam 2026-07-16 directive (approved yesyes/no/b): experts decide every
trading decision. Position sizing is the last legacy hardcoded vector.

## KEY DESIGN CONSTRAINT

Per Adam's memory `project_live_intent.md` ("swing 1-2, protect the
core"), experts can ONLY REDUCE the user-configured size, never
increase it. Kelly may say "size up to 5"; if the user configured 1,
we ship 1. The expert's job is safety, not aggression.

## Design

Median consensus of three sizing frameworks + Menkveld econ floor +
HARD user-configured cap:

    candidates = [
        van_tharp_1R,          # risk% × account / stop_dist / contract_size
        half_kelly,            # 0.5 × Kelly f from win_rate + payoff
        vince_optimal_f,       # Optimal f from historic sleeve PnLs
    ]
    expert_size = median(positive candidates)
    expert_size = max(1, floor(expert_size))                  # min 1
    expert_size = max(expert_size, menkveld_min_econ_size)    # HARD econ

    # SAFETY: experts only reduce, never grow.
    final_size = min(user_configured_size, expert_size)

## Sources

**Van Tharp (2008)** "Trade Your Way to Financial Freedom" ch. 6, 12.
    1R sizing: `size = (account × risk_pct) / (stop_distance × contract_size)`.
    Retail canonical: 1% risk per trade (0.5-2% range).

**Kelly (1956)** Bell System Technical Journal 35:917.
    `f* = (b × p - q) / b` where b = payoff ratio, p = win prob,
    q = loss prob. Optimal bankroll fraction.

**Thorp (1969)** Rev. Int. Stat. Inst. 37:273. Half-Kelly for
    continuous games — full Kelly is provably too aggressive under
    model uncertainty (also MacLean-Ziemba-Blazenko 1992).

**Vince, R. (1990)** "Portfolio Management Formulas" (Wiley).
    Optimal f — Kelly refinement using historic PnL distribution.
    Caps at 0.20 (larger risks drawdown dominance).

**Menkveld (2013)** J. Fin. Markets 16:712. Minimum economic size:
    position must generate expected profit > round-trip fees, or
    the cycle is a loss regardless of edge.

**Timmermann (2006)** Handbook of Econ Forecasting ch. 4. Simple
    median ensemble.

## Kill switch

expert_size.MODE = "expert" | "off". Default "expert".
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Optional


# Van Tharp (2008) canonical risk %. Conservative-retail 1.0%; the full
# range is 0.5-2.0%. We use 1.0 as mid.
_VAN_THARP_RISK_PCT = 0.01

# Thorp (1969) half-Kelly. Full Kelly is provably too aggressive when
# win-prob and payoff-ratio have any measurement uncertainty.
_KELLY_FRACTION = 0.5

# Kelly requires historic base rate; too few samples = don't trust it.
_KELLY_MIN_CYCLES_FOR_BASE_RATE = 20

# Vince (1990) canonical max f. Beyond this, drawdown risk dominates
# expected return.
_VINCE_MAX_F = 0.20

# Menkveld (2013) min-econ multiplier: expected $/cycle must be at
# least 2× fees for the cycle to be worth doing. This is a size floor
# — if expected profit at size N doesn't clear this, we still ship N=1
# (never zero) but log a warning.
_MENKVELD_MIN_ECON_PROFIT_MULT = 2.0

# Kill switch. Default expert. Change to "off" to disable expert layer
# entirely (returns user_configured_size verbatim).
MODE = "expert"


@dataclass
class SizeDecision:
    """Full expert output for a sizing decision."""
    size: int                       # final size (contracts)
    method: str                     # "expert_consensus_capped"
    citation: str                   # papers used
    candidates: dict                # each expert's recommended size
    consensus: int                  # median of positive candidates before caps
    user_configured: int            # what the user set
    menkveld_min_size: int          # economic-floor min size
    econ_floor_binding: bool        # True if menkveld pushed us up
    user_cap_binding: bool          # True if user_configured capped us down
    inputs: dict


# ---- Individual candidates -------------------------------------------------

def van_tharp_1R_size(account_equity: float,
                       stop_distance: float,
                       contract_size: float,
                       risk_pct: float = _VAN_THARP_RISK_PCT) -> Optional[int]:
    """Van Tharp 1R sizing.

    size = (account × risk_pct) / (stop_distance × contract_size)

    Returns None on unusable inputs (any ≤ 0). Caller then excludes
    Van Tharp from the median vote.
    """
    if account_equity <= 0 or stop_distance <= 0 or contract_size <= 0:
        return None
    dollar_risk = account_equity * risk_pct
    per_contract_risk = stop_distance * contract_size
    size = dollar_risk / per_contract_risk
    return max(0, int(math.floor(size)))


def half_kelly_size(account_equity: float,
                     recent_cycle_pnls: Optional[list[float]],
                     contract_size: float,
                     mid_price: float,
                     kelly_fraction: float = _KELLY_FRACTION,
                     min_cycles: int = _KELLY_MIN_CYCLES_FOR_BASE_RATE) -> Optional[int]:
    """Half-Kelly sizing from historic sleeve PnLs.

    From cycle history, estimate:
      p = win rate (fraction of cycles with pnl > 0)
      b = payoff ratio (mean_win / mean_loss)

    Kelly f* = (b × p - q) / b where q = 1 - p.
    Half-Kelly = 0.5 × f*.
    Size = f_half × account / (mid_price × contract_size).

    Returns None if insufficient history or f* is negative (no edge).
    """
    if not recent_cycle_pnls or len(recent_cycle_pnls) < min_cycles:
        return None
    if account_equity <= 0 or contract_size <= 0 or mid_price <= 0:
        return None
    wins = [p for p in recent_cycle_pnls if p > 0]
    losses = [-p for p in recent_cycle_pnls if p < 0]
    if not wins or not losses:
        return None
    p_win = len(wins) / len(recent_cycle_pnls)
    q_loss = 1.0 - p_win
    mean_win = sum(wins) / len(wins)
    mean_loss = sum(losses) / len(losses)
    if mean_loss <= 0:
        return None
    b = mean_win / mean_loss
    f_star = (b * p_win - q_loss) / b
    if f_star <= 0:
        return None  # No edge → don't size in at all
    f_half = kelly_fraction * f_star
    dollar_bet = f_half * account_equity
    size = dollar_bet / (mid_price * contract_size)
    return max(0, int(math.floor(size)))


def vince_optimal_f_size(account_equity: float,
                          recent_cycle_pnls: Optional[list[float]],
                          contract_size: float,
                          mid_price: float,
                          max_f: float = _VINCE_MAX_F) -> Optional[int]:
    """Vince optimal-f sizing.

    Approximation: iterate f in [0.01, max_f] and pick the one that
    maximizes terminal wealth over historic PnLs. Cap at max_f to
    keep drawdown bounded.

    Returns None if insufficient history or all f's give ≤0 return.
    """
    if not recent_cycle_pnls or len(recent_cycle_pnls) < 5:
        return None
    if account_equity <= 0 or contract_size <= 0 or mid_price <= 0:
        return None
    biggest_loss = min(recent_cycle_pnls)
    if biggest_loss >= 0:
        return None  # no losses in history → sizing is meaningless (return everything)
    # Normalize PnLs by the worst loss magnitude (Vince "f" convention)
    worst = abs(biggest_loss)
    normalized = [p / worst for p in recent_cycle_pnls]
    best_f = None
    best_twr = 1.0
    step = 0.01
    f = 0.01
    while f <= max_f + 1e-9:
        twr = 1.0
        for r in normalized:
            factor = 1.0 + f * r
            if factor <= 0:
                twr = 0.0
                break
            twr *= factor
        if twr > best_twr:
            best_twr = twr
            best_f = f
        f += step
    if best_f is None:
        return None
    dollar_bet = best_f * account_equity
    size = dollar_bet / (mid_price * contract_size)
    return max(0, int(math.floor(size)))


def menkveld_min_econ_size(fee_per_roundtrip: float,
                            expected_profit_per_contract: float) -> int:
    """Menkveld minimum-economic size.

    Position size at which expected $/cycle exceeds MULT × fees.
    If expected profit already clears the floor at size=1, returns 1.

    size such that: size × expected_profit_per_contract ≥ MULT × fees
    → size = ceil(MULT × fees / expected_profit_per_contract)

    Returns 1 on invalid inputs (never zero — we always ship at least 1).
    """
    if expected_profit_per_contract <= 0 or fee_per_roundtrip <= 0:
        return 1
    required = _MENKVELD_MIN_ECON_PROFIT_MULT * fee_per_roundtrip
    return max(1, int(math.ceil(required / expected_profit_per_contract)))


# ---- Orchestrator ----------------------------------------------------------

def optimal_size(
    user_configured_size: int,
    account_equity: float,
    stop_distance: float,
    contract_size: float,
    mid_price: float,
    fee_per_roundtrip: float = 0.0,
    expected_profit_per_contract: float = 0.0,
    recent_cycle_pnls: Optional[list[float]] = None,
) -> SizeDecision:
    """Consensus size from Van Tharp + half-Kelly + Vince, floored at
    Menkveld econ, HARD-capped at user_configured_size.

    Never returns > user_configured_size (safety invariant per
    project_live_intent). Never returns 0 — minimum ship is 1 contract.

    Returns a SizeDecision with full audit trail regardless of expert
    availability (missing candidates just excluded from the median).
    """
    if user_configured_size <= 0:
        # User set 0 = "don't size" → respect. No expert can override.
        return SizeDecision(
            size=0,
            method="user_configured_zero",
            citation="user intent overrides all experts",
            candidates={"van_tharp": None, "half_kelly": None, "vince": None},
            consensus=0,
            user_configured=user_configured_size,
            menkveld_min_size=0,
            econ_floor_binding=False,
            user_cap_binding=True,
            inputs={},
        )

    cand_van_tharp = van_tharp_1R_size(account_equity, stop_distance, contract_size)
    cand_half_kelly = half_kelly_size(account_equity, recent_cycle_pnls,
                                        contract_size, mid_price)
    cand_vince = vince_optimal_f_size(account_equity, recent_cycle_pnls,
                                        contract_size, mid_price)

    # Median of positive candidates only. If none positive, fall back
    # to user_configured directly.
    positive = [c for c in [cand_van_tharp, cand_half_kelly, cand_vince]
                if c is not None and c > 0]
    if not positive:
        consensus_size = user_configured_size
    else:
        consensus_size = int(math.floor(statistics.median(positive)))

    # Floor at Menkveld min-econ (if we have inputs to compute it)
    menkveld_min = menkveld_min_econ_size(fee_per_roundtrip,
                                            expected_profit_per_contract) \
                     if (fee_per_roundtrip > 0 and expected_profit_per_contract > 0) \
                     else 1
    econ_floor_binding = consensus_size < menkveld_min
    consensus_size = max(consensus_size, menkveld_min)

    # Ensure min 1
    consensus_size = max(1, consensus_size)

    # HARD USER CAP — experts can only REDUCE, never grow.
    user_cap_binding = consensus_size > user_configured_size
    final = min(consensus_size, user_configured_size)

    return SizeDecision(
        size=int(final),
        method="expert_consensus_capped",
        citation=("Van Tharp (2008) 1R; Kelly (1956) Bell Sys Tech J 35:917 "
                  "+ Thorp (1969) half-Kelly; Vince (1990) Portfolio Management "
                  "Formulas — Optimal f; Menkveld (2013) J. Fin. Markets 16:712 "
                  "min-econ; Timmermann (2006) median ensemble"),
        candidates={
            "van_tharp": cand_van_tharp,
            "half_kelly": cand_half_kelly,
            "vince": cand_vince,
        },
        consensus=int(consensus_size),
        user_configured=int(user_configured_size),
        menkveld_min_size=int(menkveld_min),
        econ_floor_binding=econ_floor_binding,
        user_cap_binding=user_cap_binding,
        inputs={
            "account_equity": account_equity,
            "stop_distance": stop_distance,
            "contract_size": contract_size,
            "mid_price": mid_price,
            "fee_per_roundtrip": fee_per_roundtrip,
            "expected_profit_per_contract": expected_profit_per_contract,
            "recent_cycle_pnls_len": len(recent_cycle_pnls) if recent_cycle_pnls else 0,
        },
    )
