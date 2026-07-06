"""Tests for TraderManager + AccountMarginGovernor (spec §9 + §9A)."""

import pytest

from manager import AccountMarginGovernor, InstrumentSlot, TraderManager
from paper_broker import PaperBroker, PaperConfig
from safety import TradeLog
from state_store import JsonFileStateStore


TENANT = "adam"


def make_paper(product_id, starting_balance=100_000.0):
    return PaperBroker(PaperConfig(
        product_id=product_id, contract_size=50.0, tick_size=0.005,
        fee_per_fill=2.34, margin_per_contract=275.0,
        starting_balance=starting_balance,
    ))


def default_cfg():
    return {
        "core_qty": 10, "swing_qty": 2, "max_swing_qty": 5,
        "sell_px": 65.0, "buy_px": 63.0, "contract_size": 50,
        "margin_per_contract": 275.0, "scale_up_buffer_mult": 1.5,
        "fee_per_contract_roundtrip": 4.68,
        "abort_below": 60.0, "abort_above": 70.0,
        "fee_sanity_multiplier": 2.0,
    }


# ---- basic manager behavior --------------------------------------------


def test_add_and_step_multiple_instruments(tmp_path):
    store = JsonFileStateStore(tmp_path / "s.json")
    for symbol in ("SLR-27AUG26-CDE", "GC-27AUG26-CDE"):
        store.put_config(TENANT, symbol, default_cfg())

    mgr = TraderManager(TENANT, store)
    for symbol in ("SLR-27AUG26-CDE", "GC-27AUG26-CDE"):
        broker = make_paper(symbol)
        broker.place_limit("BUY", 12, 58.44); broker.tick(58.44, 58.44)
        mgr.add_instrument(symbol, broker)

    mgr.reconcile_all()
    mgr.step_all(prices={"SLR-27AUG26-CDE": 63.5, "GC-27AUG26-CDE": 63.5})

    # Both should have armed a SELL order
    for slot in mgr.slots.values():
        assert slot.trader.s.live_order_id is not None


def test_remove_instrument(tmp_path):
    store = JsonFileStateStore(tmp_path / "s.json")
    store.put_config(TENANT, "X", default_cfg())
    mgr = TraderManager(TENANT, store)
    broker = make_paper("X"); broker.place_limit("BUY", 12, 58.44); broker.tick(58.44, 58.44)
    mgr.add_instrument("X", broker)
    assert "X" in mgr.slots
    mgr.remove_instrument("X")
    assert "X" not in mgr.slots


def test_snapshot_all_aggregates(tmp_path):
    store = JsonFileStateStore(tmp_path / "s.json")
    store.put_config(TENANT, "A", default_cfg())
    store.put_config(TENANT, "B", default_cfg())
    mgr = TraderManager(TENANT, store)
    for symbol in ("A", "B"):
        broker = make_paper(symbol)
        broker.place_limit("BUY", 12, 58.44); broker.tick(58.44, 58.44)
        mgr.add_instrument(symbol, broker)
    agg = mgr.snapshot_all()
    assert set(agg["instruments"].keys()) == {"A", "B"}
    assert agg["total_margin"] > 0
    assert agg["total_equity"] > 0


# ---- governor ----------------------------------------------------------


def test_governor_vetoes_over_cap(tmp_path):
    """Two instruments each holding a position that fully consumes the cap →
    a scale-up on either should be vetoed."""
    store = JsonFileStateStore(tmp_path / "s.json")
    store.put_config(TENANT, "A", default_cfg())
    store.put_config(TENANT, "B", default_cfg())

    # Cap is tight — 24 contracts × $275 = $6,600 in use; scale-up would need +$275
    gov = AccountMarginGovernor(TENANT, max_aggregate_margin=6600.0)
    mgr = TraderManager(TENANT, store, governor=gov)
    for symbol in ("A", "B"):
        broker = make_paper(symbol)
        broker.place_limit("BUY", 12, 58.44); broker.tick(58.44, 58.44)
        mgr.add_instrument(symbol, broker)

    slot_a = mgr.slots["A"]
    slot_a.trader.s.realized_pnl = 10_000.0  # plenty of profit → would normally scale up
    slot_a.trader.s.state = slot_a.trader.s.state.__class__.ARMED_BUY

    # Governor should veto — aggregate would go from ~6600 to ~6875, over cap 6600
    result = gov.veto_scale_up(list(mgr.slots.values()), slot_a)
    assert result is True


def test_governor_allows_under_cap(tmp_path):
    store = JsonFileStateStore(tmp_path / "s.json")
    store.put_config(TENANT, "A", default_cfg())
    gov = AccountMarginGovernor(TENANT, max_aggregate_margin=100_000.0)
    mgr = TraderManager(TENANT, store, governor=gov)
    broker = make_paper("A")
    broker.place_limit("BUY", 12, 58.44); broker.tick(58.44, 58.44)
    slot = mgr.add_instrument("A", broker)
    result = gov.veto_scale_up([slot], slot)
    assert result is False


def test_governor_hook_blocks_scale_up(tmp_path):
    """End-to-end: manager wires governor into trader._maybe_scale_up so the
    scale-up doesn't happen even when the strategy thinks it should."""
    store = JsonFileStateStore(tmp_path / "s.json")
    store.put_config(TENANT, "A", default_cfg())
    gov = AccountMarginGovernor(TENANT, max_aggregate_margin=100.0)  # very tight
    mgr = TraderManager(TENANT, store, governor=gov)
    broker = make_paper("A")
    broker.place_limit("BUY", 12, 58.44); broker.tick(58.44, 58.44)
    slot = mgr.add_instrument("A", broker)

    slot.trader.s.realized_pnl = 10_000.0  # would fund a scale-up
    original_qty = slot.trader.s.swing_qty
    slot.trader._maybe_scale_up()
    # Vetoed — swing_qty unchanged
    assert slot.trader.s.swing_qty == original_qty


def test_governor_alerts_via_notifier(tmp_path):
    from unittest.mock import MagicMock
    notifier = MagicMock()
    gov = AccountMarginGovernor(TENANT, max_aggregate_margin=0.01, notifier=notifier)
    store = JsonFileStateStore(tmp_path / "s.json")
    store.put_config(TENANT, "A", default_cfg())
    mgr = TraderManager(TENANT, store)  # no gov wired
    broker = make_paper("A"); broker.place_limit("BUY", 1, 58.44); broker.tick(58.44, 58.44)
    slot = mgr.add_instrument("A", broker)
    gov.veto_scale_up([slot], slot)
    notifier.send.assert_called()
