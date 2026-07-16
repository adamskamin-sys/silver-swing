"""Tests for the no-primary refactor: SWING_SYMBOL can be "" or "NONE"
to disable the primary-trader path entirely.

Since live_runner.run() is a long-lived process with network + threads,
these tests check the module-level PRIMARY_ENABLED gate and verify
the code branches exist as expected via source inspection.
"""
from __future__ import annotations

import importlib
import inspect
import os


def test_primary_enabled_true_when_symbol_set():
    """Default SLR symbol → PRIMARY_ENABLED=True."""
    # Reload to pick up env-var evaluation at import
    os.environ.pop("SWING_SYMBOL", None)
    os.environ["SWING_SYMBOL"] = "SLR-27AUG26-CDE"
    import live_runner
    importlib.reload(live_runner)
    assert live_runner.PRIMARY_ENABLED is True
    assert live_runner.SYMBOL == "SLR-27AUG26-CDE"


def test_primary_enabled_false_when_symbol_empty():
    """Empty SYMBOL → PRIMARY_ENABLED=False."""
    os.environ["SWING_SYMBOL"] = ""
    import live_runner
    importlib.reload(live_runner)
    assert live_runner.PRIMARY_ENABLED is False


def test_primary_enabled_false_when_symbol_none():
    """SYMBOL='NONE' (case-insensitive) → PRIMARY_ENABLED=False."""
    for val in ("NONE", "none", "None", " none "):
        os.environ["SWING_SYMBOL"] = val
        import live_runner
        importlib.reload(live_runner)
        assert live_runner.PRIMARY_ENABLED is False, (
            f"SWING_SYMBOL={val!r} should be treated as disabled"
        )
    # Reset for other tests
    os.environ["SWING_SYMBOL"] = "SLR-27AUG26-CDE"
    importlib.reload(__import__("live_runner"))


def test_run_source_gates_primary_setup_on_primary_enabled():
    """The primary broker/preflight/trader construction must be gated
    on PRIMARY_ENABLED — otherwise removing SYMBOL would still spawn
    a primary and try to preflight on the empty product_id."""
    import live_runner
    src = inspect.getsource(live_runner.run)
    assert "if PRIMARY_ENABLED:" in src, (
        "run() must gate primary setup on PRIMARY_ENABLED"
    )
    # Coinbase / broker / trader must be initialized to None
    assert "coinbase = None" in src
    assert "broker = None" in src
    assert "trader = None" in src


def test_run_source_gates_main_loop_step_on_feed():
    """The main tick loop must gate trader.step() on `feed is not None`.
    In no-primary mode, feed is None → the else branch sleeps instead."""
    import live_runner
    src = inspect.getsource(live_runner.run)
    # The feed-guarded branch
    assert "if feed is not None:" in src
    # The no-primary else branch with sleep cadence
    assert "no primary" in src.lower() or "No-primary mode" in src


def test_run_source_gates_boot_state_normalizer_on_trader():
    """boot_state_normalizer must skip when there's no primary trader
    (nothing to normalize)."""
    import live_runner
    src = inspect.getsource(live_runner.run)
    assert "if trader is not None:" in src
    assert "boot_state_normalizer" in src


def test_run_source_has_account_broker_helper():
    """A helper must exist so account-level Coinbase calls
    (futures_balance, list_open_orders, snapshot) can find a broker
    even when there's no primary. Reuses any non-primary track's broker."""
    import live_runner
    src = inspect.getsource(live_runner.run)
    assert "_account_broker" in src, (
        "run() must define an _account_broker helper for account-level calls"
    )


def test_kill_switch_semantics_documented_in_source():
    """The kill switch — reverting SWING_SYMBOL to a real value — must
    be documented in the source. Operators need to know the escape
    hatch is env-var-only (no code change)."""
    import live_runner
    src = inspect.getsource(live_runner)
    assert "kill switch" in src.lower() or "revert" in src.lower(), (
        "The no-primary kill switch (env-var revert) must be documented"
    )
