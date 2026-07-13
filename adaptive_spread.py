"""Volatility-adaptive spread widening.

Andersen-Bollerslev (1998) show realized volatility from high-frequency
data is a better short-horizon vol forecast than ATR alone. When realized
vol spikes above the "normal" (ATR-baseline) level, the current spread
targets — set from the historical ATR — become too tight for the
current regime. Filling a limit BUY into a high-vol drop is the classic
"adverse selection" problem (Cartea-Jaimungal ch.8, Chan ch.5).

Solution: multiply the spread by (recent_realized_vol / baseline_vol),
capped at spread_vol_multiplier_max. When vol spikes 2×, the spread
widens 2× (up to the cap). When vol is normal, no change.

Applied on a PER-ARM basis in swing_leg via a price adjustment helper —
NOT by rewriting sleeve.buy_px / sleeve.sell_px, which would drift the
user's target. The user's config stays the source of truth; we only
adjust WHERE we place the actual limit for this specific arm.
"""

from __future__ import annotations

from typing import Optional


def realized_vol_from_history(price_history: list, window_secs: float = 300.0) -> Optional[float]:
    """Realized vol (standard deviation of log returns) over the last N
    seconds of a price_history list of (ts, price) tuples. Returns None
    if insufficient samples."""
    import math
    import time
    if not price_history or len(price_history) < 5:
        return None
    cutoff = time.time() - window_secs
    samples = []
    for entry in price_history:
        try:
            ts = float(entry[0])
            px = float(entry[1])
        except (TypeError, ValueError, IndexError):
            continue
        if ts >= cutoff and px > 0:
            samples.append(px)
    if len(samples) < 5:
        return None
    log_rets = []
    for i in range(1, len(samples)):
        if samples[i - 1] > 0 and samples[i] > 0:
            log_rets.append(math.log(samples[i] / samples[i - 1]))
    if len(log_rets) < 3:
        return None
    mean = sum(log_rets) / len(log_rets)
    var = sum((r - mean) ** 2 for r in log_rets) / len(log_rets)
    return math.sqrt(var)


def spread_multiplier(
    realized_vol: Optional[float],
    baseline_vol: Optional[float],
    max_multiplier: float = 2.0,
) -> float:
    """Return a multiplier in [1.0, max_multiplier] for the current spread.

    realized_vol / baseline_vol, clamped [1.0, max_multiplier]. When
    realized ≤ baseline we return 1.0 (never TIGHTEN — user's config is
    the floor). Above baseline we widen up to the cap.

    Permissive-default: if either vol is missing/zero, returns 1.0.
    """
    if not realized_vol or realized_vol <= 0:
        return 1.0
    if not baseline_vol or baseline_vol <= 0:
        return 1.0
    ratio = realized_vol / baseline_vol
    return max(1.0, min(float(max_multiplier), ratio))


def adjusted_targets(
    sell_px: float,
    buy_px: float,
    multiplier: float,
) -> tuple[float, float]:
    """Widen the (sell_px, buy_px) spread symmetrically around its midpoint
    by the multiplier. Returns (new_sell_px, new_buy_px).

    Preserves the midpoint — only the spread WIDTH changes. This keeps the
    trade centered where the sleeve was designed to trade, just gives it
    more room in high-vol regimes.
    """
    if multiplier <= 1.0 or sell_px <= 0 or buy_px <= 0 or buy_px >= sell_px:
        return (sell_px, buy_px)
    mid = (sell_px + buy_px) / 2.0
    half_spread = (sell_px - buy_px) / 2.0
    new_half = half_spread * multiplier
    return (mid + new_half, mid - new_half)
