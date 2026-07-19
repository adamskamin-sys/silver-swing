"""Harvey (2018 JPM) volatility-targeted position sizing.

Option D-2 from 2026-07-19 expert-source refactor. Feature-flagged
OFF by default; tests verify (a) pure math correctness, (b) flag-off
never changes base_qty, (c) clamping bounds, (d) fail-open on
insufficient data.
"""
from __future__ import annotations

import math

import pytest

from vol_target import (
    DEFAULT_MAX_SCALE,
    DEFAULT_MIN_SCALE,
    DEFAULT_TARGET_ANNUAL_VOL,
    adjusted_qty,
    compute_realized_vol,
    flag_enabled,
    size_scale,
)


# ---- flag semantics --------------------------------------------------------


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("SWING_VOL_TARGET_ENABLED", raising=False)
    assert flag_enabled() is False


def test_flag_on(monkeypatch):
    monkeypatch.setenv("SWING_VOL_TARGET_ENABLED", "1")
    assert flag_enabled() is True


def test_adjusted_qty_flag_off_returns_base(monkeypatch):
    """Flag off → base_qty unchanged even with wild returns."""
    monkeypatch.delenv("SWING_VOL_TARGET_ENABLED", raising=False)
    wild = [0.1, -0.1, 0.1, -0.1, 0.1, -0.1, 0.1, -0.1]  # huge vol
    assert adjusted_qty(5, wild) == 5


# ---- pure math -------------------------------------------------------------


def test_realized_vol_insufficient_data():
    assert compute_realized_vol([0.01, 0.02]) is None
    assert compute_realized_vol([]) is None


def test_realized_vol_positive_on_nonzero_returns():
    returns = [0.005, -0.003, 0.008, -0.002, 0.001, 0.004, -0.006, 0.003]
    rv = compute_realized_vol(returns)
    assert rv is not None and rv > 0


def test_realized_vol_zero_input_returns_none():
    """Pure zeros give zero variance → None."""
    assert compute_realized_vol([0.0] * 20) is None


def test_size_scale_at_target():
    """When realized_vol == target, scale = 1.0."""
    assert size_scale(DEFAULT_TARGET_ANNUAL_VOL) == 1.0


def test_size_scale_high_vol_shrinks():
    """Realized vol = 2 × target → scale = 0.5."""
    scale = size_scale(DEFAULT_TARGET_ANNUAL_VOL * 2)
    assert abs(scale - 0.5) < 1e-9


def test_size_scale_low_vol_grows_but_capped():
    """Realized vol = target / 10 would want scale = 10; capped at MAX_SCALE."""
    scale = size_scale(DEFAULT_TARGET_ANNUAL_VOL / 10)
    assert scale == DEFAULT_MAX_SCALE


def test_size_scale_extreme_vol_floored():
    """Vol spike shouldn't zero out size — floored at MIN_SCALE."""
    scale = size_scale(DEFAULT_TARGET_ANNUAL_VOL * 100)
    assert scale == DEFAULT_MIN_SCALE


def test_size_scale_missing_vol_returns_one():
    """Insufficient data → scale = 1 (permissive fail-open)."""
    assert size_scale(None) == 1.0


# ---- integration -----------------------------------------------------------


def test_adjusted_qty_flag_on_high_vol_shrinks(monkeypatch):
    """Flag on + high realized vol → adjusted qty smaller than base."""
    monkeypatch.setenv("SWING_VOL_TARGET_ENABLED", "1")
    # Big returns → high realized vol → scale < 1
    high_vol = [0.05, -0.04, 0.06, -0.05, 0.04, -0.06, 0.05, -0.04,
                 0.06, -0.05]
    adj = adjusted_qty(10, high_vol)
    assert adj < 10
    assert adj >= 1  # floored


def test_adjusted_qty_flag_on_low_vol_grows(monkeypatch):
    """Flag on + very low realized vol → adjusted qty larger than base,
    bounded by MAX_SCALE."""
    monkeypatch.setenv("SWING_VOL_TARGET_ENABLED", "1")
    calm = [0.0001, -0.0001, 0.0001, -0.0001, 0.0001, -0.0001,
             0.0001, -0.0001, 0.0001, -0.0001]
    adj = adjusted_qty(5, calm)
    assert adj > 5
    assert adj <= int(5 * DEFAULT_MAX_SCALE)


def test_adjusted_qty_never_returns_zero(monkeypatch):
    """Extreme vol clamp → floored at 1, never zero."""
    monkeypatch.setenv("SWING_VOL_TARGET_ENABLED", "1")
    extreme = [0.5, -0.5] * 10  # absurd vol
    adj = adjusted_qty(1, extreme)
    assert adj >= 1


def test_adjusted_qty_zero_base_returns_zero(monkeypatch):
    """base=0 stays 0 — sizing doesn't magic contracts into existence."""
    monkeypatch.setenv("SWING_VOL_TARGET_ENABLED", "1")
    returns = [0.01, -0.01, 0.01, -0.01, 0.01, -0.01]
    assert adjusted_qty(0, returns) == 0


def test_adjusted_qty_insufficient_data_fail_open(monkeypatch):
    """Flag on but < 5 returns → base_qty unchanged (fail-open)."""
    monkeypatch.setenv("SWING_VOL_TARGET_ENABLED", "1")
    assert adjusted_qty(7, [0.01]) == 7
    assert adjusted_qty(7, []) == 7
