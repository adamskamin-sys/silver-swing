"""_sweep_orphan_orders must cancel orphan SELLs that would flip the
account SHORT on the live tenant, while preserving BUYs, sleeve-owned
orders, and orders on products not in cfg.

Adam 2026-07-19 (feedback_no_shorting.md): No shorting on adam-live.
CHN + NER had orphan SELLs from a prior bot session that would flip
short after the primary sell closed. Boot-time sweep cancels them.
"""
from __future__ import annotations

import os
import sys

# live_runner imports lots of live deps; stub before import
os.environ.setdefault("SWING_TENANT", "adam-live")
os.environ.setdefault("SWING_SYMBOL", "SLR-27AUG26-CDE")
os.environ.setdefault("SWING_DATA_DIR", "/tmp/silver-swing-tests")


class _FakeStore:
    def __init__(self):
        self._state: dict[tuple, dict] = {}
        self._cfg: dict[tuple, dict] = {}
        self._symbols: dict[str, list[str]] = {}

    def list_symbols(self, tenant):
        return list(self._symbols.get(tenant, []))

    def get_state(self, tenant, symbol):
        return self._state.get((tenant, symbol))

    def put_state(self, tenant, symbol, state):
        self._state[(tenant, symbol)] = state
        self._symbols.setdefault(tenant, []).append(symbol)

    def get_config(self, tenant, symbol):
        return self._cfg.get((tenant, symbol))

    def put_config(self, tenant, symbol, cfg):
        self._cfg[(tenant, symbol)] = cfg
        if symbol not in self._symbols.get(tenant, []):
            self._symbols.setdefault(tenant, []).append(symbol)


class _FakeBroker:
    """Records every cancel() call. Constructs itself for any product_id."""
    canceled: list[tuple[str, str]] = []  # (product_id, oid)
    orders_response: dict = {"orders": []}

    def __init__(self, cfg):
        self._pid = getattr(cfg, "product_id", None)
        self.client = self  # so client.list_orders → self.list_orders

    def list_orders(self, order_status=None):
        return _FakeBroker.orders_response

    def to_dict(self):
        return _FakeBroker.orders_response

    def cancel(self, oid):
        _FakeBroker.canceled.append((self._pid, oid))
        return True


class _FakeBrokerCfg:
    def __init__(self, product_id):
        self.product_id = product_id


def _install(monkeypatch, orders):
    """Install fakes + reset state."""
    _FakeBroker.canceled = []
    _FakeBroker.orders_response = {"orders": orders}
    import broker as _broker
    monkeypatch.setattr(_broker, "CoinbaseBroker", _FakeBroker)
    monkeypatch.setattr(_broker, "BrokerConfig", _FakeBrokerCfg)


def test_orphan_sell_gets_canceled(monkeypatch):
    from live_runner import _sweep_orphan_orders
    store = _FakeStore()
    # Sleeve on CHN owns a specific oid — that one must NOT be canceled
    store.put_config("adam-live", "CHN-19DEC30-CDE", {"contract_size": 1.0})
    store.put_state("adam-live", "CHN-19DEC30-CDE", {
        "sleeves": {"s1": {"live_order_id": "known-sell-oid"}},
    })
    _install(monkeypatch, [
        {"order_id": "known-sell-oid", "side": "SELL", "product_id": "CHN-19DEC30-CDE"},
        {"order_id": "orphan-sell-oid", "side": "SELL", "product_id": "CHN-19DEC30-CDE"},
    ])
    n = _sweep_orphan_orders(store, "adam-live")
    assert n == 1
    assert ("CHN-19DEC30-CDE", "orphan-sell-oid") in _FakeBroker.canceled
    assert ("CHN-19DEC30-CDE", "known-sell-oid") not in _FakeBroker.canceled


def test_buy_orders_never_canceled(monkeypatch):
    """BUYs can't flip us short — leave them alone even if unknown."""
    from live_runner import _sweep_orphan_orders
    store = _FakeStore()
    store.put_config("adam-live", "HYP-20DEC30-CDE", {"contract_size": 1.0})
    _install(monkeypatch, [
        {"order_id": "unknown-buy", "side": "BUY", "product_id": "HYP-20DEC30-CDE"},
    ])
    n = _sweep_orphan_orders(store, "adam-live")
    assert n == 0
    assert _FakeBroker.canceled == []


def test_sells_on_unknown_products_left_alone(monkeypatch):
    """Never cancel on a product not in cfg — could be a manual hedge."""
    from live_runner import _sweep_orphan_orders
    store = _FakeStore()
    # No cfg for MYSTERY-CDE
    _install(monkeypatch, [
        {"order_id": "sell-on-mystery", "side": "SELL", "product_id": "MYSTERY-CDE"},
    ])
    n = _sweep_orphan_orders(store, "adam-live")
    assert n == 0


def test_resting_stop_oids_are_known(monkeypatch):
    """resting_stop_oid must count as 'known' — must NOT be canceled."""
    from live_runner import _sweep_orphan_orders
    store = _FakeStore()
    store.put_config("adam-live", "XLM-31JUL26-CDE", {"contract_size": 5000.0})
    store.put_state("adam-live", "XLM-31JUL26-CDE", {
        "sleeves": {"s1": {"resting_stop_oid": "my-stop-oid"}},
    })
    _install(monkeypatch, [
        {"order_id": "my-stop-oid", "side": "SELL", "product_id": "XLM-31JUL26-CDE"},
    ])
    n = _sweep_orphan_orders(store, "adam-live")
    assert n == 0


def test_multiple_orphans_across_products(monkeypatch):
    from live_runner import _sweep_orphan_orders
    store = _FakeStore()
    store.put_config("adam-live", "CHN-19DEC30-CDE", {"contract_size": 1.0})
    store.put_config("adam-live", "NER-20DEC30-CDE", {"contract_size": 500.0})
    _install(monkeypatch, [
        {"order_id": "chn-orphan", "side": "SELL", "product_id": "CHN-19DEC30-CDE"},
        {"order_id": "ner-orphan", "side": "SELL", "product_id": "NER-20DEC30-CDE"},
    ])
    n = _sweep_orphan_orders(store, "adam-live")
    assert n == 2
    assert ("CHN-19DEC30-CDE", "chn-orphan") in _FakeBroker.canceled
    assert ("NER-20DEC30-CDE", "ner-orphan") in _FakeBroker.canceled
