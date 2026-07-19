"""Tests for state_autocorrect — CHN-class drift clamp.

Invariants:
  - state.swing_qty > config AND exchange=0 → clamp to config (existing behavior)
  - state.swing_qty > config AND exchange>0 → clamp to max(config, exchange - core - armed_sleeves)  ← CHN CASE
  - Never underflow below 0
  - Never touch state during in-flight order
  - Skip when snapshot stale AND symbol absent (can't verify)
  - Respect core_qty (don't strand a core position)
  - Respect armed_sleeve_qty (don't strand a sleeve's expected position)
"""
from __future__ import annotations

import pytest

import state_autocorrect as sa


def test_chn_class_partial_drift():
    """The scenario Adam hit 2026-07-18: state=3, config=0, exchange=1.

    Prior autocorrector wouldn't touch this because exchange != 0.
    Expected: clamp state.swing_qty to 1 (matches exchange, respects that
    the 1 contract on exchange is real primary position)."""
    state = {"swing_qty": 3}
    config = {"swing_qty": 0, "core_qty": 0}
    target = sa.target_primary_swing_qty(state, config, exchange_qty=1)
    assert target == 1


def test_original_slr_class_full_ghost():
    """Existing behavior preserved: state=2, config=0, exchange=0 → clamp to 0."""
    state = {"swing_qty": 2}
    config = {"swing_qty": 0, "core_qty": 0}
    target = sa.target_primary_swing_qty(state, config, exchange_qty=0)
    assert target == 0


def test_no_drift_returns_current():
    """When state matches config and both match exchange, return unchanged."""
    state = {"swing_qty": 1}
    config = {"swing_qty": 1, "core_qty": 0}
    target = sa.target_primary_swing_qty(state, config, exchange_qty=1)
    assert target == 1


def test_respects_core_qty():
    """exchange=5, core=3, config=0 → primary swing = 5-3-0 = 2. State=3 → clamp to 2."""
    state = {"swing_qty": 3}
    config = {"swing_qty": 0, "core_qty": 3}
    target = sa.target_primary_swing_qty(state, config, exchange_qty=5)
    assert target == 2


def test_respects_armed_sleeve_qty():
    """exchange=4, core=0, armed sleeve holds 2, config=0.

    Primary swing should be 4 - 0 - 2 = 2. State=3 → clamp to 2 (don't
    strand the sleeve's expected position)."""
    state = {
        "swing_qty": 3,
        "sleeves": {"scan-abc": {"state": "ARMED_SELL"}},
    }
    config = {
        "swing_qty": 0,
        "core_qty": 0,
        "sleeves": [{"id": "scan-abc", "qty": 2}],
    }
    target = sa.target_primary_swing_qty(state, config, exchange_qty=4)
    assert target == 2


def test_sleeve_not_armed_not_counted():
    """Sleeve in ARMED_BUY (waiting to enter) does NOT hold a position, so
    it shouldn't reduce the expected primary."""
    state = {
        "swing_qty": 3,
        "sleeves": {"scan-abc": {"state": "ARMED_BUY"}},
    }
    config = {
        "swing_qty": 0,
        "core_qty": 0,
        "sleeves": [{"id": "scan-abc", "qty": 2}],
    }
    target = sa.target_primary_swing_qty(state, config, exchange_qty=3)
    # Primary swing = 3 - 0 - 0 = 3 (sleeve not armed_sell so not counted).
    # State=3 matches target → no change.
    assert target == 3


def test_never_underflow_below_zero():
    """If exchange < core + sleeves (shouldn't happen but be defensive),
    clamp target to max(config, 0). Never negative."""
    state = {"swing_qty": 5}
    config = {"swing_qty": 0, "core_qty": 10}  # core > exchange
    target = sa.target_primary_swing_qty(state, config, exchange_qty=2)
    assert target == 0


def test_target_is_max_of_config_and_expected():
    """When config > expected_primary, target is config (don't zero out a
    position the user explicitly configured)."""
    state = {"swing_qty": 5}
    config = {"swing_qty": 3, "core_qty": 0}
    # exchange=0 (nothing held) but config says user wants 3. Target should
    # be max(3, 0) = 3. Bot will try to build to 3 (or safety-halt).
    target = sa.target_primary_swing_qty(state, config, exchange_qty=0)
    assert target == 3


def test_should_autocorrect_live_order_blocks():
    """No autocorrect while an order is in flight — the fill could land
    any second and change the calculation."""
    state = {"swing_qty": 3, "live_order_id": "abc-123"}
    config = {"swing_qty": 0, "core_qty": 0}
    ok, new_sq, why = sa.should_autocorrect(
        state, config, exchange_qty=1,
        snapshot_fresh=True, symbol_present_in_snapshot=True,
    )
    assert ok is False
    assert new_sq is None
    assert why == "live_order_in_flight"


def test_should_autocorrect_stale_snapshot_absent_blocks():
    """When snapshot is stale AND symbol not in snapshot, we can't tell if
    it's a real 0 or a Coinbase partial-200 omission — skip."""
    state = {"swing_qty": 3}
    config = {"swing_qty": 0, "core_qty": 0}
    ok, new_sq, why = sa.should_autocorrect(
        state, config, exchange_qty=0,
        snapshot_fresh=False, symbol_present_in_snapshot=False,
    )
    assert ok is False
    assert why == "snapshot_stale_and_symbol_absent"


def test_should_autocorrect_stale_but_symbol_present_ok():
    """Symbol present in snapshot means we DID see the position (or lack
    thereof) — safe to correct even if snapshot is slightly stale."""
    state = {"swing_qty": 3}
    config = {"swing_qty": 0, "core_qty": 0}
    ok, new_sq, why = sa.should_autocorrect(
        state, config, exchange_qty=1,
        snapshot_fresh=False, symbol_present_in_snapshot=True,
    )
    assert ok is True
    assert new_sq == 1
    assert why == "drift_clamped"


def test_should_autocorrect_no_drift_no_action():
    """State already matches target → no action needed, no write."""
    state = {"swing_qty": 1}
    config = {"swing_qty": 1, "core_qty": 0}
    ok, new_sq, why = sa.should_autocorrect(
        state, config, exchange_qty=1,
        snapshot_fresh=True, symbol_present_in_snapshot=True,
    )
    assert ok is False
    assert why == "no_drift"


def test_should_autocorrect_chn_case_end_to_end():
    """The full CHN scenario: state=3, config=0, exchange=1, snapshot fresh,
    no live order → autocorrect to 1."""
    state = {"swing_qty": 3, "live_order_id": None}
    config = {"swing_qty": 0, "core_qty": 0}
    ok, new_sq, why = sa.should_autocorrect(
        state, config, exchange_qty=1,
        snapshot_fresh=True, symbol_present_in_snapshot=True,
    )
    assert ok is True
    assert new_sq == 1
    assert why == "drift_clamped"


def test_malformed_state_swing_qty_returns_zero():
    """Defensive: garbage in state.swing_qty → no crash, target=0."""
    state = {"swing_qty": "not-a-number"}
    config = {"swing_qty": 0, "core_qty": 0}
    target = sa.target_primary_swing_qty(state, config, exchange_qty=0)
    assert target == 0


def test_missing_fields_default_to_zero():
    """Empty state and config dicts: everything zero, no crash."""
    target = sa.target_primary_swing_qty({}, {}, exchange_qty=0)
    assert target == 0
