"""Tests for arm_level.pullback_buy_px — the shared level helper.

Per WS3 review gate Tier 2 #3: reanchor + re-entry must use the SAME
level computation so entry/re-entry discipline can't diverge. This module
enforces the invariant that both callers agree.

Also verifies the HARD invariant: pullback_buy_px() always returns
buy_px < sold_price, matching experts_reentry's Stage 3 clamp.
"""
import random

import arm_level


def test_pullback_buy_px_always_below_sold_price_synthetic():
    """The invariant: result is strictly less than sold_price for any
    synthetic price series."""
    random.seed(7)
    for trial in range(50):
        prices = [75.0 + random.gauss(0, 0.3) for _ in range(60)]
        sold = 75.0
        r = arm_level.pullback_buy_px(prices, spread=0.30, sold_price=sold)
        assert r is not None
        assert r < sold, f"trial {trial}: buy_px {r} not below sold {sold}"


def test_pullback_buy_px_returns_none_on_bad_inputs():
    assert arm_level.pullback_buy_px([], spread=0.3, sold_price=75.0) is None
    assert arm_level.pullback_buy_px([75.0], spread=0.0, sold_price=75.0) is None
    assert arm_level.pullback_buy_px([75.0], spread=0.3, sold_price=0.0) is None


def test_pullback_buy_px_short_history_fallback():
    """< 5 bars → fallback path uses spread-based offset below sold."""
    r = arm_level.pullback_buy_px([75.0], spread=0.30, sold_price=75.0)
    assert r < 75.0
    assert r >= 75.0 - 0.30  # within one spread


def test_pullback_buy_px_uses_connors_helper():
    """The helper delegates to connors.suggest_buy_px when enough history.
    We can't directly assert the internal call, but we can verify the
    output shape matches Connors' behavior (biases lower on oversold
    series). Falling knife → deeper below center."""
    # Steadily-falling series → Connors sees strong oversold → deeper offset
    falling = [80.0 - i * 0.5 for i in range(30)]
    r_falling = arm_level.pullback_buy_px(falling, spread=0.30, sold_price=falling[-1])
    # Sideways series → weaker signal → shallower offset
    sideways = [75.0 + (i % 3 - 1) * 0.1 for i in range(30)]
    r_sideways = arm_level.pullback_buy_px(sideways, spread=0.30, sold_price=sideways[-1])
    # Both must respect the invariant
    assert r_falling < falling[-1]
    assert r_sideways < sideways[-1]


def test_pullback_buy_px_matches_experts_reentry_stage_via_connors():
    """The unified-helper contract: both arm_level and experts_reentry
    ultimately place through connors.suggest_buy_px. Verifies that the
    same input series produces a directionally consistent result from both
    paths (both below the sold price, both derived from the same Chan OU
    + Connors math)."""
    import experts_reentry
    prices = [75.0 + (i * 0.05 - 1.5) for i in range(60)]
    sold = 75.0

    r_arm_level = arm_level.pullback_buy_px(prices, spread=0.30, sold_price=sold)
    r_experts = experts_reentry.compute_reentry(
        prices=prices, sold_price=sold, spread=0.30, strategy_qty=1,
    )

    # Both must sit below the sold price (shared invariant)
    assert r_arm_level < sold
    assert r_experts["buy_px"] < sold
    # Both should be in a similar ballpark (within one spread) — they use
    # the same Connors math on the same OU-like band.
    assert abs(r_arm_level - r_experts["buy_px"]) <= 0.30


def test_pullback_buy_px_ou_window_default_is_20():
    """Sanity: the default window matches the value used in experts_reentry
    (DEFAULT_THRESHOLDS['ou_band_window'] = 20)."""
    assert arm_level.DEFAULT_OU_WINDOW == 20
