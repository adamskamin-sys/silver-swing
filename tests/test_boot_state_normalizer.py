"""Tests for boot_state_normalizer — the 2026-07-14 SLR incident fix.

Verifies that:
  * No drift = no-op (safe default)
  * Drift + safe conditions = clamp + persist + HALT
  * Drift + unsafe conditions (mid-cycle, filled_qty>0) = refuse + notify
  * Clamped state persists to Redis via _save_state
  * Notifier + trade log fire on both clamp AND refuse paths
"""
from __future__ import annotations

import boot_state_normalizer as bsn
from sim_broker import SimBroker, SimConfig
from safety import TradeLog
from state_store import JsonFileStateStore
from swing_leg import SwingTrader, State


TENANT = "adam-test"
SYMBOL = "SLR-27AUG26-CDE"


class _MockNotifier:
    def __init__(self):
        self.sent = []

    def send(self, subject, body, priority):
        self.sent.append({"subject": subject, "body": body, "priority": priority})


def _make_trader(tmp_path, cfg_swing_qty=0, state_swing_qty=0,
                 state_val=State.ARMED_BUY, filled_qty=0, live_order_id=None):
    store = JsonFileStateStore(tmp_path / "store.json")
    store.put_config(TENANT, SYMBOL, {
        "core_qty": 0, "swing_qty": cfg_swing_qty, "max_swing_qty": 5,
        "sell_px": 65.0, "buy_px": 63.0,
        "contract_size": 50, "tick_size": 0.005,
        "margin_per_contract": 275.0,
        "scale_up_buffer_mult": 1.5,
        "fee_per_contract_roundtrip": 4.68,
        "abort_below": 60.0, "abort_above": 70.0,
        "fee_sanity_multiplier": 2.0,
        "sleeves": [],
    })
    # Pre-seed state with the drift scenario
    store.put_state(TENANT, SYMBOL, {
        "state": state_val.value,
        "swing_qty": state_swing_qty,
        "filled_qty": filled_qty,
        "live_order_id": live_order_id,
        "last_sell_qty": 0, "last_sell_fill_price": None,
        "realized_pnl": 0.0, "reserved_margin": 0.0,
        "cycles": 0, "last_heartbeat_ts": 0.0,
        "trail_armed": False, "trail_high_water_price": 0.0,
        "sleeves": {},
    })
    broker = SimBroker(SimConfig(
        product_id=SYMBOL, contract_size=50.0, tick_size=0.005,
        fee_per_fill=2.34, margin_per_contract=275.0,
        starting_balance=100_000.0))
    log = TradeLog(tmp_path / "trades.jsonl")
    trader = SwingTrader(broker, store, TENANT, SYMBOL, trade_log=log)
    return trader, store, log


# ---- No-drift cases ------------------------------------------------------

def test_no_drift_returns_noop(tmp_path):
    trader, store, log = _make_trader(tmp_path, cfg_swing_qty=2, state_swing_qty=2)
    result = bsn.normalize_primary_swing_qty(trader, log=log, notifier=_MockNotifier())
    assert result["drifted"] is False
    assert result["clamped"] is False
    assert "no drift" in result["reason"]


def test_state_below_config_no_op(tmp_path):
    """state.swing_qty < config.swing_qty is FINE (bot hasn't scaled up yet)."""
    trader, store, log = _make_trader(tmp_path, cfg_swing_qty=5, state_swing_qty=2)
    result = bsn.normalize_primary_swing_qty(trader)
    assert result["drifted"] is False
    assert result["clamped"] is False


# ---- Drift + safe conditions = clamp -------------------------------------

