"""Tests for live_runner.py — mostly the DryRunBroker + preflight logic.

The full run() loop opens a WebSocket and hits live Coinbase; that path is
exercised manually, not in CI."""

import os
from unittest.mock import MagicMock

import pytest

from live_runner import DryRunBroker, _preflight
from state_store import JsonFileStateStore


# ---- DryRunBroker ----------------------------------------------------------


def test_dry_run_broker_intercepts_place_limit():
    real = MagicMock()
    dry = DryRunBroker(real)
    oid = dry.place_limit("SELL", 2, 65.0)
    assert oid.startswith("dry-run-")
    real.place_limit.assert_not_called()  # NEVER touches real


def test_dry_run_broker_intercepts_cancel_for_own_orders():
    real = MagicMock()
    dry = DryRunBroker(real)
    oid = dry.place_limit("BUY", 2, 63.0)
    dry.cancel(oid)
    real.cancel.assert_not_called()


def test_dry_run_broker_passes_through_reads():
    """position_qty, order_status of unknown ids, and other attributes all
    delegate to the wrapped broker."""
    real = MagicMock()
    real.position_qty.return_value = 12
    dry = DryRunBroker(real)
    assert dry.position_qty() == 12


def test_dry_run_broker_status_of_own_order():
    real = MagicMock()
    dry = DryRunBroker(real)
    oid = dry.place_limit("SELL", 2, 65.0)
    st = dry.order_status(oid)
    assert st["status"] == "OPEN"
    assert st["filled_qty"] == 0
    real.order_status.assert_not_called()


def test_dry_run_broker_status_of_real_order_passes_through():
    real = MagicMock()
    real.order_status.return_value = {"status": "FILLED", "filled_qty": 2}
    dry = DryRunBroker(real)
    st = dry.order_status("not-a-dry-run-id")
    assert st["status"] == "FILLED"


# ---- preflight -------------------------------------------------------------


def _valid_config():
    return {
        "core_qty": 10, "swing_qty": 2, "max_swing_qty": 5,
        "sell_px": 65.0, "buy_px": 63.0, "contract_size": 50,
        "margin_per_contract": 275.0, "scale_up_buffer_mult": 1.5,
        "fee_per_contract_roundtrip": 4.68,
        "abort_below": 60.0, "abort_above": 70.0,
        "fee_sanity_multiplier": 2.0, "exit_mode": "fixed_limit",
    }


def _mock_broker(position=12, session_open=True, balance_ok=True, roll_days=52):
    from datetime import datetime, timezone, timedelta
    b = MagicMock()
    b.position_qty.return_value = position
    b.contract_spec.return_value = {
        "product_id": "SLR-27AUG26-CDE",
        "contract_size": 50, "tick_size": 0.005,
        "session_open": session_open,
    }
    b.futures_balance.return_value = {"cfm_usd_balance": {"value": "2784"}} if balance_ok else {}
    # Roll check needs client.get_products
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(days=roll_days)
    b.client.get_products.return_value = MagicMock(to_dict=lambda: {"products": [{
        "product_id": "SLR-27AUG26-CDE",
        "future_product_details": {"contract_expiry": expiry.isoformat().replace("+00:00", "Z")},
    }]})
    return b


def test_preflight_all_passes(tmp_path):
    store = JsonFileStateStore(tmp_path / "s.json")
    store.put_config("adam", "SLR-27AUG26-CDE", _valid_config())
    ok, issues = _preflight(_mock_broker(), store, "adam", "SLR-27AUG26-CDE", MagicMock())
    assert ok, f"unexpected issues: {issues}"


def test_preflight_flags_position_below_core(tmp_path):
    store = JsonFileStateStore(tmp_path / "s.json")
    store.put_config("adam", "SLR-27AUG26-CDE", _valid_config())
    ok, issues = _preflight(_mock_broker(position=5), store, "adam", "SLR-27AUG26-CDE", MagicMock())
    assert not ok
    assert any("position" in i and "core" in i for i in issues)


def test_preflight_flags_closed_session(tmp_path):
    store = JsonFileStateStore(tmp_path / "s.json")
    store.put_config("adam", "SLR-27AUG26-CDE", _valid_config())
    ok, issues = _preflight(_mock_broker(session_open=False), store, "adam", "SLR-27AUG26-CDE", MagicMock())
    assert not ok
    assert any("session" in i for i in issues)


def test_preflight_flags_bad_config(tmp_path):
    store = JsonFileStateStore(tmp_path / "s.json")
    cfg = _valid_config(); cfg["buy_px"] = 70.0  # buy > sell
    store.put_config("adam", "SLR-27AUG26-CDE", cfg)
    ok, issues = _preflight(_mock_broker(), store, "adam", "SLR-27AUG26-CDE", MagicMock())
    assert not ok
    assert any("config" in i for i in issues)


def test_preflight_flags_kill_switch_active(tmp_path):
    from safety import KillSwitch
    store = JsonFileStateStore(tmp_path / "s.json")
    store.put_config("adam", "SLR-27AUG26-CDE", _valid_config())
    KillSwitch(store, "adam").activate(reason="test")
    ok, issues = _preflight(_mock_broker(), store, "adam", "SLR-27AUG26-CDE", MagicMock())
    assert not ok
    assert any("kill switch" in i for i in issues)


def test_preflight_flags_roll_window(tmp_path):
    store = JsonFileStateStore(tmp_path / "s.json")
    store.put_config("adam", "SLR-27AUG26-CDE", _valid_config())
    # Contract expires in 2 days — within 5-day roll window
    ok, issues = _preflight(_mock_broker(roll_days=2), store, "adam", "SLR-27AUG26-CDE", MagicMock())
    assert not ok
    assert any("ROLL" in i for i in issues)


def test_preflight_flags_missing_futures_balance(tmp_path):
    store = JsonFileStateStore(tmp_path / "s.json")
    store.put_config("adam", "SLR-27AUG26-CDE", _valid_config())
    ok, issues = _preflight(_mock_broker(balance_ok=False), store, "adam", "SLR-27AUG26-CDE", MagicMock())
    assert not ok
    assert any("futures balance" in i for i in issues)
