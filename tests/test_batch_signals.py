"""Tests for the seven-feature batch: funding, kelly, adaptive spread,
crossex, tick recording, ML predictor, dynamic correlation."""

import ast
import pathlib
import time

import pytest


# =============================================================================
# funding_signals
# =============================================================================

def test_funding_is_perp():
    from funding_signals import is_perp
    assert is_perp("BTC-PERP-INTX")
    assert is_perp("ETH-PERP-INTX")
    assert not is_perp("SLR-27AUG26-CDE")
    assert not is_perp("")


def test_funding_rate_reads_from_snapshot():
    from funding_signals import funding_rate_of
    assert funding_rate_of({"funding_rate": 0.0001}) == pytest.approx(0.0001)
    assert funding_rate_of({"predicted_funding_rate": -0.0002}) == pytest.approx(-0.0002)
    assert funding_rate_of({}) is None
    assert funding_rate_of(None) is None


def test_funding_scanner_boost_negative_funding_boosts_longs():
    from funding_signals import scanner_boost
    # Very negative funding (shorts paying longs) should BOOST long tile score
    b = scanner_boost(-0.001)  # -0.1% per 8h — extreme
    assert b > 1.0
    assert b <= 1.5  # capped


def test_funding_scanner_boost_positive_funding_penalizes_longs():
    from funding_signals import scanner_boost
    b = scanner_boost(0.001)  # +0.1% per 8h — extreme
    assert b < 1.0
    assert b >= 0.5


def test_funding_gate_blocks_buy_when_expensive():
    from funding_signals import funding_gate_ok_for_buy
    assert funding_gate_ok_for_buy(0.001, 0.0005) is False
    assert funding_gate_ok_for_buy(-0.001, 0.0005) is True
    assert funding_gate_ok_for_buy(None, 0.0005) is True  # permissive default


# =============================================================================
# kelly
# =============================================================================

def test_kelly_returns_none_when_insufficient_data():
    from kelly import compute_kelly_multiplier
    assert compute_kelly_multiplier([]) is None
    assert compute_kelly_multiplier([1.0, 2.0]) is None  # < min_cycles


def test_kelly_positive_edge_produces_positive_multiplier():
    from kelly import compute_kelly_multiplier
    # 8 cycles, 6 wins @ $10 avg, 2 losses @ $5 avg → positive edge
    pnls = [10, 10, 10, 10, 10, 10, -5, -5]
    m = compute_kelly_multiplier(pnls, kelly_fraction=0.25, min_cycles=8)
    assert m is not None
    assert 0 < m <= 1.0


def test_kelly_never_returns_above_1():
    from kelly import compute_kelly_multiplier
    # All wins → module returns exactly 1.0 (safe to use full size)
    pnls = [10] * 10
    m = compute_kelly_multiplier(pnls, min_cycles=8)
    assert m == 1.0


def test_kelly_size_from_qty_rounds_and_floors_at_1():
    from kelly import size_from_qty
    assert size_from_qty(4, 0.5) == 2
    assert size_from_qty(4, 0.1) == 1  # floor at 1
    assert size_from_qty(4, None) == 4  # None → use full qty
    assert size_from_qty(1, 0.5) == 1  # 1 × 0.5 = 0.5 → rounds to 1 via floor


# =============================================================================
# adaptive_spread
# =============================================================================

def test_realized_vol_returns_none_on_insufficient_data():
    from adaptive_spread import realized_vol_from_history
    assert realized_vol_from_history([]) is None
    assert realized_vol_from_history([(1, 100)]) is None


def test_realized_vol_computes_stdev_of_log_returns():
    from adaptive_spread import realized_vol_from_history
    now = time.time()
    # Steady price → very low vol
    hist = [(now - 5 * i, 100.0) for i in range(10)]
    v = realized_vol_from_history(hist, window_secs=60)
    # All same prices → log_returns all 0 → vol = 0
    assert v == pytest.approx(0.0)


def test_spread_multiplier_widens_only_when_vol_spikes():
    from adaptive_spread import spread_multiplier
    assert spread_multiplier(0.02, 0.01, max_multiplier=2.0) == pytest.approx(2.0)  # 2x, capped
    assert spread_multiplier(0.005, 0.01, max_multiplier=2.0) == 1.0  # tightening not allowed
    assert spread_multiplier(None, 0.01) == 1.0  # permissive default
    assert spread_multiplier(0.02, None) == 1.0


def test_adjusted_targets_widens_symmetrically():
    from adaptive_spread import adjusted_targets
    # sell=65, buy=63 → mid=64, half=1. mult=2 → new half=2 → sell=66, buy=62
    new_s, new_b = adjusted_targets(65.0, 63.0, 2.0)
    assert new_s == pytest.approx(66.0)
    assert new_b == pytest.approx(62.0)
    # mult=1 → no change
    new_s, new_b = adjusted_targets(65.0, 63.0, 1.0)
    assert (new_s, new_b) == (65.0, 63.0)


# =============================================================================
# crossex
# =============================================================================

def test_crossex_symbol_mapping():
    from crossex import binance_symbol_for
    assert binance_symbol_for("BTC-PERP-INTX") == "BTCUSDT"
    assert binance_symbol_for("ETH-PERP-INTX") == "ETHUSDT"
    assert binance_symbol_for("SLR-27AUG26-CDE") is None  # no mapping for silver
    assert binance_symbol_for("") is None


