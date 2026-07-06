"""Tests for the safety layer MVP — TradeLog, Reconciler, KillSwitch."""

import json
from unittest.mock import MagicMock

from safety import KillSwitch, TradeLog, reconcile
from state_store import JsonFileStateStore


# ============================================================================
# TradeLog
# ============================================================================


def test_trade_log_records_event(tmp_path):
    log = TradeLog(tmp_path / "trades.jsonl")
    entry = log.record("order_placed", order_id="abc", side="SELL", qty=2, price=65.0)
    assert entry["event_type"] == "order_placed"
    assert entry["order_id"] == "abc"
    assert "ts" in entry


def test_trade_log_appends_across_writes(tmp_path):
    log = TradeLog(tmp_path / "trades.jsonl")
    log.record("order_placed", order_id="a")
    log.record("order_filled", order_id="a", fill_price=65.0)
    log.record("halt", reason="test")
    events = list(log.events())
    assert len(events) == 3
    assert [e["event_type"] for e in events] == ["order_placed", "order_filled", "halt"]


def test_trade_log_survives_new_instance(tmp_path):
    p = tmp_path / "trades.jsonl"
    TradeLog(p).record("order_placed", order_id="a")
    # new instance, same path
    events = list(TradeLog(p).events())
    assert len(events) == 1


def test_trade_log_tail(tmp_path):
    log = TradeLog(tmp_path / "trades.jsonl")
    for i in range(10):
        log.record("tick", i=i)
    last3 = log.tail(3)
    assert len(last3) == 3
    assert [e["i"] for e in last3] == [7, 8, 9]


def test_trade_log_reads_missing_file_as_empty(tmp_path):
    log = TradeLog(tmp_path / "does-not-exist.jsonl")
    assert list(log.events()) == []
    assert log.tail(5) == []


def test_trade_log_handles_non_json_types(tmp_path):
    """The default=str fallback lets us log objects like datetimes without
    the whole log going down."""
    import datetime
    log = TradeLog(tmp_path / "trades.jsonl")
    log.record("halt", when=datetime.datetime(2026, 7, 6, 3, 0, 0))
    entry = list(log.events())[0]
    assert "2026-07-06" in entry["when"]


def test_trade_log_one_entry_per_line(tmp_path):
    p = tmp_path / "trades.jsonl"
    log = TradeLog(p)
    log.record("a"); log.record("b"); log.record("c")
    lines = p.read_text().strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        json.loads(line)  # each line is standalone valid JSON


# ============================================================================
# Reconciler
# ============================================================================


def test_reconcile_ok_when_positions_match():
    broker = MagicMock()
    broker.position_qty.return_value = 12
    r = reconcile(broker, believed_position=12, believed_order_id="abc")
    assert r.ok
    assert r.believed_position == 12
    assert r.actual_position == 12
    assert r.mismatches == []


def test_reconcile_flags_position_drift():
    broker = MagicMock()
    broker.position_qty.return_value = 10  # exchange shows less
    r = reconcile(broker, believed_position=12)
    assert not r.ok
    assert "position" in r.mismatches[0]
    assert "believed=12" in r.mismatches[0]
    assert "actual=10" in r.mismatches[0]


def test_reconcile_summary_reflects_state():
    broker = MagicMock()
    broker.position_qty.return_value = 12
    assert "OK" in reconcile(broker, believed_position=12).summary()

    broker.position_qty.return_value = 0
    assert "MISMATCH" in reconcile(broker, believed_position=12).summary()


def test_reconcile_works_against_paper_broker():
    """Reconciler must satisfy the Protocol against both CoinbaseBroker and PaperBroker."""
    from paper_broker import PaperBroker, PaperConfig
    b = PaperBroker(PaperConfig(
        product_id="X", contract_size=50, tick_size=0.005,
        fee_per_fill=2.34, margin_per_contract=275, starting_balance=10_000,
    ))
    b.place_limit("BUY", 3, 60.0); b.tick(60.0, 60.0)
    assert reconcile(b, believed_position=3).ok
    assert not reconcile(b, believed_position=99).ok


# ============================================================================
# KillSwitch
# ============================================================================


def test_kill_switch_defaults_off(tmp_path):
    store = JsonFileStateStore(tmp_path / "s.json")
    ks = KillSwitch(store, tenant_id="adam")
    assert not ks.is_active()
    assert ks.reason() is None


def test_kill_switch_activate_then_clear(tmp_path):
    store = JsonFileStateStore(tmp_path / "s.json")
    ks = KillSwitch(store, tenant_id="adam")
    ks.activate(reason="market gap during earnings")
    assert ks.is_active()
    assert ks.reason() == "market gap during earnings"
    ks.clear(cleared_by="adam-manual")
    assert not ks.is_active()


def test_kill_switch_persists_across_processes(tmp_path):
    """The whole point: any process can toggle it, any process sees the change."""
    p = tmp_path / "s.json"
    KillSwitch(JsonFileStateStore(p), "adam").activate("halt everything")
    # new instance (simulates a different process reading the shared store)
    assert KillSwitch(JsonFileStateStore(p), "adam").is_active()


def test_kill_switch_isolated_per_tenant(tmp_path):
    """Multi-tenant safety: adam's kill switch does NOT affect charlie's account."""
    store = JsonFileStateStore(tmp_path / "s.json")
    KillSwitch(store, "adam").activate("adam paused")
    assert KillSwitch(store, "adam").is_active()
    assert not KillSwitch(store, "charlie").is_active()


def test_kill_switch_preserves_previous_reason_after_clear(tmp_path):
    """Audit trail — after clearing, we can see WHY it was on."""
    store = JsonFileStateStore(tmp_path / "s.json")
    ks = KillSwitch(store, "adam")
    ks.activate(reason="market gap")
    ks.clear(cleared_by="adam")
    stored = store.get_config("adam", "__account_kill_switch__")
    assert stored["previous_reason"] == "market gap"
    assert stored["cleared_by"] == "adam"
