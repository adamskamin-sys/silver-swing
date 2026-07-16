"""Expert-driven trail distance + ratchet cadence.

Adam 2026-07-16 directive (approved yes/none/b): every trading decision
uses experts. Trail distance was previously a per-sleeve config value
or a single-formula "adaptive" (k×σ×price with hardcoded k=2.5). Neither
was a real expert consensus; both bled the same class as the PT stop.

## Design

Median-consensus over three independent trailing frameworks, floored
at Menkveld fee-safety, capped at Van Tharp risk, tightened by Ho-Stoll
inventory age. Ratchet cadence adapts to regime via Kaufman ER.

    candidates = [
        chande_atr_trail,         # Chande (1994) N × ATR chandelier exit
        wilder_sar_trail,         # Wilder (1978) parabolic SAR
        turtle_lookback_trail,    # Faith (2007) 20-day trailing low
    ]
    consensus = median(candidates)
    distance = max(consensus, fee_floor)              # HARD Menkveld
    distance = min(distance, van_tharp_cap)           # HARD Van Tharp
    distance *= ho_stoll_age_tightener                # ≤1.0 as pos ages

## Ratchet cadence (Kaufman ER-adaptive)

    ER > 0.7 (strong trend)   → ratchet every tick
    ER > 0.3 (mild trend)     → ratchet every 5 ticks
    else (ranging)            → ratchet every 15 ticks

Cadence gate is a MIN interval — the trail can only tighten (never
loosen). When conditions are calm, we don't want to nudge the trail
on every tick and get whipsawed.

## Sources

**Wilder (1978)** "New Concepts in Technical Trading Systems" ch. 4.
    Parabolic SAR — trailing stop with acceleration factor.
    Also provides ADX/DMI (used in expert_gate).

**Chande, T. (1994)** "The New Technical Trader" (Wiley). Chandelier
    Exit: trail_stop = HH - (N × ATR), N typically 2.5-3.0. Standard
    for commodity/futures trend following.

**Faith, C. (2007)** "Way of the Turtle" (McGraw-Hill). Original
    Turtle rules: exit long when price closes below 10-day (or 20-day)
    low. Position-based rather than volatility-based.

**Ho-Stoll (1981)** "Optimal dealer pricing" (J. Fin. Econ. 9:47).
    Inventory age adjustment — as position ages without exit, tighten
    the trail to encourage getting flat.

**Menkveld (2013)** J. Fin. Markets 16:712. Fee floor: same 3× round-
    trip fees / (contract_size × qty) as expert_spread + expert_stop.

**Van Tharp (2008)** "Trade Your Way to Financial Freedom" ch. 6.
    10%-of-mid sanity cap. Same as expert_stop.

**Kaufman (2013)** "Trading Systems and Methods" 5th ed. Efficiency
    Ratio for regime-adaptive cadence. Same ER as expert_gate.

**Timmermann (2006)** Handbook of Econ Forecasting ch. 4. Simple
    median ensemble — same aggregation rule as spread + stop + gate.

## Kill switch

expert_trail.MODE = "expert" | "off". Default "expert".
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Optional


# Chande (1994) canonical N × ATR. 2.75 is the standard mid-range for
# futures/commodities; 2.0 for short-term equities, 3.0 for long-term.
_CHANDE_N_ATR = 2.75

# Wilder (1978) canonical SAR acceleration bounds. Starts at 0.02,
# increases by 0.02 per new HWM, capped at 0.20.
_WILDER_SAR_ACCEL_START = 0.02
_WILDER_SAR_ACCEL_STEP = 0.02
_WILDER_SAR_ACCEL_CAP = 0.20

# Faith (2007) Turtle rule: original was 10-day low for short-term or
# 20-day low for long-term. We use 20 samples (~1-2 hours at our tick
# rate) — retail equivalent of the "long-term" rule.
_TURTLE_LOOKBACK_SAMPLES = 20

# Menkveld (2013) fee floor multiplier. Same 3× as spread + stop.
_FEE_FLOOR_MULTIPLIER = 3.0

# Van Tharp (2008) sanity cap. 10% of mid — same as expert_stop.
_SANITY_CAP_FRAC_OF_MID = 0.10

# Ho-Stoll (1981) age tightener anchors. Linear interp between:
#   age 0h  → multiplier 1.0 (no tightening)
#   age 24h → multiplier 0.5 (trail cut in half — encourage exit)
# Cap at 0.5 for older positions (never tighter than half).
_HO_STOLL_MAX_TIGHTEN_AGE_SECS = 24 * 3600.0
_HO_STOLL_MAX_TIGHTEN_FACTOR = 0.5

# Kaufman (2013) ratchet cadence thresholds. Same ER as expert_gate.
_KAUFMAN_TREND_STRONG = 0.7
_KAUFMAN_TREND_MILD = 0.3

# Cadence: ticks between ratchet updates by regime
_CADENCE_STRONG_TREND = 1     # every tick
_CADENCE_MILD_TREND = 5       # every 5 ticks
_CADENCE_RANGING = 15         # every 15 ticks

# Kill switch. Change to "off" to revert to legacy trail math.
MODE = "expert"


@dataclass
class TrailDecision:
    """Full expert output for a trail-distance decision."""
    trail_distance: float          # final $ distance below HWM
    method: str                    # "expert_consensus"
    citation: str                  # papers used
    candidates: dict               # each expert's raw candidate
    consensus: float               # median before floor/cap/age
    fee_floor: float               # Menkveld floor
    fee_floor_binding: bool
    sanity_cap: float              # Van Tharp cap
    sanity_cap_binding: bool
    age_tightener_factor: float    # ≤1.0 multiplier
    inputs: dict


# ---- Individual expert candidates -------------------------------------------

def chande_atr_trail(atr_est: float, n: float = _CHANDE_N_ATR) -> float:
    """Chande (1994) Chandelier Exit distance: N × ATR."""
    if atr_est <= 0:
        return 0.0
    return float(n) * float(atr_est)


def wilder_sar_trail(highest_high: float, current_sar: float,
                      accel_factor: float = _WILDER_SAR_ACCEL_START) -> float:
    """Wilder (1978) Parabolic SAR distance from HWM.

    SAR next = SAR + AF × (HH - SAR). Distance = HH - SAR_next.
    Acceleration factor is stateful (increases with each new HH), so
    this function assumes the caller passes the current SAR + AF.

    For a first-time call (no state), pass current_sar = HH - ATR × 2
    and accel_factor = 0.02.
    """
    if highest_high <= 0 or current_sar >= highest_high:
        return 0.0
    af = max(_WILDER_SAR_ACCEL_START, min(accel_factor, _WILDER_SAR_ACCEL_CAP))
    new_sar = current_sar + af * (highest_high - current_sar)
    dist = highest_high - new_sar
    return max(0.0, dist)


def turtle_lookback_trail(prices: list[float],
                           lookback: int = _TURTLE_LOOKBACK_SAMPLES) -> float:
    """Faith (2007) Turtle lookback trail distance.

    Distance = HH - lookback-low. When lookback low is close to HH
    (recent consolidation), trail is tight; when lookback low is far
    (recent trend up), trail is wide.

    Returns 0.0 if insufficient history.
    """
    if not prices or len(prices) < lookback:
        return 0.0
    window = prices[-lookback:]
    hh = max(window)
    ll = min(window)
    return max(0.0, hh - ll)


def ho_stoll_age_tightener(position_age_secs: float) -> float:
    """Ho-Stoll (1981) inventory age adjustment. Returns a multiplier
    ≤ 1.0 that scales the trail distance down as the position ages.

    Linear interp: age 0 → 1.0; age _HO_STOLL_MAX_TIGHTEN_AGE_SECS →
    _HO_STOLL_MAX_TIGHTEN_FACTOR. Older positions capped at max tighten.
    """
    if position_age_secs <= 0:
        return 1.0
    if position_age_secs >= _HO_STOLL_MAX_TIGHTEN_AGE_SECS:
        return _HO_STOLL_MAX_TIGHTEN_FACTOR
    frac = position_age_secs / _HO_STOLL_MAX_TIGHTEN_AGE_SECS
    return 1.0 - frac * (1.0 - _HO_STOLL_MAX_TIGHTEN_FACTOR)


def fee_floor_distance(fee_per_roundtrip: float,
                        contract_size: float,
                        qty: int) -> float:
    """Menkveld fee floor. Same formula as expert_stop.fee_floor_distance."""
    if fee_per_roundtrip <= 0 or contract_size <= 0 or qty <= 0:
        return 0.0
    return _FEE_FLOOR_MULTIPLIER * float(fee_per_roundtrip) / (float(contract_size) * int(qty))


def sanity_cap_distance(mid_price: float) -> float:
    """Van Tharp 10% cap. Same as expert_stop.sanity_cap_distance."""
    if mid_price <= 0:
        return 0.0
    return _SANITY_CAP_FRAC_OF_MID * float(mid_price)


# ---- Ratchet cadence -------------------------------------------------------

def kaufman_ratchet_cadence(prices: list[float], window: int = 20) -> int:
    """Kaufman ER-adaptive cadence. Returns the number of ticks between
    trail-updates. Strong trend = every tick; ranging = every 15 ticks.

    Returns 1 (safest — every tick) if history is insufficient.
    """
    if len(prices) < window + 1:
        return _CADENCE_STRONG_TREND
    window_prices = prices[-(window + 1):]
    net = abs(window_prices[-1] - window_prices[0])
    total = sum(abs(window_prices[i] - window_prices[i - 1])
                for i in range(1, len(window_prices)))
    if total <= 0:
        return _CADENCE_STRONG_TREND
    er = net / total
    if er > _KAUFMAN_TREND_STRONG:
        return _CADENCE_STRONG_TREND
    if er > _KAUFMAN_TREND_MILD:
        return _CADENCE_MILD_TREND
    return _CADENCE_RANGING


# ---- Orchestrator ----------------------------------------------------------

def optimal_trail_distance(
    mid_price: float,
    highest_high: float,
    atr_est: float,
    prices: list[float],
    fee_per_roundtrip: float,
    contract_size: float,
    qty: int,
    position_age_secs: float = 0.0,
    sar_current: Optional[float] = None,
    sar_accel: float = _WILDER_SAR_ACCEL_START,
) -> Optional[TrailDecision]:
    """Consensus trail distance from Chande + Wilder-SAR + Turtle experts,
    floored at Menkveld fees, capped at Van Tharp risk, tightened by
    Ho-Stoll inventory age.

    Returns None on unusable inputs (mid_price ≤ 0, atr ≤ 0) so the
    caller can fall back to legacy math.
    """
    if mid_price <= 0 or atr_est <= 0 or highest_high <= 0:
        return None

    # Independent expert votes
    cand_chande = chande_atr_trail(atr_est)
    # Wilder SAR: if caller didn't provide state, seed with Chande baseline.
    seed_sar = sar_current if sar_current is not None else (highest_high - cand_chande)
    cand_wilder = wilder_sar_trail(highest_high, seed_sar, sar_accel)
    cand_turtle = turtle_lookback_trail(prices)

    # Filter zero-candidates (insufficient data) from the median vote.
    # Median of only positive candidates is more informative than
    # medianing in a bunch of zeros.
    positive_cands = [c for c in [cand_chande, cand_wilder, cand_turtle] if c > 0]
    if not positive_cands:
        return None
    consensus = statistics.median(positive_cands)

    candidates = {
        "chande_atr": round(cand_chande, 8),
        "wilder_sar": round(cand_wilder, 8),
        "turtle_lookback": round(cand_turtle, 8),
    }

    # Menkveld hard floor
    floor = fee_floor_distance(fee_per_roundtrip, contract_size, qty)
    fee_floor_binding = False
    if consensus < floor:
        final = floor
        fee_floor_binding = True
    else:
        final = consensus

    # Van Tharp hard cap
    cap = sanity_cap_distance(mid_price)
    sanity_cap_binding = False
    if cap > 0 and final > cap:
        final = cap
        sanity_cap_binding = True

    # Ho-Stoll age tightener (multiplicative, after floor/cap)
    tightener = ho_stoll_age_tightener(position_age_secs)
    final *= tightener

    # After age-tighten, re-enforce fee floor — never let age make the
    # trail go below the mathematical break-even distance.
    if final < floor:
        final = floor
        fee_floor_binding = True

    return TrailDecision(
        trail_distance=round(final, 8),
        method="expert_consensus",
        citation=("Chande (1994) Chandelier Exit; Wilder (1978) Parabolic SAR; "
                  "Faith (2007) Turtle lookback; Ho-Stoll (1981) inventory age; "
                  "Menkveld (2013) J. Fin. Markets 16:712 fee floor; "
                  "Van Tharp (2008) 1R cap; Timmermann (2006) median ensemble"),
        candidates=candidates,
        consensus=round(consensus, 8),
        fee_floor=round(floor, 8),
        fee_floor_binding=fee_floor_binding,
        sanity_cap=round(cap, 8),
        sanity_cap_binding=sanity_cap_binding,
        age_tightener_factor=round(tightener, 6),
        inputs={
            "mid_price": mid_price,
            "highest_high": highest_high,
            "atr_est": round(atr_est, 8),
            "fee_per_roundtrip": fee_per_roundtrip,
            "contract_size": contract_size,
            "qty": qty,
            "position_age_secs": position_age_secs,
            "prices_len": len(prices),
            "sar_current": sar_current,
            "sar_accel": sar_accel,
        },
    )
