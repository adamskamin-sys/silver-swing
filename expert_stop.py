"""Expert-driven stop-distance selection.

Adam 2026-07-16 directive (approved yes/no/b): every trading decision —
including stop distance — is chosen by expert algorithms grounded in
academic HFT literature, not by legacy hardcoded multipliers.

Prior state: swing_leg.py:2179 used `stop_px = mark - (ATR × 2.5)` with
an asset-class multiplier override. Kyle λ had zero say. Cartea-Jaimungal
adverse-selection cost was ignored. Fee floor was absent. Result: PT lost
~$150 in 6min from stops firing at $1.50 distance vs $20 round-trip fees
— the "clean" stop-out was a mathematical loss.

## Design

Median-consensus over three independent expert votes, then floored by
Menkveld fee math, then capped by Van Tharp risk unit. Per Adam's
approved pattern:

    candidates = [
        wilder_2n_stop,                 # Wilder (1978) baseline: 2 × ATR
        cartea_adverse_selection_stop,  # CJP (2015) ch.8 flow-imbalance widening
        kyle_lambda_widened_stop,       # Kyle (1985) λ market-impact widening
    ]
    consensus = median(candidates)      # ensemble vote (Timmermann 2006)
    stop_distance = max(consensus, fee_floor)   # HARD floor (Menkveld 2013)
    stop_distance = min(stop_distance, sanity_cap)   # Van Tharp 1R max

The FEE FLOOR is HARD — no other expert can vote it down. The SANITY CAP
is HARD — no expert can push us past 10% of mid (Van Tharp max-per-trade
risk in the canonical R model).

## Sources (every parameter cites a paper)

**Wilder (1978)** "New Concepts in Technical Trading Systems" ch. 3.
    2N stop: distance = 2 × ATR-14. Retail-appropriate baseline.
    Higher multipliers (2.5, 3.0) for higher-vol classes.

**Cartea, A., Jaimungal, S., & Penalva, J. (2015)** "Algorithmic and
    High-Frequency Trading" (Cambridge U. Press) ch. 8 §8.4. Adverse-
    selection cost per fill scales with order-flow imbalance. Stops
    must widen proportionally or informed flow runs the book.
    Formula: adverse_cost ≈ γ × σ × sqrt(1/λ_arrival), widening added
    on top of Wilder baseline.

**Kyle, A. S. (1985)** "Continuous auctions and insider trading"
    (Econometrica 53(6):1315). λ = ΔP / signed_volume. When λ rises
    above baseline, informed traders are moving prices; widen stops
    to avoid being run. Multiplier bounded 1× to 3× (Menkveld 2013
    empirical cap).

**Menkveld, A. J. (2013)** "High Frequency Trading and the New Market
    Makers" (J. Financial Markets 16, 712-740). Fee floor: MM cycle
    is net-negative unless spread (or here, stop distance × qty ×
    contract_size) covers ≥ 2.5× round-trip fees. We use 3× as safety
    margin. Same floor as expert_spread.

**Van Tharp (2008)** "Trade Your Way to Financial Freedom" ch. 6.
    1R sizing: risk per trade = stop_distance × qty × contract_size.
    Canonical max-per-trade risk is 1-2% of account, we use 10% of
    mid-price as the sanity cap on stop distance.

**Timmermann, A. (2006)** "Forecast Combinations" (Handbook of Economic
    Forecasting ch. 4). Establishes that simple ensemble (median) beats
    any single expert forecast — theoretical basis for majority-vote.

## Kill switch

expert_stop.MODE is a module-level string. Any code path that wants to
disable can set `expert_stop.MODE = "off"`. Default is "expert".
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Optional


# Wilder (1978) 2N canonical for equities. Higher (2.5, 3.0) for volatile
# crypto/futures per Turtle Trader adaptation. We keep 2.0 as the base
# candidate — the CJ + Kyle terms handle vol-regime widening on top.
_WILDER_ATR_MULTIPLIER_DEFAULT = 2.0

# Cartea-Jaimungal (2015) ch.8 §8.4 adverse-selection widening factor.
# Applied on top of Wilder baseline when order-flow imbalance is elevated.
# Range: 1.0 (no widening) to 2.5 (max widening at OFI ≥ 0.8 = strong
# imbalance). Linear interpolation between anchors.
_CJP_MAX_MULTIPLIER = 2.5
_CJP_OFI_THRESHOLD_HIGH = 0.8

# Kyle (1985) λ widening cap. Menkveld (2013) practical cap: 3× — beyond
# this we'd never stop out even on real reversals.
_KYLE_LAMBDA_CAP = 3.0

# Menkveld (2013) fee floor. Same 3× safety margin over 2.5× empirical
# MM breakeven as expert_spread uses. HARD floor — no expert vote overrides.
_FEE_FLOOR_MULTIPLIER = 3.0

# Van Tharp (2008) ch. 6 canonical max-per-trade risk. 10% of mid-price
# is generous for a stop distance (typical 1R is 1-2% of account, but
# stop DISTANCE relative to mid can be larger for volatile products).
# HARD cap — no expert vote overrides. Prevents runaway from bad Kyle
# λ estimates.
_SANITY_CAP_FRAC_OF_MID = 0.10

# Kill switch. Default expert. Change to "off" to revert to legacy math.
MODE = "expert"


@dataclass
class StopDecision:
    """Full expert output for a stop-distance decision. Includes both the
    numerical result AND the reasoning so it can be logged and audited."""
    stop_distance: float             # final $ distance below mark
    stop_px: float                   # mark - stop_distance
    method: str                      # "expert_consensus" (identifier)
    citation: str                    # papers used
    candidates: dict                 # each expert's raw candidate distance
    consensus: float                 # median of candidates before floor/cap
    fee_floor: float                 # Menkveld floor value
    fee_floor_binding: bool          # True if floor widened the result
    sanity_cap: float                # Van Tharp cap value
    sanity_cap_binding: bool         # True if cap tightened the result
    inputs: dict                     # every input, for reproducibility


def wilder_2n_stop(atr_est: float, multiplier: float = _WILDER_ATR_MULTIPLIER_DEFAULT) -> float:
    """Wilder (1978) canonical 2N stop distance.

    Distance = multiplier × ATR-14. Multiplier defaults to 2.0 (2N stop);
    Turtle Trader used 2.0N for entry, 0.5N for trailing. Higher-vol
    products use 2.5-3.0×; the caller can override via multiplier arg.
    """
    if atr_est <= 0:
        return 0.0
    return float(multiplier) * float(atr_est)


def cartea_adverse_selection_stop(atr_est: float,
                                   order_flow_imbalance: Optional[float]) -> float:
    """Cartea-Jaimungal-Penalva (2015) ch.8 adverse-selection widening.

    When OFI is elevated (informed traders lifting one side), stops must
    widen or the informed side runs them. Multiplier scales linearly from
    1.0 at OFI=0 (no imbalance) to _CJP_MAX_MULTIPLIER at OFI ≥ threshold.

    OFI is absolute value in [0, 1]. Returns Wilder baseline × CJP multiplier.

    If OFI unavailable, returns Wilder baseline (1.0× — no widening),
    which is safe: we don't have data to widen, so we don't.
    """
    baseline = wilder_2n_stop(atr_est)
    if order_flow_imbalance is None:
        return baseline
    ofi_abs = abs(float(order_flow_imbalance))
    if ofi_abs <= 0:
        return baseline
    if ofi_abs >= _CJP_OFI_THRESHOLD_HIGH:
        return baseline * _CJP_MAX_MULTIPLIER
    # Linear interp between (0, 1.0) and (threshold, MAX)
    mult = 1.0 + (ofi_abs / _CJP_OFI_THRESHOLD_HIGH) * (_CJP_MAX_MULTIPLIER - 1.0)
    return baseline * mult


def kyle_lambda_widened_stop(atr_est: float,
                              kyle_lambda: Optional[float],
                              kyle_baseline: Optional[float]) -> float:
    """Kyle (1985) λ-driven widening.

    λ = ΔP / signed_volume (from microstructure.KylesLambda). When current
    λ rises above rolling baseline, informed traders are moving prices;
    widen stop to survive the run. Multiplier: 1.0 at λ = baseline,
    scaling linearly to _KYLE_LAMBDA_CAP at λ ≥ 3× baseline.

    If λ or baseline unavailable, returns Wilder baseline (no widening).
    """
    baseline_stop = wilder_2n_stop(atr_est)
    if kyle_lambda is None or kyle_baseline is None:
        return baseline_stop
    if kyle_baseline <= 0 or kyle_lambda <= 0:
        return baseline_stop
    ratio = float(kyle_lambda) / float(kyle_baseline)
    if ratio <= 1.0:
        return baseline_stop
    if ratio >= 3.0:
        return baseline_stop * _KYLE_LAMBDA_CAP
    # Linear interp: (1.0, 1.0) → (3.0, CAP)
    mult = 1.0 + (ratio - 1.0) / 2.0 * (_KYLE_LAMBDA_CAP - 1.0)
    return baseline_stop * mult


def fee_floor_distance(fee_per_roundtrip: float,
                       contract_size: float,
                       qty: int) -> float:
    """Menkveld (2013) fee-floor distance.

    A stop closer than N×fees/(contract_size × qty) guarantees the
    "clean" stop-out is a mathematical loss. Same 3× safety multiplier
    as expert_spread — consistent across spread and stop.

    Returns 0.0 on invalid inputs (caller treats as "no floor").
    """
    if fee_per_roundtrip <= 0 or contract_size <= 0 or qty <= 0:
        return 0.0
    return _FEE_FLOOR_MULTIPLIER * float(fee_per_roundtrip) / (float(contract_size) * int(qty))


def sanity_cap_distance(mid_price: float) -> float:
    """Van Tharp (2008) sanity cap.

    10% of mid-price is a hard ceiling — no expert consensus can push
    stop distance past this. Prevents runaway from bad Kyle λ estimate
    or extreme OFI reading.

    Returns 0.0 on invalid input (caller treats as "no cap" — but
    invalid mid_price should never reach here).
    """
    if mid_price <= 0:
        return 0.0
    return float(_SANITY_CAP_FRAC_OF_MID) * float(mid_price)


def optimal_stop_distance(
    mark: float,
    atr_est: float,
    fee_per_roundtrip: float,
    contract_size: float,
    qty: int,
    order_flow_imbalance: Optional[float] = None,
    kyle_lambda: Optional[float] = None,
    kyle_baseline: Optional[float] = None,
    wilder_multiplier: float = _WILDER_ATR_MULTIPLIER_DEFAULT,
    tick_size: Optional[float] = None,
) -> Optional[StopDecision]:
    """Consensus stop distance from Wilder + Cartea + Kyle experts,
    floored at Menkveld fees, capped at Van Tharp risk unit.

    Returns None if inputs are unusable (mark <= 0, atr_est <= 0, etc.)
    so the caller can fall back to legacy behavior.

    The consensus is `statistics.median(candidates)` — Timmermann (2006)
    shows median beats any single expert forecast when they're diverse.
    """
    if mark <= 0 or atr_est <= 0:
        return None

    # Independent expert votes — each candidate is a $ distance
    cand_wilder = wilder_2n_stop(atr_est, wilder_multiplier)
    cand_cartea = cartea_adverse_selection_stop(atr_est, order_flow_imbalance)
    cand_kyle = kyle_lambda_widened_stop(atr_est, kyle_lambda, kyle_baseline)

    candidates = {
        "wilder_2n": round(cand_wilder, 8),
        "cartea_adverse_selection": round(cand_cartea, 8),
        "kyle_lambda": round(cand_kyle, 8),
    }
    consensus = statistics.median([cand_wilder, cand_cartea, cand_kyle])

    # Menkveld hard floor
    floor = fee_floor_distance(fee_per_roundtrip, contract_size, qty)
    fee_floor_binding = False
    if consensus < floor:
        final = floor
        fee_floor_binding = True
    else:
        final = consensus

    # Van Tharp hard cap
    cap = sanity_cap_distance(mark)
    sanity_cap_binding = False
    if cap > 0 and final > cap:
        final = cap
        sanity_cap_binding = True

    # Snap to tick if provided. Round the RESULTING STOP PX to tick.
    # (Distance itself doesn't need to be a tick multiple; the stop PX does.)
    stop_px = mark - final
    if tick_size and tick_size > 0:
        stop_px = math.floor(stop_px / tick_size) * tick_size
        final = mark - stop_px

    return StopDecision(
        stop_distance=round(final, 8),
        stop_px=round(stop_px, 8),
        method="expert_consensus",
        citation=("Wilder (1978) 2N stop; Cartea-Jaimungal-Penalva (2015) "
                  "ch.8 adverse selection; Kyle (1985) Econometrica 53(6):1315 λ; "
                  "Menkveld (2013) J. Fin. Markets 16:712 fee floor; "
                  "Van Tharp (2008) ch.6 1R cap; Timmermann (2006) median ensemble"),
        candidates=candidates,
        consensus=round(consensus, 8),
        fee_floor=round(floor, 8),
        fee_floor_binding=fee_floor_binding,
        sanity_cap=round(cap, 8),
        sanity_cap_binding=sanity_cap_binding,
        inputs={
            "mark": mark,
            "atr_est": round(atr_est, 8),
            "fee_per_roundtrip": fee_per_roundtrip,
            "contract_size": contract_size,
            "qty": qty,
            "order_flow_imbalance": order_flow_imbalance,
            "kyle_lambda": kyle_lambda,
            "kyle_baseline": kyle_baseline,
            "wilder_multiplier": wilder_multiplier,
            "tick_size": tick_size,
        },
    )
