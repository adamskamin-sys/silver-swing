"""yang_zhang.py — Yang-Zhang volatility estimator (OHLC-based).

References
----------
Yang, Dennis, and Qiang Zhang. "Drift-Independent Volatility Estimation
Based on High, Low, Open, and Close Prices." *Journal of Business*,
Vol. 73, No. 3, 2000, pp. 477-491.

Purpose
-------
The BEST volatility estimator for OHLC bar data. It combines three sources:

    - Overnight variance (close-to-open)
    - Open-to-close variance (Rogers-Satchell drift-independent estimator)
    - Close-to-close variance (traditional)

Yang-Zhang is provably 7-14× MORE EFFICIENT than close-to-close volatility
(the standard ATR-14 uses just close-to-close). More efficient = less
noisy vol estimate = better stop sizing, better position sizing, better
regime detection.

For a swing bot that trades volatility, using a better vol estimate
directly improves:
    - Stop distances (fewer premature stop-outs from noise)
    - Position sizing (Van Tharp 1R correctly scaled)
    - Spread widths (Bollinger-like envelopes fit the true regime)
    - Regime classification (calm vs stressed thresholds crisper)

Formula (annualized volatility)
-------------------------------
    σ_YZ² = σ_overnight² + k·σ_open_to_close² + (1-k)·σ_rogers_satchell²

where:
    k = 0.34 / (1.34 + (N+1)/(N-1))   (per Yang-Zhang paper, minimizes est. variance)
    N = number of periods

    σ_overnight² = Σ(ln(O_i / C_{i-1}))² / (N-1)
    σ_open_to_close² = Σ(ln(C_i / O_i))² / (N-1)
    σ_rogers_satchell² = (1/N) Σ [ln(H_i/C_i)·ln(H_i/O_i) + ln(L_i/C_i)·ln(L_i/O_i)]

Usage in the expert stack
-------------------------
- Wherever we currently use ATR-14 for stop/trail/spread sizing, YZ
  volatility is a strictly better input (though on the same scale requires
  a small unit conversion — ATR is dollar-denominated, YZ is log-return).
- To convert: YZ_dollar ≈ YZ_return × current_price × sqrt(bars_per_hold)
- Best usage: alongside ATR (not replacing it) — YZ for POSITION SIZING
  and REGIME, ATR for STOP LEVELS (users read stops in dollars).

Fail-safe: returns None on insufficient/malformed OHLC data.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence


def _bar_ohlc(bar) -> Optional[tuple[float, float, float, float]]:
    """Extract (open, high, low, close) from a bar. Returns None if invalid."""
    try:
        if isinstance(bar, dict):
            o = float(bar.get("open", 0))
            h = float(bar.get("high", 0))
            l = float(bar.get("low", 0))
            c = float(bar.get("close", 0))
        else:
            o = float(getattr(bar, "open", 0))
            h = float(getattr(bar, "high", 0))
            l = float(getattr(bar, "low", 0))
            c = float(getattr(bar, "close", 0))
        if min(o, h, l, c) <= 0:
            return None
        return (o, h, l, c)
    except (TypeError, ValueError, AttributeError):
        return None


def yang_zhang_variance(bars: Sequence) -> Optional[float]:
    """Compute Yang-Zhang variance from an OHLC bar sequence.

    Returns:
        Yang-Zhang variance (in units of log-return²), or None if <2 valid bars.

    Take sqrt() for volatility (standard deviation of log returns per bar).
    """
    ohlcs = [_bar_ohlc(b) for b in bars]
    ohlcs = [x for x in ohlcs if x is not None]
    n = len(ohlcs)
    if n < 2:
        return None

    # Overnight variance: close_{i-1} to open_i
    sum_overnight = 0.0
    # Open-to-close: within-bar drift
    sum_open_to_close = 0.0
    # Rogers-Satchell: drift-independent within-bar variance
    sum_rs = 0.0

    for i in range(1, n):
        o_prev, _, _, c_prev = ohlcs[i - 1]
        o, h, l, c = ohlcs[i]

        # Overnight return
        overnight = math.log(o / c_prev)
        sum_overnight += overnight ** 2

        # Open-to-close return
        oc = math.log(c / o)
        sum_open_to_close += oc ** 2

        # Rogers-Satchell contribution
        rs_bar = math.log(h / c) * math.log(h / o) + math.log(l / c) * math.log(l / o)
        sum_rs += rs_bar

    m = n - 1  # number of valid bar-to-bar comparisons
    if m <= 0:
        return None

    sigma_overnight_sq = sum_overnight / m
    sigma_oc_sq = sum_open_to_close / m
    sigma_rs_sq = sum_rs / m

    # Yang-Zhang k weight (minimizes variance of the estimator)
    k = 0.34 / (1.34 + (m + 1) / max(1, m - 1))

    yz_variance = sigma_overnight_sq + k * sigma_oc_sq + (1 - k) * sigma_rs_sq
    return max(0.0, yz_variance)  # clamp — floating-point can yield tiny negatives


def yang_zhang_volatility(bars: Sequence,
                          annualization: Optional[float] = None) -> Optional[float]:
    """Yang-Zhang volatility = sqrt(variance), optionally annualized.

    Args:
        bars: OHLC bar sequence.
        annualization: multiplier to convert per-bar vol to annualized
            (e.g., sqrt(252 * bars_per_day) for daily). None = per-bar.

    Returns:
        Volatility in log-return space (multiply by price × sqrt(N) to get
        dollar-space estimate for N-bar holding period), or None.
    """
    v = yang_zhang_variance(bars)
    if v is None:
        return None
    vol = math.sqrt(v)
    if annualization is not None and annualization > 0:
        vol = vol * math.sqrt(annualization)
    return round(vol, 8)


def yz_vs_atr_assessment(bars: Sequence, atr_14: Optional[float]) -> Optional[dict]:
    """Compare Yang-Zhang volatility to a traditional ATR-14 to detect
    when the ATR is UNDERSTATING true volatility (a known ATR failure mode
    when overnight gaps dominate).

    Args:
        bars: OHLC bars (need ≥ 15 for ATR-14 comparison).
        atr_14: the existing dollar-space ATR-14 value (from Wilder ATR).

    Returns:
        {
            "yz_vol_return": <log-return space>,
            "yz_vol_dollar": <dollar equivalent, approx>,
            "atr_14_dollar": <passed value>,
            "yz_over_atr_ratio": <yz$ / atr>,
            "assessment": <"atr_understates_vol" | "atr_ok" | "atr_overstates_vol">,
            "reason": <explanation>,
        }
        or None if insufficient data.

    Interpretation:
        * ratio > 1.5 → ATR is understating (overnight gaps or intraday
          extremes are being missed). Use YZ for sizing.
        * 0.7 <= ratio <= 1.5 → ATR is reasonable.
        * ratio < 0.7 → ATR is overstating (rare — usually one big spike
          inflating ATR that's already dissipated). Use YZ for sizing.
    """
    ohlcs = [_bar_ohlc(b) for b in bars]
    ohlcs = [x for x in ohlcs if x is not None]
    if len(ohlcs) < 15 or not atr_14 or atr_14 <= 0:
        return None

    yz_vol_return = yang_zhang_volatility(bars)
    if yz_vol_return is None:
        return None

    # Approximate dollar-space YZ using current price and 1-bar holding.
    current_price = ohlcs[-1][3]  # last close
    yz_vol_dollar = yz_vol_return * current_price

    ratio = yz_vol_dollar / atr_14 if atr_14 > 0 else 0.0

    if ratio > 1.5:
        assessment = "atr_understates_vol"
        reason = (f"Yang-Zhang dollar-vol {yz_vol_dollar:.4f} is {ratio:.2f}× ATR-14 "
                  f"{atr_14:.4f} — ATR is missing overnight-gap variance. Use YZ.")
    elif ratio >= 0.7:
        assessment = "atr_ok"
        reason = f"YZ {yz_vol_dollar:.4f} ≈ ATR {atr_14:.4f} (ratio {ratio:.2f}) — either OK."
    else:
        assessment = "atr_overstates_vol"
        reason = (f"YZ {yz_vol_dollar:.4f} is only {ratio:.2f}× ATR {atr_14:.4f} — "
                  "ATR inflated by a single dissipated spike. Use YZ.")

    return {
        "yz_vol_return": round(yz_vol_return, 8),
        "yz_vol_dollar": round(yz_vol_dollar, 6),
        "atr_14_dollar": round(atr_14, 6),
        "yz_over_atr_ratio": round(ratio, 3),
        "assessment": assessment,
        "reason": reason,
    }
