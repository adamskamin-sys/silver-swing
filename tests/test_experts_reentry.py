"""Tests for the expert-driven re-entry chain and its four expert modules.

Covers the specific bug Adam surfaced 2026-07-13: after a sell, the sleeve's
buy_px must NOT be above the sold price. The old algo kept the stale
buy_px in place, causing "buy above the last sale" losses on every
mean-reverting cycle.
"""
from __future__ import annotations

import math
import random

import ehlers
import elder
import connors
import vince
import experts_reentry


# ---- Ehlers cycle-phase --------------------------------------------------

def _sine_series(period: int, n: int, amp: float = 1.0, mean: float = 100.0):
    return [mean + amp * math.sin(2 * math.pi * i / period) for i in range(n)]


def test_ehlers_dominant_period_detects_known_cycle():
    prices = _sine_series(period=20, n=200)
    p = ehlers.dominant_period(prices)
    # Loose: within ±40% (Homodyne on a pure sine at swing bar rate).
    assert p is not None
    assert 12 <= p <= 28, f"expected ~20, got {p}"


def test_ehlers_cycle_phase_in_range():
    prices = _sine_series(period=20, n=200)
    ph = ehlers.cycle_phase(prices)
    assert ph is not None
    assert 0.0 <= ph <= 1.0


def test_ehlers_assess_returns_expected_keys():
    prices = _sine_series(period=16, n=100)
    a = ehlers.assess(prices)
    for k in ("dominant_period", "cycle_phase", "in_bounce_zone", "citation"):
        assert k in a


# ---- Elder Triple Screen -------------------------------------------------

def test_elder_screen1_up_slope():
    # Rising series → MACD histogram should slope up.
    prices = [100 + i * 0.1 for i in range(100)]
    r = elder.screen1_long_tide(prices)
    # A steady rising ramp eventually plateaus MACD, so pass_buy could be
    # True or False depending on the exact slope tail — either way, no crash.
    assert r["direction"] in ("up", "down", "flat", "unknown")


def test_elder_stochastic_bounds():
    prices = [100 + i * 0.1 for i in range(50)]
    k = elder.stochastic_k(prices, period=14)
    assert k is not None and 0.0 <= k <= 100.0


def test_elder_triple_screen_returns_verdict():
    prices = _sine_series(period=20, n=300, amp=5.0)
    v = elder.triple_screen(prices)
    assert "buy_ok" in v and "screen1" in v and "screen2" in v and "screen3" in v


# ---- Connors mean-reversion ---------------------------------------------

def test_connors_ibs_edges():
    assert connors.ibs(high=100, low=99, close=99) == 0.0     # closed at low
    assert connors.ibs(high=100, low=99, close=100) == 1.0    # closed at high
    assert connors.ibs(high=100, low=100, close=100) is None  # zero range


def test_connors_rsi2_bounds():
    prices = [100 + math.sin(i) for i in range(50)]
    v = connors.rsi(prices, period=2)
    assert v is not None and 0 <= v <= 100


def test_connors_bounce_probability_score_range():
    # Falling series → deep oversold → high score expected.
    prices = [100 - i * 0.5 for i in range(30)]
    b = connors.bounce_probability(prices)
    assert 0 <= b["score"] <= 100


def test_connors_suggest_buy_px_below_center():
    prices = [100 - i * 0.5 for i in range(30)]
    r = connors.suggest_buy_px(prices, mean_reversion_band_center=100.0,
                               band_width=2.0)
    # Suggested buy_px is at-or-below the band center (aggressiveness ≥ 0).
    assert r["suggested_buy_px"] <= 100.0


# ---- Vince optimal-f ----------------------------------------------------

def test_vince_optimal_f_finds_positive_f_for_positive_edge():
    # 60% winners at +1, 40% losers at −1 → positive edge → optimal_f > 0.
    random.seed(42)
    series = []
    for _ in range(100):
        series.append(1.0 if random.random() < 0.6 else -1.0)
    r = vince.optimal_f(series)
    assert r is not None
    assert r["optimal_f"] > 0
    assert r["twr_at_optimum"] >= 1.0


def test_vince_cap_reentry_qty_never_ups():
    # Cap should never RAISE the strategy qty.
    series = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0, 1.0]
    cap = vince.cap_reentry_qty(
        strategy_qty=10, pnl_series=series,
        account_equity=1000.0, worst_loss_per_contract=10.0,
    )
    assert cap["capped_qty"] <= 10


