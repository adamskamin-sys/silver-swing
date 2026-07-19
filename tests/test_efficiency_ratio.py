"""Kaufman Efficiency Ratio + ER modulation invariants.

Replaces the retired arbitrary 25% crypto bump on stop/trail
multipliers with a citable canonical mechanism (Perry J. Kaufman,
"Trading Systems and Methods" 5th ed. 2013).
"""
from __future__ import annotations

import math

from expert_params import (
    compute_efficiency_ratio,
    er_modulation,
    expert_params,
    multipliers_for,
)


def _candles(closes: list[float]) -> list[dict]:
    return [{"high": c, "low": c, "close": c} for c in closes]


def test_er_pure_trend_is_one():
    """Perfect one-directional move: every step is signal, no noise → ER = 1.0."""
    closes = [100.0 + i for i in range(25)]  # monotonic
    assert compute_efficiency_ratio(_candles(closes), period=20) == 1.0


def test_er_pure_noise_is_zero():
    """Alternating up-down of equal size ends where it started → gross > 0,
    net = 0 → ER = 0."""
    closes = [100.0 + (i % 2) * 1.0 for i in range(25)]  # 100,101,100,101,...
    er = compute_efficiency_ratio(_candles(closes), period=20)
    assert er == 0.0


def test_er_insufficient_data_returns_neutral():
    """Less than period+1 candles → 1.0 (no modulation, canonical fallback)."""
    assert compute_efficiency_ratio(_candles([1.0, 2.0]), period=20) == 1.0
    assert compute_efficiency_ratio([], period=20) == 1.0


def test_er_bounded_zero_to_one():
    """Even pathological inputs stay in [0, 1]."""
    import random
    random.seed(42)
    closes = [50.0 + random.uniform(-20, 20) for _ in range(30)]
    er = compute_efficiency_ratio(_candles(closes), period=20)
    assert 0.0 <= er <= 1.0


def test_er_modulation_at_extremes():
    """ER=1 → no modulation. ER=0 → +50% ceiling."""
    assert er_modulation(1.0) == 1.0
    assert er_modulation(0.0) == 1.5
    # Mid: ER=0.5 → +25% (matches retired crypto bump average)
    assert abs(er_modulation(0.5) - 1.25) < 1e-9


def test_er_modulation_clamps_out_of_range():
    """Safety: even bogus ER values don't produce absurd multipliers."""
    assert er_modulation(-1.0) == er_modulation(0.0)
    assert er_modulation(2.0) == er_modulation(1.0)


def test_expert_params_default_er_is_canonical():
    """Callers who don't pass ER get canonical multipliers unchanged.
    Prevents any legacy code path from silently getting different numbers."""
    p_default = expert_params("SLR-27AUG26-CDE", 0.09)
    p_er_one = expert_params("SLR-27AUG26-CDE", 0.09, er=1.0)
    assert p_default["stop_loss_distance"] == p_er_one["stop_loss_distance"]
    assert p_default["trail_distance"] == p_er_one["trail_distance"]


def test_er_modulation_flag_default_off(monkeypatch):
    """With flag off (default), ER is IGNORED — distances match canonical
    Van Tharp/Turtle values regardless of ER passed. Prevents expert_guard
    from crying wolf against config that hasn't been modulated."""
    monkeypatch.delenv("SWING_ER_MODULATION_ENABLED", raising=False)
    hi = expert_params("BTC-PERP-INTX", 100.0, er=1.0)
    lo = expert_params("BTC-PERP-INTX", 100.0, er=0.2)
    # Flag off → distances are identical regardless of ER
    for key in ("trail_distance", "stop_loss_distance",
                 "trail_activation_offset", "ratchet_distance",
                 "ratchet_activation", "reanchor_threshold",
                 "buy_trail_distance"):
        assert lo[key] == hi[key], (
            f"{key}: {lo[key]} != {hi[key]} — ER should NOT affect distances when flag off"
        )
    # ER still reported for observability
    assert lo["efficiency_ratio"] == 0.2
    assert lo["er_modulation_enabled"] is False


def test_er_modulation_flag_on_widens_low_er(monkeypatch):
    """With flag ON, noisy regime (low ER) widens stops uniformly."""
    monkeypatch.setenv("SWING_ER_MODULATION_ENABLED", "1")
    hi = expert_params("BTC-PERP-INTX", 100.0, er=1.0)
    lo = expert_params("BTC-PERP-INTX", 100.0, er=0.2)
    ratio = er_modulation(0.2) / er_modulation(1.0)
    for key in ("trail_distance", "stop_loss_distance",
                 "trail_activation_offset", "ratchet_distance",
                 "ratchet_activation", "reanchor_threshold",
                 "buy_trail_distance"):
        assert abs(lo[key] / hi[key] - ratio) < 1e-3, (
            f"{key}: {lo[key]}/{hi[key]} = {lo[key]/hi[key]:.4f} vs expected {ratio:.4f}"
        )
    assert lo["er_modulation_enabled"] is True


def test_crypto_bump_removed_from_multipliers():
    """Crypto base multipliers now match metals — the 25-50% bump was
    RETIRED and replaced by ER modulation. If someone re-adds the bump
    to the crypto multiplier row this fails."""
    metals = multipliers_for("SLR-27AUG26-CDE")
    crypto = multipliers_for("BTC-PERP-INTX")
    assert crypto["trail_x_atr"] == metals["trail_x_atr"] == 2.0
    assert crypto["stop_x_atr"] == metals["stop_x_atr"] == 2.0
    assert crypto["ratchet_x_atr"] == metals["ratchet_x_atr"] == 3.0


def test_expert_params_reports_er_metadata(monkeypatch):
    """Downstream (dashboard, expert_guard) needs to display + audit ER.
    With flag ON, er_modulation reflects the applied factor. With flag off
    (default), it reports 1.0 — no widening applied."""
    monkeypatch.delenv("SWING_ER_MODULATION_ENABLED", raising=False)
    p_off = expert_params("SLR-27AUG26-CDE", 0.09, er=0.6)
    assert p_off["efficiency_ratio"] == 0.6
    assert p_off["er_modulation"] == 1.0  # flag off → no modulation applied
    assert p_off["er_modulation_enabled"] is False

    monkeypatch.setenv("SWING_ER_MODULATION_ENABLED", "1")
    p_on = expert_params("SLR-27AUG26-CDE", 0.09, er=0.6)
    assert abs(p_on["er_modulation"] - er_modulation(0.6)) < 1e-9
    assert p_on["er_modulation_enabled"] is True
