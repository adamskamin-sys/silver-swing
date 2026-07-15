"""kama.py — Kaufman's Adaptive Moving Average (KAMA).

Reference: Perry J. Kaufman, "Trading Systems and Methods" (5th ed., 2013),
Chapter 17 "Adaptive Techniques." Also "Smarter Trading" (1995).

Purpose
-------
A moving average that SPEEDS UP when the market is trending and SLOWS DOWN
when it's chopping. Perfect for a volatility-driven swing bot because it
avoids the whipsaw of a fixed-window MA in noisy sideways markets while
still catching real trend moves quickly.

Formula
-------
    ER = |close_now - close_n_bars_ago| / sum(|close_i - close_{i-1}|)
    SC = (ER * (fast_alpha - slow_alpha) + slow_alpha) ^ 2
    KAMA_now = KAMA_prev + SC * (close_now - KAMA_prev)

where:
    fast_alpha = 2 / (fast_period + 1)   (default fast_period = 2)
    slow_alpha = 2 / (slow_period + 1)   (default slow_period = 30)
    ER (Efficiency Ratio) = 0..1 — 1 = perfect trend, 0 = pure chop.

The ER is the SAME efficiency ratio we compute in regime.py. This gives
KAMA a direct interpretation: high ER → KAMA moves fast (aggressive trend
follow), low ER → KAMA hugs slow (defensive in chop).

Usage in the expert stack
-------------------------
- **Entry timing:** cross of price above KAMA in an uptrend = pullback
  buy signal (mean-reversion within the trend, per Kaufman).
- **Exit timing:** cross of price below KAMA = pullback aborted, get out.
- **Volatility gate:** when KAMA slope is near zero (chop), be more
  cautious — swing setups have lower win rate in flat markets.

Fail-safe: returns None on insufficient data. All callers must handle None
(matches the pattern in regime.py / connors.py).
"""
from __future__ import annotations

from typing import Optional, Sequence


DEFAULT_ER_PERIOD = 10
DEFAULT_FAST_PERIOD = 2
DEFAULT_SLOW_PERIOD = 30


def efficiency_ratio(prices: Sequence[float], period: int = DEFAULT_ER_PERIOD) -> Optional[float]:
    """Kaufman's Efficiency Ratio for the last `period` bars.

    Returns a value in [0, 1]. 1 = perfectly trending, 0 = pure chop.
    Returns None if there's not enough data.

    Note: regime.py computes efficiency ratio for regime detection too.
    This standalone version is here so kama() is self-contained and can
    be imported without dragging in regime's full API. The math is
    identical.
    """
    ps = [float(p) for p in (prices or []) if p is not None]
    if len(ps) <= period:
        return None
    directional = abs(ps[-1] - ps[-1 - period])
    volatility = sum(abs(ps[i] - ps[i - 1]) for i in range(-period, 0))
    if volatility <= 0:
        return None
    return min(1.0, max(0.0, directional / volatility))


def kama(prices: Sequence[float],
         er_period: int = DEFAULT_ER_PERIOD,
         fast_period: int = DEFAULT_FAST_PERIOD,
         slow_period: int = DEFAULT_SLOW_PERIOD) -> Optional[float]:
    """Compute the current KAMA value from a price series.

    Args:
        prices: recent close prices. Need >= er_period + 1 for a value.
        er_period: lookback for the efficiency ratio (default 10).
        fast_period: fastest smoothing period (default 2, per Kaufman).
        slow_period: slowest smoothing period (default 30, per Kaufman).

    Returns:
        The current KAMA value, or None if insufficient data.

    Invariant: KAMA is always between the fastest EMA and slowest EMA
    the alpha bracket allows. It cannot overshoot — it's a smoothed
    weighted average.
    """
    ps = [float(p) for p in (prices or []) if p is not None]
    if len(ps) < er_period + 1:
        return None

    fast_alpha = 2.0 / (fast_period + 1)
    slow_alpha = 2.0 / (slow_period + 1)

    # Initialize KAMA at the first computable bar (er_period).
    # Prior bars: use price itself (no smoothing yet).
    kama_val = ps[er_period]

    for i in range(er_period + 1, len(ps)):
        # Efficiency ratio over the last er_period bars ending at i.
        directional = abs(ps[i] - ps[i - er_period])
        volatility = sum(abs(ps[j] - ps[j - 1]) for j in range(i - er_period + 1, i + 1))
        if volatility <= 0:
            er = 0.0
        else:
            er = min(1.0, max(0.0, directional / volatility))
        # Smoothing constant
        sc = (er * (fast_alpha - slow_alpha) + slow_alpha) ** 2
        kama_val = kama_val + sc * (ps[i] - kama_val)

    return float(kama_val)


def kama_signal(prices: Sequence[float],
                er_period: int = DEFAULT_ER_PERIOD) -> Optional[dict]:
    """Interpret KAMA vs current price for entry/exit signals.

    Returns:
        {
            "kama": <current kama value>,
            "price": <current close>,
            "er": <current efficiency ratio, 0..1>,
            "signal": "buy" | "sell" | "hold",
            "reason": <short explanation>,
        }
        or None if insufficient data.

    Interpretation (per Kaufman):
        * price > kama AND ER > 0.3 → trend up, pullback buys favored
        * price < kama AND ER > 0.3 → trend down, avoid buys
        * ER < 0.3 → chop, no directional signal
    """
    k = kama(prices, er_period=er_period)
    if k is None:
        return None
    er = efficiency_ratio(prices, period=er_period)
    if er is None:
        return None
    ps = [float(p) for p in (prices or []) if p is not None]
    price = ps[-1]

    if er < 0.3:
        signal = "hold"
        reason = f"chop (ER={er:.2f} below 0.3 threshold)"
    elif price > k:
        signal = "buy"
        reason = f"price {price:.4f} > KAMA {k:.4f} in uptrend (ER={er:.2f})"
    else:
        signal = "sell"
        reason = f"price {price:.4f} < KAMA {k:.4f} in downtrend (ER={er:.2f})"

    return {
        "kama": round(k, 6),
        "price": round(price, 6),
        "er": round(er, 4),
        "signal": signal,
        "reason": reason,
    }
