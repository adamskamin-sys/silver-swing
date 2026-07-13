"""Larry Connors & Cesar Alvarez statistical mean-reversion signals (crew).

References
----------
Connors, Larry & Cesar Alvarez. *Short Term Trading Strategies That Work*.
TradingMarkets Publishing Group, 2008.
    - Ch. 2 "The 2-Period RSI" — RSI-2 as the highest-signal-to-noise
      short-term oscillator; buy zones documented at RSI-2 < 5.
    - Ch. 6 "TPS (Time, Price, Scale-in)".

Connors, Larry & Cesar Alvarez. *High Probability ETF Trading*.
TradingMarkets, 2009.
    - Ch. 4 "IBS Internal Bar Strength" — (close-low)/(high-low).
      Historical hit-rate documentation for IBS < 0.2 buys.

Purpose
-------
Give re-entry a *statistical* target instead of a fixed price offset. Today
the bot places buy_px at some ATR-derived offset from a reference; when the
reference is above the last sell, we buy back HIGHER than we sold. This
module converts recent-bar internals (IBS, RSI-2) into a bounce-probability
score and a buy_px anchored to the OU-implied mean-reversion band (Chan) —
so the buy target is placed at a level the data says is statistically
oversold, not at a place the state machine thinks looks like a range floor.

Key metrics
-----------
- IBS  = (close − low) / (high − low)     ∈ [0, 1].  < 0.2 = oversold.
- RSI-2 = 2-period RSI                    ∈ [0, 100]. < 5   = deep oversold.
- cumRSI = sum of last 2 daily RSI-2s     — Connors' STTSTW filter.
"""
from __future__ import annotations

from typing import Optional, Sequence


# -- IBS: Internal Bar Strength --------------------------------------------

def ibs(high: float, low: float, close: float) -> Optional[float]:
    """Internal Bar Strength — where in the bar's range did we close?
    < 0.2 = near the low (oversold); > 0.8 = near the high.
    Returns None on a zero-range bar (no info)."""
    if high == low:
        return None
    return (float(close) - float(low)) / (float(high) - float(low))


def ibs_from_closes(closes: Sequence[float], window: int = 1) -> Optional[float]:
    """When only closes are available, approximate IBS from the last-N-close
    range. Useful for the tick-stream we get from a live feed where we don't
    always have real high/low bars."""
    if len(closes) < window + 1:
        return None
    tail = closes[-(window + 1):]
    hi = max(tail)
    lo = min(tail)
    if hi == lo:
        return None
    return (float(tail[-1]) - lo) / (hi - lo)


# -- RSI-2 (Wilder's RSI, 2-period; Connors STTSTW Ch. 2) -------------------

def rsi(prices: Sequence[float], period: int = 2) -> Optional[float]:
    """Wilder's RSI. Connors' variant uses period=2 for maximum short-term
    signal — the deepest oversold zones (RSI-2 < 5) have documented +EV
    for 1-5 day bounces in STTSTW."""
    if len(prices) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = float(prices[i]) - float(prices[i - 1])
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    # Wilder smoothing for the remaining bars
    for i in range(period + 1, len(prices)):
        d = float(prices[i]) - float(prices[i - 1])
        g = max(d, 0.0)
        l = max(-d, 0.0)
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def cum_rsi2(prices: Sequence[float]) -> Optional[float]:
    """Sum of the last two RSI-2 values. Connors uses cumRSI < 35 in
    STTSTW Ch. 4 as an additional buy filter — it's an autocorrelation
    check that ensures the oversold is not a one-bar spike."""
    if len(prices) < 5:
        return None
    r_now = rsi(prices, 2)
    r_prev = rsi(prices[:-1], 2)
    if r_now is None or r_prev is None:
        return None
    return r_now + r_prev


# -- Composite oversold score ----------------------------------------------

def bounce_probability(prices: Sequence[float]) -> dict:
    """Combined Connors score — is now statistically a good time to buy?
    Returns:
      score      — 0 to 100, higher = stronger oversold (Connors composite)
      ibs        — internal bar strength approximation
      rsi2       — 2-period RSI
      cum_rsi2   — sum of last two RSI-2
      buy_zone   — True if score >= 60 (Connors "high-probability" zone)
    """
    ibs_val = ibs_from_closes(prices, window=5)
    rsi2 = rsi(prices, 2)
    crsi = cum_rsi2(prices)

    score = 0.0
    parts: dict[str, float] = {}
    # IBS component (0..40 points). IBS < 0.2 = full 40.
    if ibs_val is not None:
        if ibs_val <= 0.2:
            parts["ibs"] = 40.0
        elif ibs_val <= 0.5:
            parts["ibs"] = 40.0 * (0.5 - ibs_val) / 0.3
        else:
            parts["ibs"] = 0.0
        score += parts["ibs"]
    # RSI-2 component (0..40 points). RSI-2 < 5 = full 40.
    if rsi2 is not None:
        if rsi2 <= 5:
            parts["rsi2"] = 40.0
        elif rsi2 <= 25:
            parts["rsi2"] = 40.0 * (25 - rsi2) / 20.0
        else:
            parts["rsi2"] = 0.0
        score += parts["rsi2"]
    # cumRSI component (0..20 points). cumRSI < 35 = full 20.
    if crsi is not None:
        if crsi <= 35:
            parts["cum_rsi2"] = 20.0
        elif crsi <= 70:
            parts["cum_rsi2"] = 20.0 * (70 - crsi) / 35.0
        else:
            parts["cum_rsi2"] = 0.0
        score += parts["cum_rsi2"]

    return {
        "score": round(score, 1),
        "ibs": round(ibs_val, 3) if ibs_val is not None else None,
        "rsi2": round(rsi2, 1) if rsi2 is not None else None,
        "cum_rsi2": round(crsi, 1) if crsi is not None else None,
        "parts": parts,
        "buy_zone": score >= 60.0,
        "citation": "Connors 2008 STTSTW Ch. 2, 4; 2009 HPETF Ch. 4",
    }


def suggest_buy_px(prices: Sequence[float], mean_reversion_band_center: float,
                   band_width: float) -> dict:
    """Convert Connors signal into a concrete buy_px suggestion.
    Places the buy_px LOWER within the OU band the deeper the oversold,
    letting the statistical signal shape entry price — not a fixed offset.

    mean_reversion_band_center = OU-implied bounce-target (from Chan)
    band_width                 = 2 × band_std (Chan bounce band)

    Returns dict with suggested_buy_px, offset_from_center, plus the raw
    Connors score for logging."""
    bp = bounce_probability(prices)
    score = bp.get("score", 0.0) or 0.0
    # score 0..100 → offset 0..0.5 × band_width below center. A very strong
    # oversold reading anchors the buy target lower in the band; a weak
    # reading anchors it near the center.
    aggressiveness = min(max(score, 0.0), 100.0) / 100.0
    offset = 0.5 * band_width * aggressiveness
    buy_px = float(mean_reversion_band_center) - offset
    return {
        "suggested_buy_px": round(buy_px, 6),
        "offset_below_center": round(offset, 6),
        "band_center": float(mean_reversion_band_center),
        "band_width": float(band_width),
        "connors": bp,
    }
