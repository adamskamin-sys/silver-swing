"""Classic technical indicators — RSI, Bollinger Bands, MACD.

Shipped as SHADOW signals (never gate arms) so we can validate whether
they add edge on our 5s-tick timeframe before promoting to a live gate.

Sources:
  - RSI: Wilder (1978), *New Concepts in Technical Trading Systems*
  - Bollinger Bands: Bollinger (1992), *Bollinger on Bollinger Bands*
  - MACD: Appel (1979), *The Moving Average Convergence-Divergence Trading Method*

These are older single-signal indicators. Academic replications
(Brock-Lakonishok-LeBaron 1992, Sullivan-Timmermann-White 1999) find
they beat random only marginally after transaction costs — which is
why our primary stack uses their statistically-grounded modern
equivalents (Roll autocorrelation, Andersen-Bollerslev vol, multi-
horizon scoring). But they're cheap to compute and might add
confirmation value alongside VPIN / trade OFI signals; shadow
harness will tell us.
"""

from __future__ import annotations

import math
from typing import Optional


# =============================================================================
# RSI (Wilder 1978)
# =============================================================================


def compute_rsi(prices: list[float], period: int = 14) -> Optional[float]:
    """Wilder's Relative Strength Index. Returns a value in [0, 100], or
    None if insufficient data. RSI > 70 conventionally = overbought;
    RSI < 30 = oversold.

    Wilder-smoothing: after seeding with the simple average of the first
    `period` gains/losses, each new observation contributes 1/period.
    """
    if not prices or len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))
    # Seed
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # Wilder smoothing over the remainder
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def rsi_signal(rsi: Optional[float],
               oversold: float = 30.0,
               overbought: float = 70.0) -> Optional[str]:
    """Map RSI value → 'bullish' (oversold), 'bearish' (overbought),
    or None (in the middle band, no signal)."""
    if rsi is None:
        return None
    if rsi <= oversold:
        return "bullish"
    if rsi >= overbought:
        return "bearish"
    return None


# =============================================================================
# Bollinger Bands (Bollinger 1992)
# =============================================================================


def compute_bollinger_bands(prices: list[float],
                             period: int = 20,
                             num_stdev: float = 2.0) -> Optional[tuple]:
    """Return (lower, middle, upper) — middle = SMA(period), lower/upper
    = middle ± num_stdev × stdev. None if insufficient data."""
    if not prices or len(prices) < period:
        return None
    window = prices[-period:]
    mean = sum(window) / period
    var = sum((p - mean) ** 2 for p in window) / period
    stdev = math.sqrt(var)
    return (mean - num_stdev * stdev, mean, mean + num_stdev * stdev)


def bollinger_signal(price: float,
                     bands: Optional[tuple]) -> Optional[str]:
    """Map (price, bands) → 'bullish' (price ≤ lower — mean-reversion long),
    'bearish' (price ≥ upper — mean-reversion short), or None (inside)."""
    if bands is None or price is None or price <= 0:
        return None
    lower, _, upper = bands
    if price <= lower:
        return "bullish"
    if price >= upper:
        return "bearish"
    return None


# =============================================================================
# MACD (Appel 1979)
# =============================================================================


def _ema(prices: list[float], period: int) -> Optional[list[float]]:
    """Return the EMA series for the given period. None if insufficient data."""
    if not prices or len(prices) < period:
        return None
    k = 2.0 / (period + 1)
    ema = [sum(prices[:period]) / period]  # seed with SMA
    for p in prices[period:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return ema


def compute_macd(prices: list[float],
                 fast: int = 12, slow: int = 26,
                 signal_period: int = 9) -> Optional[tuple]:
    """Return (macd_line, signal_line, histogram) at the LATEST bar, or
    None if insufficient data. Uses standard EMA(12) − EMA(26) with a
    9-period EMA of the MACD as the signal line."""
    if not prices or len(prices) < slow + signal_period:
        return None
    ema_fast = _ema(prices, fast)
    ema_slow = _ema(prices, slow)
    if ema_fast is None or ema_slow is None:
        return None
    # Align tails — ema_slow is shorter (fewer entries produced)
    min_len = min(len(ema_fast), len(ema_slow))
    macd_series = [ema_fast[-min_len + i] - ema_slow[-min_len + i]
                   for i in range(min_len)]
    if len(macd_series) < signal_period:
        return None
    signal_series = _ema(macd_series, signal_period)
    if signal_series is None or not signal_series:
        return None
    macd = macd_series[-1]
    sig = signal_series[-1]
    return (macd, sig, macd - sig)


def macd_signal(macd_tuple: Optional[tuple]) -> Optional[str]:
    """Map MACD → 'bullish' (MACD > signal, histogram > 0),
    'bearish' (MACD < signal), or None. Simple crossover flag."""
    if macd_tuple is None:
        return None
    macd, sig, hist = macd_tuple
    if hist > 0 and macd > 0:
        return "bullish"
    if hist < 0 and macd < 0:
        return "bearish"
    return None
