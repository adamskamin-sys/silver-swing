"""Tests for expert_gate.py — reentry-after-stop majority-vote gate.

Regression guards against re-opening the PT/HYPE bleed via silent
revert of the gate or its thresholds.
"""
from __future__ import annotations


def test_kill_switch_default_is_expert():
    """MODE defaults to 'expert'. Legacy default is a backdoor to the
    bleed — regression guard."""
    import importlib
    import expert_gate
    importlib.reload(expert_gate)
    assert expert_gate.MODE == "expert"


def test_majority_threshold_is_three_of_five():
    from expert_gate import _MAJORITY_THRESHOLD, _TOTAL_VOTERS
    assert _MAJORITY_THRESHOLD == 3
    assert _TOTAL_VOTERS == 5


def test_kaufman_er_flags_trend():
    """Straight uptrend has ER ≈ 1.0 (trending). Random walk has ER < 0.5."""
    from expert_gate import kaufman_efficiency_ratio
    trend = [100 + i for i in range(30)]     # linear uptrend
    er_trend = kaufman_efficiency_ratio(trend)
    assert er_trend is not None and er_trend > 0.9

    # Zigzag = high total distance, tiny net → low ER
    zigzag = [100 + ((-1)**i) * 0.5 for i in range(30)]
    er_zig = kaufman_efficiency_ratio(zigzag)
    assert er_zig is not None and er_zig < 0.2


def test_kaufman_reentry_ok_blocks_downtrend_buy():
    """Buy reentry in a downtrend → Kaufman votes NO."""
    from expert_gate import kaufman_reentry_ok
    downtrend = [100 - i for i in range(30)]
    assert kaufman_reentry_ok(downtrend, reentry_direction="buy") is False


def test_kaufman_reentry_ok_allows_uptrend_buy():
    """Buy reentry in an uptrend → Kaufman allows (trend in our favor)."""
    from expert_gate import kaufman_reentry_ok
    uptrend = [100 + i for i in range(30)]
    assert kaufman_reentry_ok(uptrend, reentry_direction="buy") is True


def test_cartea_ofi_toxicity_thresholds():
    """|OFI| < 0.5 → allow. ≥ 0.5 → block."""
    from expert_gate import cartea_ofi_toxicity_ok
    assert cartea_ofi_toxicity_ok(0.3) is True
    assert cartea_ofi_toxicity_ok(-0.3) is True   # abs used
    assert cartea_ofi_toxicity_ok(0.6) is False
    assert cartea_ofi_toxicity_ok(None) is None    # no data = no vote


def test_kyle_lambda_ratio_threshold():
    """λ / baseline < 1.5 → allow. ≥ 1.5 → block (informed traders present)."""
    from expert_gate import kyle_lambda_ok
    assert kyle_lambda_ok(1.0, 1.0) is True    # at baseline
    assert kyle_lambda_ok(1.4, 1.0) is True    # slight elevation
    assert kyle_lambda_ok(1.5, 1.0) is False   # threshold breach
    assert kyle_lambda_ok(3.0, 1.0) is False
    assert kyle_lambda_ok(None, 1.0) is None


def test_menkveld_cycle_econ_ok():
    """Last N cycles net positive → allow. Net negative → block."""
    from expert_gate import menkveld_cycle_econ_ok
    assert menkveld_cycle_econ_ok([10, 5, -3, 8, 2]) is True    # net +22
    assert menkveld_cycle_econ_ok([-10, -5, -3, -8, -2]) is False  # net -28
    assert menkveld_cycle_econ_ok([]) is None                    # no data
    assert menkveld_cycle_econ_ok([5]) is None                   # too thin


def test_hasbrouck_cadence_bounds():
    """Cadence floor is bounded [30s, 900s] regardless of Kyle inputs."""
    from expert_gate import (hasbrouck_lambda_half_life_secs,
                              _HARD_CADENCE_FLOOR_SECS,
                              _HARD_CADENCE_CEILING_SECS)
    # None inputs → floor
    assert hasbrouck_lambda_half_life_secs(None, None) == _HARD_CADENCE_FLOOR_SECS
    # Below baseline → floor
    assert hasbrouck_lambda_half_life_secs(0.5, 1.0) == _HARD_CADENCE_FLOOR_SECS
    # Extreme λ → capped at ceiling
    huge = hasbrouck_lambda_half_life_secs(1000.0, 1.0)
    assert huge == _HARD_CADENCE_CEILING_SECS


