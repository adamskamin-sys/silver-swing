"""Market-regime detection (crew).

The research was explicit: trend-following and stops add value in MOMENTUM
regimes and hurt in mean-reverting / chop (Kaminski-Lo). Now that the bot has an
MOP-2012 trend filter, it should CONDITION on the regime instead of running the
same way in every market. This classifies the current state per product from its
recent price series, using estimators (no ML, all stdlib):

  - Hurst exponent (variance-of-lagged-differences slope): >0.5 trending,
    ~0.5 random walk, <0.5 mean-reverting.
  - Lag-1 autocorrelation of returns: positive = momentum, negative = reversion.
  - Realized-vol state vs its own recent history: calm / normal / stressed.

Read-only. Returns a label the strategy (or a human) can gate on.
"""

from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Optional


def _closes(candles) -> list[float]:
    out = []
    for c in candles or []:
        v = getattr(c, "close", None)
        if v is None and isinstance(c, dict):
            v = c.get("close")
        if v is not None:
            out.append(float(v))
    return out


def hurst_exponent(prices: list[float]) -> Optional[float]:
    """Slope of log(std of lag-k differences) vs log(k). ~0.5 random walk,
    >0.5 trending/persistent, <0.5 mean-reverting/anti-persistent."""
    p = [float(x) for x in prices if x]
    n = len(p)
    if n < 40:
        return None
    max_lag = min(20, n // 2)
    xs, ys = [], []
    for lag in range(2, max_lag):
        diffs = [p[i + lag] - p[i] for i in range(n - lag)]
        if len(diffs) < 2:
            continue
        sd = pstdev(diffs)
        if sd <= 0:
            continue
        xs.append(math.log(lag))
        ys.append(math.log(sd))
    if len(xs) < 3:
        return None
    mx, my = mean(xs), mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(len(xs))) / den


def autocorrelation(series: list[float], lag: int = 1) -> Optional[float]:
    s = [float(x) for x in series]
    n = len(s)
    if n < lag + 3:
        return None
    m = mean(s)
    den = sum((x - m) ** 2 for x in s)
    if den == 0:
        return None
    num = sum((s[i] - m) * (s[i - lag] - m) for i in range(lag, n))
    return num / den


def efficiency_ratio(prices: list[float], period: int = 30) -> Optional[float]:
    """Kaufman's Efficiency Ratio: |net move| / sum(|bar moves|) over `period`.
    ~1.0 = a clean directional trend (every step in the same direction), ~0.0 =
    chop (lots of motion, no progress). This is the robust trend/chop
    discriminator — unlike the diffs-Hurst, it fires on deterministic trends."""
    p = [float(x) for x in prices if x]
    if len(p) < period + 1:
        return None
    seg = p[-(period + 1):]
    net = abs(seg[-1] - seg[0])
    path = sum(abs(seg[i] - seg[i - 1]) for i in range(1, len(seg)))
    if path <= 0:
        return None
    return net / path


def _returns(prices: list[float]) -> list[float]:
    return [(prices[i] - prices[i - 1]) / prices[i - 1]
            for i in range(1, len(prices)) if prices[i - 1]]


def classify_regime(candles, vol_lookback: int = 200) -> dict:
    """Classify the current regime from a candle series (oldest -> newest)."""
    closes = _closes(candles)
    if len(closes) < 40:
        return {"regime": "unknown", "note": "need >= 40 candles"}
    h = hurst_exponent(closes)
    rets = _returns(closes)
    ac1 = autocorrelation(rets, 1)

    # Volatility state: recent realized vol vs its own longer history.
    recent_vol = pstdev(rets[-min(len(rets), 30):]) if len(rets) >= 5 else 0.0
    hist = rets[-min(len(rets), vol_lookback):]
    hist_vol = pstdev(hist) if len(hist) >= 5 else recent_vol
    vol_ratio = (recent_vol / hist_vol) if hist_vol > 0 else 1.0
    vol_state = "stressed" if vol_ratio >= 1.5 else ("calm" if vol_ratio <= 0.6 else "normal")

    # Efficiency Ratio (Kaufman) — the primary trend/chop discriminator.
    er = efficiency_ratio(closes, min(len(closes) - 1, 30))

    # Regime from Efficiency Ratio + Hurst + autocorrelation agreement.
    trend_votes = 0
    if er is not None and er >= 0.40:      # clean directional move
        trend_votes += 2                   # ER is the strongest single signal
    if h is not None and h >= 0.55:
        trend_votes += 1
    if ac1 is not None and ac1 >= 0.05:
        trend_votes += 1
    revert_votes = 0
    if er is not None and er <= 0.20:      # lots of motion, no progress
        revert_votes += 1
    if h is not None and h <= 0.45:
        revert_votes += 1
    if ac1 is not None and ac1 <= -0.05:
        revert_votes += 1

    if trend_votes >= 2 and trend_votes > revert_votes:
        regime = "trend"
    elif revert_votes >= 1 and revert_votes >= trend_votes:
        regime = "mean_revert"
    else:
        regime = "chop"

    return {
        "regime": regime,
        "efficiency_ratio": round(er, 3) if er is not None else None,
        "hurst": round(h, 3) if h is not None else None,
        "autocorr_lag1": round(ac1, 4) if ac1 is not None else None,
        "vol_state": vol_state,
        "vol_ratio": round(vol_ratio, 2),
        "trend_ok": regime == "trend",
        "advice": {
            "trend": "Momentum regime — trend-following + trailing stops are in their element; full size.",
            "mean_revert": "Mean-reverting — trend entries get whipsawed and stops hurt (Kaminski-Lo). Stand down trend legs or switch to fade logic.",
            "chop": "No clear regime — reduce size / sit out; this is where trend systems bleed.",
        }[regime],
    }
