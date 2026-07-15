"""vwap.py — Volume Weighted Average Price + Anchored VWAP.

References
----------
Berkowitz, Stephen A., Dennis E. Logue, and Eugene A. Noser Jr.
"The Total Cost of Transactions on the NYSE." Journal of Finance, 1988.
    - First formal treatment of VWAP as an execution benchmark.

Kissell, Robert. *The Science of Algorithmic Trading and Portfolio
Management*. Academic Press, 2013. Ch. 4-5.
    - VWAP execution strategy, participation-rate models.

Anchored VWAP popularized by Paul Levine (1990s technician) and
brought mainstream by Brian Shannon (*Maximum Trading Gains with
Anchored VWAP*, 2022).

Purpose
-------
Two-part module for a volatility-driven swing bot:

1. **Session VWAP** — the true average price weighted by volume, computed
   from the start of the current session. Institutional-grade execution
   benchmark. Trading BELOW session VWAP = executing better than the
   average participant (winning); ABOVE = paying up (losing).

2. **Anchored VWAP** — VWAP starting from a specific event (major swing
   high, low, earnings, funding fixing). Becomes a magnetic support/
   resistance level for future swings. Price mean-reverts to Anchored
   VWAP with high frequency across many market regimes.

Usage in the expert stack
-------------------------
- **Entry timing:** Buy pullbacks to Anchored VWAP from major low; sell
  bounces from Anchored VWAP from major high.
- **Execution quality:** Only buy when session-VWAP is not far above
  price — else you're paying up vs the average participant.
- **Volatility gate:** wide VWAP band std_dev = high volatility, wider
  stops warranted.

Fail-safe: returns None on insufficient/malformed bar data.
"""
from __future__ import annotations

from typing import Optional, Sequence


def _bar_price(bar) -> Optional[float]:
    """Typical price for a bar: (high + low + close) / 3."""
    try:
        if isinstance(bar, dict):
            h = float(bar.get("high", 0))
            l = float(bar.get("low", 0))
            c = float(bar.get("close", 0))
        else:
            h = float(getattr(bar, "high", 0))
            l = float(getattr(bar, "low", 0))
            c = float(getattr(bar, "close", 0))
        if h <= 0 or l <= 0 or c <= 0:
            return None
        return (h + l + c) / 3.0
    except (TypeError, ValueError, AttributeError):
        return None


def _bar_volume(bar) -> float:
    """Volume for a bar. Returns 0.0 if not present (still contributes
    via price if computing unweighted VWAP fallback)."""
    try:
        if isinstance(bar, dict):
            return float(bar.get("volume", 0) or 0)
        return float(getattr(bar, "volume", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def vwap(bars: Sequence, unweighted_fallback: bool = True) -> Optional[float]:
    """Compute VWAP over the given bar series.

    Args:
        bars: sequence of OHLCV bars (dicts with high/low/close/volume,
              or objects with attributes).
        unweighted_fallback: if all bars have zero volume (unusual — e.g.,
              synthetic data), fall back to a simple typical-price average.
              Default True.

    Returns:
        The volume-weighted average price, or None if no valid bars.
    """
    if not bars:
        return None
    total_pv = 0.0
    total_v = 0.0
    typical_sum = 0.0
    typical_count = 0
    for b in bars:
        p = _bar_price(b)
        if p is None:
            continue
        v = _bar_volume(b)
        total_pv += p * v
        total_v += v
        typical_sum += p
        typical_count += 1
    if total_v > 0:
        return round(total_pv / total_v, 6)
    if unweighted_fallback and typical_count > 0:
        return round(typical_sum / typical_count, 6)
    return None


def anchored_vwap(bars: Sequence, anchor_idx: int = 0) -> Optional[float]:
    """VWAP anchored to a specific bar index (a major swing high/low,
    earnings event, or funding fixing).

    Args:
        bars: full OHLCV series.
        anchor_idx: index in bars to start VWAP from (0 = beginning of series).
                    Negative indices work (e.g., -60 = last 60 bars).

    Returns:
        VWAP from anchor_idx to end of bars, or None if invalid.

    Usage:
        # VWAP anchored to a major swing low 100 bars ago:
        avwap = anchored_vwap(bars, anchor_idx=-100)
        # Price mean-reverting to avwap = mean-reversion buy candidate.
    """
    if not bars:
        return None
    if anchor_idx < 0:
        anchor_idx = max(0, len(bars) + anchor_idx)
    if anchor_idx >= len(bars):
        return None
    return vwap(bars[anchor_idx:])


def vwap_bands(bars: Sequence, num_std: float = 1.0) -> Optional[dict]:
    """Compute VWAP plus/minus N standard deviations of typical price.
    Bollinger-style envelope but volume-weighted center.

    Returns:
        {"vwap": ..., "upper": ..., "lower": ..., "std": ...} or None.
    """
    v = vwap(bars)
    if v is None:
        return None
    typicals = [_bar_price(b) for b in bars]
    typicals = [t for t in typicals if t is not None]
    if len(typicals) < 2:
        return None
    variance = sum((t - v) ** 2 for t in typicals) / len(typicals)
    std = variance ** 0.5
    return {
        "vwap": round(v, 6),
        "upper": round(v + num_std * std, 6),
        "lower": round(v - num_std * std, 6),
        "std": round(std, 6),
    }


def vwap_signal(bars: Sequence, price: float) -> Optional[dict]:
    """Interpret current price vs VWAP for execution/entry signals.

    Returns:
        {
            "vwap": <value>,
            "price": <current>,
            "price_vs_vwap": <"above"|"below"|"at">,
            "distance_pct": <(price - vwap) / vwap * 100>,
            "signal": "good_buy" | "expensive_buy" | "at_fair",
            "reason": <explanation>,
        }
        or None if insufficient data.

    Interpretation:
        * price < vwap → paying LESS than the average participant → good
          execution quality for a buy
        * price > vwap → paying MORE → expensive; wait for pullback
        * price ≈ vwap (within 0.1%) → at fair; neutral
    """
    v = vwap(bars)
    if v is None or price <= 0:
        return None
    pct = (price - v) / v * 100.0
    if abs(pct) < 0.1:
        return {
            "vwap": round(v, 6),
            "price": round(price, 6),
            "price_vs_vwap": "at",
            "distance_pct": round(pct, 4),
            "signal": "at_fair",
            "reason": f"price {price:.4f} at VWAP {v:.4f} (fair execution)",
        }
    if price < v:
        return {
            "vwap": round(v, 6),
            "price": round(price, 6),
            "price_vs_vwap": "below",
            "distance_pct": round(pct, 4),
            "signal": "good_buy",
            "reason": f"price {price:.4f} below VWAP {v:.4f} ({pct:.2f}%) — better than avg execution",
        }
    return {
        "vwap": round(v, 6),
        "price": round(price, 6),
        "price_vs_vwap": "above",
        "distance_pct": round(pct, 4),
        "signal": "expensive_buy",
        "reason": f"price {price:.4f} above VWAP {v:.4f} (+{pct:.2f}%) — paying up vs avg",
    }
