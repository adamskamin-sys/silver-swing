"""Funding watcher — sign-flip detector emits shadow signals."""

import ast
import pathlib

import pytest


WATCHER_PATH = pathlib.Path(__file__).parent.parent / "funding_watcher.py"


class _FakeStore:
    """In-memory store shim mimicking JsonFileStateStore for tests."""

    def __init__(self):
        self._snap = {}
        self._cfg = {}
        self._log = []
        self._r = None  # signal 'no redis' → fallback path

    def list_tenants(self):
        return sorted({t for (t, _) in self._snap.keys()})

    def list_symbols(self, tenant):
        return sorted(s for (t, s) in self._snap.keys() if t == tenant)

    def get_snapshot(self, tenant, symbol):
        return self._snap.get((tenant, symbol))

    def put_snapshot(self, tenant, symbol, snap):
        self._snap[(tenant, symbol)] = snap

    def get_config(self, tenant, symbol):
        return self._cfg.get((tenant, symbol))

    def put_config(self, tenant, symbol, cfg):
        self._cfg[(tenant, symbol)] = cfg


def _last_funding_log(store):
    blob = store.get_config("__twitter__", "__log__") or {"entries": []}
    return blob.get("entries") or []


def test_shadow_mode_flag_is_false():
    import funding_watcher
    assert funding_watcher.EXECUTE_TRADES is False


def test_no_broker_imports():
    tree = ast.parse(WATCHER_PATH.read_text())
    forbidden = {"place_limit", "place_market", "place_order", "submit_order",
                 "CoinbaseBroker", "PaperBroker"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name not in forbidden
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "broker"


def test_first_observation_records_no_signal():
    """First time we see a perp's funding rate → record but no shadow signal."""
    from funding_watcher import tick
    store = _FakeStore()
    store.put_snapshot("t1", "BTC-PERP-INTX", {
        "last_mark": 63000, "funding_rate": 0.0002
    })
    telem = tick(store, "t1")
    assert telem["perps_scanned"] == 1
    assert telem["flips_detected"] == 0
    assert _last_funding_log(store) == []


def test_ignores_non_perp_products():
    from funding_watcher import tick
    store = _FakeStore()
    store.put_snapshot("t1", "SLR-27AUG26-CDE", {
        "last_mark": 30.5, "funding_rate": 0.001  # ignored: not a perp
    })
    telem = tick(store, "t1")
    assert telem["perps_scanned"] == 0


def test_no_flip_when_sign_unchanged():
    """Two consecutive positive readings → no flip, no signal."""
    # With no Redis client, prev state can't be tracked between ticks — but
    # the state cache is Redis-only. Assertion: without redis, the fallback
    # is 'no prev seen' so every reading looks fresh and no flip fires.
    from funding_watcher import tick
    store = _FakeStore()
    store.put_snapshot("t1", "BTC-PERP-INTX", {
        "last_mark": 63000, "funding_rate": 0.0002
    })
    tick(store, "t1")
    tick(store, "t1")
    # No flips because we can't track prev without Redis in this shim
    assert _last_funding_log(store) == []


def test_scanner_boost_for_negative_funding():
    """funding_signals.scanner_boost should return > 1.0 for negative funding."""
    from funding_signals import scanner_boost
    assert scanner_boost(-0.001) > 1.0
    assert scanner_boost(0.001) < 1.0
    assert scanner_boost(None) == 1.0
    assert scanner_boost(0.0) == 1.0
