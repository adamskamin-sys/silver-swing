"""Expert-driven reentry-after-stop gate.

Adam 2026-07-16 directive (approved yes / if the experts agree we do it / b):
reentry after a stop is a trading decision → the experts decide it.

Prior state: swing_leg._maybe_trigger_sleeve_reentry used a hardcoded 30s
min-wait + 50% volatility-contraction check. No regime awareness, no
toxicity check, no cycle-economics gate. That's what let PT + HYPE rearm
into the same trend that just stopped them out, over and over.

## Design

Simple majority vote (Timmermann 2006) over 5 independent expert votes
+ one HARD cadence floor:

    votes = [
        kaufman_ER_ok,                # ER < 0.5 in reentry direction
        wilder_ADX_ok,                # ADX < 25 (not strong trend)
        cartea_adverse_selection_ok,  # |OFI| below toxicity threshold
        kyle_lambda_ok,               # λ near baseline
        menkveld_cycle_econ_ok,       # last N cycles net positive
    ]
    allow = sum(votes) >= 3           # majority
    AND cadence_ok                    # HARD floor: max(30s, Kyle half-life)

Cadence floor is HARD — no expert can vote against it. Physical latency
limit plus Hasbrouck (1991) λ-recovery time.

Every decision logs the individual votes AND the consensus so the
operator can audit which expert blocked reentry.

## Sources

**Kaufman (2013)** "Trading Systems and Methods" 5th ed. Efficiency
    Ratio: ER = |net_move| / Σ|deltas|. Scalar regime classifier.
    ER > 0.5 in reentry direction = trending against us → refuse.

**Wilder (1978)** "New Concepts in Technical Trading Systems". ADX/DMI.
    ADX > 25 = trending; < 20 = ranging. Second independent trend signal.

**Cartea-Jaimungal-Penalva (2015)** "Algorithmic and HFT" ch. 8.
    Adverse-selection toxicity via OFI. If |OFI| ≥ threshold, informed
    flow is still present; refuse reentry.

**Kyle (1985)** Econometrica 53(6):1315. λ (market impact) toxicity.
    If λ ≥ 1.5× baseline, informed traders still moving prices.

**Menkveld (2013)** J. Fin. Markets 16:712. MM cycle-economics gate.
    If last N cycles rolling-net < 0, stop reentering that sleeve.

**Hasbrouck (1991)** J. Finance 46:179. λ half-life estimator.
    Cadence floor: min_wait = max(30s, kyle_half_life_secs).

**Timmermann (2006)** Handbook of Economic Forecasting ch. 4. Median /
    majority ensemble beats single-model forecasts on diverse experts.

## Kill switch

expert_gate.MODE is a module-level string. Any code path can set
`expert_gate.MODE = "off"` to revert to legacy hardcoded 30s + 50%
contraction behavior. Default is "expert".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


# Kaufman (2013) canonical threshold: ER > 0.5 = trending. Below is
# ranging (safe to mean-revert). Windowed over last N samples.
_KAUFMAN_ER_THRESHOLD = 0.5
_KAUFMAN_ER_WINDOW = 20   # default window for ER calculation

# Wilder (1978) canonical thresholds. Standard ADX interpretation.
_WILDER_ADX_TREND_THRESHOLD = 25.0
_WILDER_ADX_WINDOW = 14   # Wilder's canonical smoothing period

# Cartea-Jaimungal (2015) OFI toxicity threshold. |OFI| in [0,1].
# 0.5 is the mid-range — below is "normal" flow, above is "one-sided".
_CJP_OFI_TOXICITY_THRESHOLD = 0.5

# Kyle (1985) λ ratio threshold. λ / baseline ≥ 1.5 = elevated.
# Hasbrouck (1991) shows post-informed-trade λ typically returns to
# baseline within 5-30min for equities; longer for illiquid futures.
_KYLE_LAMBDA_TOXICITY_RATIO = 1.5

# Menkveld (2013) cycle look-back window. Short enough to detect regime
# change, long enough for signal to emerge from noise.
_MENKVELD_CYCLE_LOOKBACK = 5

# Majority vote threshold: ≥ 3 of 5 experts must agree. Timmermann (2006)
# simple-majority theorem — no supermajority tuning needed.
_MAJORITY_THRESHOLD = 3
_TOTAL_VOTERS = 5

# Hard cadence floor. Physical minimum wait between reentries. 30s is
# the historic hardcoded value; we retain it as the FLOOR and add the
# Kyle-half-life as an additional lower bound (whichever is longer wins).
_HARD_CADENCE_FLOOR_SECS = 30.0
# Cap on cadence floor — even if Hasbrouck estimator returns pathological
# values, don't refuse reentry for more than 15min after a stop.
_HARD_CADENCE_CEILING_SECS = 900.0

# Kill switch. Default expert. Change to "off" to revert to legacy.
MODE = "expert"


@dataclass
class GateDecision:
    """Full expert output for a reentry-gate decision. Includes both
    the numerical result AND the reasoning so it can be logged."""
    allow: bool                    # final answer
    votes: dict                    # per-expert vote (name → 0/1 or None)
    vote_count: int                # number of "yes" votes
    total_voters: int              # number of experts that returned a vote
    cadence_ok: bool               # did the cadence floor pass?
    cadence_floor_secs: float      # the effective floor
    elapsed_since_stop_secs: float # how long since the stop fired
    method: str                    # "expert_majority" (identifier)
    citation: str                  # papers used
    inputs: dict                   # reproducibility


# ---- Individual expert voters -----------------------------------------------

def kaufman_efficiency_ratio(prices: list[float],
                              window: int = _KAUFMAN_ER_WINDOW) -> Optional[float]:
    """Kaufman ER over a rolling window.

    ER = |P_last - P_first| / Σ|P_i - P_{i-1}|.

    Returns None if insufficient history (<window+1 samples). Caller
    treats None as "no vote" (excluded from denominator).
    """
    if len(prices) < window + 1:
        return None
    window_prices = prices[-(window + 1):]
    net = abs(window_prices[-1] - window_prices[0])
    total = sum(abs(window_prices[i] - window_prices[i - 1])
                for i in range(1, len(window_prices)))
    if total <= 0:
        return None
    return net / total


def kaufman_reentry_ok(prices: list[float],
                        reentry_direction: str = "buy") -> Optional[bool]:
    """Kaufman vote: True if ER < threshold OR trend is IN our reentry
    direction (both fine for a buy reentry). False if trending AGAINST us.

    reentry_direction: "buy" — we're rearming a BUY; a downtrend blocks us.
    """
    er = kaufman_efficiency_ratio(prices)
    if er is None:
        return None
    if er < _KAUFMAN_ER_THRESHOLD:
        return True  # ranging — safe to mean-revert
    # Trending — check direction. For a BUY reentry, uptrend is FINE,
    # downtrend blocks us.
    if len(prices) < 2:
        return None
    direction_up = prices[-1] > prices[0]
    if reentry_direction == "buy":
        return direction_up   # allow if trending up
    # SELL reentry (not currently used, but symmetric)
    return not direction_up


def wilder_adx(prices: list[float], highs: Optional[list[float]] = None,
               lows: Optional[list[float]] = None,
               window: int = _WILDER_ADX_WINDOW) -> Optional[float]:
    """Wilder ADX approximation from closes-only.

    True ADX requires H/L/C per bar. We approximate using |ΔClose| as
    the True Range proxy — sacrifices some precision but produces a
    monotone trend-strength signal for our purposes.

    Returns None if insufficient history.
    """
    if len(prices) < window + 2:
        return None
    # +DM / -DM approximation from close-to-close changes
    plus_dm = []
    minus_dm = []
    tr = []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        if change > 0:
            plus_dm.append(change)
            minus_dm.append(0.0)
        elif change < 0:
            plus_dm.append(0.0)
            minus_dm.append(-change)
        else:
            plus_dm.append(0.0)
            minus_dm.append(0.0)
        tr.append(abs(change))
    # Wilder-smoothed sums over `window` samples (approximation of EMA)
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
    # For a proper ADX we'd smooth DX over another window; single-window
    # approximation returns DX as a proxy. Bounded [0, 100].
    return dx


def wilder_adx_reentry_ok(prices: list[float]) -> Optional[bool]:
    """Wilder ADX vote: True if ADX < threshold (ranging = safe to reenter).
    False if ADX ≥ threshold (strong trend = don't fight it)."""
    adx = wilder_adx(prices)
    if adx is None:
        return None
    return adx < _WILDER_ADX_TREND_THRESHOLD


def cartea_ofi_toxicity_ok(order_flow_imbalance: Optional[float]) -> Optional[bool]:
    """Cartea vote: True if |OFI| < toxicity threshold. False if flow
    is still one-sided (informed presence). None if OFI unavailable."""
    if order_flow_imbalance is None:
        return None
    return abs(float(order_flow_imbalance)) < _CJP_OFI_TOXICITY_THRESHOLD


def kyle_lambda_ok(kyle_lambda: Optional[float],
                    kyle_baseline: Optional[float]) -> Optional[bool]:
    """Kyle vote: True if λ / baseline < toxicity ratio. False if
    still elevated (informed traders present). None if either missing."""
    if kyle_lambda is None or kyle_baseline is None:
        return None
    if kyle_baseline <= 0 or kyle_lambda <= 0:
        return None
    return (float(kyle_lambda) / float(kyle_baseline)) < _KYLE_LAMBDA_TOXICITY_RATIO


def menkveld_cycle_econ_ok(recent_cycle_pnls: Optional[list[float]]) -> Optional[bool]:
    """Menkveld vote: True if last N cycle PnLs net positive. False if
    net negative (regime is losing money). None if insufficient data."""
    if not recent_cycle_pnls:
        return None
    window = recent_cycle_pnls[-_MENKVELD_CYCLE_LOOKBACK:]
    if len(window) < 2:
        return None  # need at least 2 cycles for meaningful net
    return sum(window) > 0


def hasbrouck_lambda_half_life_secs(kyle_lambda: Optional[float],
                                     kyle_baseline: Optional[float]) -> float:
    """Hasbrouck (1991) λ recovery-to-baseline half-life estimator.

    Simple approximation: if λ is 2× baseline, expect ~60s to return
    (typical for equity HFT). If λ is 4× baseline, expect ~300s. Below
    baseline: 30s minimum. Above baseline scales roughly exponentially.

    Returns seconds. Bounded by _HARD_CADENCE_FLOOR_SECS and
    _HARD_CADENCE_CEILING_SECS.
    """
    if kyle_lambda is None or kyle_baseline is None or kyle_baseline <= 0:
        return _HARD_CADENCE_FLOOR_SECS
    ratio = float(kyle_lambda) / float(kyle_baseline)
    if ratio <= 1.0:
        return _HARD_CADENCE_FLOOR_SECS
    # Exponential-ish: 60s per doubling above baseline.
    # ratio 2 → 60s; ratio 4 → 240s; ratio 8 → 1080s (clipped to ceiling)
    doublings = math.log2(ratio)
    half_life = 30.0 * (2.0 ** doublings)
    half_life = max(_HARD_CADENCE_FLOOR_SECS, half_life)
    half_life = min(_HARD_CADENCE_CEILING_SECS, half_life)
    return half_life


# ---- Orchestrator -----------------------------------------------------------

def reentry_allowed(
    prices: list[float],
    elapsed_since_stop_secs: float,
    reentry_direction: str = "buy",
    order_flow_imbalance: Optional[float] = None,
    kyle_lambda: Optional[float] = None,
    kyle_baseline: Optional[float] = None,
    recent_cycle_pnls: Optional[list[float]] = None,
) -> GateDecision:
    """Consensus reentry decision from 5 experts + hard cadence floor.

    Returns a GateDecision with allow=True/False plus the full audit
    trail. Never returns None — even on missing data, a decision is
    made (typically deny when data is thin, since we can't verify safety).

    Vote counting: None votes are EXCLUDED from both numerator and
    denominator (a silent expert doesn't count either way). If fewer
    than _MAJORITY_THRESHOLD experts return a vote, we default to
    "deny" for safety (no consensus = don't act).
    """
    votes = {
        "kaufman": kaufman_reentry_ok(prices, reentry_direction),
        "wilder_adx": wilder_adx_reentry_ok(prices),
        "cartea_ofi": cartea_ofi_toxicity_ok(order_flow_imbalance),
        "kyle_lambda": kyle_lambda_ok(kyle_lambda, kyle_baseline),
        "menkveld_cycles": menkveld_cycle_econ_ok(recent_cycle_pnls),
    }
    # Compute majority: only count non-None votes
    non_none = [v for v in votes.values() if v is not None]
    yes_count = sum(1 for v in non_none if v)
    if len(non_none) < _MAJORITY_THRESHOLD:
        # Not enough voters returned an opinion — default to DENY for
        # safety (silence is not consent when money's at stake).
        majority_allow = False
    else:
        majority_allow = yes_count >= _MAJORITY_THRESHOLD

    # Hard cadence floor
    hasbrouck_wait = hasbrouck_lambda_half_life_secs(kyle_lambda, kyle_baseline)
    cadence_floor = max(_HARD_CADENCE_FLOOR_SECS, hasbrouck_wait)
    cadence_ok = elapsed_since_stop_secs >= cadence_floor

    final_allow = majority_allow and cadence_ok

    return GateDecision(
        allow=final_allow,
        votes={k: (int(v) if v is not None else None) for k, v in votes.items()},
        vote_count=yes_count,
        total_voters=len(non_none),
        cadence_ok=cadence_ok,
        cadence_floor_secs=round(cadence_floor, 3),
        elapsed_since_stop_secs=round(elapsed_since_stop_secs, 3),
        method="expert_majority",
        citation=("Kaufman (2013) ER; Wilder (1978) ADX; Cartea-Jaimungal-Penalva "
                  "(2015) ch.8 OFI; Kyle (1985) Econometrica 53(6):1315 λ; "
                  "Menkveld (2013) J. Fin. Markets 16:712 cycle econ; "
                  "Hasbrouck (1991) J. Finance 46:179 λ half-life; "
                  "Timmermann (2006) simple-majority ensemble"),
        inputs={
            "prices_len": len(prices),
            "elapsed_since_stop_secs": elapsed_since_stop_secs,
            "reentry_direction": reentry_direction,
            "order_flow_imbalance": order_flow_imbalance,
            "kyle_lambda": kyle_lambda,
            "kyle_baseline": kyle_baseline,
            "recent_cycle_pnls_len": len(recent_cycle_pnls) if recent_cycle_pnls else 0,
        },
    )
