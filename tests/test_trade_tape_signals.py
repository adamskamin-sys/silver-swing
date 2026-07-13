"""Tests for the trade-tape microstructure signals + gate + shadow harness.

Covers:
  - TradeTapeOFI rolling window signed volume
  - AggressorRunDetector edge-triggered threshold crossing
  - _trade_ofi_ok_for gate direction logic
  - tape_shadow guarantees no broker imports (mirrors twitter_shadow_only)
"""

import ast
import pathlib
import time

import pytest

from microstructure import (
    AggressorRunDetector, MicrostructureFilter, TradeTapeOFI,
)
from sleeves import SleeveConfig, SleeveState, SleeveStateEnum
from swing_leg import SwingTrader


# =============================================================================
# TradeTapeOFI
# =============================================================================

def test_ofi_none_when_empty():
    ofi = TradeTapeOFI(max_window_secs=60)
    assert ofi.ofi(60) is None


def test_ofi_positive_when_buyers_dominant():
    ofi = TradeTapeOFI(max_window_secs=60)
    now = time.time()
    ofi.update(100.0, 5.0, "buy", ts=now)
    ofi.update(100.1, 5.0, "buy", ts=now)
    ofi.update(100.2, 1.0, "sell", ts=now)
    v = ofi.ofi(60)
    assert v is not None
    assert 0.5 < v <= 1.0


def test_ofi_negative_when_sellers_dominant():
    ofi = TradeTapeOFI(max_window_secs=60)
    now = time.time()
    ofi.update(100.0, 1.0, "buy", ts=now)
    ofi.update(100.0, 5.0, "sell", ts=now)
    ofi.update(100.0, 5.0, "sell", ts=now)
    v = ofi.ofi(60)
    assert v is not None
    assert -1.0 <= v < -0.5


def test_ofi_rolls_off_older_than_window():
    ofi = TradeTapeOFI(max_window_secs=300)
    old = time.time() - 1000  # way outside 60s window
    ofi.update(100.0, 10.0, "buy", ts=old)
    # Add a fresh sell → in a 60s query window we should see all-sell
    ofi.update(100.0, 5.0, "sell", ts=time.time())
    v = ofi.ofi(60)
    assert v is not None
    assert v == pytest.approx(-1.0)


# =============================================================================
# AggressorRunDetector
# =============================================================================

def test_run_starts_at_zero():
    d = AggressorRunDetector(threshold=8)
    assert d.state()["current_run"] == 0


def test_run_increments_on_same_side():
    d = AggressorRunDetector(threshold=8)
    for _ in range(5):
        r = d.update(100.0, 1.0, "buy")
        assert r is None  # below threshold
    assert d.state()["current_run"] == 5
    assert d.state()["current_side"] == "buy"


def test_run_resets_on_opposite_side():
    d = AggressorRunDetector(threshold=8)
    for _ in range(4):
        d.update(100.0, 1.0, "buy")
    d.update(100.0, 1.0, "sell")
    assert d.state()["current_run"] == 1
    assert d.state()["current_side"] == "sell"


def test_run_crossing_edge_triggered():
    """Only ONE crossing event per run, even if the run keeps going."""
    d = AggressorRunDetector(threshold=3)
    assert d.update(100.0, 1.0, "buy") is None
    assert d.update(100.0, 1.0, "buy") is None
    crossing = d.update(100.0, 1.0, "buy")  # 3rd → crosses threshold
    assert crossing is not None
    assert crossing["run_length"] == 3
    assert crossing["side"] == "buy"
    # More same-side prints — no more crossings until direction resets
    assert d.update(100.0, 1.0, "buy") is None
    assert d.update(100.0, 1.0, "buy") is None
    # Reset direction, then re-cross
    d.update(100.0, 1.0, "sell")
    d.update(100.0, 1.0, "sell")
    crossing2 = d.update(100.0, 1.0, "sell")
    assert crossing2 is not None
    assert crossing2["side"] == "sell"


# =============================================================================
# MicrostructureFilter wires the tape signals + fires callback
# =============================================================================

