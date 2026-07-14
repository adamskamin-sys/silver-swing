"""Sanity tests for bollinger_squeeze — the module is EXPERIMENTAL per its
docstring; these tests only cover the math + the fire-gate discipline
(Bollinger's rules: squeeze has NO direction, requires independent confirm)."""
from __future__ import annotations

import bollinger_squeeze as bs


# ---- bollinger_bands / bandwidth / percent_b -----------------------------

def test_bands_none_when_insufficient_history():
    assert bs.bollinger_bands([1.0] * 10, n=20) is None
    assert bs.bandwidth([1.0] * 10, n=20) is None
    assert bs.percent_b([1.0] * 10, n=20) is None


def test_bands_symmetric_around_mid_flat_series():
    """Flat closes → sd=0 → lo == mid == hi."""
    closes = [50.0] * 20
    lo, mid, hi = bs.bollinger_bands(closes, n=20, k=2.0)
    assert lo == mid == hi == 50.0


def test_bandwidth_flat_series_is_zero():
    assert bs.bandwidth([50.0] * 20) == 0.0


def test_bandwidth_positive_for_volatile_series():
    closes = [50.0 + (i % 4) * 0.5 for i in range(20)]
    w = bs.bandwidth(closes)
    assert w is not None and w > 0


def test_percent_b_none_when_bands_collapsed():
    """Flat series → hi == lo → %b undefined."""
    assert bs.percent_b([50.0] * 20) is None


def test_percent_b_at_midband_is_half():
    closes = [50.0 - 1.0, 50.0 + 1.0] * 10  # symmetric around 50, last = 51
    # last close 51 above mid 50, %b > 0.5
    pb = bs.percent_b(closes)
    assert pb is not None
    assert pb > 0.5


# ---- is_squeeze ----------------------------------------------------------

def test_is_squeeze_false_when_not_enough_history():
    assert bs.is_squeeze([50.0] * 50, lookback=126) is False


def test_is_squeeze_true_when_bandwidth_at_lookback_low():
    """Volatile first half, dead-flat second half → current width is at low."""
    volatile = [50.0 + (i % 5) * 2.0 for i in range(200)]
    flat_tail = [50.0] * 60
    closes = volatile + flat_tail
    assert bs.is_squeeze(closes, lookback=126) is True


def test_is_squeeze_false_when_bandwidth_expanded():
    """Flat first, volatile tail → current width is at the high, not low."""
    flat = [50.0] * 200
    volatile_tail = [50.0 + (i % 5) * 2.0 for i in range(60)]
    closes = flat + volatile_tail
    assert bs.is_squeeze(closes, lookback=126) is False


# ---- squeeze_long_signal — the discipline gates --------------------------

def _build_squeeze_release_up_series():
    """History that:
    - was squeezing right up until the last bar (coiled_recently=True on closes[:-1])
    - expands on the last bar and breaks above the upper band (%b > 1)."""
    # 200 volatile bars (drive lookback highs) + 60 dead-flat + one HUGE up-break
    volatile = [50.0 + (i % 5) * 2.0 for i in range(200)]
    flat = [50.0] * 60
    breakout = [55.0]                              # violent close above upper band
    return volatile + flat + breakout


def test_signal_false_when_no_prior_squeeze():
    """Rising series with no coil → no fire."""
    closes = [50.0 + i * 0.1 for i in range(300)]
    fire, why = bs.squeeze_long_signal(
        closes, trend_is_up=True, volume_confirms=True)
    assert fire is False
    assert "no prior squeeze" in why or "not yet released" in why


def test_signal_false_when_no_upward_release():
    """Coiled but not yet released — no direction picked. Tail must be
    slightly noisy so bands are computable (fully-flat tail collapses %b)."""
    volatile = [50.0 + (i % 5) * 2.0 for i in range(200)]
    coiled_tail = [50.0 + ((i % 3) - 1) * 0.05 for i in range(61)]  # ±0.05 wobble
    closes = volatile + coiled_tail
    fire, why = bs.squeeze_long_signal(
        closes, trend_is_up=True, volume_confirms=True)
    assert fire is False
    assert "not yet released" in why or "beware head-fake" in why


def test_signal_false_when_trend_disagrees():
    """Squeeze released up but trend gate says down → refuse (Bollinger rule #1)."""
    closes = _build_squeeze_release_up_series()
    fire, why = bs.squeeze_long_signal(
        closes, trend_is_up=False, volume_confirms=True)
    assert fire is False
    assert "trend gate disagrees" in why or "head-fake" in why


def test_signal_false_when_volume_missing():
    """Bollinger's rule #2: independent non-price confirm required."""
    closes = _build_squeeze_release_up_series()
    fire, why = bs.squeeze_long_signal(
        closes, trend_is_up=True, volume_confirms=False)
    assert fire is False
    assert "volume" in why.lower()


def test_signal_true_when_all_gates_pass():
    """Coiled → released up → trend agrees → volume confirms → FIRE."""
    closes = _build_squeeze_release_up_series()
    fire, why = bs.squeeze_long_signal(
        closes, trend_is_up=True, volume_confirms=True)
    assert fire is True
    assert "released up" in why
    assert "trend + volume confirm" in why


def test_squeeze_alone_never_fires():
    """The core Bollinger discipline: squeeze itself is directionless.
    Try every mid-signal state and require squeeze-only to be False."""
    # Just-coiled, still flat, no expansion
    coiled = [50.0 + (i % 5) * 2.0 for i in range(200)] + [50.0] * 61
    fire, _ = bs.squeeze_long_signal(
        coiled, trend_is_up=True, volume_confirms=True)
    assert fire is False, "Squeeze alone must NEVER fire (Bollinger's rule)"
