"""Tests for expert_size.py — position sizing with SAFETY-CAP invariant.

CRITICAL invariant this file protects: experts can ONLY REDUCE size,
never grow. If any test here fails, the 'swing 1-2, protect the core'
rule from project_live_intent has been broken.
"""
from __future__ import annotations


def test_kill_switch_default_is_expert():
    import importlib
    import expert_size
    importlib.reload(expert_size)
    assert expert_size.MODE == "expert"


def test_van_tharp_risk_pct_is_one_percent():
    """Van Tharp canonical 1% risk. Retail-safe."""
    from expert_size import _VAN_THARP_RISK_PCT
    assert _VAN_THARP_RISK_PCT == 0.01


def test_kelly_fraction_is_half():
    """Half-Kelly per Thorp 1969. Full Kelly is provably too aggressive."""
    from expert_size import _KELLY_FRACTION
    assert _KELLY_FRACTION == 0.5


def test_vince_max_f_is_20_percent():
    """Vince canonical cap — beyond this drawdown risk dominates."""
    from expert_size import _VINCE_MAX_F
    assert _VINCE_MAX_F == 0.20


def test_van_tharp_1R_math():
    """Sanity: $10k account, 1% risk, $5 stop distance, contract_size=10
    → 100 / 50 = 2 contracts."""
    from expert_size import van_tharp_1R_size
    assert van_tharp_1R_size(account_equity=10_000, stop_distance=5.0,
                              contract_size=10.0) == 2


def test_van_tharp_returns_none_on_invalid():
    from expert_size import van_tharp_1R_size
    assert van_tharp_1R_size(0, 1, 1) is None
    assert van_tharp_1R_size(100, 0, 1) is None
    assert van_tharp_1R_size(100, 1, 0) is None


def test_half_kelly_needs_min_history():
    """Half-Kelly requires _KELLY_MIN_CYCLES_FOR_BASE_RATE samples."""
    from expert_size import half_kelly_size, _KELLY_MIN_CYCLES_FOR_BASE_RATE
    # Not enough history
    assert half_kelly_size(10000, [10, -5] * 5,   # only 10 samples
                            contract_size=1, mid_price=100) is None
    # Enough samples but no edge → None
    losing_history = [-10] * _KELLY_MIN_CYCLES_FOR_BASE_RATE
    assert half_kelly_size(10000, losing_history,
                            contract_size=1, mid_price=100) is None


def test_half_kelly_returns_positive_with_edge():
    """20 cycles, all winners → Kelly returns something."""
    from expert_size import half_kelly_size
    winning = [10] * 20   # all winners
    # No losses → returns None (undefined payoff ratio)
    assert half_kelly_size(10000, winning, contract_size=1, mid_price=100) is None
    # Mixed with edge: 15 wins of $10, 5 losses of $2 → strong edge
    mixed = [10] * 15 + [-2] * 5
    result = half_kelly_size(10000, mixed, contract_size=1, mid_price=100)
    assert result is not None and result > 0


def test_vince_optimal_f_finds_growth_f():
    """Positive-expectancy history → Vince finds an f > 0."""
    from expert_size import vince_optimal_f_size
    edge = [5, -2, 3, -1, 4, -2, 6, -1, 3, -2]   # net positive
    result = vince_optimal_f_size(10000, edge, contract_size=1, mid_price=100)
    assert result is not None


def test_menkveld_min_econ_size():
    """expected_profit_per_contract=$5, fee=$10, mult=2.0
    → need 20 / 5 = 4 contracts."""
    from expert_size import menkveld_min_econ_size
    assert menkveld_min_econ_size(fee_per_roundtrip=10.0,
                                    expected_profit_per_contract=5.0) == 4


def test_SAFETY_CAP_INVARIANT_never_exceeds_user_configured():
    """CRITICAL INVARIANT: expert output ≤ user_configured, always.
    This is the whole point of the safety-cap-only design. Regression
    guard against experts silently increasing risk."""
    from expert_size import optimal_size
    # Setup where every expert would want to size UP: big account, small
    # position, tight stop, strong edge.
    d = optimal_size(
        user_configured_size=1,   # user says 1
        account_equity=1_000_000,  # huge account
        stop_distance=0.10,         # tight stop
        contract_size=1.0,
        mid_price=10.0,
        fee_per_roundtrip=0.10,
        expected_profit_per_contract=1.0,
        recent_cycle_pnls=[5] * 30,   # all winners
    )
    assert d.size == 1, (
        f"SAFETY-CAP INVARIANT VIOLATED: expert returned size={d.size} "
        f"> user_configured=1. This would let experts grow position "
        f"against 'swing 1-2, protect the core' rule."
    )


def test_expert_reduces_size_when_stop_too_tight():
    """User says 5, but Van Tharp with 1% risk on $8k account and $6 stop
    on 10-size contract → risk_dollars=80, per_contract=60 → 1 contract.
    Expert should ship 1."""
    from expert_size import optimal_size
    d = optimal_size(
        user_configured_size=5,
        account_equity=8_000,
        stop_distance=6.0,
        contract_size=10.0,
        mid_price=1680.0,
        fee_per_roundtrip=20.0,
        expected_profit_per_contract=50.0,
    )
    # Van Tharp = 80 / 60 = 1.33 → floor to 1
    assert d.candidates["van_tharp"] == 1
    assert d.size == 1
    assert d.size < d.user_configured   # expert reduced


def test_never_returns_zero_when_user_wants_positive():
    """Ship at least 1 contract when user_configured > 0."""
    from expert_size import optimal_size
    d = optimal_size(
        user_configured_size=1,
        account_equity=100,           # tiny account
        stop_distance=100.0,           # absurd stop
        contract_size=100.0,
        mid_price=1000.0,
        fee_per_roundtrip=1.0,
        expected_profit_per_contract=0.01,
    )
    assert d.size >= 1


def test_returns_zero_when_user_configured_zero():
    """Respect user intent: 0 = don't size."""
    from expert_size import optimal_size
    d = optimal_size(
        user_configured_size=0,
        account_equity=100_000,
        stop_distance=1.0,
        contract_size=1.0,
        mid_price=100.0,
    )
    assert d.size == 0


def test_wire_up_has_expert_size_import():
    """Regression: swing_leg must import + call expert_size."""
    src = open("/Users/adamkamin/silver-swing/swing_leg.py").read()
    assert "import expert_size" in src
    assert "optimal_size" in src


def test_wire_up_has_expert_size_adjust_method():
    """SwingTrader must have _expert_size_adjust method."""
    from swing_leg import SwingTrader
    assert hasattr(SwingTrader, "_expert_size_adjust"), (
        "SwingTrader._expert_size_adjust missing — wire-up removed."
    )


def test_wire_up_calls_size_adjust_in_primary_buy_path():
    """Regression: primary BUY arm must consult _expert_size_adjust."""
    import inspect
    from swing_leg import SwingTrader
    src = inspect.getsource(SwingTrader._ensure_armed)
    assert "_expert_size_adjust" in src, (
        "Primary buy path must call _expert_size_adjust for safety-cap."
    )


def test_wire_up_calls_size_adjust_in_sleeve_arm():
    """Regression: sleeve BUY arm must consult _expert_size_adjust."""
    import inspect
    from swing_leg import SwingTrader
    src = inspect.getsource(SwingTrader._sleeve_arm)
    assert "_expert_size_adjust" in src, (
        "Sleeve buy path must call _expert_size_adjust for safety-cap."
    )
