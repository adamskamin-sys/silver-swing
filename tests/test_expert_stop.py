"""Tests for expert_stop.py — stop-distance expert consensus.

Focus: fee floor, sanity cap, consensus math, kill switch, and the
regression guards that prevent the PT-bleed class from returning.
Every test names the invariant it protects.
"""
from __future__ import annotations


def test_kill_switch_default_is_expert():
    """expert_stop.MODE default is 'expert'. Any test that reads MODE
    should see it. If a test flips MODE, it MUST restore. Regression
    guard against silent revert of the default."""
    import importlib
    import expert_stop
    importlib.reload(expert_stop)
    assert expert_stop.MODE == "expert", (
        "expert_stop.MODE must default to 'expert' — legacy default is a "
        "backdoor to the PT bleed."
    )


def test_fee_floor_multiplier_is_three():
    """Same Menkveld 2013 3× safety margin as expert_spread. If this
    diverges, the two floors get out of sync and one gate leaks."""
    from expert_stop import _FEE_FLOOR_MULTIPLIER
    assert _FEE_FLOOR_MULTIPLIER == 3.0


def test_sanity_cap_is_ten_percent():
    """Van Tharp canonical max-per-trade sanity cap. If this drifts,
    a bad Kyle λ estimate could runaway to unreasonable stop distances."""
    from expert_stop import _SANITY_CAP_FRAC_OF_MID
    assert _SANITY_CAP_FRAC_OF_MID == 0.10


def test_pt_shape_input_hits_fee_floor():
    """PT (PLAT nano-futures) pre-fix bled at $1.50 stop distance vs $20
    round-trip fees. Post-fix: fee floor MUST push stop to $6.00 min.
    This is the direct regression guard against re-opening the PT bleed."""
    from expert_stop import optimal_stop_distance
    d = optimal_stop_distance(
        mark=1680.0,
        atr_est=0.6,            # historic PT ATR before the vol spike
        fee_per_roundtrip=20.0,
        contract_size=10.0,
        qty=1,
    )
    assert d is not None
    assert d.stop_distance >= 6.0 - 1e-9, (
        f"PT-shape stop distance must be ≥ $6.00 (3× fees / contract_size). "
        f"Got {d.stop_distance}. Regression to PT bleed."
    )
    assert d.fee_floor_binding is True, (
        "Fee floor must have engaged for this PT-shape input."
    )


def test_hype_shape_input_hits_fee_floor():
    """HYPE ($65 mark, $0.50 fees, contract_size=1) had spread=$0.03 pre-fix.
    Stop distance must be ≥ $1.50 (3 × 0.50 / 1)."""
    from expert_stop import optimal_stop_distance
    d = optimal_stop_distance(
        mark=65.0,
        atr_est=0.05,           # quiet HYPE
        fee_per_roundtrip=0.50,
        contract_size=1.0,
        qty=1,
    )
    assert d is not None
    assert d.stop_distance >= 1.50 - 1e-9
    assert d.fee_floor_binding is True


def test_sanity_cap_prevents_runaway():
    """Extreme Kyle λ spike should NOT push stop past 10% of mid.
    Simulates a bad estimator reading — the cap must protect us."""
    from expert_stop import optimal_stop_distance
    d = optimal_stop_distance(
        mark=100.0,
        atr_est=50.0,           # extreme ATR
        fee_per_roundtrip=0.0,
        contract_size=1.0,
        qty=1,
        kyle_lambda=999.0,       # extreme λ
        kyle_baseline=1.0,
    )
    assert d is not None
    assert d.stop_distance <= 10.0 + 1e-9, (
        "Van Tharp 10%-of-mid cap must bound stop distance even with "
        "extreme expert inputs."
    )
    assert d.sanity_cap_binding is True


def test_consensus_is_median_of_three_experts():
    """The consensus formula is statistics.median, not mean or max.
    Regression guard against changing the ensemble rule."""
    from expert_stop import optimal_stop_distance, wilder_2n_stop
    # No OFI, no Kyle — all three candidates degenerate to Wilder baseline
    d = optimal_stop_distance(
        mark=100.0,
        atr_est=1.0,
        fee_per_roundtrip=0.001,   # trivial (won't bind floor)
        contract_size=1.0,
        qty=1,
    )
    assert d is not None
    # All three equal Wilder baseline (2.0 × 1.0 = 2.0)
    wilder = wilder_2n_stop(1.0)
    assert d.consensus == wilder
    assert d.candidates["wilder_2n"] == wilder
    assert d.candidates["cartea_adverse_selection"] == wilder
    assert d.candidates["kyle_lambda"] == wilder


