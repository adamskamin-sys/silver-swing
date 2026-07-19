"""main.refresh_portfolio_snapshot must auto-bootstrap specs for any
Coinbase-held product missing contract_size or fee_per_contract_roundtrip
on the live tenant's cfg.

Adam 2026-07-19: buying a product manually outside the bot
(SLR-27AUG26-CDE) never triggered _Track init → no cfg → dashboard
modal blocked preset apply with 'specs haven't loaded yet.' The 2s
portfolio refresh now seeds specs for every held product with no cfg.

Invariants tested:
  - Held product with NO cfg gets bootstrap called
  - Held product with partial cfg (contract_size only, no fees) still triggered
  - Held product with COMPLETE cfg is NOT re-bootstrapped (avoids preview spam)
  - qty=0 rows skipped
  - Bootstrap failure does not raise (snapshot still returned)
"""
from __future__ import annotations

import pytest


class _FakeStore:
    def __init__(self):
        self._cfg: dict[tuple, dict] = {}

    def get_config(self, tenant, symbol):
        return self._cfg.get((tenant, symbol))

    def put_config(self, tenant, symbol, cfg):
        self._cfg[(tenant, symbol)] = cfg


class _FakeBroker:
    def __init__(self, snap):
        self._snap = snap

    def portfolio_snapshot(self):
        return self._snap


@pytest.fixture
def patched_deps(monkeypatch):
    """Isolate main.refresh_portfolio_snapshot from broker + expert params."""
    import main

    calls: list[tuple[str, str]] = []

    def _fake_bootstrap(store, tenant, symbol):
        calls.append((tenant, symbol))
        store.put_config(tenant, symbol, {
            **(store.get_config(tenant, symbol) or {}),
            "contract_size": 50.0,
            "fee_per_contract_roundtrip": 3.10,
        })

    monkeypatch.setattr(main, "_refresh_contract_spec_into_config", _fake_bootstrap)
    monkeypatch.setattr(main, "_attach_expert_params", lambda broker, snap: None)
    return calls


def _install_broker(monkeypatch, snap):
    import main
    fake = _FakeBroker(snap)

    class _FakeBrokerCls:
        def __init__(self, cfg):
            pass

        def portfolio_snapshot(self_inner):
            return fake.portfolio_snapshot()

    class _FakeBrokerConfig:
        def __init__(self, product_id):
            self.product_id = product_id

    def _fake_import(name, *a, **kw):
        raise ImportError("shouldn't reach real broker")

    # Directly patch what refresh_portfolio_snapshot imports at call time.
    import broker as _real_broker
    monkeypatch.setattr(_real_broker, "CoinbaseBroker", _FakeBrokerCls)
    monkeypatch.setattr(_real_broker, "BrokerConfig", _FakeBrokerConfig)


def test_held_product_with_no_cfg_gets_bootstrapped(patched_deps, monkeypatch):
    store = _FakeStore()
    _install_broker(monkeypatch, {
        "derivatives": [
            {"product_id": "SLR-27AUG26-CDE", "qty": 1, "mark": 56.03},
        ],
    })
    from main import refresh_portfolio_snapshot
    n = refresh_portfolio_snapshot(store, "adam-live")
    assert n == 1
    assert ("adam-live", "SLR-27AUG26-CDE") in patched_deps
    cfg = store.get_config("adam-live", "SLR-27AUG26-CDE")
    assert cfg["contract_size"] == 50.0
    assert cfg["fee_per_contract_roundtrip"] == 3.10


def test_held_product_with_partial_cfg_gets_bootstrapped(patched_deps, monkeypatch):
    """cfg has contract_size but no fees — still needs bootstrap."""
    store = _FakeStore()
    store.put_config("adam-live", "XYZ-CDE", {"contract_size": 10.0})
    _install_broker(monkeypatch, {
        "derivatives": [{"product_id": "XYZ-CDE", "qty": 2}],
    })
    from main import refresh_portfolio_snapshot
    refresh_portfolio_snapshot(store, "adam-live")
    assert ("adam-live", "XYZ-CDE") in patched_deps


def test_held_product_with_complete_cfg_not_rebootstrapped(patched_deps, monkeypatch):
    """Complete cfg → no bootstrap call. Avoids preview_order spam every 2s."""
    store = _FakeStore()
    store.put_config("adam-live", "ABC-CDE", {
        "contract_size": 25.0,
        "fee_per_contract_roundtrip": 1.75,
    })
    _install_broker(monkeypatch, {
        "derivatives": [{"product_id": "ABC-CDE", "qty": 1}],
    })
    from main import refresh_portfolio_snapshot
    refresh_portfolio_snapshot(store, "adam-live")
    assert ("adam-live", "ABC-CDE") not in patched_deps


