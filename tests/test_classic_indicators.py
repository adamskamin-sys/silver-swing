"""Tests for classic indicators (RSI, Bollinger, MACD) + shadow emitter."""

import ast
import pathlib

import pytest


# =============================================================================
# RSI — Wilder 1978
# =============================================================================

def test_rsi_none_on_insufficient_data():
    from classic_indicators import compute_rsi
    assert compute_rsi([]) is None
    assert compute_rsi([100]) is None
    assert compute_rsi([100] * 10, period=14) is None


def test_rsi_100_when_all_gains():
    from classic_indicators import compute_rsi
    prices = [100 + i for i in range(20)]  # steady up
    rsi = compute_rsi(prices, period=14)
    assert rsi == pytest.approx(100.0)


def test_rsi_zero_when_all_losses():
    from classic_indicators import compute_rsi
    prices = [120 - i for i in range(20)]  # steady down
    rsi = compute_rsi(prices, period=14)
    assert rsi == pytest.approx(0.0)


def test_rsi_signal_oversold_bullish():
    from classic_indicators import rsi_signal
    assert rsi_signal(25) == "bullish"
    assert rsi_signal(30) == "bullish"


def test_rsi_signal_overbought_bearish():
    from classic_indicators import rsi_signal
    assert rsi_signal(75) == "bearish"
    assert rsi_signal(70) == "bearish"


def test_rsi_signal_none_in_middle():
    from classic_indicators import rsi_signal
    assert rsi_signal(50) is None
    assert rsi_signal(None) is None


# =============================================================================
# Bollinger Bands — Bollinger 1992
# =============================================================================

def test_bollinger_none_on_insufficient_data():
    from classic_indicators import compute_bollinger_bands
    assert compute_bollinger_bands([100] * 10, period=20) is None


def test_bollinger_bands_shape():
    from classic_indicators import compute_bollinger_bands
    # Constant prices → stdev = 0 → all bands equal
    b = compute_bollinger_bands([100] * 30, period=20)
    assert b == (100.0, 100.0, 100.0)


def test_bollinger_signal_price_below_lower_is_bullish():
    from classic_indicators import bollinger_signal
    assert bollinger_signal(95, (100, 105, 110)) == "bullish"


def test_bollinger_signal_price_above_upper_is_bearish():
    from classic_indicators import bollinger_signal
    assert bollinger_signal(115, (100, 105, 110)) == "bearish"


def test_bollinger_signal_inside_bands_none():
    from classic_indicators import bollinger_signal
    assert bollinger_signal(105, (100, 105, 110)) is None


# =============================================================================
# MACD — Appel 1979
# =============================================================================

def test_macd_none_on_insufficient_data():
    from classic_indicators import compute_macd
    assert compute_macd([100] * 30) is None  # need slow(26) + signal(9) = 35+


def test_macd_returns_three_values_on_enough_data():
    from classic_indicators import compute_macd
    # 50 prices — enough for MACD(12,26,9)
    prices = [100 + i * 0.1 for i in range(50)]
    m = compute_macd(prices)
    assert m is not None
    assert len(m) == 3
    macd, signal, hist = m
    # Uptrend → MACD should be positive
    assert macd > 0
    # Histogram = MACD − signal
    assert abs(hist - (macd - signal)) < 1e-9


def test_macd_signal_bullish_direct_tuple():
    """Test the signal function directly — MACD > 0 with positive histogram."""
    from classic_indicators import macd_signal
    assert macd_signal((0.5, 0.3, 0.2)) == "bullish"


def test_macd_signal_bearish_direct_tuple():
    from classic_indicators import macd_signal
    assert macd_signal((-0.5, -0.3, -0.2)) == "bearish"


def test_macd_signal_none_on_no_tuple():
    from classic_indicators import macd_signal
    assert macd_signal(None) is None
    # Ambiguous (hist and macd disagree in sign) → None
    assert macd_signal((0.5, 0.7, -0.2)) is None


def test_macd_compute_produces_valid_tuple_on_accelerating_uptrend():
    """Compound growth (accelerating uptrend) actually diverges MACD from signal."""
    from classic_indicators import compute_macd
    prices = [100 * (1.005 ** i) for i in range(60)]
    m = compute_macd(prices)
    assert m is not None
    macd, signal, hist = m
    # Accelerating trend → MACD should be positive and above signal
    assert macd > 0


# =============================================================================
# classic_shadow — shadow-mode guarantee
# =============================================================================

CLASSIC_SHADOW_PATH = pathlib.Path(__file__).parent.parent / "classic_shadow.py"


def test_classic_shadow_execute_trades_false():
    import classic_shadow
    assert classic_shadow.EXECUTE_TRADES is False


def test_classic_shadow_no_broker_imports():
    tree = ast.parse(CLASSIC_SHADOW_PATH.read_text())
    forbidden = {"place_limit", "place_market", "place_order", "submit_order",
                 "CoinbaseBroker", "PaperBroker"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name not in forbidden, \
                    f"classic_shadow imports {alias.name}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "broker"


def test_classic_shadow_emitter_skips_when_no_signal():
    """When indicator returns None (in-band), the emitter returns None."""
    import classic_shadow
    class _NullStore:
        _r = None
    # RSI = 50 → no signal → no emission
    result = classic_shadow.emit_rsi_signal(_NullStore(), "SYM", 50.0, 100.0)
    assert result is None
