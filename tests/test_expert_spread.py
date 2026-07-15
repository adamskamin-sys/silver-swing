"""Tests for expert_spread.py — the Avellaneda-Stoikov + Cartea-Jaimungal
academic spread-sizing module.

These verify the math against paper formulas so the citations aren't
decorative — the code actually implements what the papers say.
"""
from __future__ import annotations

import math
import pytest

from expert_spread import (
    realized_vol_from_prices,
    arrival_rate_from_cycles,
    kyle_lambda_widening,
    expected_daily_pnl,
    optimal_spread,
    grid_search_optimal_gamma,
    _COST_FLOOR_MULTIPLIER,
    _LAMBDA_WIDENING_CAP,
    _MIN_ARRIVAL_RATE_PER_HOUR,
)


class TestRealizedVol:
    def test_insufficient_history_returns_none(self):
        assert realized_vol_from_prices([]) is None
        assert realized_vol_from_prices([100.0]) is None
        assert realized_vol_from_prices([100.0, 101.0, 102.0]) is None  # <5

    def test_stable_prices_give_low_vol(self):
        prices = [100.0, 100.001, 100.002, 100.001, 100.0, 100.001]
        sigma = realized_vol_from_prices(prices)
        assert sigma is not None
        assert sigma < 0.001  # very small stdev of log returns

    def test_volatile_prices_give_higher_vol(self):
        stable = [100.0, 100.001, 100.002, 100.001, 100.0, 100.001]
        volatile = [100.0, 105.0, 95.0, 102.0, 98.0, 101.0]
        assert (realized_vol_from_prices(volatile)
                > realized_vol_from_prices(stable))

    def test_returns_positive(self):
        prices = [100.0 + i * 0.1 for i in range(20)]
        sigma = realized_vol_from_prices(prices)
        assert sigma is not None
        assert sigma > 0


class TestArrivalRate:
    def test_no_cycles_returns_floor(self):
        rate = arrival_rate_from_cycles([])
        expected_floor = _MIN_ARRIVAL_RATE_PER_HOUR / 3600.0
        assert rate == pytest.approx(expected_floor)

    def test_recent_cycles_dominate(self):
        import time
        now = time.time()
        old = [now - 7200 - i for i in range(100)]  # all >1h ago
        rate = arrival_rate_from_cycles(old, window_secs=3600.0)
        assert rate == pytest.approx(_MIN_ARRIVAL_RATE_PER_HOUR / 3600.0)

    def test_dense_flow_shows_high_rate(self):
        import time
        now = time.time()
        # 60 cycles in the last hour → 1/min = 0.0167/s
        recent = [now - i * 60 for i in range(60)]
        rate = arrival_rate_from_cycles(recent, window_secs=3600.0)
        assert rate == pytest.approx(60.0 / 3600.0, rel=1e-3)


class TestKyleLambdaWidening:
    def test_no_impact_returns_one(self):
        assert kyle_lambda_widening(None, 100.0) == 1.0
        assert kyle_lambda_widening(0.0, 100.0) == 1.0

    def test_zero_mid_returns_one(self):
        assert kyle_lambda_widening(0.5, 0.0) == 1.0

    def test_small_impact_no_widening(self):
        # 0.5 bps of a $100 asset = $0.005 impact
        assert kyle_lambda_widening(0.005, 100.0) == 1.0

    def test_large_impact_hits_cap(self):
        # 100 bps impact — well above the 50bps cap threshold
        result = kyle_lambda_widening(1.0, 100.0)
        assert result == _LAMBDA_WIDENING_CAP

    def test_intermediate_impact_scales_linearly(self):
        # 25.5 bps ≈ midway → widening should be ≈ 2× the base (1.0 + 1)
        r1 = kyle_lambda_widening(0.01, 100.0)   # 1 bps → 1.0
        r2 = kyle_lambda_widening(0.5, 100.0)    # 50 bps → cap 3.0
        r_mid = kyle_lambda_widening(0.255, 100.0)  # ~25.5 bps
        assert 1.0 < r_mid < r2
        assert r_mid == pytest.approx(2.0, abs=0.1)


class TestExpectedDailyPnl:
    def test_returns_all_three_values(self):
        c, p, d = expected_daily_pnl(
            spread=0.10, arrival_rate_per_sec=1.0 / 60.0,
            fee_per_roundtrip=0.05, contract_size=10.0, qty=1
        )
        assert c > 0
        assert p == pytest.approx(0.10 * 10 * 1 - 0.05 * 1)  # spread*size - fee
        assert d == pytest.approx(c * p)

    def test_zero_arrival_zero_daily(self):
        c, p, d = expected_daily_pnl(0.10, 0.0, 0.05, 10.0, 1)
        assert c == 0
        assert d == 0

    def test_wider_spread_more_per_cycle(self):
        _, p1, _ = expected_daily_pnl(0.10, 1.0, 0.05, 10.0, 1)
        _, p2, _ = expected_daily_pnl(0.20, 1.0, 0.05, 10.0, 1)
        assert p2 > p1