def test_filter_maintains_tape_signals_without_env_flags(monkeypatch):
    """Even with all SWING_MS_* off, TradeTapeOFI + AggressorRun update."""
    for k in ("SWING_MS_ALL", "SWING_MS_VPIN", "SWING_MS_LAMBDA",
              "SWING_MS_OBI", "SWING_MS_SPREAD_BAND", "SWING_MS_AUTOCORR"):
        monkeypatch.delenv(k, raising=False)
    f = MicrostructureFilter()
    for _ in range(5):
        f.on_trade(100.0, 1.0, "buy")
    assert f.trade_ofi.ofi(60) == pytest.approx(1.0)  # all buys
    assert f.aggressor_run.state()["current_run"] == 5


def test_filter_fires_callback_on_run_crossing(monkeypatch):
    monkeypatch.setenv("SWING_MS_AGGRESSOR_RUN_THRESHOLD", "3")
    f = MicrostructureFilter()
    received = []
    f.on_aggressor_run_crossing = lambda c, _f: received.append(c)
    for _ in range(3):
        f.on_trade(100.0, 1.0, "buy")
    assert len(received) == 1
    assert received[0]["run_length"] == 3


# =============================================================================
# _trade_ofi_ok_for gate on SwingTrader
# =============================================================================

class _MSHost:
    """Minimal shim exposing a `.ms` attribute (mirrors SwingTrader.self.ms)."""
    def __init__(self, ofi_value):
        self.ms = _MSStub(ofi_value)


class _MSStub:
    def __init__(self, val):
        self.trade_ofi = _OFIStub(val)


class _OFIStub:
    def __init__(self, val): self._v = val
    def ofi(self, _w): return self._v


def test_trade_ofi_ok_when_no_data():
    host = _MSHost(None)
    sc = SleeveConfig(id="s1", name="t", qty=1,
                      trade_ofi_gate_enabled=True, trade_ofi_threshold=0.65)
    assert SwingTrader._trade_ofi_ok_for(host, sc, "BUY") is True
    assert SwingTrader._trade_ofi_ok_for(host, sc, "SELL") is True


def test_trade_ofi_gate_blocks_sell_into_buyer_pressure():
    host = _MSHost(0.80)  # buyers dominant
    sc = SleeveConfig(id="s1", name="t", qty=1,
                      trade_ofi_gate_enabled=True, trade_ofi_threshold=0.65)
    assert SwingTrader._trade_ofi_ok_for(host, sc, "SELL") is False


def test_trade_ofi_gate_blocks_buy_into_seller_pressure():
    host = _MSHost(-0.80)  # sellers dominant
    sc = SleeveConfig(id="s1", name="t", qty=1,
                      trade_ofi_gate_enabled=True, trade_ofi_threshold=0.65)
    assert SwingTrader._trade_ofi_ok_for(host, sc, "BUY") is False


def test_trade_ofi_gate_allows_arm_when_ofi_below_threshold():
    host = _MSHost(0.30)  # mild imbalance, below threshold
    sc = SleeveConfig(id="s1", name="t", qty=1,
                      trade_ofi_gate_enabled=True, trade_ofi_threshold=0.65)
    assert SwingTrader._trade_ofi_ok_for(host, sc, "BUY") is True
    assert SwingTrader._trade_ofi_ok_for(host, sc, "SELL") is True


# =============================================================================
# tape_shadow shadow-mode guarantee (mirror of test_twitter_shadow_only)
# =============================================================================

TAPE_PATH = pathlib.Path(__file__).parent.parent / "tape_shadow.py"


def test_tape_shadow_execute_trades_flag_is_false():
    import tape_shadow
    assert tape_shadow.EXECUTE_TRADES is False


def test_tape_shadow_no_broker_imports():
    tree = ast.parse(TAPE_PATH.read_text())
    forbidden_names = {"place_limit", "place_market", "place_order",
                       "submit_order", "CoinbaseBroker", "PaperBroker"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name not in forbidden_names, \
                    f"tape_shadow imports {alias.name} — shadow-mode broken"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "broker", \
                    "tape_shadow imports broker module — shadow-mode broken"


def test_tape_shadow_no_order_placing_calls():
    tree = ast.parse(TAPE_PATH.read_text())
    forbidden = {"place_limit", "place_market", "place_order", "submit_order"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden, \
                f"tape_shadow calls .{node.func.attr}(...) — shadow-mode broken"
