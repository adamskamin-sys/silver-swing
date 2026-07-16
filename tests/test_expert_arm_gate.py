"""Tests for expert_arm_gate.py — 6-voter supermajority initial-entry gate."""
from __future__ import annotations


def test_kill_switch_default_is_expert():
    import importlib
    import expert_arm_gate
    importlib.reload(expert_arm_gate)
    assert expert_arm_gate.MODE == "expert"


def test_supermajority_is_four_of_six():
    from expert_arm_gate import _SUPERMAJORITY_THRESHOLD, _TOTAL_VOTERS
    assert _SUPERMAJORITY_THRESHOLD == 4
    assert _TOTAL_VOTERS == 6


def test_connors_rsi2_extreme_uptrend_is_overbought():
    """All-up moves → RSI(2) = 100 (max overbought)."""
    from expert_arm_gate import connors_rsi2
    all_up = [100 + i for i in range(30)]
    rsi = connors_rsi2(all_up)
    assert rsi is not None
    assert rsi > 90  # extreme overbought


def test_connors_rsi2_extreme_downtrend_is_oversold():
    """All-down moves → RSI(2) → 0 (max oversold)."""
    from expert_arm_gate import connors_rsi2
    all_down = [100 - i for i in range(30)]
    rsi = connors_rsi2(all_down)
    assert rsi is not None
    assert rsi < 10  # extreme oversold


def test_connors_rsi2_arm_ok_rejects_overbought_buy():
    """RSI(2) > 70 → reject BUY."""
    from expert_arm_gate import connors_rsi2_arm_ok
    all_up = [100 + i for i in range(30)]
    assert connors_rsi2_arm_ok(all_up, arm_direction="buy") is False


def test_connors_rsi2_arm_ok_allows_oversold_buy():
    """RSI(2) < 70 → allow BUY (permissive — no oversold requirement)."""
    from expert_arm_gate import connors_rsi2_arm_ok
    all_down = [100 - i for i in range(30)]
    assert connors_rsi2_arm_ok(all_down, arm_direction="buy") is True


def test_bollinger_position_at_mean_is_zero():
    """Constant history → mean = current, position = 0 or None (zero stdev)."""
    from expert_arm_gate import bollinger_position
    flat = [100.0] * 30
    # Zero stdev → returns None (undefined position)
    assert bollinger_position(flat) is None


def test_bollinger_arm_ok_rejects_extended_high():
    """Price sharply above mean → reject BUY."""
    from expert_arm_gate import bollinger_arm_ok
    prices = [100.0] * 20 + [110.0]     # 20-period mean=100; current=110
    assert bollinger_arm_ok(prices, arm_direction="buy") is False


def test_bollinger_arm_ok_allows_below_mean():
    """Price near or below mean → allow BUY."""
    from expert_arm_gate import bollinger_arm_ok
    prices = list(range(90, 110))    # mean ≈ 99.5, current = 109 → high
    # This is above mean, might be flagged. Test a clearer below-mean case:
    prices = [100 + (i % 3) for i in range(19)] + [99]
    result = bollinger_arm_ok(prices, arm_direction="buy")
    # 19 samples of 100/101/102 zigzag, then last=99 (below mean)
    # Should allow (position ≤ +0.5σ)
    assert result is True or result is None   # allow or insufficient data


def test_ranging_with_dip_gets_supermajority_allow():
    """Ideal buy setup: ranging, recent dip, calm flow. All 6 experts allow."""
    from expert_arm_gate import arm_allowed
    ranging_dip = ([100, 101, 100, 99] * 7) + [98, 97]   # 30 samples
    d = arm_allowed(
        prices=ranging_dip,
        arm_direction="buy",
        order_flow_imbalance=0.1,
        kyle_lambda=1.0, kyle_baseline=1.0,
    )
    assert d.allow is True
    assert d.vote_count >= 4


def test_downtrend_toxic_denies():
    """Downtrend + toxic flow → deny."""
    from expert_arm_gate import arm_allowed
    downtrend = [100 - i for i in range(30)]
    d = arm_allowed(
        prices=downtrend,
        arm_direction="buy",
        order_flow_imbalance=0.8,
        kyle_lambda=3.0, kyle_baseline=1.0,
    )
    assert d.allow is False