def test_drift_clamped_when_armed_buy_and_no_fill(tmp_path):
    """THE SLR CASE: config.swing_qty=0, state.swing_qty=2, state=ARMED_BUY,
    filled_qty=0 → safe to clamp."""
    trader, store, log = _make_trader(
        tmp_path, cfg_swing_qty=0, state_swing_qty=2,
        state_val=State.ARMED_BUY, filled_qty=0,
        live_order_id="stale-order-abc")
    notifier = _MockNotifier()
    result = bsn.normalize_primary_swing_qty(trader, log=log, notifier=notifier)

    assert result["drifted"] is True
    assert result["clamped"] is True
    assert result["from"] == 2
    assert result["to"] == 0
    # In-memory
    assert trader.s.swing_qty == 0
    assert trader.s.live_order_id is None
    assert trader.s.state == State.HALTED
    assert "boot-normalize" in (trader.s.halt_reason or "")
    # Redis persistence
    persisted = store.get_state(TENANT, SYMBOL)
    assert persisted["swing_qty"] == 0
    assert persisted["live_order_id"] is None
    assert persisted["state"] == "HALTED"
    # Notifier fired with CRIT
    assert len(notifier.sent) == 1
    from alerting import Priority
    assert notifier.sent[0]["priority"] == Priority.CRIT


def test_drift_clamped_when_halted_and_no_fill(tmp_path):
    """Drift with state=HALTED + filled_qty=0 is also safe to clamp."""
    trader, store, log = _make_trader(
        tmp_path, cfg_swing_qty=1, state_swing_qty=4,
        state_val=State.HALTED, filled_qty=0)
    result = bsn.normalize_primary_swing_qty(trader)
    assert result["clamped"] is True
    assert trader.s.swing_qty == 1


# ---- Drift + unsafe conditions = refuse ----------------------------------

def test_drift_refused_when_filled_qty_nonzero(tmp_path):
    """If state.filled_qty > 0, bot may have a real live position — DON'T
    clamp; that would erase evidence of a real fill."""
    trader, store, log = _make_trader(
        tmp_path, cfg_swing_qty=0, state_swing_qty=2,
        state_val=State.ARMED_BUY, filled_qty=2)
    notifier = _MockNotifier()
    result = bsn.normalize_primary_swing_qty(trader, log=log, notifier=notifier)

    assert result["drifted"] is True
    assert result["clamped"] is False
    assert "not safe" in result["reason"].lower() or "not clamping" in result["reason"].lower() or "manual review" in result["reason"].lower()
    # State untouched
    assert trader.s.swing_qty == 2
    # Still notified so operator sees the drift
    assert len(notifier.sent) == 1
    from alerting import Priority
    assert notifier.sent[0]["priority"] == Priority.CRIT


def test_drift_refused_when_state_is_armed_sell(tmp_path):
    """ARMED_SELL = mid-cycle sell; do NOT touch state.swing_qty here."""
    trader, store, log = _make_trader(
        tmp_path, cfg_swing_qty=0, state_swing_qty=2,
        state_val=State.ARMED_SELL, filled_qty=0)
    result = bsn.normalize_primary_swing_qty(trader)
    assert result["drifted"] is True
    assert result["clamped"] is False
    assert trader.s.swing_qty == 2  # untouched


# ---- Log emission --------------------------------------------------------

def test_clamp_emits_trade_log_event(tmp_path):
    trader, store, log = _make_trader(
        tmp_path, cfg_swing_qty=0, state_swing_qty=3,
        state_val=State.ARMED_BUY, filled_qty=0)
    bsn.normalize_primary_swing_qty(trader, log=log)
    events = list(log.events())
    kinds = [e.get("event_type") for e in events]
    assert "boot_state_normalize_clamped" in kinds


def test_refuse_emits_trade_log_event(tmp_path):
    trader, store, log = _make_trader(
        tmp_path, cfg_swing_qty=0, state_swing_qty=3,
        state_val=State.ARMED_SELL, filled_qty=0)
    bsn.normalize_primary_swing_qty(trader, log=log)
    events = list(log.events())
    kinds = [e.get("event_type") for e in events]
    assert "boot_state_normalize_refused" in kinds


# ---- Missing optional args (log/notifier) --------------------------------

def test_no_log_no_notifier_still_clamps(tmp_path):
    """Both log and notifier are optional — the fix must still apply."""
    trader, store, log = _make_trader(
        tmp_path, cfg_swing_qty=0, state_swing_qty=2,
        state_val=State.ARMED_BUY, filled_qty=0)
    result = bsn.normalize_primary_swing_qty(trader)
    assert result["clamped"] is True
    assert trader.s.swing_qty == 0
