"""Moskowitz-Ooi-Pedersen — Time Series Momentum (JFE 2012).

The single most-cited edge in the trend literature. MOP show that an
asset's past 12-month return predicts its next-month return with pooled
t-stat = 4.34 across 58 futures. Hurst-Ooi-Pedersen (2017 JPM) replicate
over a century.

Practical implication (Kaminski-Lo 2014): stops only ADD value when
combined with a trend filter. Without one, stops cut winners and let
losers run. Our bot ships stops (Van Tharp, Le Beau chandelier,
protect-half) but has NO trend-entry filter — so this closes a real gap.

Adapted for our 5s-tick timescale: MOP's 12-month lookback is too long
for our cycle rate. Default to 30-day trailing return (well-supported by
Hurst-Ooi-Pedersen 2017 who show TS-momentum works at multiple horizons
including 1-month).

Ships as:
  1. `compute_ts_momentum(candles, lookback_days)` — the return signal
  2. `ts_momentum_signal(ret)` — bullish/bearish/neutral map
  3. `ts_momentum_ok_for_buy(candles, ...)` — gate helper for _sleeve_arm
  4. Scanner boost for positive-momentum products (mirror funding_boost)
"""

from __future__ import annotations

import math
from typing import Optional


def compute_ts_momentum(prices: list[float],
                        lookback_bars: int = 30) -> Optional[float]:
    """Log return of the last `lookback_bars` closes. Returns None if
    insufficient data.

    prices: list of close prices in chronological order (oldest first,
    newest last). lookback_bars = number of bars to look back. For our
    ~1h candles used by the scanner, 30 bars = ~30 hours of trading.
    Adjust upward (say 30 * 24 = 720 for 1h bars over 30 days) at the
    caller if longer horizons desired.
    """
    if not prices or len(prices) < lookback_bars + 1:
        return None
    start = prices[-lookback_bars - 1]
    end = prices[-1]
    if start <= 0 or end <= 0:
        return None
    return math.log(end / start)


def ts_momentum_signal(log_return: Optional[float],
                       neutral_band: float = 0.001) -> str:
    """Map log return → 'bullish' / 'bearish' / 'neutral'.

    neutral_band: |return| below this is considered non-directional. 0.1%
    default — anything smaller is noise at our tick cadence.
    """
    if log_return is None:
        return "neutral"
    if log_return > neutral_band:
        return "bullish"
    if log_return < -neutral_band:
        return "bearish"
    return "neutral"


def ts_momentum_ok_for_buy(prices: list[float],
                           lookback_bars: int = 30,
                           neutral_band: float = 0.001) -> tuple[bool, Optional[float]]:
    """MOP entry filter: BLOCK new BUY arms when trailing return is
    strongly negative. Returns (ok, log_return).

    Permissive-default: True when insufficient data. Bullish + neutral →
    allow. Bearish (log_return < -neutral_band) → block.
    """
    lr = compute_ts_momentum(prices, lookback_bars)
    if lr is None:
        return (True, None)  # permissive default
    return (lr >= -neutral_band, lr)


def scanner_boost(log_return: Optional[float],
                  max_boost: float = 0.3) -> float:
    """Return a multiplier in [1 - max_boost, 1 + max_boost] to apply to
    the scanner tile's expected $/day. MOP-positive products rank higher
    (we're a long-biased bot; trending UP is good). Symmetric on the
    downside — actively-falling products get penalized.

    log_return scaled so ±5% (i.e., ±0.05 log return over 30 bars, a
    strong signal) maps to full boost/penalty. Clamped.
    """
    if log_return is None:
        return 1.0
    scale = 0.05
    signal = max(-1.0, min(1.0, log_return / scale))
    return 1.0 + max_boost * signal