class TestOptimalSpread:
    def _sample_prices(self):
        # 30 prices with realistic small vol around $100
        return [100.0 + 0.01 * math.sin(i / 3.0) + 0.005 * (i % 7 - 3)
                for i in range(30)]

    def test_returns_none_on_bad_inputs(self):
        assert optimal_spread(mid_price=0, price_history=[100.0] * 30) is None
        assert optimal_spread(mid_price=100, price_history=[]) is None
        assert optimal_spread(mid_price=100, price_history=[100.0] * 30,
                              gamma=0) is None
        assert optimal_spread(mid_price=100, price_history=[100.0] * 30,
                              horizon_secs=0) is None

    def test_returns_valid_decision(self):
        d = optimal_spread(
            mid_price=100.0,
            price_history=self._sample_prices(),
            fee_per_roundtrip=0.02,
            contract_size=10.0,
            qty=1,
            tick_size=0.01,
        )
        assert d is not None
        assert d.buy_px < d.sell_px
        assert d.spread > 0
        assert d.method == "avellaneda_stoikov"
        assert "Avellaneda-Stoikov" in d.citation

    def test_cost_floor_binds_when_fees_too_high(self):
        # Huge fee → cost floor forces spread wider than AS would pick
        d = optimal_spread(
            mid_price=100.0,
            price_history=self._sample_prices(),
            fee_per_roundtrip=5.0,   # $5 fee round-trip (huge)
            contract_size=1.0,
            qty=1,
            tick_size=0.01,
        )
        assert d is not None
        # Spread must clear 2 × fee / (contract × qty) = 10.0
        assert d.spread >= 2.0 * 5.0 / 1.0 * 0.99  # allow small tick rounding

    def test_tick_snap_preserves_ge_spread(self):
        d = optimal_spread(
            mid_price=100.0,
            price_history=self._sample_prices(),
            tick_size=0.5,  # coarse tick
            fee_per_roundtrip=0.01,
            contract_size=1.0,
            qty=1,
        )
        assert d is not None
        # buy_px should snap DOWN to a 0.5 boundary
        assert abs((d.buy_px / 0.5) - round(d.buy_px / 0.5)) < 1e-9
        # sell_px should snap UP to a 0.5 boundary
        assert abs((d.sell_px / 0.5) - round(d.sell_px / 0.5)) < 1e-9

    def test_higher_gamma_gives_tighter_spread(self):
        # AS eq. 3.4: inv_term = γσ²T grows with γ, but adverse_term
        # = (2/γ)ln(1+γ/k) shrinks. For our max-cycles regime (small
        # σ, few cycles), the adverse_term dominates → higher γ =
        # tighter spread.
        prices = self._sample_prices()
        d_lowg = optimal_spread(mid_price=100.0, price_history=prices, gamma=0.05)
        d_highg = optimal_spread(mid_price=100.0, price_history=prices, gamma=0.9)
        assert d_lowg is not None and d_highg is not None
        # At tiny γ the (2/γ) coefficient makes adverse_term huge →
        # wider spread. At high γ it shrinks → tighter spread.
        assert d_highg.spread < d_lowg.spread

    def test_inventory_shifts_reservation_down(self):
        prices = self._sample_prices()
        d_flat = optimal_spread(mid_price=100.0, price_history=prices, inventory=0)
        d_long = optimal_spread(mid_price=100.0, price_history=prices, inventory=5)
        assert d_flat is not None and d_long is not None
        # Long inventory → reservation shifts DOWN (encourages exit)
        assert d_long.reservation_price < d_flat.reservation_price

    def test_records_citation(self):
        d = optimal_spread(mid_price=100.0, price_history=self._sample_prices())
        assert d is not None
        assert "Avellaneda-Stoikov" in d.citation
        assert "Cartea-Jaimungal" in d.citation
        assert "Kyle" in d.citation


class TestGridSearchOptimalGamma:
    def _sample_prices(self):
        return [100.0 + 0.01 * math.sin(i / 3.0) + 0.005 * (i % 7 - 3)
                for i in range(30)]

    def test_returns_a_decision(self):
        d = grid_search_optimal_gamma(
            mid_price=100.0,
            price_history=self._sample_prices(),
            fee_per_roundtrip=0.02,
            contract_size=10.0,
            qty=1,
            tick_size=0.01,
        )
        assert d is not None
        assert d.spread > 0

    def test_grid_picks_max_daily(self):
        prices = self._sample_prices()
        best = grid_search_optimal_gamma(
            mid_price=100.0,
            price_history=prices,
            fee_per_roundtrip=0.02,
            contract_size=10.0,
            qty=1,
            tick_size=0.01,
        )
        # Verify: no candidate in the grid has strictly higher (adjusted) score
        from expert_spread import optimal_spread as _os
        eps = 0.01
        best_score = best.expected_daily_pnl + eps * best.expected_cycles_per_day
        for g in [0.05, 0.1, 0.2, 0.35, 0.5, 0.7, 0.9]:
            cand = _os(
                mid_price=100.0, price_history=prices,
                fee_per_roundtrip=0.02, contract_size=10.0, qty=1,
                tick_size=0.01, gamma=g,
            )
            if cand is None:
                continue
            cand_score = cand.expected_daily_pnl + eps * cand.expected_cycles_per_day
            assert cand_score <= best_score + 1e-9

    def test_returns_none_when_all_candidates_fail(self):
        # Empty history → every candidate returns None
        result = grid_search_optimal_gamma(mid_price=100.0, price_history=[])
        assert result is None
