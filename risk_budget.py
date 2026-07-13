"""Rob Carver — Systematic Trading — portfolio-level risk budgeting.

Complements Van Tharp (single-drawdown circuit breaker) + Vince Kelly (per-
sleeve sizing) with a PORTFOLIO layer: how much of the aggregate daily
volatility budget does each sleeve claim?

Carver's core insight (Systematic Trading ch. 9-10): sizing to a target
"risk units" makes strategies with wildly different underlyings truly
comparable. A 1-contract SLR sleeve (contract_size=50, ATR=$0.10 → daily
$vol ≈ $5) and a 1-contract BTC-nano sleeve (contract_size=0.01, ATR=$500
→ daily $vol ≈ $5) contribute similar risk despite the price scales being
100,000× different. Without volatility normalization, "1 contract each"
would silently make BTC dominate a 1:1 portfolio.

Formulas implemented:

1. per_contract_daily_dollar_vol(price, atr, contract_size):
   ATR × contract_size ≈ typical daily dollar move per contract.

2. contracts_for_risk_target(target_$vol, per_contract_$vol):
   contracts = target / per_contract. Rounded to integer, floored at 1
   (never zero — halt logic should own that decision).

3. sleeve_risk_contribution(sleeve, snapshot, expert_params):
   Estimated daily $ volatility this sleeve produces at its current qty.
   Used to compute the portfolio risk-budget share.

4. instrument_diversification_multiplier(correlation_matrix):
   Carver's IDM. With correlations, effective portfolio risk is less than
   the sum of individual risks. IDM = sqrt(N / (sum of correlations))
   where N is the number of sleeves. Ranges [1.0, sqrt(N)] — higher when
   sleeves are uncorrelated, 1.0 when they're perfectly correlated.

5. portfolio_risk_scale(sleeves, correlation_matrix, target_total_$vol):
   Overall scaling factor. Multiplies each sleeve's naive contract count
   by this so the aggregate risk lands near the target.

DEFAULT DISABLED (opt-in per sleeve via risk_budget_enabled). Turning this
on can WIDELY change contract counts on high-vol products — a BTC-PERP
sleeve targeting $50/day of vol might size to 10 contracts on a quiet day
and 2 contracts on a volatile day. That's the intended behavior, but it
can surprise users used to "always 1 contract."
"""

from __future__ import annotations

import math
from typing import Optional


DEFAULT_TARGET_DAILY_DOLLAR_VOL = 50.0  # per-sleeve default risk budget


def per_contract_daily_dollar_vol(
    price: float,
    atr: float,
    contract_size: float,
) -> Optional[float]:
    """Rough per-contract daily dollar vol from ATR × contract size.

    ATR is Wilder's 14-period range on 5-min candles (from expert_params).
    Multiplied by contract_size gives dollar range per contract. This is
    an approximation of daily vol (Carver uses annualized stdev but ATR
    is a decent proxy at our granularity and it's already computed).

    Returns None if any input is invalid.
    """
    if price is None or price <= 0:
        return None
    if atr is None or atr <= 0:
        return None
    if contract_size is None or contract_size <= 0:
        return None
    return atr * contract_size


def contracts_for_risk_target(
    target_dollar_vol: float,
    per_contract_dollar_vol: Optional[float],
    minimum: int = 1,
) -> int:
    """Contracts needed to hit target daily $ vol at the observed per-
    contract vol. Rounded to nearest int, floored at `minimum` (default 1
    — halt logic is the way to hit 0, not sizing).
    """
    if per_contract_dollar_vol is None or per_contract_dollar_vol <= 0:
        return max(minimum, 1)
    if target_dollar_vol <= 0:
        return max(minimum, 1)
    raw = target_dollar_vol / per_contract_dollar_vol
    return max(minimum, int(round(raw)))


def instrument_diversification_multiplier(
    correlations: list[list[float]],
) -> float:
    """Carver's IDM (Systematic Trading eq. 9).

    Diagonal correlations should be 1.0; off-diagonals in [-1, +1]. IDM
    scales the target risk UP when sleeves are diversified (aggregate is
    less than sum) — you can hold more contracts to hit the same portfolio
    vol target. Perfectly correlated → IDM = 1 (holding two doesn't help).
    Perfectly uncorrelated → IDM = sqrt(N).

    Formula: IDM = sqrt(N / (1' × C × 1))  where C is the corr matrix.
    """
    if not correlations:
        return 1.0
    n = len(correlations)
    if n == 0:
        return 1.0
    total = 0.0
    for row in correlations:
        for c in row:
            try:
                total += float(c)
            except (TypeError, ValueError):
                pass
    if total <= 0:
        return 1.0
    return math.sqrt(n / total)


def sleeve_carver_qty(
    sc,
    ss,
    snapshot: Optional[dict],
    expert_params: Optional[dict],
    target_dollar_vol: Optional[float] = None,
) -> Optional[int]:
    """Given a sleeve config + its snapshot + expert_params, return the
    Carver-recommended integer contract count. None when we lack the data
    to compute it (caller should fall back to sc.qty).

    target_dollar_vol per-sleeve defaults to DEFAULT_TARGET_DAILY_DOLLAR_VOL.
    Adam can override via sc.risk_units_target × config's dollars-per-unit.
    """
    if snapshot is None:
        return None
    price = float(snapshot.get("last_mark") or 0)
    contract_size = 0.0
    # Prefer snapshot's contract_size, fall back to expert_params if empty.
    for key in ("contract_size", "csize"):
        v = snapshot.get(key)
        if v is not None:
            try:
                contract_size = float(v)
                break
            except (TypeError, ValueError):
                pass
    atr = 0.0
    if expert_params and isinstance(expert_params, dict):
        try:
            atr = float(expert_params.get("atr") or 0)
        except (TypeError, ValueError):
            atr = 0.0
    if price <= 0 or contract_size <= 0 or atr <= 0:
        return None
    per_ct_vol = per_contract_daily_dollar_vol(price, atr, contract_size)
    if not per_ct_vol or per_ct_vol <= 0:
        return None
    tv = target_dollar_vol if target_dollar_vol is not None else DEFAULT_TARGET_DAILY_DOLLAR_VOL
    if not tv or tv <= 0:
        return None
    return contracts_for_risk_target(tv, per_ct_vol, minimum=1)


def sleeve_risk_contribution(
    qty: int,
    snapshot: Optional[dict],
    expert_params: Optional[dict],
) -> Optional[float]:
    """Estimated daily $ vol this sleeve is contributing at `qty`
    contracts. For portfolio risk display + warning when one sleeve
    dominates. Returns None when insufficient data.
    """
    if snapshot is None:
        return None
    price = float(snapshot.get("last_mark") or 0)
    contract_size = 0.0
    v = snapshot.get("contract_size")
    if v is not None:
        try:
            contract_size = float(v)
        except (TypeError, ValueError):
            contract_size = 0.0
    atr = 0.0
    if expert_params and isinstance(expert_params, dict):
        try:
            atr = float(expert_params.get("atr") or 0)
        except (TypeError, ValueError):
            atr = 0.0
    per_ct = per_contract_daily_dollar_vol(price, atr, contract_size)
    if per_ct is None:
        return None
    return per_ct * max(1, int(qty))
