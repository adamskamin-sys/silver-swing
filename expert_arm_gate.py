"""Expert-driven INITIAL-ENTRY regime gate.

Adam 2026-07-16 directive (approved yes/no/b): experts decide every
trading decision. This module gates every FIRST BUY on a sleeve or
primary swing — separate from expert_gate (which handles reentry
after a stop).

## Design

Six-voter SUPERMAJORITY (≥ 4 of 6). Stricter than reentry's simple
majority because we're committing fresh capital, not resuming a paused
sleeve. No cadence floor — this isn't a rearm.

    votes = [
        kaufman_ok,             # Kaufman ER regime check
        wilder_adx_ok,          # ADX < 25 (not strong trend against)
        cartea_ofi_ok,          # |OFI| below toxicity
        kyle_lambda_ok,         # λ near baseline
        connors_rsi2_ok,        # RSI(2) not overbought
        bollinger_ok,           # price not extended above mean
    ]
    allow = sum(votes) >= 4

Silence-is-deny default: if < 4 experts return a vote (insufficient
data), DENY. Silence isn't consent when committing capital.

## Sources

Four voters shared with expert_gate:
- Kaufman (2013) — Efficiency Ratio
- Wilder (1978) — ADX/DMI
- Cartea-Jaimungal-Penalva (2015) ch. 8 — OFI toxicity
- Kyle (1985) Econometrica 53(6):1315 — λ

Two voters specific to initial entry (mean-reversion signals):

**Connors, L. (2009)** "Short Term Trading Strategies That Work"
    (Trading Markets Publishing). RSI(2) — 2-period RSI. Under 30 =
    oversold (favorable buy); over 70 = overbought (unfavorable).
    We reject only extreme overbought — more permissive than Connors's
    strict "buy oversold only" rule, since our other experts already
    handle regime/direction.

**Bollinger, J. (2001)** "Bollinger on Bollinger Bands" (McGraw-Hill).
    Price ± N × σ around moving average. Buy zone: below mean. Reject
    only extended-above-mean prices (mid + 0.5σ threshold — permissive).

**Timmermann (2006)** Handbook of Econ Forecasting ch. 4. Same
    ensemble aggregation rule.

## Kill switch

expert_arm_gate.MODE = "expert" | "off". Default "expert".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


# Same thresholds as expert_gate (consistency across expert layer)
_KAUFMAN_ER_THRESHOLD = 0.5
_KAUFMAN_ER_WINDOW = 20
_WILDER_ADX_TREND_THRESHOLD = 25.0
_WILDER_ADX_WINDOW = 14
_CJP_OFI_TOXICITY_THRESHOLD = 0.5
_KYLE_LAMBDA_TOXICITY_RATIO = 1.5

# Connors (2009) RSI(2). Standard oversold = 30, overbought = 70. For
# BUY entry, we reject only overbought (permissive) — we don't require
# oversold, since other voters handle direction.
_CONNORS_RSI2_PERIOD = 2
_CONNORS_RSI2_BUY_REJECT_ABOVE = 70.0

# Bollinger (2001) band settings. Standard N=20, k=2. For BUY entry,
# reject prices > mid + 0.5σ (permissive — only extended-above extremes).
_BOLLINGER_WINDOW = 20
_BOLLINGER_BUY_REJECT_STDEV_ABOVE_MEAN = 0.5

# Supermajority: ≥ 4 of 6 experts must agree.
_SUPERMAJORITY_THRESHOLD = 4
_TOTAL_VOTERS = 6

# Kill switch. Default "expert". Change to "off" to skip the gate.
MODE = "expert"


@dataclass
class ArmGateDecision:
    """Full expert output for an initial-entry gate decision."""
    allow: bool
    votes: dict                     # per-expert vote (name → 1/0/None)
    vote_count: int                 # number of "yes" votes
    total_voters: int               # number returning a vote (excludes None)
    method: str                     # "expert_supermajority"
    citation: str
    inputs: dict


# ---- Voters shared with expert_gate ---------------------------------------

def _kaufman_efficiency_ratio(prices: list[float],
                                window: int = _KAUFMAN_ER_WINDOW) -> Optional[float]:
    if len(prices) < window + 1:
        return None
    window_prices = prices[-(window + 1):]
    net = abs(window_prices[-1] - window_prices[0])
    total = sum(abs(window_prices[i] - window_prices[i - 1])
                for i in range(1, len(window_prices)))
    if total <= 0:
        return None
    return net / total


def kaufman_arm_ok(prices: list[float],
                    arm_direction: str = "buy") -> Optional[bool]:
    """Same logic as expert_gate.kaufman_reentry_ok. For BUY entry,
    reject a strong downtrend (ER > 0.5 AND direction = down)."""
    er = _kaufman_efficiency_ratio(prices)
    if er is None:
        return None
    if er < _KAUFMAN_ER_THRESHOLD:
        return True  # ranging = safe entry regime
    if len(prices) < 2:
        return None
    direction_up = prices[-1] > prices[0]
    if arm_direction == "buy":
        return direction_up
    return not direction_up


def wilder_adx_arm_ok(prices: list[float],
                        window: int = _WILDER_ADX_WINDOW) -> Optional[bool]:
    """ADX < 25 = not strong trend → allow. Same logic as expert_gate."""
    if len(prices) < window + 2:
        return None
    plus_dm = []; minus_dm = []; tr = []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        if change > 0:
            plus_dm.append(change); minus_dm.append(0.0)
        elif change < 0:
            plus_dm.append(0.0); minus_dm.append(-change)
        else:
            plus_dm.append(0.0); minus_dm.append(0.0)
        tr.append(abs(change))
    if len(tr) < window:
        return None
    recent_plus = sum(plus_dm[-window:])
    recent_minus = sum(minus_dm[-window:])
    recent_tr = sum(tr[-window:])
    if recent_tr <= 0:
        return None
    plus_di = 100.0 * recent_plus / recent_tr
    minus_di = 100.0 * recent_minus / recent_tr
    di_sum = plus_di + minus_di
    if di_sum <= 0:
        return None
    dx = 100.0 * abs(plus_di - minus_di) / di_sum
    return dx < _WILDER_ADX_TREND_THRESHOLD


def cartea_ofi_arm_ok(order_flow_imbalance: Optional[float]) -> Optional[bool]:
    if order_flow_imbalance is None:
        return None
    return abs(float(order_flow_imbalance)) < _CJP_OFI_TOXICITY_THRESHOLD


def kyle_lambda_arm_ok(kyle_lambda: Optional[float],
                         kyle_baseline: Optional[float]) -> Optional[bool]:
    if kyle_lambda is None or kyle_baseline is None:
        return None
    if kyle_baseline <= 0 or kyle_lambda <= 0:
        return None
    return (float(kyle_lambda) / float(kyle_baseline)) < _KYLE_LAMBDA_TOXICITY_RATIO


# ---- New voters for initial entry -----------------------------------------

def connors_rsi2(prices: list[float],
                  period: int = _CONNORS_RSI2_PERIOD) -> Optional[float]:
    """Connors (2009) RSI(2) — 2-period RSI. Returns value in [0, 100].

    Standard RSI formula: RSI = 100 - (100 / (1 + RS))
    where RS = avg_gain / avg_loss over the period.

    Returns None if insufficient history (need > period + 1 samples).
    """
    if len(prices) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(len(prices) - period, len(prices)):
        change = prices[i] - prices[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        elif change < 0:
            gains.append(0.0)
            losses.append(-change)
        else:
            gains.append(0.0)
            losses.append(0.0)
    if not gains or not losses:
        return None
    avg_gain = sum(gains) / len(gains)
    avg_loss = sum(losses) / len(losses)
    if avg_loss <= 0:
        # No losses = pure up move; RSI = 100 (max overbought)
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def connors_rsi2_arm_ok(prices: list[float],
                          arm_direction: str = "buy") -> Optional[bool]:
    """Connors RSI(2) vote: reject overbought for BUY (> 70), reject
    oversold for SELL (< 30). Permissive — no requirement for
    complementary condition (don't REQUIRE oversold to buy)."""
    rsi = connors_rsi2(prices)
    if rsi is None:
        return None
    if arm_direction == "buy":
        return rsi < _CONNORS_RSI2_BUY_REJECT_ABOVE
    return rsi > (100.0 - _CONNORS_RSI2_BUY_REJECT_ABOVE)


def bollinger_position(prices: list[float],
                        window: int = _BOLLINGER_WINDOW) -> Optional[float]:
    """Returns (last_price - mean) / stdev = "how many σ above mean."

    Positive = above mean; negative = below. None if insufficient
    history or zero stdev.
    """
    if len(prices) < window:
        return None
    window_prices = prices[-window:]
    mean = sum(window_prices) / window
    var = sum((p - mean) ** 2 for p in window_prices) / window
    stdev = math.sqrt(var)
    if stdev <= 0:
        return None
    return (prices[-1] - mean) / stdev


def bollinger_arm_ok(prices: list[float],
                      arm_direction: str = "buy") -> Optional[bool]:
    """Bollinger vote: reject extended-above-mean for BUY (> +0.5σ),
    reject extended-below-mean for SELL (< -0.5σ)."""
    z = bollinger_position(prices)
    if z is None:
        return None
    if arm_direction == "buy":
        return z <= _BOLLINGER_BUY_REJECT_STDEV_ABOVE_MEAN
    return z >= -_BOLLINGER_BUY_REJECT_STDEV_ABOVE_MEAN


# ---- Orchestrator ---------------------------------------------------------

def arm_allowed(
    prices: list[float],
    arm_direction: str = "buy",
    order_flow_imbalance: Optional[float] = None,
    kyle_lambda: Optional[float] = None,
    kyle_baseline: Optional[float] = None,
) -> ArmGateDecision:
    """Consensus initial-entry decision from 6 experts + supermajority.

    Returns ArmGateDecision with allow=True/False plus full audit trail.
    Silence-is-deny: if < _SUPERMAJORITY_THRESHOLD voters return, DENY.
    """
    votes = {
        "kaufman": kaufman_arm_ok(prices, arm_direction),
        "wilder_adx": wilder_adx_arm_ok(prices),
        "cartea_ofi": cartea_ofi_arm_ok(order_flow_imbalance),
        "kyle_lambda": kyle_lambda_arm_ok(kyle_lambda, kyle_baseline),
        "connors_rsi2": connors_rsi2_arm_ok(prices, arm_direction),
        "bollinger": bollinger_arm_ok(prices, arm_direction),
    }
    non_none = [v for v in votes.values() if v is not None]
    yes_count = sum(1 for v in non_none if v)
    # Cold-start grace: if ALL voters returned None (truly no data),
    # allow the arm — this matches the existing _sleeve_trend_ok_for_buy
    # pattern which is permissive at cold start rather than stalling the
    # sleeve indefinitely. Once we have ANY data but < supermajority,
    # deny (partial data suggests something to check).
    if len(non_none) == 0:
        allow = True   # cold start — no data to base a decision on
    elif len(non_none) < _SUPERMAJORITY_THRESHOLD:
        allow = False   # partial data, insufficient consensus → deny
    else:
        allow = yes_count >= _SUPERMAJORITY_THRESHOLD

    return ArmGateDecision(
        allow=allow,
        votes={k: (int(v) if v is not None else None) for k, v in votes.items()},
        vote_count=yes_count,
        total_voters=len(non_none),
        method="expert_supermajority",
        citation=("Kaufman (2013) ER; Wilder (1978) ADX; Cartea-Jaimungal-Penalva "
                  "(2015) ch.8 OFI; Kyle (1985) Econometrica 53(6):1315 λ; "
                  "Connors (2009) RSI(2); Bollinger (2001) BB; "
                  "Timmermann (2006) supermajority ensemble"),
        inputs={
            "prices_len": len(prices),
            "arm_direction": arm_direction,
            "order_flow_imbalance": order_flow_imbalance,
            "kyle_lambda": kyle_lambda,
            "kyle_baseline": kyle_baseline,
        },
    )
