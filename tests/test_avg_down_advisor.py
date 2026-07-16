"""Tests for avg_down_advisor.py — pure unit tests, no live store required.

The advisor is pure computation: given a state/config/snapshot dict, it
returns expert-derived scale-in parameters. We test the helper functions
directly and the full advise() path via a mock store.
"""
from __future__ import annotations

import types
import pytest

from avg_down_advisor import _safe_float, _green_sleeve


# ── _safe_float ──────────────────────────────────────────────────────────────

class TestSafeFloat:
    def test_basic(self):
        assert _safe_float(1.5) == 1.5

    def test_none_returns_default(self):
        assert _safe_float(None) == 0.0
        assert _safe_float(None, 9.9) == 9.9

    def test_zero_returns_default(self):
        # _safe_float treats 0 as falsy — returns the default not 0.0
        assert _safe_float(0, 5.0) == 5.0

    def test_string_numeric(self):
        assert _safe_float("3.14") == pytest.approx(3.14)

    def test_bad_string_returns_default(self):
        assert _safe_float("nope", 7.0) == 7.0


# ── _green_sleeve ─────────────────────────────────────────────────────────────

def _make_state(sleeve_states):
    return {"sleeves": sleeve_states}

def _make_config(sleeves_list):
    return {"sleeves": sleeves_list}


class TestGreenSleeve:
    def test_empty_state(self):
        sid, ss, sc = _green_sleeve({}, {})
        assert sid is None

    def test_no_armed_sell(self):
        state = _make_state({"s1": {"state": "ARMED_BUY", "own_avg_entry": 100.0}})
        config = _make_config([{"id": "s1", "qty": 1, "buy_px": 90, "sell_px": 110}])
        sid, _, _ = _green_sleeve(state, config)
        assert sid is None

    def test_armed_sell_no_avg_entry(self):
        state = _make_state({"s1": {"state": "ARMED_SELL", "own_avg_entry": None}})
        config = _make_config([{"id": "s1", "qty": 1, "buy_px": 90, "sell_px": 110}])
        sid, _, _ = _green_sleeve(state, config)
        assert sid is None

    def test_armed_sell_no_config(self):
        state = _make_state({"s1": {"state": "ARMED_SELL", "own_avg_entry": 100.0}})
        config = _make_config([])  # no sleeve config for s1
        sid, _, _ = _green_sleeve(state, config)
        assert sid is None

    def test_returns_armed_sell_with_avg(self):
        state = _make_state({"s1": {"state": "ARMED_SELL", "own_avg_entry": 100.0, "armed_sell_since_ts": 1000.0}})
        config = _make_config([{"id": "s1", "qty": 2, "buy_px": 90.0, "sell_px": 115.0}])
        sid, ss, sc = _green_sleeve(state, config)
        assert sid == "s1"
        assert ss["own_avg_entry"] == 100.0
        assert sc["qty"] == 2

    def test_prefers_most_recent(self):
        state = _make_state({
            "s1": {"state": "ARMED_SELL", "own_avg_entry": 100.0, "armed_sell_since_ts": 500.0},
            "s2": {"state": "ARMED_SELL", "own_avg_entry": 200.0, "armed_sell_since_ts": 1500.0},
        })
        config = _make_config([
            {"id": "s1", "qty": 1, "buy_px": 90, "sell_px": 110},
            {"id": "s2", "qty": 1, "buy_px": 180, "sell_px": 220},
        ])
        sid, _, _ = _green_sleeve(state, config)
        assert sid == "s2"  # higher ts wins


# ── advise() via mock store ───────────────────────────────────────────────────

class _MockStore:
    """Minimal stand-in for state_store.make_store()."""
    def __init__(self, state=None, config=None, snapshot=None, portfolio=None):
        self._state = state or {}
        self._config = config or {}
        self._snapshot = snapshot or {}
        self._portfolio = portfolio or {}

    def get_state(self, tenant, symbol):
        return self._state

    def get_config(self, tenant, symbol):
        if symbol == "__portfolio__":
            return self._portfolio
        return self._config

    def get_snapshot(self, tenant, symbol):
        return self._snapshot