def test_pt_bleed_scenario_gate_denies():
    """The exact PT-bleed scenario: rearming into a downtrend with toxic
    OFI, elevated λ, losing cycles, only 45s elapsed. Gate MUST deny.
    Direct regression guard against the PT bleed pattern."""
    from expert_gate import reentry_allowed
    downtrend = [1680 - i * 0.5 for i in range(30)]
    dec = reentry_allowed(
        prices=downtrend,
        elapsed_since_stop_secs=45.0,
        reentry_direction="buy",
        order_flow_imbalance=0.7,          # toxic
        kyle_lambda=3.0, kyle_baseline=1.0,  # 3× baseline
        recent_cycle_pnls=[-10, -8, -12, -5, -15],
    )
    assert dec.allow is False, (
        f"PT-bleed scenario must deny reentry. Got votes={dec.votes} "
        f"count={dec.vote_count}/{dec.total_voters} cadence_ok={dec.cadence_ok}"
    )


def test_ranging_calm_scenario_allows():
    """Sideways market, calm flow, decent cycle history, long enough wait.
    Gate should allow reentry."""
    from expert_gate import reentry_allowed
    # Zigzag around 100 = ranging
    ranging = [100 + ((-1)**i) * 0.5 for i in range(30)]
    dec = reentry_allowed(
        prices=ranging,
        elapsed_since_stop_secs=120.0,     # well past floor
        reentry_direction="buy",
        order_flow_imbalance=0.1,          # calm
        kyle_lambda=1.0, kyle_baseline=1.0,  # baseline
        recent_cycle_pnls=[5, 3, 8, 2, 10],  # winning
    )
    assert dec.allow is True, (
        f"Calm-ranging-winning scenario must allow reentry. Got "
        f"votes={dec.votes} cadence_ok={dec.cadence_ok}"
    )


def test_cadence_floor_hard_blocks_even_with_all_yes_votes():
    """If all 5 experts vote YES but cadence floor not met, still DENY.
    Cadence is HARD — no expert overrides it."""
    from expert_gate import reentry_allowed
    ranging = [100 + ((-1)**i) * 0.5 for i in range(30)]
    dec = reentry_allowed(
        prices=ranging,
        elapsed_since_stop_secs=5.0,       # very short
        reentry_direction="buy",
        order_flow_imbalance=0.05,
        kyle_lambda=1.0, kyle_baseline=1.0,
        recent_cycle_pnls=[10, 10, 10, 10, 10],
    )
    assert dec.vote_count == dec.total_voters   # all voted yes
    assert dec.cadence_ok is False               # but cadence not met
    assert dec.allow is False                    # HARD floor wins


def test_missing_data_defaults_to_deny():
    """Silence is not consent. If < majority voters return, deny for safety."""
    from expert_gate import reentry_allowed
    dec = reentry_allowed(
        prices=[100, 101],                  # too short for kaufman/wilder
        elapsed_since_stop_secs=120.0,
        reentry_direction="buy",
        # no ofi, no kyle, no cycles → 0 votes
    )
    assert dec.total_voters == 0
    assert dec.allow is False, (
        "With < majority voters, must default to deny. Silence ≠ consent."
    )


def test_wire_up_swing_leg_has_expert_gate_import():
    """Regression: swing_leg must import expert_gate in the reentry path."""
    import inspect
    from swing_leg import SwingTrader
    src = inspect.getsource(SwingTrader._maybe_trigger_sleeve_reentry)
    assert "import expert_gate" in src
    assert "reentry_allowed" in src


def test_wire_up_has_kill_switch_check():
    """Wire-up must gate on expert_gate.MODE."""
    import inspect
    from swing_leg import SwingTrader
    src = inspect.getsource(SwingTrader._maybe_trigger_sleeve_reentry)
    assert 'MODE' in src and '"expert"' in src


def test_wire_up_records_gate_decision():
    """Every gate consultation must log a sleeve_reentry_gate_decision
    event so operator can audit the vote."""
    import inspect
    from swing_leg import SwingTrader
    src = inspect.getsource(SwingTrader._maybe_trigger_sleeve_reentry)
    assert "sleeve_reentry_gate_decision" in src


def test_arm_path_blocked_while_reentry_pending():
    """Regression: the normal arm path in _sleeve_step must NOT run while
    reentry_pending=True. Root cause of NEAR/HYPE churn loop: gate denied
    reentry but _maybe_trigger_sleeve_reentry() return value was discarded,
    so execution fell straight into 'Arm if no live order' and placed a buy
    immediately — cadence floor bypassed entirely.

    Fix: after calling _maybe_trigger_sleeve_reentry(), check ss.reentry_pending
    and return early. This verifies that guard exists in the source."""
    import inspect
    from swing_leg import SwingTrader
    src = inspect.getsource(SwingTrader._sleeve_step)
    # The guard must appear AFTER the _maybe_trigger_sleeve_reentry call
    reentry_call_pos = src.find("_maybe_trigger_sleeve_reentry")
    assert reentry_call_pos != -1, "_maybe_trigger_sleeve_reentry not found in _sleeve_step"
    guard_pos = src.find("reentry_pending", reentry_call_pos)
    assert guard_pos != -1, (
        "No reentry_pending guard found after _maybe_trigger_sleeve_reentry call. "
        "This means the arm path can bypass the expert gate — the NEAR/HYPE churn loop."
    )
