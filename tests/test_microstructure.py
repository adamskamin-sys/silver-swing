"""Tests for microstructure signals. Focus on signal MATH — that each
estimator produces sane values under known inputs. Integration with the
trader is exercised via test_swing_trader.
"""

import math
import os

from microstructure import (
    EffectiveSpreadEstimator,
    KylesLambda,
    L2Book,
    MicrostructureFilter,
    OrderBookImbalance,
    ReturnAutocorrelation,
    VPINEstimator,
)


# ---- EffectiveSpreadEstimator ------------------------------------------------

def test_spread_estimator_returns_median():
    e = EffectiveSpreadEstimator(window_secs=60)
    for i, s in enumerate([0.10, 0.20, 0.30, 0.40, 0.50]):
        e.update(100.0, 100.0 + s, ts=i)
    v = e.value()
    assert abs(v - 0.30) < 1e-9


def test_spread_estimator_evicts_old_samples():
    e = EffectiveSpreadEstimator(window_secs=5)
    e.update(100.0, 100.5, ts=0)   # spread 0.5
    e.update(100.0, 100.1, ts=100)  # spread 0.1, well after window
    # 0.5 sample should have been evicted
    assert abs(e.value() - 0.1) < 1e-9


def test_spread_estimator_ignores_crossed_book():
    e = EffectiveSpreadEstimator()
    e.update(100.0, 99.5, ts=0)   # ask < bid: reject
    assert e.value() is None


# ---- ReturnAutocorrelation ---------------------------------------------------

def test_autocorr_negative_for_bid_ask_bounce():
    """Price oscillates between two values → strong negative autocorrelation."""
    ac = ReturnAutocorrelation(window=100)
    for i in range(60):
        ac.update(100.0 if i % 2 == 0 else 100.5)
    v = ac.value()
    assert v is not None
    assert v < -0.5  # strong negative


def test_autocorr_positive_for_trend():
    """Monotone up → returns are all positive → positive autocorrelation."""
    ac = ReturnAutocorrelation(window=100)
    for i in range(60):
        ac.update(100.0 + i * 0.01)
    v = ac.value()
    assert v is not None
    assert v > 0.0


def test_autocorr_returns_none_before_min_samples():
    ac = ReturnAutocorrelation(window=100)
    for _ in range(5):
        ac.update(100.0)
    assert ac.value() is None


# ---- OrderBookImbalance ------------------------------------------------------

def test_obi_positive_when_bids_dominate():
    book = L2Book()
    book.apply_snapshot(
        bids=[(100.0, 50), (99.9, 30), (99.8, 20)],
        asks=[(100.1, 10), (100.2, 5), (100.3, 5)],
    )
    obi = OrderBookImbalance(book, levels=3)
    v = obi.value()
    # bid_sz=100, ask_sz=20 → (100-20)/120 = 0.667
    assert abs(v - (80.0 / 120.0)) < 1e-9


def test_obi_negative_when_asks_dominate():
    book = L2Book()
    book.apply_snapshot(
        bids=[(100.0, 10)],
        asks=[(100.1, 90)],
    )
    obi = OrderBookImbalance(book, levels=1)
    assert obi.value() < 0


def test_obi_updates_apply_deltas():
    book = L2Book()
    book.apply_snapshot(bids=[(100.0, 10)], asks=[(100.1, 10)])
    book.apply_update("bid", 100.0, 50)
    obi = OrderBookImbalance(book, levels=1)
    assert obi.value() > 0.5


def test_obi_zero_size_removes_level():
    book = L2Book()
    book.apply_snapshot(bids=[(100.0, 10)], asks=[(100.1, 10)])
    book.apply_update("bid", 100.0, 0)
    obi = OrderBookImbalance(book, levels=1)
    assert obi.value() is None  # empty bid side


# ---- VPINEstimator -----------------------------------------------------------

def test_vpin_high_when_flow_is_one_sided():
    v = VPINEstimator(bucket_size=100, window=10)
    for _ in range(300):
        v.update(price=100.0, size=1.0, side="buy")
    assert v.value() > 0.9  # nearly all buys


def test_vpin_low_when_flow_is_balanced():
    v = VPINEstimator(bucket_size=100, window=10)
    for i in range(600):
        v.update(price=100.0, size=1.0, side="buy" if i % 2 == 0 else "sell")
    val = v.value()
    assert val is not None
    assert val < 0.1


def test_vpin_none_when_no_buckets_yet():
    v = VPINEstimator(bucket_size=100, window=10)
    v.update(price=100.0, size=1.0, side="buy")
    assert v.value() is None


def test_vpin_tick_rule_classifies_when_side_missing():
    v = VPINEstimator(bucket_size=10, window=5)
    # up-tick up-tick up-tick → all buys
    for p in [100.0, 100.1, 100.2, 100.3, 100.4]:
        for _ in range(3):  # 3 units each
            v.update(price=p, size=1.0, side=None)
    assert v.value() is not None


# ---- KylesLambda -------------------------------------------------------------

