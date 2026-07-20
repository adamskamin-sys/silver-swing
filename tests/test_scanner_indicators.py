"""Sanity + citation tests for scanner_indicators.py.

Each test uses a well-known synthetic pattern where the correct output
is derivable by hand, so regressions are obvious.
"""
from __future__ import annotations
import math
import pytest

import scanner_indicators as si


# ---- fixtures ------------------------------------------------------------

def _bar(o, h, l, c, v, start=0):
    return {"open": o, "high": h, "low": l, "close": c, "volume": v, "start": start}


def _flat_bars(n=20, price=100.0, volume=100.0):
    return [_bar(price, price, price, price, volume, i) for i in range(n)]


def _trending_up(n=20, start=100.0, step=0.5, volume=100.0):
    """Monotonic uptrend — Roll's estimator should return 0 (positive cov)."""
    return [
        _bar(start + i * step, start + i * step + 0.1,
             start + i * step - 0.05, start + i * step + 0.05, volume, i)
        for i in range(n)
    ]


def _mean_reverting(n=20, mid=100.0, amp=1.0, volume=100.0):
    """Alternating closes → strong negative cov → Roll spread > 0."""
    out = []
    for i in range(n):
        c = mid + (amp if i % 2 == 0 else -amp)
        out.append(_bar(c, c + 0.2, c - 0.2, c, volume, i))
    return out


# ---- ATR -----------------------------------------------------------------

def test_atr_flat_market_is_zero():
    """Flat prices → TR = 0 every bar → ATR = 0."""
    assert si.average_true_range(_flat_bars(20)) == 0.0


def test_atr_positive_when_range_positive():
    bars = [_bar(100, 102, 98, 100, 50, i) for i in range(20)]
    atr = si.average_true_range(bars)
    # H-L = 4 every bar, prev close = 100 → TR = max(4, 2, 2) = 4
    assert atr == pytest.approx(4.0, rel=0.01)


def test_atr_uses_last_period_bars():
    """Only the last N=14 TRs go into the mean."""
    # First 10 bars: huge range. Last 14: tiny range. Mean = ~last 14.
    bars = ([_bar(100, 120, 80, 100, 50, i) for i in range(10)]
            + [_bar(100, 100.5, 99.5, 100, 50, i + 10) for i in range(14)])
    atr = si.average_true_range(bars, period=14)
    # Only the last 14 TRs → each ~1.0. First 10 dropped.
    assert atr < 5.0


def test_atr_insufficient_data():
    assert si.average_true_range([_bar(100, 101, 99, 100, 1, 0)]) == 0.0
    assert si.average_true_range([]) == 0.0


# ---- Roll spread ---------------------------------------------------------

def test_roll_zero_on_trending_market():
    """Roll estimator undefined when cov(Δp_t, Δp_{t-1}) >= 0."""
    assert si.roll_effective_spread(_trending_up()) == 0.0


def test_roll_positive_on_mean_reverting():
    """Alternating closes → very negative serial cov → Roll > 0."""
    spread = si.roll_effective_spread(_mean_reverting(mid=100, amp=1.0))
    assert spread > 0.0


def test_roll_insufficient_data():
    assert si.roll_effective_spread([_bar(100, 100, 100, 100, 1, 0)]) == 0.0


# ---- Amihud --------------------------------------------------------------

def test_amihud_higher_when_less_volume():
    """Same price moves + less volume → higher illiquidity ratio."""
    high_vol = [_bar(100, 101, 99, 100 + (i % 2), 10000, i) for i in range(10)]
    low_vol = [_bar(100, 101, 99, 100 + (i % 2), 10, i) for i in range(10)]
    a_high = si.amihud_illiquidity(high_vol)
    a_low = si.amihud_illiquidity(low_vol)
    assert a_low > a_high, "low-volume should be MORE illiquid"


def test_amihud_zero_on_zero_returns():
    """Flat prices → returns = 0 → Amihud = 0."""
    assert si.amihud_illiquidity(_flat_bars(20)) == 0.0


# ---- Kyle proxy ----------------------------------------------------------

def test_kyle_proxy_higher_when_price_moves_more_per_unit_vol():
    small_moves = [_bar(100, 100.1, 99.9, 100 + i * 0.01, 1000, i) for i in range(10)]
    big_moves = [_bar(100, 105, 95, 100 + i * 2.0, 1000, i) for i in range(10)]
    k_small = si.kyle_lambda_proxy(small_moves)
    k_big = si.kyle_lambda_proxy(big_moves)
    assert k_big > k_small


# ---- Yang-Zhang volatility -----------------------------------------------

def test_yang_zhang_zero_on_flat():
    assert si.yang_zhang_volatility(_flat_bars(20)) == 0.0


def test_yang_zhang_positive_on_volatility():
    bars = [_bar(100 + i, 100 + i + 2, 100 + i - 2, 100 + i + 0.5, 100, i)
            for i in range(20)]
    assert si.yang_zhang_volatility(bars) > 0


# ---- Hasbrouck cost ------------------------------------------------------

def test_hasbrouck_scales_with_range():
    narrow = [_bar(100, 100.05, 99.95, 100, 100, i) for i in range(10)]
    wide = [_bar(100, 105, 95, 100, 100, i) for i in range(10)]
    assert si.hasbrouck_effective_cost(wide) > si.hasbrouck_effective_cost(narrow)


# ---- OFI proxy -----------------------------------------------------------

def test_ofi_positive_when_up_bars_dominant():
    """close > open every bar → positive OFI proxy."""
    bars = [_bar(100, 101, 100, 100.5, 100, i) for i in range(10)]
    assert si.ofi_from_ohlcv(bars) > 0


def test_ofi_negative_when_down_bars_dominant():
    bars = [_bar(100, 100, 99, 99.5, 100, i) for i in range(10)]
    assert si.ofi_from_ohlcv(bars) < 0


def test_ofi_bounded_in_negative_one_to_one():
    assert -1.0 <= si.ofi_from_ohlcv(_mean_reverting(20)) <= 1.0


# ---- compute_all ---------------------------------------------------------

def test_compute_all_returns_all_keys():
    bars = _mean_reverting(20)
    result = si.compute_all(bars, mid_price=100.0)
    for key in ("atr", "amihud_illiq", "roll_spread", "kyle_lambda",
                "yang_zhang_vol", "hasbrouck_cost", "ofi", "bars_used",
                "mid_price"):
        assert key in result
    assert result["bars_used"] == 20
    assert result["mid_price"] == 100.0


def test_compute_all_zero_bars_safe():
    result = si.compute_all([], mid_price=0.0)
    assert result["bars_used"] == 0
    for k in ("atr", "amihud_illiq", "roll_spread", "kyle_lambda",
              "yang_zhang_vol", "hasbrouck_cost", "ofi"):
        assert result[k] == 0.0


def test_compute_all_ignores_invalid_bars():
    bars = [_bar(0, 0, 0, 0, 0, 0),          # invalid close
            {"foo": "bar"},                    # missing keys
            None,                              # not a dict
            _bar(100, 90, 110, 100, 50, 0),   # H<L invalid
            _bar(100, 101, 99, 100, 100, 1),  # valid
            _bar(100, 101, 99, 100.5, 100, 2)] # valid
    result = si.compute_all(bars)
    assert result["bars_used"] == 2  # only the 2 valid bars
