"""Tests for the server-side config validator (spec §10)."""

import pytest

from config_validator import validate_config


def valid_config():
    return {
        "core_qty": 10,
        "swing_qty": 2,
        "max_swing_qty": 5,
        "sell_px": 65.0,
        "buy_px": 63.0,
        "contract_size": 50,
        "margin_per_contract": 275.0,
        "scale_up_buffer_mult": 1.5,
        "fee_per_contract_roundtrip": 4.68,
        "abort_below": 60.0,
        "abort_above": 70.0,
        "fee_sanity_multiplier": 2.0,
        "exit_mode": "fixed_limit",
    }


def issue_fields(result):
    return {i.field for i in result.issues}


def test_valid_config_passes():
    r = validate_config(valid_config())
    assert r.ok
    assert r.issues == []


# ---- core_qty ---------------------------------------------------------------


def test_core_qty_zero_rejected():
    cfg = valid_config(); cfg["core_qty"] = 0
    r = validate_config(cfg)
    assert not r.ok
    assert "core_qty" in issue_fields(r)


def test_core_qty_negative_rejected():
    cfg = valid_config(); cfg["core_qty"] = -1
    r = validate_config(cfg)
    assert "core_qty" in issue_fields(r)


def test_core_qty_fractional_rejected():
    cfg = valid_config(); cfg["core_qty"] = 5.5
    r = validate_config(cfg)
    assert "core_qty" in issue_fields(r)


def test_core_qty_missing_rejected():
    cfg = valid_config(); del cfg["core_qty"]
    r = validate_config(cfg)
    assert "core_qty" in issue_fields(r)


# ---- swing_qty --------------------------------------------------------------


def test_swing_qty_exceeds_max_rejected():
    cfg = valid_config(); cfg["swing_qty"] = 10; cfg["max_swing_qty"] = 5
    r = validate_config(cfg)
    assert "swing_qty" in issue_fields(r)


def test_swing_qty_equal_max_ok():
    cfg = valid_config(); cfg["swing_qty"] = 5; cfg["max_swing_qty"] = 5
    r = validate_config(cfg)
    assert r.ok


# ---- price cross-fields -----------------------------------------------------


def test_buy_gte_sell_rejected():
    cfg = valid_config(); cfg["buy_px"] = 65.0; cfg["sell_px"] = 65.0
    r = validate_config(cfg)
    assert "buy_px" in issue_fields(r)


def test_buy_above_sell_rejected():
    cfg = valid_config(); cfg["buy_px"] = 66.0; cfg["sell_px"] = 65.0
    r = validate_config(cfg)
    assert "buy_px" in issue_fields(r)


def test_abort_below_gte_buy_rejected():
    """abort_below must sit outside the range (below buy_px)."""
    cfg = valid_config(); cfg["abort_below"] = 63.0; cfg["buy_px"] = 63.0
    r = validate_config(cfg)
    assert "abort_below" in issue_fields(r)


def test_abort_above_lte_sell_rejected():
    cfg = valid_config(); cfg["abort_above"] = 65.0; cfg["sell_px"] = 65.0
    r = validate_config(cfg)
    assert "abort_above" in issue_fields(r)


def test_abort_below_gte_abort_above_rejected():
    cfg = valid_config(); cfg["abort_below"] = 70.0; cfg["abort_above"] = 70.0
    r = validate_config(cfg)
    assert "abort_below" in issue_fields(r)


# ---- multipliers ------------------------------------------------------------


def test_scale_up_below_1_rejected():
    cfg = valid_config(); cfg["scale_up_buffer_mult"] = 0.5
    r = validate_config(cfg)
    assert "scale_up_buffer_mult" in issue_fields(r)


def test_fee_sanity_below_1_rejected():
    cfg = valid_config(); cfg["fee_sanity_multiplier"] = 0.9
    r = validate_config(cfg)
    assert "fee_sanity_multiplier" in issue_fields(r)


# ---- exit_mode --------------------------------------------------------------


def test_unknown_exit_mode_rejected():
    cfg = valid_config(); cfg["exit_mode"] = "wing_it"
    r = validate_config(cfg)
    assert "exit_mode" in issue_fields(r)


def test_trailing_stop_requires_trail_fields():
    cfg = valid_config(); cfg["exit_mode"] = "trailing_stop"
    r = validate_config(cfg)
    assert "trail_distance" in issue_fields(r)
    assert "trail_trigger" in issue_fields(r)


def test_trailing_stop_trigger_below_sell_rejected():
    cfg = valid_config()
    cfg["exit_mode"] = "trailing_stop"
    cfg["trail_distance"] = 0.20
    cfg["trail_trigger"] = 60.0  # below sell_px 65
    r = validate_config(cfg)
    assert "trail_trigger" in issue_fields(r)


def test_trailing_stop_with_valid_trail_fields_ok():
    cfg = valid_config()
    cfg["exit_mode"] = "trailing_stop"
    cfg["trail_distance"] = 0.20
    cfg["trail_trigger"] = 65.0
    r = validate_config(cfg)
    assert r.ok


# ---- money fields -----------------------------------------------------------


def test_negative_margin_rejected():
    cfg = valid_config(); cfg["margin_per_contract"] = -1
    r = validate_config(cfg)
    assert "margin_per_contract" in issue_fields(r)


def test_negative_fee_rejected():
    cfg = valid_config(); cfg["fee_per_contract_roundtrip"] = -1.0
    r = validate_config(cfg)
    assert "fee_per_contract_roundtrip" in issue_fields(r)


# ---- issue-collection semantics ---------------------------------------------


def test_multiple_issues_collected():
    """Field-level errors accumulate — the UI shows all of them at once, not one-at-a-time."""
    cfg = valid_config()
    cfg["core_qty"] = 0
    cfg["buy_px"] = 100.0
    cfg["exit_mode"] = "nonsense"
    r = validate_config(cfg)
    fields = issue_fields(r)
    assert "core_qty" in fields
    assert "buy_px" in fields
    assert "exit_mode" in fields


def test_result_serializes_cleanly():
    cfg = valid_config(); cfg["core_qty"] = 0
    r = validate_config(cfg)
    d = r.to_dict()
    assert "ok" in d and "issues" in d
    assert any(i["field"] == "core_qty" for i in d["issues"])
