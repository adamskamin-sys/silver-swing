"""Perpetuals funding-rate signal.

Coinbase perpetual futures (BTC-PERP-INTX, ETH-PERP-INTX, etc.) pay/collect
funding every 8 hours. Sign convention:
  funding_rate > 0  → longs pay shorts (bearish for longs, bullish for shorts)
  funding_rate < 0  → shorts pay longs (bullish for longs — you get paid to hold)

Extreme funding regimes are strong short-horizon direction signals: when
funding is very negative on a long position, the market is aggressively
shorting a squeeze setup — being long collects the funding AND catches the
squeeze. Aksoy-Cheng (2018), Hasbrouck (2021) both find funding-rate
extremes predict short-term reversals.

We use funding as:
  1. Scanner tile boost/penalty: strongly-negative funding on longs
     increases the tile score (getting paid to hold).
  2. Optional per-sleeve gate: refuse BUY arms if funding is strongly
     positive (paying to hold long into likely reversal).

Non-perp products (silver, oil, standard futures) don't have funding —
returns None everywhere, permissive-default.
"""

from __future__ import annotations

from typing import Optional


def is_perp(symbol: str) -> bool:
    if not symbol:
        return False
    return "-PERP-" in symbol.upper()


def funding_rate_of(snapshot: Optional[dict]) -> Optional[float]:
    """Read funding_rate off a snapshot. None if missing or product isn't perp."""
    if not snapshot:
        return None
    for key in ("funding_rate", "predicted_funding_rate", "current_funding_rate"):
        v = snapshot.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def scanner_boost(funding_rate: Optional[float]) -> float:
    """Return a multiplier to apply to a long-side tile score.

    Sign convention (LONG position perspective):
      Very negative funding (-0.02% and below) → BOOST (get paid + short-squeeze setup)
      Slightly negative                        → mild boost
      Zero to slightly positive                → neutral
      Strongly positive (0.05% and above)      → PENALTY (paying to hold, reversal risk)

    Multiplier is capped at [0.5, 1.5]. Applied as `weighted_score *= boost`.
    Range chosen conservatively — Aksoy-Cheng show funding predicts 50-100 bp
    over ~24h, but our positions turn faster than that so we don't over-weight.
    """
    if funding_rate is None:
        return 1.0
    # Coinbase funding is a percent per 8h. -0.02% = -0.0002.
    # Scale so that ±0.05% (extreme) maps to ±50% boost, clamped.
    scale = 0.05 / 100.0  # 0.05%
    signal = -funding_rate / scale  # negate: negative funding = positive for LONG
    boost = 1.0 + 0.5 * max(-1.0, min(1.0, signal))
    return max(0.5, min(1.5, boost))


def funding_gate_ok_for_buy(funding_rate: Optional[float], threshold: float) -> bool:
    """Block BUY arms when funding is more positive than threshold (paying to
    hold long during a probable reversal). Permissive-default (True) when
    funding data is missing.

    Threshold is a fraction (0.0005 = 0.05% per 8h). Van Tharp / Aksoy-Cheng
    style entry filter — don't fight expensive carry.
    """
    if funding_rate is None:
        return True
    return funding_rate < threshold
