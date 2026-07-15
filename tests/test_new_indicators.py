"""Smoke tests for the 5 new indicator modules added 2026-07-15:

  * kama.py           — Kaufman's Adaptive Moving Average
  * vwap.py           — VWAP + Anchored VWAP + bands
  * cvd.py            — Cumulative Volume Delta divergence
  * ehlers_fisher.py  — Ehlers Fisher Transform
  * yang_zhang.py     — Yang-Zhang volatility estimator

Basic sanity: functions don't crash on well-formed sample data, and
return sensibly-shaped results. NOT exhaustive coverage — these are
verify-they-work-and-don't-explode tests suitable for a first landing.
Deeper property-based tests can come as follow-up.
"""
from __future__ import annotations

import random


def _synthetic_prices(n: int = 100, seed: int = 42) -> list[float]:
    """Generate a synthetic price series with mild trend + noise."""
    random.seed(seed)
    price = 100.0
    out = []
    for _ in range(n):
        price *= 1 + random.gauss(0.0005, 0.01)  # small trend + 1% noise
        out.append(round(price, 4))
    return out


def _synthetic_bars(n: int = 100, seed: int = 42) -> list[dict]:
    """Generate synthetic OHLCV bars."""
    random.seed(seed)
    price = 100.0
    out = []
    for _ in range(n):
        open_p = price
        move = random.gauss(0.0005, 0.01)
        close_p = price * (1 + move)
        high_p = max(open_p, close_p) * (1 + abs(random.gauss(0, 0.005)))
        low_p = min(open_p, close_p) * (1 - abs(random.gauss(0, 0.005)))
        vol = max(1.0, random.gauss(1000, 200))
        out.append({
            "open": round(open_p, 4),
            "high": round(high_p, 4),
            "low": round(low_p, 4),
            "close": round(close_p, 4),
            "volume": round(vol, 2),
        })
        price = close_p
    return out


# ---- KAMA ------------------------------------------------------------
def test_kama_returns_value():
    import kama
    prices = _synthetic_prices(50)
    k = kama.kama(prices)
    assert k is not None, "KAMA should return a value on 50 prices"
    assert isinstance(k, float)
    # KAMA should be within the price range
    assert min(prices) * 0.9 < k < max(prices) * 1.1


def test_kama_er_returns_value():
    import kama
    prices = _synthetic_prices(50)
    er = kama.efficiency_ratio(prices)
    assert er is not None
    assert 0.0 <= er <= 1.0


def test_kama_signal_returns_dict():
    import kama
    prices = _synthetic_prices(50)
    sig = kama.kama_signal(prices)
    assert sig is not None
    assert sig["signal"] in ("buy", "sell", "hold")
    assert "reason" in sig


def test_kama_returns_none_on_insufficient_data():
    import kama
    assert kama.kama([]) is None
    assert kama.kama([100.0]) is None


# ---- VWAP ------------------------------------------------------------
def test_vwap_returns_value():
    import vwap
    bars = _synthetic_bars(50)
    v = vwap.vwap(bars)
    assert v is not None
    assert isinstance(v, float)


def test_anchored_vwap_returns_value():
    import vwap
    bars = _synthetic_bars(100)
    av = vwap.anchored_vwap(bars, anchor_idx=-50)
    assert av is not None


def test_vwap_bands():
    import vwap
    bars = _synthetic_bars(50)
    bands = vwap.vwap_bands(bars, num_std=1.0)
    assert bands is not None
    assert bands["upper"] > bands["vwap"] > bands["lower"]
    assert bands["std"] > 0


def test_vwap_signal_returns_dict():
    import vwap
    bars = _synthetic_bars(50)
    v = vwap.vwap(bars)
    sig = vwap.vwap_signal(bars, price=v)  # price == vwap
    assert sig["price_vs_vwap"] == "at"


def test_vwap_returns_none_on_empty():
    import vwap
    assert vwap.vwap([]) is None


# ---- CVD -------------------------------------------------------------
def test_cvd_from_bars_returns_list():
    import cvd
    bars = _synthetic_bars(50)
    result = cvd.cvd_from_bars(bars)
    assert result is not None
    assert len(result) == len(bars) - 1


def test_cvd_divergence_returns_dict():
    import cvd
    bars = _synthetic_bars(50)
    div = cvd.cvd_divergence(bars, lookback=20)
    assert div is not None
    assert div["divergence"] in ("bullish", "bearish", "none")


def test_cvd_returns_none_on_insufficient_data():
    import cvd
    assert cvd.cvd_from_bars([]) is None
    assert cvd.cvd_from_bars([{"close": 100}]) is None


# ---- Fisher Transform ------------------------------------------------
def test_fisher_transform_returns_dict():
    import ehlers_fisher
    prices = _synthetic_prices(50)
    f = ehlers_fisher.fisher_transform(prices)
    assert f is not None
    assert f["crossover"] in ("up", "down", "none")


def test_fisher_returns_none_on_insufficient_data():
    import ehlers_fisher
    assert ehlers_fisher.fisher_transform([100.0]) is None


# ---- Yang-Zhang ------------------------------------------------------
def test_yang_zhang_variance_returns_value():
    import yang_zhang
    bars = _synthetic_bars(30)
    v = yang_zhang.yang_zhang_variance(bars)
    assert v is not None
    assert v >= 0.0


def test_yang_zhang_vol_returns_value():
    import yang_zhang
    bars = _synthetic_bars(30)
    vol = yang_zhang.yang_zhang_volatility(bars)
    assert vol is not None
    assert vol >= 0.0


def test_yz_vs_atr_assessment():
    import yang_zhang
    bars = _synthetic_bars(30)
    result = yang_zhang.yz_vs_atr_assessment(bars, atr_14=1.0)
    assert result is not None
    assert result["assessment"] in ("atr_understates_vol", "atr_ok", "atr_overstates_vol")


def test_yz_returns_none_on_insufficient_data():
    import yang_zhang
    assert yang_zhang.yang_zhang_variance([]) is None
    assert yang_zhang.yang_zhang_volatility([{"open": 0, "high": 0, "low": 0, "close": 0}]) is None
