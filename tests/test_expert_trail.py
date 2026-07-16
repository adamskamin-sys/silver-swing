"""Tests for expert_trail.py — trail distance expert consensus."""
from __future__ import annotations


def test_kill_switch_default_is_expert():
    import importlib
    import expert_trail
    importlib.reload(expert_trail)
    assert expert_trail.MODE == "expert"


def test_fee_floor_multiplier_matches_other_experts():
    """Same 3× as expert_spread + expert_stop for consistency."""
    from expert_trail import _FEE_FLOOR_MULTIPLIER
    assert _FEE_FLOOR_MULTIPLIER == 3.0


def test_sanity_cap_matches_expert_stop():
    """Same 10%-of-mid cap as expert_stop."""
    from expert_trail import _SANITY_CAP_FRAC_OF_MID
    assert _SANITY_CAP_FRAC_OF_MID == 0.10


def test_chande_atr_trail_uses_2_75_multiplier():
    """Chande canonical N = 2.75 for mid-range futures/commodities."""
    from expert_trail import chande_atr_trail, _CHANDE_N_ATR
    assert _CHANDE_N_ATR == 2.75
    d = chande_atr_trail(atr_est=1.0)
    assert abs(d - 2.75) < 1e-9


def test_wilder_sar_advances_toward_hh():
    """SAR must move toward HH each step (never past it)."""
    from expert_trail import wilder_sar_trail
    d = wilder_sar_trail(highest_high=100.0, current_sar=95.0, accel_factor=0.02)
    # New SAR = 95 + 0.02 × (100 - 95) = 95.10; distance = 100 - 95.10 = 4.90
    assert abs(d - 4.90) < 1e-6


def test_turtle_lookback_returns_hh_ll_range():
    """Distance = HH - LL over lookback window."""
    from expert_trail import turtle_lookback_trail
    prices = list(range(80, 100))     # 20 samples, 80..99
    d = turtle_lookback_trail(prices, lookback=20)
    # HH = 99, LL = 80, distance = 19
    assert abs(d - 19.0) < 1e-9


def test_ho_stoll_tightens_with_age():
    """Age 0 → 1.0. Age 24h → 0.5. Interpolates linearly."""
    from expert_trail import ho_stoll_age_tightener
    assert ho_stoll_age_tightener(0) == 1.0
    assert ho_stoll_age_tightener(24 * 3600) == 0.5
    # Halfway (12h) → 0.75
    assert abs(ho_stoll_age_tightener(12 * 3600) - 0.75) < 1e-9
    # Older than 24h capped at 0.5
    assert ho_stoll_age_tightener(48 * 3600) == 0.5


def test_pt_shape_input_hits_fee_floor():
    """PT ($1680 mark, $20 fees, contract_size 10): trail distance MUST
    be ≥ $6.00 (3× fees / contract_size). Regression guard against the
    same bleed class we fixed for spread + stop."""
    from expert_trail import optimal_trail_distance
    d = optimal_trail_distance(
        mid_price=1680.0,
        highest_high=1690.0,
        atr_est=1.5,
        prices=[1680 + i * 0.2 for i in range(30)],
        fee_per_roundtrip=20.0,
        contract_size=10.0,
        qty=1,
        position_age_secs=0.0,
    )
    assert d is not None
    assert d.trail_distance >= 6.0 - 1e-9, (
        f"PT-shape trail distance must be ≥ $6.00. Got {d.trail_distance}."
    )
    assert d.fee_floor_binding is True


def test_sanity_cap_prevents_runaway():
    """Extreme ATR must not push trail past 10% of mid."""
    from expert_trail import optimal_trail_distance
    d = optimal_trail_distance(
        mid_price=100.0,
        highest_high=105.0,
        atr_est=50.0,             # absurd ATR
        prices=[100 + i * 0.1 for i in range(30)],
        fee_per_roundtrip=0.0,
        contract_size=1.0,
        qty=1,
    )
    assert d is not None
    assert d.trail_distance <= 10.0 + 1e-9, (
        "Van Tharp 10%-of-mid cap must bound trail distance."
    )
    assert d.sanity_cap_binding is True


def test_age_never_pushes_below_fee_floor():
    """Ho-Stoll tightener can reduce distance, but re-applying Menkveld
    floor after must guarantee we don't fall below break-even."""
    from expert_trail import optimal_trail_distance
    d = optimal_trail_distance(
        mid_price=1680.0,
        highest_high=1690.0,
        atr_est=1.5,
        prices=[1680 + i * 0.2 for i in range(30)],
        fee_per_roundtrip=20.0,
        contract_size=10.0,
        qty=1,
        position_age_secs=48 * 3600.0,   # 48h — max age tightening
    )
    assert d is not None
    # Menkveld floor = 3 × 20 / 10 = 6.0. Even at max age tighten (0.5),
    # the re-enforced floor must guarantee distance ≥ 6.0.
    assert d.trail_distance >= 6.0 - 1e-9


def test_kaufman_cadence_strong_trend_ratchets_every_tick():
    """Strong trend (ER > 0.7) → cadence 1 tick."""
    from expert_trail import kaufman_ratchet_cadence
    trend = [100 + i for i in range(30)]  # linear uptrend, ER ≈ 1.0
    assert kaufman_ratchet_cadence(trend) == 1


def test_kaufman_cadence_ranging_ratchets_slowly():
    """Ranging (ER < 0.3) → cadence 15 ticks."""
    from expert_trail import kaufman_ratchet_cadence
    zigzag = [100 + ((-1)**i) * 0.5 for i in range(30)]
    assert kaufman_ratchet_cadence(zigzag) == 15


def test_returns_none_on_invalid_inputs():
    from expert_trail import optimal_trail_distance
    assert optimal_trail_distance(
        mid_price=0.0, highest_high=100, atr_est=1, prices=[100]*10,
        fee_per_roundtrip=0, contract_size=1, qty=1) is None
    assert optimal_trail_distance(
        mid_price=100, highest_high=100, atr_est=0.0, prices=[100]*10,
        fee_per_roundtrip=0, contract_size=1, qty=1) is None


def test_wire_up_swing_leg_has_expert_trail_import():
    """Regression: swing_leg must import expert_trail."""
    src = open("/Users/adamkamin/silver-swing/swing_leg.py").read()
    assert "import expert_trail" in src
    assert "optimal_trail_distance" in src


def test_wire_up_has_kill_switch_check():
    src = open("/Users/adamkamin/silver-swing/swing_leg.py").read()
    # Look near the expert_trail import
    idx = src.find("import expert_trail")
    assert idx > 0
    surrounding = src[idx : idx + 500]
    assert 'MODE' in surrounding
    assert '"expert"' in surrounding
