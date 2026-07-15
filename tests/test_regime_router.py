"""Tests for regime_router — the strategy adjuster on top of regime.classify_regime."""
from __future__ import annotations

from regime_router import regime_adjustments


class TestNeutralDefaults:
    def test_none_returns_neutral(self):
        r = regime_adjustments(None)
        assert r["gamma_multiplier"] == 1.0
        assert r["qty_multiplier"] == 1.0
        assert r["should_arm"] is True

    def test_unknown_regime_neutral(self):
        r = regime_adjustments({"regime": "unknown"})
        assert r["gamma_multiplier"] == 1.0
        assert r["qty_multiplier"] == 1.0
        assert r["should_arm"] is True


class TestTrendRegime:
    def test_wider_spread(self):
        r = regime_adjustments({"regime": "trend", "vol_state": "normal"})
        assert r["gamma_multiplier"] < 1.0  # lower γ = wider spread in AS
        assert r["should_arm"] is True

    def test_records_regime_in_inputs(self):
        r = regime_adjustments({"regime": "trend", "vol_state": "normal",
                                 "efficiency_ratio": 0.6})
        assert r["inputs"]["regime"] == "trend"
        assert r["inputs"]["efficiency_ratio"] == 0.6


class TestMeanRevertRegime:
    def test_tighter_spread(self):
        r = regime_adjustments({"regime": "mean_revert", "vol_state": "normal"})
        assert r["gamma_multiplier"] > 1.0  # higher γ = tighter spread
        assert r["should_arm"] is True

    def test_qty_unchanged_at_normal_vol(self):
        r = regime_adjustments({"regime": "mean_revert", "vol_state": "normal"})
        assert r["qty_multiplier"] == 1.0


class TestChopRegime:
    def test_should_not_arm(self):
        r = regime_adjustments({"regime": "chop", "vol_state": "normal"})
        assert r["should_arm"] is False

    def test_qty_downscale(self):
        r = regime_adjustments({"regime": "chop", "vol_state": "normal"})
        assert r["qty_multiplier"] < 0.5


class TestVolAmplifier:
    def test_stressed_vol_shrinks_qty(self):
        trend_normal = regime_adjustments({"regime": "trend", "vol_state": "normal"})
        trend_stressed = regime_adjustments({"regime": "trend", "vol_state": "stressed"})
        assert trend_stressed["qty_multiplier"] < trend_normal["qty_multiplier"]

    def test_calm_vol_boosts_qty(self):
        trend_normal = regime_adjustments({"regime": "trend", "vol_state": "normal"})
        trend_calm = regime_adjustments({"regime": "trend", "vol_state": "calm"})
        assert trend_calm["qty_multiplier"] > trend_normal["qty_multiplier"]


class TestBounds:
    def test_gamma_bounded(self):
        # Extreme mean_revert + calm → should still cap at 2.0
        r = regime_adjustments({"regime": "mean_revert", "vol_state": "calm"})
        assert 0.3 <= r["gamma_multiplier"] <= 2.0

    def test_qty_bounded(self):
        for regime in ("trend", "mean_revert", "chop", "unknown"):
            for vol in ("stressed", "normal", "calm"):
                r = regime_adjustments({"regime": regime, "vol_state": vol})
                assert 0.0 <= r["qty_multiplier"] <= 1.5


class TestReasonMessages:
    def test_reason_includes_regime(self):
        for regime in ("trend", "mean_revert", "chop"):
            r = regime_adjustments({"regime": regime, "vol_state": "normal"})
            assert regime in r["reason"] or regime.replace("_", "-") in r["reason"]

    def test_reason_includes_vol_state(self):
        r = regime_adjustments({"regime": "trend", "vol_state": "stressed"})
        assert "stressed" in r["reason"]
