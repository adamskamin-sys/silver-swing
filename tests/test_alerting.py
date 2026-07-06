"""Tests for the alerting layer + heartbeat check."""

import io
import time
from unittest.mock import MagicMock, patch

import pytest

from alerting import (LogNotifier, MultiNotifier, Priority, TelegramNotifier,
                      default_notifier)
from state_store import JsonFileStateStore


# ---- LogNotifier ------------------------------------------------------------


def test_log_notifier_writes_to_stream():
    buf = io.StringIO()
    n = LogNotifier(stream=buf)
    n.send("test", "message body", Priority.INFO)
    out = buf.getvalue()
    assert "test" in out
    assert "message body" in out
    assert "INFO" in out


def test_log_notifier_writes_to_file(tmp_path):
    buf = io.StringIO()
    p = tmp_path / "alerts.log"
    n = LogNotifier(path=str(p), stream=buf)
    n.send("halt", "reason X", Priority.CRIT)
    assert "halt" in p.read_text()
    assert "CRIT" in p.read_text()


def test_log_notifier_priority_appears_in_output():
    buf = io.StringIO()
    n = LogNotifier(stream=buf)
    for prio in (Priority.INFO, Priority.WARN, Priority.CRIT):
        n.send("s", "b", prio)
    out = buf.getvalue()
    assert "INFO" in out and "WARN" in out and "CRIT" in out


def test_log_notifier_survives_broken_stream():
    """A broken stream shouldn't crash the caller — alerting must never break the bot."""
    class BrokenStream:
        def write(self, *a, **k): raise IOError("closed")
        def flush(self): raise IOError("closed")

    LogNotifier(stream=BrokenStream()).send("x", "y", Priority.INFO)  # must not raise


# ---- TelegramNotifier -------------------------------------------------------


def test_telegram_notifier_skips_when_unconfigured(monkeypatch):
    """Without env vars set, TelegramNotifier is a no-op (not an error)."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    n = TelegramNotifier()
    # Would raise if it tried to send with no token
    n.send("s", "b", Priority.INFO)


def test_telegram_notifier_sends_when_configured():
    n = TelegramNotifier(token="tok", chat_id="12345")
    with patch("alerting.requests.post") as post:
        n.send("halt", "market gap", Priority.CRIT)
        assert post.called
        call = post.call_args
        assert "tok" in call.args[0]
        assert call.kwargs["json"]["chat_id"] == "12345"
        assert "halt" in call.kwargs["json"]["text"]


def test_telegram_notifier_survives_network_error():
    n = TelegramNotifier(token="tok", chat_id="12345")
    with patch("alerting.requests.post", side_effect=ConnectionError("boom")):
        n.send("s", "b", Priority.CRIT)  # must not raise


# ---- MultiNotifier ----------------------------------------------------------


def test_multi_notifier_fans_out():
    a, b = MagicMock(), MagicMock()
    m = MultiNotifier(a, b)
    m.send("s", "body", Priority.WARN)
    a.send.assert_called_once_with("s", "body", Priority.WARN)
    b.send.assert_called_once_with("s", "body", Priority.WARN)


def test_multi_notifier_child_failure_isolated():
    """One child raising must not stop others from receiving the alert."""
    good = MagicMock()
    bad = MagicMock()
    bad.send.side_effect = RuntimeError("boom")
    MultiNotifier(bad, good).send("s", "b", Priority.CRIT)
    good.send.assert_called_once()


# ---- default_notifier -------------------------------------------------------


def test_default_notifier_is_log_only_by_default(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    n = default_notifier()
    assert isinstance(n, LogNotifier)


def test_default_notifier_adds_telegram_when_env_set(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    n = default_notifier()
    assert isinstance(n, MultiNotifier)
    assert any(isinstance(c, TelegramNotifier) for c in n.notifiers)


# ---- SwingTrader integration ------------------------------------------------


def test_swing_trader_halt_fires_notifier(tmp_path):
    """Wire a notifier into the trader and confirm HALT triggers it with CRIT."""
    from paper_broker import PaperBroker, PaperConfig
    from safety import TradeLog
    from swing_leg import SwingTrader

    store = JsonFileStateStore(tmp_path / "store.json")
    store.put_config("adam", "SLR-27AUG26-CDE", {
        "core_qty": 10, "swing_qty": 2, "max_swing_qty": 5,
        "sell_px": 65.0, "buy_px": 63.0, "contract_size": 50,
        "margin_per_contract": 275.0, "scale_up_buffer_mult": 1.5,
        "fee_per_contract_roundtrip": 4.68,
        "abort_below": 60.0, "abort_above": 70.0,
        "fee_sanity_multiplier": 2.0,
    })
    log = TradeLog(tmp_path / "trades.jsonl")
    broker = PaperBroker(PaperConfig(
        product_id="SLR-27AUG26-CDE", contract_size=50.0, tick_size=0.005,
        fee_per_fill=2.34, margin_per_contract=275.0, starting_balance=100_000.0,
    ))
    broker.place_limit("BUY", 8, 58.44); broker.tick(58.44, 58.44)  # below core

    notifier = MagicMock()
    trader = SwingTrader(broker, store, "adam", "SLR-27AUG26-CDE",
                         trade_log=log, notifier=notifier)
    trader.reconcile()  # should HALT (position 8 below core 10)

    notifier.send.assert_called()
    args = notifier.send.call_args
    assert "HALT" in args[0][0]  # subject
    assert args[0][2] == Priority.CRIT


# ---- Heartbeat check --------------------------------------------------------


def test_heartbeat_check_fresh_returns_zero(tmp_path):
    from check_heartbeat import check
    store = JsonFileStateStore(tmp_path / "store.json")
    store.put_state("adam", "SLR", {
        "state": "ARMED_SELL", "last_heartbeat_ts": time.time(),
    })
    notifier = MagicMock()
    rc = check(store, "adam", "SLR", stale_seconds=120, notifier=notifier)
    assert rc == 0
    assert not notifier.send.called


def test_heartbeat_check_stale_alerts_and_returns_one(tmp_path):
    from check_heartbeat import check
    store = JsonFileStateStore(tmp_path / "store.json")
    store.put_state("adam", "SLR", {
        "state": "ARMED_SELL", "last_heartbeat_ts": time.time() - 3600,  # 1h old
    })
    notifier = MagicMock()
    rc = check(store, "adam", "SLR", stale_seconds=120, notifier=notifier)
    assert rc == 1
    notifier.send.assert_called_once()
    assert notifier.send.call_args[0][2] == Priority.CRIT


def test_heartbeat_check_missing_state_returns_two(tmp_path):
    from check_heartbeat import check
    store = JsonFileStateStore(tmp_path / "store.json")
    notifier = MagicMock()
    rc = check(store, "adam", "SLR", stale_seconds=120, notifier=notifier)
    assert rc == 2
    notifier.send.assert_called_once()  # warned about missing state
