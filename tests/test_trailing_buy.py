"""Unit tests for the trailing-buy state machine in swing_leg.

Doesn't run a full SwingTrader cycle — just tests _trailing_buy_ready in
isolation with hand-rolled SleeveConfig + SleeveState fixtures. That's
enough because the state machine is self-contained (reads sc.buy_px,
sc.buy_trail_enabled, sc.buy_trail_distance, ss.buy_trail_armed,
ss.buy_trail_low_water; returns arm price or None).

Expert canon verified:
  - Livermore's pivot: wait until price bounces off a low before buying
  - Le Beau's 0.5×ATR entry filter: default distance from expert_params
  - Cap at buy_px: never overpays vs original target
  - Disabled path: identical to legacy (returns buy_px, no state change)
"""

import pytest

from sleeves import SleeveConfig, SleeveState, SleeveStateEnum
from swing_leg import SwingTrader


# The bound method needs a `self` with self._record. A minimal shim is enough.
class _RecShim:
    def __init__(self):
        self.events = []

    def _record(self, name, **kwargs):
        self.events.append((name, kwargs))


def _make_sc(buy_px=63.0, distance=0.5, enabled=True):
    return SleeveConfig(
        id="s1", name="test", qty=1,
        buy_px=buy_px, sell_px=65.0,
        buy_trail_enabled=enabled,
        buy_trail_distance=distance,
    )


def _make_ss():
    return SleeveState(id="s1", state=SleeveStateEnum.ARMED_BUY)


def _tb(shim, sc, ss, price):
    """Invoke _trailing_buy_ready without needing a full SwingTrader."""
    return SwingTrader._trailing_buy_ready(shim, sc, ss, price)


def test_disabled_returns_buy_px_immediately():
    """Legacy behavior preserved when trailing_buy is off."""
    shim = _RecShim()
    sc = _make_sc(buy_px=63.0, enabled=False)
    ss = _make_ss()
    assert _tb(shim, sc, ss, 64.0) == 63.0  # above buy_px
    assert _tb(shim, sc, ss, 63.0) == 63.0  # at buy_px
    assert _tb(shim, sc, ss, 62.0) == 63.0  # below buy_px
    assert ss.buy_trail_armed is False
    assert shim.events == []


def test_zero_distance_treated_as_disabled():
    shim = _RecShim()
    sc = _make_sc(buy_px=63.0, distance=0.0, enabled=True)
    ss = _make_ss()
    assert _tb(shim, sc, ss, 62.0) == 63.0
    assert ss.buy_trail_armed is False


def test_not_yet_armed_above_buy_px_returns_none():
    """Mark still above buy_px, never dipped → wait, don't arm."""
    shim = _RecShim()
    sc = _make_sc(buy_px=63.0, distance=0.5)
    ss = _make_ss()
    assert _tb(shim, sc, ss, 64.0) is None
    assert ss.buy_trail_armed is False


def test_armed_recovery_above_buy_px_fires_bounce_at_buy_px_cap():
    """Once armed, a full recovery above buy_px is a valid bounce
    confirmation — fire at min(mark, buy_px), NEVER disarm silently
    (which would let the sleeve miss a real confirmation)."""
    shim = _RecShim()
    sc = _make_sc(buy_px=63.0, distance=0.5)
    ss = _make_ss()
    # Arm at 62.5, then market recovers strongly to 64.0. Since armed and
    # 64.0 - 62.5 = 1.5 >= 0.5, bounce confirms. Cap at buy_px = 63.0.
    _tb(shim, sc, ss, 62.5)
    arm = _tb(shim, sc, ss, 64.0)
    assert arm == pytest.approx(63.0)
    assert ss.buy_trail_armed is False


def test_phase2_cross_arms_and_starts_tracking():
    """First tick with mark <= buy_px arms the trail."""
    shim = _RecShim()
    sc = _make_sc(buy_px=63.0, distance=0.5)
    ss = _make_ss()
    assert _tb(shim, sc, ss, 62.8) is None
    assert ss.buy_trail_armed is True
    assert ss.buy_trail_low_water == pytest.approx(62.8)
    assert any(name == "buy_trail_armed" for name, _ in shim.events)


def test_phase3_further_drop_updates_low_water():
    shim = _RecShim()
    sc = _make_sc(buy_px=63.0, distance=0.5)
    ss = _make_ss()
    _tb(shim, sc, ss, 62.8)  # arm at 62.8
    assert _tb(shim, sc, ss, 62.5) is None
    assert ss.buy_trail_low_water == pytest.approx(62.5)
    assert _tb(shim, sc, ss, 62.0) is None
    assert ss.buy_trail_low_water == pytest.approx(62.0)


def test_phase4_bounce_below_buy_px_fires_at_current_mark():
    """Deep dip: bounce fires with arm_price = current mark (below buy_px)."""
    shim = _RecShim()
    sc = _make_sc(buy_px=63.0, distance=0.5)
    ss = _make_ss()
    _tb(shim, sc, ss, 62.5)  # arm at 62.5
    _tb(shim, sc, ss, 62.0)  # new low
    _tb(shim, sc, ss, 61.5)  # new low
    # Bounce: low=61.5, need mark >= 62.0 to fire
    assert _tb(shim, sc, ss, 61.8) is None  # not enough
    arm = _tb(shim, sc, ss, 62.2)  # 61.5 + 0.5 = 62.0 → 62.2 >= 62.0
    # Arm price = min(62.2, buy_px=63.0) = 62.2 (better than target)
    assert arm == pytest.approx(62.2)
    # State reset after firing
    assert ss.buy_trail_armed is False
    assert ss.buy_trail_low_water == 0.0
    assert any(name == "buy_trail_bounce_confirmed" for name, _ in shim.events)


def test_phase4_bounce_caps_arm_price_at_buy_px():
    """Shallow dip that bounces above buy_px → arm at buy_px, never overpay."""
    shim = _RecShim()
    sc = _make_sc(buy_px=63.0, distance=0.5)
    ss = _make_ss()
    _tb(shim, sc, ss, 62.9)  # arm at 62.9 (barely below)
    # Bounce carries above buy_px: 62.9 + 0.5 = 63.4 → mark at 63.4
    arm = _tb(shim, sc, ss, 63.4)
    assert arm == pytest.approx(63.0)  # capped at buy_px


def test_between_low_and_low_plus_distance_keeps_waiting():
    """Small bounce that's not enough — hold state, don't fire."""
    shim = _RecShim()
    sc = _make_sc(buy_px=63.0, distance=0.5)
    ss = _make_ss()
    _tb(shim, sc, ss, 62.0)   # low_water = 62.0
    assert _tb(shim, sc, ss, 62.3) is None
    assert ss.buy_trail_armed is True
    assert ss.buy_trail_low_water == pytest.approx(62.0)


def test_expert_default_distance_from_atr():
    """expert_params.expert_params() emits buy_trail_distance = 0.5×ATR
    for metals/energy — the Le Beau entry-filter canonical."""
    from expert_params import expert_params
    p = expert_params("SLR-27AUG26-CDE", atr=0.10)
    assert p["buy_trail_distance"] == pytest.approx(0.05)  # 0.10 × 0.5
    p_crypto = expert_params("BTC-PERP-INTX", atr=100.0)
    assert p_crypto["buy_trail_distance"] == pytest.approx(75.0)  # 100 × 0.75
