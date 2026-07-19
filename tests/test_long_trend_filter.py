"""Long-horizon canonical trend filter — Faber 200-day SMA + MOP 12-month TSM.

Option D-1 from 2026-07-19 expert-source refactor. Ships flag OFF by default;
tests verify (a) pure math correctness, (b) flag-off never blocks BUY,
(c) verdict cache staleness handling, (d) mode combinators.
"""
from __future__ import annotations

import time

import pytest

from trend_filter import (
    compute_faber_gap,
    compute_tsm_sign,
    long_trend_flag_enabled,
    long_trend_ok_for_buy,
    long_trend_verdict,
    load_long_trend_verdict,
    save_long_trend_verdict,
)


class _MinStore:
    def __init__(self):
        self._c: dict = {}

    def get_config(self, tenant, symbol):
        return self._c.get((tenant, symbol))

    def put_config(self, tenant, symbol, cfg):
        self._c[(tenant, symbol)] = cfg


# ---- pure math -------------------------------------------------------------


def test_tsm_sign_uptrend():
    """Monotonic uptrend → +1."""
    closes = [50.0 + i * 0.1 for i in range(300)]
    assert compute_tsm_sign(closes, lookback_days=252) == 1


def test_tsm_sign_downtrend():
    closes = [100.0 - i * 0.1 for i in range(300)]
    assert compute_tsm_sign(closes, lookback_days=252) == -1


def test_tsm_sign_insufficient_data():
    assert compute_tsm_sign([50.0] * 10, lookback_days=252) is None


def test_faber_gap_price_above_sma():
    """Rising series; last close above 200-SMA → positive gap."""
    closes = [50.0 + i * 0.1 for i in range(250)]
    gap = compute_faber_gap(closes, window=200)
    assert gap is not None and gap > 0


def test_faber_gap_price_below_sma():
    """Falling series; last close below 200-SMA → negative gap."""
    closes = [100.0 - i * 0.1 for i in range(250)]
    gap = compute_faber_gap(closes, window=200)
    assert gap is not None and gap < 0


def test_faber_gap_insufficient_data():
    assert compute_faber_gap([50.0] * 100, window=200) is None


# ---- verdict combinator ----------------------------------------------------


def test_verdict_either_permissive_on_missing():
    """When both signals are missing, 'either' mode allows BUY."""
    v = long_trend_verdict([], mode="either")
    assert v["buy_ok"] is True


def test_verdict_both_conservative_needs_both_positive():
    """'both' mode requires TSM=+ AND Faber=+."""
    # Uptrend — both positive
    closes = [50.0 + i * 0.1 for i in range(300)]
    v_up = long_trend_verdict(closes, mode="both")
    assert v_up["buy_ok"] is True
    # Downtrend — both negative
    closes_dn = [100.0 - i * 0.1 for i in range(300)]
    v_dn = long_trend_verdict(closes_dn, mode="both")
    assert v_dn["buy_ok"] is False


def test_verdict_records_signals():
    closes = [50.0 + i * 0.1 for i in range(300)]
    v = long_trend_verdict(closes)
    assert "tsm_sign" in v and "faber_gap" in v and "computed_at" in v


# ---- flag semantics --------------------------------------------------------


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("SWING_TREND_FILTER_ENABLED", raising=False)
    assert long_trend_flag_enabled() is False


def test_flag_on(monkeypatch):
    monkeypatch.setenv("SWING_TREND_FILTER_ENABLED", "1")
    assert long_trend_flag_enabled() is True


def test_buy_ok_flag_off_always_allows(monkeypatch):
    """Flag off → always (True, 'flag_off') even if a NEGATIVE verdict
    is cached. Live behavior unchanged until flag is flipped."""
    monkeypatch.delenv("SWING_TREND_FILTER_ENABLED", raising=False)
    store = _MinStore()
    # Cache a hard NEGATIVE verdict
    save_long_trend_verdict(store, "adam-live", "BTC-PERP", {
        "buy_ok": False, "mode": "both", "tsm_sign": -1,
        "faber_gap": -0.15, "computed_at": time.time(),
    })
    allowed, reason = long_trend_ok_for_buy(store, "adam-live", "BTC-PERP")
    assert allowed is True
    assert reason == "flag_off"


def test_buy_ok_no_cache_fails_open(monkeypatch):
    """Flag on but no cache → allow (fail-open on Coinbase outage /
    cold start). Otherwise a daily-candle outage freezes BUY arms."""
    monkeypatch.setenv("SWING_TREND_FILTER_ENABLED", "1")
    store = _MinStore()
    allowed, reason = long_trend_ok_for_buy(store, "adam-live", "NEW-CDE")
    assert allowed is True
    assert reason == "no_cache_fail_open"


def test_buy_ok_stale_cache_fails_open(monkeypatch):
    """Verdict older than 12h is treated as no cache."""
    monkeypatch.setenv("SWING_TREND_FILTER_ENABLED", "1")
    store = _MinStore()
    save_long_trend_verdict(store, "adam-live", "OLD-CDE", {
        "buy_ok": False, "mode": "both", "tsm_sign": -1,
        "faber_gap": -0.15, "computed_at": time.time() - 13 * 3600,
    })
    allowed, reason = long_trend_ok_for_buy(store, "adam-live", "OLD-CDE")
    assert allowed is True
    assert reason == "no_cache_fail_open"


def test_buy_ok_trend_down_blocks(monkeypatch):
    """Flag on + fresh negative verdict → block with descriptive reason."""
    monkeypatch.setenv("SWING_TREND_FILTER_ENABLED", "1")
    store = _MinStore()
    save_long_trend_verdict(store, "adam-live", "DOWN-CDE", {
        "buy_ok": False, "mode": "both", "tsm_sign": -1,
        "faber_gap": -0.12, "computed_at": time.time(),
    })
    allowed, reason = long_trend_ok_for_buy(store, "adam-live", "DOWN-CDE")
    assert allowed is False
    assert "trend_down" in reason


def test_buy_ok_trend_up_allows(monkeypatch):
    monkeypatch.setenv("SWING_TREND_FILTER_ENABLED", "1")
    store = _MinStore()
    save_long_trend_verdict(store, "adam-live", "UP-CDE", {
        "buy_ok": True, "mode": "either", "tsm_sign": 1,
        "faber_gap": 0.08, "computed_at": time.time(),
    })
    allowed, reason = long_trend_ok_for_buy(store, "adam-live", "UP-CDE")
    assert allowed is True
    assert reason == "trend_up"


# ---- cache round-trip ------------------------------------------------------


def test_verdict_cache_round_trip():
    store = _MinStore()
    v = {
        "buy_ok": True, "mode": "either", "tsm_sign": 1,
        "faber_gap": 0.05, "computed_at": time.time(),
    }
    save_long_trend_verdict(store, "adam-live", "X-CDE", v)
    loaded = load_long_trend_verdict(store, "adam-live", "X-CDE")
    assert loaded is not None
    assert loaded["buy_ok"] is True
    assert loaded["tsm_sign"] == 1