def test_kyle_lambda_positive_for_impact():
    """When more volume produces more price change, λ > 0."""
    k = KylesLambda(window=100, interval_secs=1.0)
    # bucket 1: big buy volume, price rises 0.5
    t = 0
    k.update(price=100.0, size=100, side="buy", ts=t)
    k.update(price=100.5, size=1, side="buy", ts=t + 0.5)
    # force flush by starting a new bucket
    k.update(price=100.5, size=10, side="buy", ts=t + 2.0)
    k.update(price=100.55, size=1, side="buy", ts=t + 2.5)
    k.update(price=100.55, size=50, side="buy", ts=t + 4.0)
    k.update(price=100.8, size=1, side="buy", ts=t + 4.5)
    k.update(price=100.8, size=1, side="buy", ts=t + 6.0)
    v = k.value()
    assert v is not None
    assert v > 0


def test_kyle_lambda_none_with_no_data():
    k = KylesLambda()
    assert k.value() is None


# ---- MicrostructureFilter ---------------------------------------------------

def test_filter_disabled_by_default(monkeypatch):
    for key in list(os.environ):
        if key.startswith("SWING_MS_"):
            monkeypatch.delenv(key, raising=False)
    f = MicrostructureFilter()
    assert not f.any_enabled()
    assert f.should_pause_arm("BUY") is None
    assert f.size_scale() == 1.0


def test_filter_all_on_enables_everything(monkeypatch):
    monkeypatch.setenv("SWING_MS_ALL", "1")
    f = MicrostructureFilter()
    assert f.any_enabled()
    assert f.needs_l2()
    assert f.needs_trades()


def test_filter_autocorr_pauses_when_trending(monkeypatch):
    monkeypatch.setenv("SWING_MS_AUTOCORR", "1")
    monkeypatch.setenv("SWING_MS_AUTOCORR_MAX", "0.0")
    f = MicrostructureFilter()
    for i in range(60):
        f.on_ticker(100.0, 100.05, 100.0 + i * 0.01)
    reason = f.should_pause_arm("BUY")
    assert reason is not None
    assert "autocorr" in reason


def test_filter_vpin_pauses_when_toxic(monkeypatch):
    monkeypatch.setenv("SWING_MS_VPIN", "1")
    monkeypatch.setenv("SWING_MS_VPIN_MAX", "0.5")
    monkeypatch.setenv("SWING_MS_VPIN_BUCKET", "50")
    monkeypatch.setenv("SWING_MS_VPIN_WINDOW", "10")
    f = MicrostructureFilter()
    for _ in range(300):
        f.on_trade(100.0, 1.0, "buy")
    reason = f.should_pause_arm("BUY")
    assert reason is not None
    assert "vpin" in reason


def test_filter_obi_delays_buy_into_ask_heavy_book(monkeypatch):
    monkeypatch.setenv("SWING_MS_OBI", "1")
    monkeypatch.setenv("SWING_MS_OBI_THRESHOLD", "0.3")
    f = MicrostructureFilter()
    f.on_l2_snapshot(
        bids=[(100.0, 5)],
        asks=[(100.1, 95)],
    )
    reason = f.should_pause_arm("BUY")
    assert reason is not None
    assert "obi" in reason


def test_filter_obi_permits_sell_into_ask_heavy_book(monkeypatch):
    monkeypatch.setenv("SWING_MS_OBI", "1")
    monkeypatch.setenv("SWING_MS_OBI_THRESHOLD", "0.3")
    f = MicrostructureFilter()
    f.on_l2_snapshot(
        bids=[(100.0, 5)],
        asks=[(100.1, 95)],
    )
    # SELL into ask-heavy book is fine (sellers already in ask). Pause is on
    # SELL into BID-heavy books (someone bidding aggressively).
    assert f.should_pause_arm("SELL") is None


def test_filter_adaptive_band_widens_with_measured_spread(monkeypatch):
    monkeypatch.setenv("SWING_MS_SPREAD_BAND", "1")
    monkeypatch.setenv("SWING_MS_SPREAD_K", "2.0")
    f = MicrostructureFilter()
    for i in range(10):
        f.on_ticker(100.0, 100.1, 100.05)  # spread = 0.10
    mid = 100.05
    buy = f.adjusted_buy_px(cfg_buy=99.0, mid=mid)
    sell = f.adjusted_sell_px(cfg_sell=101.0, mid=mid)
    # buy = mid - 2*0.10 = 99.85; sell = mid + 2*0.10 = 100.25
    assert abs(buy - (mid - 0.2)) < 1e-6
    assert abs(sell - (mid + 0.2)) < 1e-6


def test_filter_snapshot_reflects_enabled(monkeypatch):
    monkeypatch.setenv("SWING_MS_SPREAD_BAND", "1")
    monkeypatch.setenv("SWING_MS_AUTOCORR", "1")
    f = MicrostructureFilter()
    for i in range(30):
        f.on_ticker(100.0, 100.1, 100.05)
    snap = f.snapshot()
    assert "spread_median" in snap
    assert "autocorr_lag1" in snap
    assert "vpin" not in snap  # not enabled
