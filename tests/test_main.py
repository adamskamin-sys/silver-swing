"""Tests for main.py — mostly the helpers (config seeding, mode dispatch).
The full paper loop is exercised end-to-end in a manual smoke, not here,
since it involves an actual WebSocket."""

import pytest

from state_store import JsonFileStateStore


def test_seed_config_populates_when_missing(tmp_path, monkeypatch):
    from main import _seed_config_if_missing, _default_paper_config
    store = JsonFileStateStore(tmp_path / "store.json")
    _seed_config_if_missing(store, "adam", "SLR-27AUG26-CDE")
    cfg = store.get_config("adam", "SLR-27AUG26-CDE")
    assert cfg is not None
    assert cfg["margin_per_contract"] == 275.0  # empirical


def test_seed_config_does_not_clobber_existing(tmp_path):
    from main import _seed_config_if_missing
    store = JsonFileStateStore(tmp_path / "store.json")
    store.put_config("adam", "SLR-27AUG26-CDE", {"sell_px": 999.0})
    _seed_config_if_missing(store, "adam", "SLR-27AUG26-CDE")
    assert store.get_config("adam", "SLR-27AUG26-CDE")["sell_px"] == 999.0


def test_live_mode_refuses_without_confirm(monkeypatch, capsys):
    """live mode now delegates to live_runner.run(), which enforces the
    dry-run OR confirm env var before doing anything else. Same net effect
    from main.py's perspective: exit code 2 with a REFUSING message."""
    monkeypatch.setenv("SWING_MODE", "live")
    monkeypatch.delenv("SWING_LIVE_CONFIRM", raising=False)
    monkeypatch.delenv("SWING_LIVE_DRY_RUN", raising=False)
    from main import run_live_mode
    rc = run_live_mode()
    assert rc == 2  # explicit non-zero exit
    out = capsys.readouterr().out
    assert "REFUSING" in out


def test_unknown_mode_exits_with_code_2(monkeypatch, capsys):
    monkeypatch.setenv("SWING_MODE", "nonsense")
    from main import main
    rc = main()
    assert rc == 2
    assert "unknown SWING_MODE" in capsys.readouterr().out
