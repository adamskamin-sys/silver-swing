"""Andrew Lo — Adaptive Markets Hypothesis — regime detection.

Lo (2004, 2017 *Adaptive Markets*) argues markets alternate between
mean-reversion regimes (efficient, oscillating) and momentum regimes
(trending, one-way pressure). A single strategy calibrated for one
regime bleeds in the other. Turtle System's own record shows this:
trend-following (Turtle) crushes momentum regimes, mean-reversion
strategies crush chop.

Our bot's default is a mean-reversion cycle machine (sell high, buy
low). When the tape is trending, mean-reversion fills into the trend
and gets steamrollered. When the tape is choppy, wide-target trend
strategies never fire.

Three regimes classified from price history:
  - mean_reversion  — low realized-return autocorrelation, high
                      oscillation, ATR-normalized range < 1.0
  - momentum        — high (positive) autocorrelation, strong trend,
                      one-way pressure (aggressor OFI persistence)
  - chop            — negative autocorrelation, high vol, no clear
                      direction (whipsaws in both directions)

Classification uses THREE features:
  1. Lag-1 autocorrelation of log returns (Roll — already computed
     in microstructure.py). Positive = trending, negative = MR / chop.
  2. ADX-lite trend strength: |mark - mean(recent_prices)| / atr.
     >1.0 = strong trend, <0.5 = chop.
  3. Realized-vol / baseline-vol ratio (from adaptive_spread.py).
     >2.0 = whipsaw / chop, ~1.0 = normal.

Applied at arm time — sleeves with regime_adaptive_enabled get behavior
overrides per regime (see swing_leg._regime_adjustments).
"""

from __future__ import annotations

import math
from typing import Optional


REGIME_MEAN_REVERSION = "mean_reversion"
REGIME_MOMENTUM = "momentum"
REGIME_CHOP = "chop"
REGIME_UNKNOWN = "unknown"


def _log_returns(prices: list[float]) -> list[float]:
    out = []
    for i in range(1, len(prices)):
        if prices[i - 1] > 0 and prices[i] > 0:
            out.append(math.log(prices[i] / prices[i - 1]))
    return out


def lag1_autocorrelation(prices: list[float]) -> Optional[float]:
    """Roll (1984) lag-1 autocorrelation of log returns. Returns None if
    insufficient data. Positive = momentum / trending. Negative = mean-
    reversion / bid-ask bounce. Near zero = random walk."""
    if len(prices) < 10:
        return None
    rets = _log_returns(prices)
    if len(rets) < 5:
        return None
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / n
    if var <= 0:
        return 0.0
    cov = sum((rets[i] - mean) * (rets[i - 1] - mean) for i in range(1, n)) / n
    return cov / var


def trend_strength(prices: list[float], atr: float) -> Optional[float]:
    """ADX-lite: |mark - mean(prices)| / ATR. Higher = stronger trend.
    Returns None if inputs insufficient."""
    if not prices or len(prices) < 5 or not atr or atr <= 0:
        return None
    mark = prices[-1]
    mean = sum(prices) / len(prices)
    return abs(mark - mean) / atr


def realized_vol_ratio(prices: list[float], atr: float) -> Optional[float]:
    """Ratio of realized log-return stdev to ATR-implied vol. >1.0 means
    current vol is elevated vs baseline; >2.0 = whipsaw territory."""
    if not prices or len(prices) < 10 or not atr or atr <= 0 or prices[-1] <= 0:
        return None
    rets = _log_returns(prices)
    if len(rets) < 5:
        return None
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / n
    if var <= 0:
        return 0.0
    realized = math.sqrt(var)
    # ATR-implied per-tick vol: atr / price approximates per-bar log-vol.
    implied = atr / prices[-1]
    if implied <= 0:
        return None
    return realized / implied


def classify_regime(
    prices: list[float],
    atr: float,
    momentum_autocorr_threshold: float = 0.15,
    chop_vol_ratio_threshold: float = 2.0,
    momentum_trend_threshold: float = 1.0,
) -> str:
    """Return one of REGIME_*. Uses the three features above with defensible
    thresholds (Lo's own studies suggest lag-1 |corr| > 0.1 is significant;
    ATR-normalized trend > 1.0 is Wilder's ADX-strong threshold)."""
    ac = lag1_autocorrelation(prices)
    ts = trend_strength(prices, atr)
    vr = realized_vol_ratio(prices, atr)
    if ac is None or ts is None:
        return REGIME_UNKNOWN
    # Chop: high vol ratio + no clear trend
    if vr is not None and vr >= chop_vol_ratio_threshold and ts < momentum_trend_threshold:
        return REGIME_CHOP
    # Momentum: positive autocorrelation AND strong trend
    if ac >= momentum_autocorr_threshold and ts >= momentum_trend_threshold:
        return REGIME_MOMENTUM
    # Default: mean-reversion (Lo's null hypothesis for our timescale)
    return REGIME_MEAN_REVERSION


def regime_adjustments(regime: str) -> dict:
    """Multipliers to apply per regime. Sleeves opt in via
    regime_adaptive_enabled. Returned dict is applied by swing_leg.

    momentum: widen spread (ride the trend), lengthen buy-trail (avoid
      false-signal bounces), reduce size on aggressive shorts.
    chop: tighten spread (grab tiny mean-reversion), shorten trail,
      reduce size (whipsaw risk).
    mean_reversion: default multipliers (no adjustment).
    """
    if regime == REGIME_MOMENTUM:
        return {
            "spread_multiplier": 1.5,       # wider — let trends breathe
            "buy_trail_multiplier": 2.0,    # require stronger bounce confirmation
            "size_multiplier": 0.75,        # smaller — trend can accelerate against us
        }
    if regime == REGIME_CHOP:
        return {
            "spread_multiplier": 0.75,      # tighter — small MR captures
            "buy_trail_multiplier": 0.5,    # smaller bounce = faster re-entry
            "size_multiplier": 0.5,         # smaller — whipsaw protection
        }
    # mean_reversion / unknown → no adjustment
    return {
        "spread_multiplier": 1.0,
        "buy_trail_multiplier": 1.0,
        "size_multiplier": 1.0,
    }