def test_crossex_gate_permissive_when_ref_missing(monkeypatch):
    from crossex import crossex_gate_ok
    # Silver has no binance mapping → permissive default
    ok, div = crossex_gate_ok("SLR-27AUG26-CDE", 30.5, max_divergence_pct=1.0)
    assert ok is True
    assert div is None


# =============================================================================
# tick_recorder
# =============================================================================

def test_tick_recorder_no_op_when_env_off(monkeypatch, tmp_path):
    from tick_recorder import TickRecorder
    monkeypatch.delenv("SWING_TICK_RECORDING", raising=False)
    r = TickRecorder("TEST", base_dir=str(tmp_path))
    r.on_ticker(100.0, 100.1, 100.05)
    r.on_trade(100.0, 1.0, "buy")
    # No files should be created — recorder is disabled
    assert list(tmp_path.iterdir()) == []


def test_tick_recorder_writes_when_env_on(monkeypatch, tmp_path):
    from tick_recorder import TickRecorder
    monkeypatch.setenv("SWING_TICK_RECORDING", "1")
    r = TickRecorder("TEST", base_dir=str(tmp_path))
    r.on_ticker(100.0, 100.1, 100.05)
    r.on_trade(100.0, 1.0, "buy")
    r.close()
    # A file should have been created under a date dir
    date_dirs = list(tmp_path.iterdir())
    assert len(date_dirs) == 1
    files = list(date_dirs[0].iterdir())
    assert len(files) == 1
    content = files[0].read_text()
    assert '"kind":"ticker"' in content
    assert '"kind":"trade"' in content


# =============================================================================
# ml_predictor
# =============================================================================

def test_ml_extract_returns_none_when_no_microstructure():
    from ml_predictor import extract_features
    assert extract_features({}) is None
    assert extract_features(None) is None


def test_ml_extract_produces_fixed_schema():
    from ml_predictor import extract_features, FEATURE_NAMES
    snap = {
        "last_mark": 100.0,
        "microstructure": {
            "vpin": 0.4,
            "trade_ofi_60s": 0.2,
            "trade_ofi_300s": 0.1,
            "aggressor_run": {"current_run": 5, "current_side": "buy", "threshold": 8},
        },
        "expert_params": {"atr": 0.5},
        "price_history": [],
    }
    feats = extract_features(snap)
    assert feats is not None
    assert len(feats) == len(FEATURE_NAMES)


def test_ml_predict_bullish_when_features_align():
    from ml_predictor import predict
    # Strong bullish signals: low VPIN, positive OFI, positive returns
    feats = [
        0.2,   # low vpin (below 0.5 neutral → bullish contribution via negative weight)
        0.8,   # strong buy-side OFI
        0.6,   # medium-term buy OFI
        0.005, # ATR normalized
        1.0,   # buyer-aggressor run
        1.0,   # US session
        0.001, # positive 1m return
        0.002, # positive 5m return
    ]
    score = predict(feats)
    assert score > 0


def test_ml_predict_bearish_when_features_align():
    from ml_predictor import predict
    feats = [
        0.8,   # high vpin (above 0.5 → bearish penalty)
        -0.8, -0.6, 0.005, -1.0, 0.0, -0.002, -0.003,
    ]
    score = predict(feats)
    assert score < 0


# =============================================================================
# ml_predictor shadow-mode guarantee
# =============================================================================

ML_PATH = pathlib.Path(__file__).parent.parent / "ml_predictor.py"


def test_ml_predictor_execute_trades_flag_is_false():
    import ml_predictor
    assert ml_predictor.EXECUTE_TRADES is False


def test_ml_predictor_no_broker_imports():
    tree = ast.parse(ML_PATH.read_text())
    forbidden = {"place_limit", "place_market", "place_order", "submit_order",
                 "CoinbaseBroker", "PaperBroker"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name not in forbidden, \
                    f"ml_predictor imports {alias.name} — shadow-mode broken"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "broker", \
                    "ml_predictor imports broker — shadow-mode broken"


# =============================================================================
# correlation dynamic
# =============================================================================

def test_dynamic_correlation_none_when_no_history():
    class _NoStore:
        def get_snapshot(self, *args, **kwargs): return None
        def list_symbols(self, *_): return []
    from correlation import rolling_correlation
    assert rolling_correlation(_NoStore(), "t", "A", "B") is None


def test_dynamic_correlation_returns_high_for_lockstep_prices():
    """Two symbols whose prices move in lockstep should have corr ≈ +1."""
    class _StubStore:
        def __init__(self):
            base = time.time()
            self._hist_a = [(base + i * 60, 100 + i) for i in range(20)]
            # B is 2× A (perfect linear relationship)
            self._hist_b = [(base + i * 60, 200 + 2 * i) for i in range(20)]
        def get_snapshot(self, tenant, sym):
            return {"price_history": self._hist_a if sym == "A" else self._hist_b}
        def list_symbols(self, _t):
            return ["A", "B"]
    from correlation import rolling_correlation
    c = rolling_correlation(_StubStore(), "t", "A", "B")
    assert c is not None
    assert c > 0.99
