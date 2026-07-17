"""Regression test for the 2026-07-17 XLP walk-down bug.

Adam 2026-07-17: XLP-20DEC30-CDE hit 65+ reentry_reeval_replaced
events in 3 min, buy_px marching $0.148 → $0.117 chasing itself
away from the $0.187 mark. Root cause was swing_leg.py:3114 —

    sold_ref = float(ss.last_sell_fill_price or sc.buy_px)

When a fresh sleeve had no last_sell_fill_price, sold_ref fell back
to sc.buy_px (the CURRENT buy target). Then arm_level.pullback_buy_px
clamped `buy_px < sold_ref` with epsilon = max(spread/4, sold*0.0005),
producing new_buy_px = current_buy_px - epsilon every iteration.
Runaway walk-down.

Fix: fallback chain now goes last_sell_fill_price → last_price →
sc.sell_px → sc.buy_px. Each falls back only when the previous is
0/None. Fresh sleeves anchor on current mark, not on themselves.

This test drives arm_level.pullback_buy_px with realistic XLP-shaped
inputs and asserts the SAME sleeve, called twice in a row without a
sell in between, does NOT produce a runaway walk-down when the fix
is applied.
"""
from __future__ import annotations


def test_arm_level_does_not_walk_down_when_sold_ref_is_current_mark():
    """With sold_ref = current market price (post-fix), arm_level should
    return a buy_px near-mark, not walk down endlessly."""
    import arm_level

    mark = 0.187
    # Simulate the XLP scenario: fresh sleeve, no history to speak of
    prices = [0.186, 0.187, 0.186, 0.187, 0.186, 0.187]
    spread = 0.005

    # Two consecutive calls with sold_ref = mark. Result should be stable
    # (not walking down by a fixed epsilon each iteration).
    buy_1 = arm_level.pullback_buy_px(prices, spread=spread, sold_price=mark)
    buy_2 = arm_level.pullback_buy_px(prices, spread=spread, sold_price=mark)
    assert buy_1 is not None and buy_2 is not None
    # Both anchored near mark
    assert 0.15 < buy_1 < mark
    assert 0.15 < buy_2 < mark
    # Second call not systematically lower than first (no walk-down)
    assert abs(buy_1 - buy_2) < 1e-6, (
        f"arm_level should be stable across calls with same inputs; "
        f"got buy_1={buy_1}, buy_2={buy_2}"
    )


def test_arm_level_walks_down_when_sold_ref_is_current_buy_px_regression():
    """Demonstrates the OLD bug: with sold_ref = current buy_px (which
    fresh sleeves would fall back to), arm_level's clamp forces buy_px
    below sold_ref, producing exactly the walk-down we observed on XLP.

    This test intentionally passes sold_price=current_buy_px to prove
    the old fallback path was pathological."""
    import arm_level

    current_buy_px = 0.148  # like a fresh XLP arm
    spread = 0.005
    # Bland history — Connors will suggest something around mark, which
    # is above sold_ref, so it triggers the clamp
    prices = [0.187] * 20

    # OLD path: sold_ref = current_buy_px
    buy_1 = arm_level.pullback_buy_px(prices, spread=spread, sold_price=current_buy_px)
    assert buy_1 is not None
    # Post-clamp, buy_1 < current_buy_px. Simulate next iteration:
    buy_2 = arm_level.pullback_buy_px(prices, spread=spread, sold_price=buy_1)
    assert buy_2 is not None
    # If we passed the OLD (buggy) buy_px each time, we'd see walk-down
    assert buy_2 < buy_1, (
        f"Regression baseline: with sold_ref=current_buy, arm_level MUST "
        f"clamp buy_px below → walk-down. buy_1={buy_1}, buy_2={buy_2}. "
        f"This test documents the OLD pathology so future refactors don't "
        f"accidentally reintroduce it via the fallback."
    )


def test_reeval_fallback_chain_documented_in_source():
    """Regression guard: the fix's fallback chain must be present in
    swing_leg.py — last_sell_fill_price → last_price → sell_px → buy_px."""
    src = open("/Users/adamkamin/silver-swing/swing_leg.py").read()
    # The 4-way fallback chain
    assert "ss.last_sell_fill_price" in src
    assert "or last_price" in src
    assert "or sc.sell_px" in src
    # Comment mentions the incident
    assert "XLP" in src or "walk-down" in src.lower(), (
        "The fix comment referencing the XLP incident must stay so future "
        "sessions don't revert to the pathological fallback."
    )


def test_bug_pattern_specifically_documented_in_comment():
    """The pathological pattern (fallback to sc.buy_px alone) must be
    called out in a comment near the fix so nobody re-introduces it."""
    src = open("/Users/adamkamin/silver-swing/swing_leg.py").read()
    # Look for the fix block
    idx = src.find("XLP runaway fix")
    assert idx > 0, "Fix comment referencing XLP runaway must be present"
    ctx = src[idx : idx + 800]
    # Must mention the mechanism
    assert "sold_ref" in ctx.lower()
    assert "sc.buy_px" in ctx