def test_cold_start_allows_when_zero_voters():
    """If ALL voters return None (truly no data), allow — matches the
    existing _sleeve_trend_ok_for_buy pattern (permissive at cold start
    rather than stalling the sleeve). Consistency with the codebase."""
    from expert_arm_gate import arm_allowed
    d = arm_allowed(
        prices=[100, 101],   # too short for any voter
        arm_direction="buy",
    )
    assert d.total_voters == 0
    assert d.allow is True   # cold start grace


def test_partial_data_below_supermajority_denies():
    """If SOME voters return but < 4, deny. Partial data suggests
    something's off and we shouldn't take the risk."""
    from expert_arm_gate import arm_allowed
    d = arm_allowed(
        prices=[100, 101],   # too short for kaufman/wilder/connors/bollinger
        arm_direction="buy",
        order_flow_imbalance=0.7,   # toxic → 1 vote (no)
        # No Kyle
    )
    assert 0 < d.total_voters < 4
    assert d.allow is False


def test_supermajority_stricter_than_reentry():
    """Initial-entry uses 4-of-6 (supermajority), reentry uses 3-of-5
    (majority). Initial entry should be MORE conservative because we're
    committing fresh capital."""
    from expert_arm_gate import _SUPERMAJORITY_THRESHOLD, _TOTAL_VOTERS
    from expert_gate import _MAJORITY_THRESHOLD, _TOTAL_VOTERS as _REG_TOTAL
    arm_ratio = _SUPERMAJORITY_THRESHOLD / _TOTAL_VOTERS
    reentry_ratio = _MAJORITY_THRESHOLD / _REG_TOTAL
    assert arm_ratio >= reentry_ratio, (
        f"Initial-entry threshold ({_SUPERMAJORITY_THRESHOLD}/{_TOTAL_VOTERS}) "
        f"must be ≥ reentry threshold ({_MAJORITY_THRESHOLD}/{_REG_TOTAL})."
    )


def test_wire_up_swing_leg_has_expert_arm_gate_import():
    """Regression: swing_leg must import + call expert_arm_gate."""
    src = open("/Users/adamkamin/silver-swing/swing_leg.py").read()
    assert "import expert_arm_gate" in src
    assert "arm_allowed" in src


def test_wire_up_has_helper_method():
    from swing_leg import SwingTrader
    assert hasattr(SwingTrader, "_expert_arm_gate_allows")


def test_wire_up_gates_primary_buy():
    """Regression: primary BUY arm must call the gate."""
    import inspect
    from swing_leg import SwingTrader
    src = inspect.getsource(SwingTrader._ensure_armed)
    assert "_expert_arm_gate_allows" in src
    assert 'prices_source="primary"' in src


def test_wire_up_gates_sleeve_buy():
    """Regression: sleeve BUY arm must call the gate."""
    import inspect
    from swing_leg import SwingTrader
    src = inspect.getsource(SwingTrader._sleeve_arm)
    assert "_expert_arm_gate_allows" in src


def test_wire_up_has_kill_switch_check():
    """Helper must check MODE for kill switch."""
    import inspect
    from swing_leg import SwingTrader
    src = inspect.getsource(SwingTrader._expert_arm_gate_allows)
    assert 'MODE' in src and '"expert"' in src


def test_gate_error_fails_safe_to_allow():
    """If the gate itself errors, we ALLOW the arm (legacy behavior).
    Refusing on error would break all trading if the expert layer has
    a bug — worse than no gate."""
    import inspect
    from swing_leg import SwingTrader
    src = inspect.getsource(SwingTrader._expert_arm_gate_allows)
    # Look for "return True" in the exception handler
    assert "except Exception" in src
    # After the except block, there must be a return True (fail-safe)
    lines = src.splitlines()
    for i, ln in enumerate(lines):
        if "except Exception" in ln:
            # Find the next 'return' after this line
            for j in range(i + 1, min(i + 15, len(lines))):
                if "return True" in lines[j]:
                    return
    assert False, "Fail-safe 'return True' not found after except block"
