"""Tests for exit strategies (FixedLimitExit, TrailingStopExit) and the selector."""

from dataclasses import dataclass
from typing import Optional

import pytest

from strategies import (BuyDirective, FixedLimitExit, SellDirective,
                        TrailingStopExit, strategy_by_name)


@dataclass
class FakeState:
    """Minimal state shape the strategies actually touch."""
    swing_qty: int = 2
    trail_armed: bool = False
    trail_high_water_price: float = 0.0


@dataclass
class FakeCfg:
    sell_px: float = 65.0
    buy_px: float = 63.0
    trail_trigger: float = 65.0
    trail_distance: float = 0.20
    reanchor_threshold: float = 2.0
    tick_size: float = 0.005
    contract_size: float = 50.0
    fee_per_contract_roundtrip: float = 4.68


# ============================================================================
# FixedLimitExit
# ============================================================================


def test_fixed_limit_sell_returns_cfg_sell_px():
    d = FixedLimitExit().sell_action(FakeState(swing_qty=3), FakeCfg(), current_price=60.0)
    assert d == SellDirective(qty=3, limit_price=65.0)


def test_fixed_limit_buy_returns_cfg_buy_px():
    d = FixedLimitExit().buy_action(FakeState(swing_qty=3), FakeCfg(), current_price=60.0)
    assert d == BuyDirective(qty=3, limit_price=63.0)


def test_fixed_limit_ignores_price():
    """Price doesn't matter — the levels are the whole logic."""
    s = FakeState()
    cfg = FakeCfg()
    strat = FixedLimitExit()
    for p in (30.0, 60.0, 100.0):
        assert strat.sell_action(s, cfg, current_price=p).limit_price == 65.0
        assert strat.buy_action(s, cfg, current_price=p).limit_price == 63.0


# ============================================================================
# TrailingStopExit
# ============================================================================


def test_trailing_returns_none_below_trigger():
    """Below the trail_trigger, no order is placed and no state mutates."""
    s = FakeState()
    result = TrailingStopExit().sell_action(s, FakeCfg(trail_trigger=65.0), current_price=63.0)
    assert result is None
    assert s.trail_armed is False


def test_trailing_arms_at_trigger():
    s = FakeState()
    result = TrailingStopExit().sell_action(s, FakeCfg(trail_trigger=65.0), current_price=65.0)
    # Just armed — no sell yet
    assert result is None
    assert s.trail_armed is True
    assert s.trail_high_water_price == 65.0


def test_trailing_high_water_ratchets_up():
    s = FakeState(trail_armed=True, trail_high_water_price=65.0)
    strat = TrailingStopExit()
    strat.sell_action(s, FakeCfg(), current_price=67.0)
    assert s.trail_high_water_price == 67.0
    strat.sell_action(s, FakeCfg(), current_price=66.0)  # doesn't ratchet DOWN
    assert s.trail_high_water_price == 67.0


def test_trailing_fires_when_price_falls_through_stop():
    """HWM 67, distance 0.20 → stop at 66.80. Price falls to 66.79 → fire."""
    s = FakeState(trail_armed=True, trail_high_water_price=67.0)
    d = TrailingStopExit().sell_action(s, FakeCfg(trail_distance=0.20), current_price=66.79)
    assert d is not None
    assert d.qty == 2
    # Fill price is one tick under current, so ~66.785
    assert d.limit_price == pytest.approx(66.785, abs=0.001)


def test_trailing_holds_while_price_still_above_stop():
    """HWM 67, stop 66.80. Price at 66.90 → still in trail, no order."""
    s = FakeState(trail_armed=True, trail_high_water_price=67.0)
    result = TrailingStopExit().sell_action(s, FakeCfg(trail_distance=0.20), current_price=66.90)
    assert result is None


def test_trailing_resets_on_sell_filled():
    """Once a trail-triggered sell fills, the next cycle starts fresh."""
    s = FakeState(trail_armed=True, trail_high_water_price=67.0)
    TrailingStopExit().on_sell_filled(s, FakeCfg(), fill_price=66.80)
    assert s.trail_armed is False
    assert s.trail_high_water_price == 0.0


# ---- Re-anchor (spec §6) ----------------------------------------------------


def test_buy_no_reanchor_when_fill_near_old_range():
    """Sold at ~65 (near cfg.sell_px). Range intact — rebuy at cfg.buy_px."""
    d = TrailingStopExit().buy_action(
        FakeState(), FakeCfg(sell_px=65.0, buy_px=63.0, reanchor_threshold=2.0),
        current_price=64.5, last_sell_fill_price=65.5,
    )
    assert d.limit_price == 63.0  # unchanged


def test_buy_reanchors_when_fill_far_above_old_range():
    """Trailing exit filled at 79 (way above cfg.sell_px=65). Range dead —
    rebuy at floor(79) − 1 = 78."""
    d = TrailingStopExit().buy_action(
        FakeState(), FakeCfg(sell_px=65.0, buy_px=63.0, reanchor_threshold=2.0),
        current_price=79.0, last_sell_fill_price=79.5,
    )
    assert d.limit_price == 78.0


def test_buy_no_reanchor_when_missing_fill_price():
    d = TrailingStopExit().buy_action(
        FakeState(), FakeCfg(sell_px=65.0, buy_px=63.0),
        current_price=64.0, last_sell_fill_price=None,
    )
    assert d.limit_price == 63.0


# ============================================================================
# Selector
# ============================================================================


def test_selector_fixed_limit():
    assert isinstance(strategy_by_name("fixed_limit"), FixedLimitExit)


def test_selector_trailing_stop():
    assert isinstance(strategy_by_name("trailing_stop"), TrailingStopExit)


def test_selector_unknown_raises():
    with pytest.raises(ValueError, match="unknown exit_mode"):
        strategy_by_name("moon_shot")