def _make_advisor_modules(monkeypatch, store: _MockStore):
    """Patch state_store.make_store() to return our mock, plus stub experts."""
    import sys

    # state_store stub
    ss_mod = types.ModuleType("state_store")
    ss_mod.make_store = lambda path: store
    monkeypatch.setitem(sys.modules, "state_store", ss_mod)

    # avg_down_signal stub — always GREEN
    sig_mod = types.ModuleType("avg_down_signal")
    sig_mod.average_down_signal = lambda **kw: {
        "light": "green",
        "reasons": ["test signal green"],
        "checks": {"regime": True, "at_floor": True},
    }
    monkeypatch.setitem(sys.modules, "avg_down_signal", sig_mod)

    # expert_stop stub
    stop_mod = types.ModuleType("expert_stop")
    StopResult = types.SimpleNamespace
    stop_mod.optimal_stop_distance = lambda **kw: StopResult(stop_distance=2.0)
    monkeypatch.setitem(sys.modules, "expert_stop", stop_mod)

    # expert_params stub
    ep_mod = types.ModuleType("expert_params")
    ep_mod.expert_params = lambda symbol, atr: {
        "trail_activation_offset": 3.0,
        "trail_distance": 2.0,
    }
    monkeypatch.setitem(sys.modules, "expert_params", ep_mod)


class TestAdvise:
    def _armed_sell_store(self, own_avg=100.0, mark=95.0, qty=2, sell_px=120.0):
        state = {
            "sleeves": {
                "s1": {
                    "state": "ARMED_SELL",
                    "own_avg_entry": own_avg,
                    "armed_sell_since_ts": 9000.0,
                }
            }
        }
        config = {
            "sleeves": [{"id": "s1", "qty": qty, "buy_px": 90.0, "sell_px": sell_px, "name": "Test sleeve"}],
            "tick_size": 0.01,
            "contract_size": 1.0,
        }
        snapshot = {"last_mark": mark, "atr": 1.5}
        return _MockStore(state=state, config=config, snapshot=snapshot)

    def test_no_armed_sell_returns_amber(self, monkeypatch):
        store = _MockStore(state={}, config={}, snapshot={})
        _make_advisor_modules(monkeypatch, store)
        import avg_down_advisor
        result = avg_down_advisor.advise("ZEC-20DEC30-CDE", "adam-live")
        assert result["ok"] is False
        assert result["light"] == "amber"

    def test_green_signal_returns_full_advice(self, monkeypatch):
        store = self._armed_sell_store(own_avg=100.0, mark=95.0, qty=2, sell_px=120.0)
        _make_advisor_modules(monkeypatch, store)
        import importlib, avg_down_advisor
        importlib.reload(avg_down_advisor)
        _make_advisor_modules(monkeypatch, store)

        result = avg_down_advisor.advise("ZEC-20DEC30-CDE", "adam-live")
        assert result["ok"] is True
        assert result["light"] == "green"
        assert result["recommended_add_qty"] == 1
        assert result["suggested_buy_px"] < result["current_mark"] + 0.01  # at or below mark
        assert result["blended_entry_px"] < result["current_avg_entry"]    # scale-in lowers avg
        assert result["new_stop_px"] < result["blended_entry_px"]          # stop below entry
        assert result["new_trail_trigger"] > result["blended_entry_px"]    # trail above entry
        assert result["new_sell_px"] == pytest.approx(120.0)              # sell unchanged

    def test_blended_entry_math(self, monkeypatch):
        # Own_avg=100, qty=2, add_qty=1 at mark=90 → blended = (200+90)/3 = 96.67
        store = self._armed_sell_store(own_avg=100.0, mark=90.0, qty=2)
        _make_advisor_modules(monkeypatch, store)
        import importlib, avg_down_advisor
        importlib.reload(avg_down_advisor)
        _make_advisor_modules(monkeypatch, store)

        result = avg_down_advisor.advise("ZEC-20DEC30-CDE", "adam-live")
        assert result["ok"] is True
        expected_blended = (100.0 * 2 + 90.0 * 1) / 3
        assert result["blended_entry_px"] == pytest.approx(expected_blended, abs=0.02)

    def test_no_mark_returns_red(self, monkeypatch):
        state = {
            "sleeves": {
                "s1": {"state": "ARMED_SELL", "own_avg_entry": 100.0, "armed_sell_since_ts": 1.0}
            }
        }
        config = {
            "sleeves": [{"id": "s1", "qty": 1, "buy_px": None, "sell_px": 120.0}],
        }
        store = _MockStore(state=state, config=config, snapshot={})
        _make_advisor_modules(monkeypatch, store)
        import importlib, avg_down_advisor
        importlib.reload(avg_down_advisor)
        _make_advisor_modules(monkeypatch, store)

        result = avg_down_advisor.advise("ZEC-20DEC30-CDE", "adam-live")
        assert result["ok"] is False
        assert result["light"] == "red"
