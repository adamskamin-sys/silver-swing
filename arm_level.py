"""arm_level.py — single source of truth for pullback buy_px computation.

Per WS3 review gate (Tier 2 #3): "Level logic unified with expert_reentry.
The reanchor pullback price uses the SAME level computation as
expert_reentry's Stage 3 (not a parallel fast_ema−ATR formula), so
entry and re-entry discipline can't diverge. Verify: both call one
shared helper."

Both experts_reentry (post-sell) and reentry_reeval wiring
(on-drift/on-stale) call `pullback_buy_px(prices, spread, sold_price)`
to compute where the resting buy should sit. Prevents two independent
formulas drifting apart.

Backing algorithm: Chan Ornstein-Uhlenbeck bounce band + Connors
statistical mean-reversion — same as experts_reentry.compute_reentry
Stage 3+5. The band center is a 20-bar SMA, width is 2×20-bar std;
Connors' bounce_probability score shifts the price LOWER within the
band as oversold deepens. Buy is capped BELOW the sold price (anti-
"buy above last sale" invariant).
"""
from __future__ import annotations

from typing import Optional, Sequence


DEFAULT_OU_WINDOW = 20


def pullback_buy_px(prices: Sequence[float], spread: float,
                    sold_price: float,
                    ou_window: int = DEFAULT_OU_WINDOW) -> Optional[float]:
    """Compute a mean-reversion-aware buy price BELOW sold_price.

    Args:
        prices: recent close series (>= ou_window bars ideal).
        spread: the sleeve's target spread (sell_px - buy_px). Used for
                the fallback offset when Connors isn't computable.
        sold_price: the reference we must sit below (usually
                    ss.last_sell_fill_price OR the current mark).
        ou_window: OU band lookback window (default 20).

    Returns:
        A buy_px strictly less than sold_price, or None if not computable.

    Invariants:
        * Result is always < sold_price (WS3 hard invariant).
        * Uses Connors' suggested_buy_px when history >= ou_window;
          falls back to sold_price - spread/2 otherwise.
    """
    ps = [float(p) for p in (prices or []) if p is not None]
    if not ps or spread <= 0 or sold_price <= 0:
        return None

    # Compute OU band from tail
    tail = ps[-ou_window:] if len(ps) >= ou_window else ps
    if len(tail) < 5:
        # Not enough history — fallback to spread-based offset below sold price
        return sold_price - max(spread / 2.0, sold_price * 0.0005)

    mean = sum(tail) / len(tail)
    var = sum((p - mean) ** 2 for p in tail) / len(tail)
    std = var ** 0.5
    band_center = mean
    band_width = max(2 * std, spread)  # never narrower than the sleeve's own spread

    # Delegate the buy_px placement to Connors' suggest_buy_px, same as
    # experts_reentry does. Fail-safe on any error.
    try:
        import connors
        suggestion = connors.suggest_buy_px(ps, band_center, band_width)
        buy_px = float(suggestion.get("suggested_buy_px") or (band_center - std))
    except Exception:
        buy_px = band_center - std

    # HARD invariant: buy_px must be strictly less than sold_price.
    # Same clamp expert_reentry uses (see experts_reentry.compute_reentry).
    epsilon = max(spread / 4.0, sold_price * 0.0005)
    if buy_px >= sold_price:
        buy_px = sold_price - epsilon

    return round(buy_px, 6)