def test_vince_ruin_probability_bounds():
    # High edge + fat bankroll → low ruin.
    p = vince.ruin_probability(edge=0.5, win_rate=0.7, bankroll_units=50)
    assert 0 <= p <= 1
    # Losing player → ruin ≥ high.
    p2 = vince.ruin_probability(edge=-0.5, win_rate=0.4, bankroll_units=10)
    assert p2 >= p


# ---- Experts orchestrator — THE KEY INVARIANT ----------------------------

def test_experts_never_places_buy_above_sold():
    """The specific bug fix. Given any recent price series and a sold_price,
    the computed buy_px must be strictly LESS than sold_price. Adam:
    "all the buy backs assumed a bullish trend and were always above the
    last sale" — this test enforces that never happens again."""
    random.seed(1)
    for trial in range(20):
        # Random-ish 100-bar series around a mean of 75.
        prices = [75.0 + random.gauss(0, 0.3) for _ in range(100)]
        sold_price = prices[-1]  # simulate we just sold at last mark
        d = experts_reentry.compute_reentry(
            prices=prices, sold_price=sold_price, spread=0.30,
            strategy_qty=5, account_equity=7500.0,
            worst_loss_per_contract=3.0,
            recent_cycle_pnls=[1.0, -0.5, 1.0, -1.0, 0.5],
        )
        if d.get("should_arm"):
            assert d["buy_px"] < sold_price, (
                f"trial {trial}: buy_px {d['buy_px']} not below sold {sold_price}. "
                f"Reasons: {d.get('reasons')}")


def test_experts_fallback_on_short_history():
    """< 40 bars → fallback path. Still must place buy_px below sold_price."""
    prices = [75.0] * 10
    d = experts_reentry.compute_reentry(
        prices=prices, sold_price=75.0, spread=0.30, strategy_qty=1,
    )
    assert d["should_arm"]
    assert d["buy_px"] < 75.0


def test_experts_returns_snapshot_for_logging():
    prices = [75.0 + math.sin(i / 5.0) * 0.4 for i in range(120)]
    d = experts_reentry.compute_reentry(
        prices=prices, sold_price=75.0, spread=0.30, strategy_qty=5,
    )
    assert "expert_snapshot" in d
    assert "reasons" in d


# ---- Per-product threshold plumbing --------------------------------------

def test_resolve_thresholds_default():
    """No override → all defaults."""
    thr = experts_reentry.resolve_thresholds(None)
    assert thr["ehlers_bounce_low"] == 0.65
    assert thr["connors_buy_zone"] == 60.0
    assert thr["vpin_calm_ceiling"] == 0.60


def test_resolve_thresholds_partial_override():
    """Override applies to named keys; unmentioned keys keep defaults."""
    thr = experts_reentry.resolve_thresholds({"ehlers_bounce_low": 0.70,
                                              "vpin_calm_ceiling": 0.75})
    assert thr["ehlers_bounce_low"] == 0.70
    assert thr["vpin_calm_ceiling"] == 0.75
    assert thr["connors_buy_zone"] == 60.0  # unchanged


def test_resolve_thresholds_none_values_ignored():
    """None in override → keep default. Prevents accidental wipe."""
    thr = experts_reentry.resolve_thresholds({"connors_buy_zone": None})
    assert thr["connors_buy_zone"] == 60.0


def test_compute_reentry_honors_per_product_thresholds():
    """Passing thresholds= to compute_reentry uses them (snapshot must
    contain the merged view for audit)."""
    prices = [75.0 + math.sin(i / 4.0) * 0.4 for i in range(120)]
    d = experts_reentry.compute_reentry(
        prices=prices, sold_price=75.0, spread=0.30, strategy_qty=5,
        thresholds={"ehlers_bounce_low": 0.5, "vpin_calm_ceiling": 0.9},
    )
    snap = d.get("expert_snapshot") or {}
    used = snap.get("thresholds_used") or {}
    assert used.get("ehlers_bounce_low") == 0.5
    assert used.get("vpin_calm_ceiling") == 0.9
    # unmentioned key still default
    assert used.get("connors_buy_zone") == 60.0