def test_cartea_widens_on_high_ofi():
    """When OFI is high (informed flow), Cartea candidate must widen.
    This is what protects us when the market turns toxic — the same
    signal that let PT get run should widen future stops."""
    from expert_stop import cartea_adverse_selection_stop, wilder_2n_stop
    atr = 1.0
    baseline = wilder_2n_stop(atr)
    # Low OFI = no widening
    low = cartea_adverse_selection_stop(atr, order_flow_imbalance=0.05)
    assert abs(low - baseline * (1.0 + 0.05/0.8 * 1.5)) < 1e-6
    # High OFI = max widening (2.5×)
    high = cartea_adverse_selection_stop(atr, order_flow_imbalance=0.85)
    assert high == baseline * 2.5


def test_kyle_widens_on_elevated_lambda():
    """When Kyle λ is above baseline, widen. Bounded 1×-3×."""
    from expert_stop import kyle_lambda_widened_stop, wilder_2n_stop
    atr = 1.0
    baseline = wilder_2n_stop(atr)
    # λ = baseline → no widening
    at_baseline = kyle_lambda_widened_stop(atr, kyle_lambda=1.0, kyle_baseline=1.0)
    assert at_baseline == baseline
    # λ = 2× baseline → half of the way to CAP (linear interp)
    doubled = kyle_lambda_widened_stop(atr, kyle_lambda=2.0, kyle_baseline=1.0)
    assert doubled > baseline
    # λ = 3× baseline → hit the CAP (3×)
    tripled = kyle_lambda_widened_stop(atr, kyle_lambda=3.0, kyle_baseline=1.0)
    assert tripled == baseline * 3.0
    # λ = 10× baseline → still capped at 3×
    extreme = kyle_lambda_widened_stop(atr, kyle_lambda=10.0, kyle_baseline=1.0)
    assert extreme == baseline * 3.0


def test_returns_none_on_invalid_inputs():
    """Guard: mark≤0 or atr_est≤0 must return None so caller falls back."""
    from expert_stop import optimal_stop_distance
    assert optimal_stop_distance(mark=0.0, atr_est=1.0,
                                  fee_per_roundtrip=0, contract_size=1, qty=1) is None
    assert optimal_stop_distance(mark=100.0, atr_est=0.0,
                                  fee_per_roundtrip=0, contract_size=1, qty=1) is None


def test_wire_up_swing_leg_has_expert_stop_import():
    """Regression: swing_leg.py must import expert_stop in the auto-
    refresh path. If this test fails, the wire-up was removed."""
    import inspect
    from swing_leg import SwingTrader
    src = inspect.getsource(SwingTrader._maybe_auto_refresh_stop_loss)
    assert "import expert_stop" in src, (
        "SwingTrader._maybe_auto_refresh_stop_loss must import expert_stop "
        "to consult the consensus. Missing = regression to legacy 2.5×ATR."
    )
    assert "optimal_stop_distance" in src, (
        "Auto-refresh must call optimal_stop_distance() specifically."
    )


def test_wire_up_has_kill_switch_check():
    """The wire-up must gate on expert_stop.MODE so operators can revert
    to legacy in an emergency."""
    import inspect
    from swing_leg import SwingTrader
    src = inspect.getsource(SwingTrader._maybe_auto_refresh_stop_loss)
    assert 'MODE' in src and '"expert"' in src, (
        "Wire-up must check expert_stop.MODE == 'expert' as kill switch."
    )


def test_wire_up_has_fallback_on_expert_error():
    """If expert_stop raises OR returns None, the wire-up must fall back
    to legacy math — never leave the stop unset."""
    import inspect
    from swing_leg import SwingTrader
    src = inspect.getsource(SwingTrader._maybe_auto_refresh_stop_loss)
    # Look for the fallback block after the expert call
    assert "if new_stop_px is None" in src, (
        "Wire-up must fall back to legacy math when expert returns None."
    )
    assert "wilder_mult" in src, (
        "Fallback must use the same Wilder multiplier the expert would have."
    )
