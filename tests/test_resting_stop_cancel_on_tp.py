"""Regression tests for the resting-stop cancel-on-TP fix (commit e3a258c + follow-up).

Scenario: a sleeve holds a position (ARMED_SELL) protected by a resting
stop-limit on Coinbase (resting_stop_oid). Its take-profit limit sell fills
first, flipping the sleeve to ARMED_BUY. The resting stop is now dangling —
if price recovers above stop_px it fills and creates an accidental short.

The reconcile sweeper is the ONLY hook that runs for such a product once its
position hits 0 and it stops being ticked (ZEC-style). These tests pin two
bugs problem-scout found in the original fix:

  Blocker A — the sweeper guarded on `str(ss.state) == "ARMED_BUY"`, but
    SleeveStateEnum is a (str, Enum), so `str(state)` is
    'SleeveStateEnum.ARMED_BUY' and the comparison was ALWAYS False. The
    cancel branch was dead code → the stop kept dangling. Fixed by comparing
    the enum directly.

  Blocker B — on a transient cancel failure the code nulled resting_stop_oid
    anyway, discarding the only handle to the still-OPEN order so no later
    reconcile could retry. Fixed by retaining the oid on cancel exception.
"""
from __future__ import annotations
from unittest.mock import MagicMock

import pytest

from safety import TradeLog
from sim_broker import SimBroker, SimConfig
from state_store import JsonFileStateStore
from swing_leg import SleeveStateEnum, SwingTrader


TENANT = "adam"
SYMBOL = "SLR-27AUG26-CDE"


def _cfg():
    return {
        "core_qty": 0, "swing_qty": 0, "max_swing_qty": 5,
        "sell_px": 65.0, "buy_px": 63.0,
        "contract_size": 50, "tick_size": 0.005,
        "margin_per_contract": 275.0,
        "scale_up_buffer_mult": 1.5,
        "fee_per_contract_roundtrip": 4.68,
        "abort_below": 60.0, "abort_above": 70.0,
        "fee_sanity_multiplier": 2.0,
        "sleeves": [{
            "id": "sleeve-1", "name": "TestSleeve",
            "qty": 1, "sell_px": 65.0, "buy_px": 63.0,
            "trail_distance": 0.2, "reanchor_threshold": 2.0,
        }],
    }


def _make_trader(tmp_path):
    store = JsonFileStateStore(tmp_path / "store.json")
    store.put_config(TENANT, SYMBOL, _cfg())
    broker = SimBroker(SimConfig(
        product_id=SYMBOL, contract_size=50.0, tick_size=0.005,
        fee_per_fill=2.34, margin_per_contract=275.0, starting_balance=100_000.0,
    ))
    log = TradeLog(tmp_path / "trades.jsonl")
    trader = SwingTrader(broker, store, TENANT, SYMBOL, trade_log=log)
    return trader, broker, log


def _arm_resting_stop(trader, state, oid="rest-stop-oid-1"):
    """Put the sleeve in `state` with an OPEN resting stop on the book."""
    sc = trader._load_sleeves_cfg()[0]
    ss = trader.s.sleeves[sc.id]
    ss.live_order_id = None  # so the earlier reconcile loop skips it
    ss.state = state
    ss.resting_stop_oid = oid
    ss.resting_stop_px = 62.0
    ss.resting_stop_stage = 1
    return sc, ss


def test_reconcile_cancels_dangling_stop_when_tp_beat_it(tmp_path):
    """ARMED_BUY + OPEN resting stop → cancel it and clear the handle.

    Blocker A regression: with the old `str(ss.state) == "ARMED_BUY"` guard
    this cancel never fired and the stop dangled.
    """
    trader, broker, _ = _make_trader(tmp_path)
    _, ss = _arm_resting_stop(trader, SleeveStateEnum.ARMED_BUY)

    broker.position_qty = MagicMock(return_value=0)  # don't halt
    broker.order_status = MagicMock(return_value={"status": "OPEN"})
    broker.cancel = MagicMock(return_value={"success": True})

    trader.reconcile()

    broker.cancel.assert_called_once_with("rest-stop-oid-1")
    assert ss.resting_stop_oid is None
    assert ss.resting_stop_px is None
    assert ss.resting_stop_stage is None


def test_reconcile_keeps_handle_when_cancel_fails(tmp_path):
    """Transient cancel failure → retain oid so a later reconcile retries.

    Blocker B regression: the old code nulled resting_stop_oid on the
    exception path, stranding the still-OPEN stop with no handle.
    """
    trader, broker, _ = _make_trader(tmp_path)
    _, ss = _arm_resting_stop(trader, SleeveStateEnum.ARMED_BUY)

    broker.position_qty = MagicMock(return_value=0)
    broker.order_status = MagicMock(return_value={"status": "OPEN"})
    broker.cancel = MagicMock(side_effect=RuntimeError("coinbase 500"))

    trader.reconcile()

    broker.cancel.assert_called_once_with("rest-stop-oid-1")
    # Handle MUST survive so the next reconcile can retry.
    assert ss.resting_stop_oid == "rest-stop-oid-1"
    assert ss.resting_stop_px == 62.0
    assert ss.resting_stop_stage == 1


def test_reconcile_retries_cancel_on_next_pass(tmp_path):
    """After a failed cancel, a subsequent reconcile retries and succeeds."""
    trader, broker, _ = _make_trader(tmp_path)
    _, ss = _arm_resting_stop(trader, SleeveStateEnum.ARMED_BUY)

    broker.position_qty = MagicMock(return_value=0)
    broker.order_status = MagicMock(return_value={"status": "OPEN"})
    broker.cancel = MagicMock(side_effect=RuntimeError("coinbase 500"))
    trader.reconcile()
    assert ss.resting_stop_oid == "rest-stop-oid-1"  # retained

    broker.cancel = MagicMock(return_value={"success": True})
    trader.reconcile()
    broker.cancel.assert_called_once_with("rest-stop-oid-1")
    assert ss.resting_stop_oid is None


def test_reconcile_does_not_cancel_stop_while_still_holding(tmp_path):
    """ARMED_SELL (still holding, stop legitimately protecting) → do NOT cancel."""
    trader, broker, _ = _make_trader(tmp_path)
    _, ss = _arm_resting_stop(trader, SleeveStateEnum.ARMED_SELL)

    broker.position_qty = MagicMock(return_value=0)
    broker.order_status = MagicMock(return_value={"status": "OPEN"})
    broker.cancel = MagicMock(return_value={"success": True})

    trader.reconcile()

    broker.cancel.assert_not_called()
    assert ss.resting_stop_oid == "rest-stop-oid-1"  # still resting, untouched