def test_qty_zero_skipped(patched_deps, monkeypatch):
    """Coinbase-listed row with qty=0 (closed position) doesn't warrant seeding."""
    store = _FakeStore()
    _install_broker(monkeypatch, {
        "derivatives": [{"product_id": "GHOST-CDE", "qty": 0}],
    })
    from main import refresh_portfolio_snapshot
    refresh_portfolio_snapshot(store, "adam-live")
    assert not patched_deps


def test_bootstrap_backoff_after_recent_attempt(patched_deps, monkeypatch):
    """After a bootstrap attempt (even a failed one), further attempts on
    the same product are skipped for 5 min. Prevents 2s Coinbase spam."""
    import time
    store = _FakeStore()
    # Pre-seed cfg as if a prior attempt just happened, no specs written yet
    store.put_config("adam-live", "PENDING-CDE", {
        "_bootstrap_last_attempt_ts": time.time() - 10,  # 10s ago
    })
    _install_broker(monkeypatch, {
        "derivatives": [{"product_id": "PENDING-CDE", "qty": 1}],
    })
    from main import refresh_portfolio_snapshot
    refresh_portfolio_snapshot(store, "adam-live")
    # Bootstrap MUST NOT fire again — still inside backoff window
    assert ("adam-live", "PENDING-CDE") not in patched_deps


def test_bootstrap_retries_after_backoff_expires(patched_deps, monkeypatch):
    """After 5+ min, bootstrap retries. Simulates Coinbase recovering
    after a transient issue."""
    import time
    store = _FakeStore()
    store.put_config("adam-live", "STALE-CDE", {
        "_bootstrap_last_attempt_ts": time.time() - 400,  # 400s ago > 300s backoff
    })
    _install_broker(monkeypatch, {
        "derivatives": [{"product_id": "STALE-CDE", "qty": 1}],
    })
    from main import refresh_portfolio_snapshot
    refresh_portfolio_snapshot(store, "adam-live")
    assert ("adam-live", "STALE-CDE") in patched_deps


def test_fractional_qty_still_triggers_bootstrap(patched_deps, monkeypatch):
    """Coinbase may report fractional qty on certain products. Old
    int(qty) cast would have made 0.5 → 0 and skipped bootstrap."""
    store = _FakeStore()
    _install_broker(monkeypatch, {
        "derivatives": [{"product_id": "FRAC-CDE", "qty": 0.5}],
    })
    from main import refresh_portfolio_snapshot
    refresh_portfolio_snapshot(store, "adam-live")
    assert ("adam-live", "FRAC-CDE") in patched_deps


def test_bootstrap_failure_does_not_abort_snapshot(monkeypatch):
    """Bootstrap raising for one product must not break the snapshot write
    or bootstrap of other products."""
    import main

    calls: list[str] = []

    def _fake_bootstrap(store, tenant, symbol):
        calls.append(symbol)
        if symbol == "BAD-CDE":
            raise RuntimeError("Coinbase 500")
        store.put_config(tenant, symbol, {
            "contract_size": 50.0, "fee_per_contract_roundtrip": 3.10,
        })

    monkeypatch.setattr(main, "_refresh_contract_spec_into_config", _fake_bootstrap)
    monkeypatch.setattr(main, "_attach_expert_params", lambda broker, snap: None)
    store = _FakeStore()
    _install_broker(monkeypatch, {
        "derivatives": [
            {"product_id": "BAD-CDE", "qty": 1},
            {"product_id": "GOOD-CDE", "qty": 1},
        ],
    })
    from main import refresh_portfolio_snapshot
    n = refresh_portfolio_snapshot(store, "adam-live")
    assert n == 2
    assert "BAD-CDE" in calls
    assert "GOOD-CDE" in calls
    # Snapshot was still written
    pf = store.get_config("adam-live", "__portfolio__")
    assert pf is not None
    assert pf["_refresh_ok"] is True
    # Good product got its cfg with specs
    good = store.get_config("adam-live", "GOOD-CDE")
    assert good is not None
    assert good["contract_size"] == 50.0
    # Bad product got a backoff marker but no specs — ensures the
    # 5-min backoff kicks in so we don't hammer Coinbase on failures.
    bad = store.get_config("adam-live", "BAD-CDE")
    assert bad is not None
    assert bad.get("contract_size") is None
    assert bad.get("_bootstrap_last_attempt_ts") is not None
