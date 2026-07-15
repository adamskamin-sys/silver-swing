"""Regression tests for the 2026-07-15 HYPE stuck-state bug.

Root cause: when the broker returns a non-FILLED status (CANCELLED/EXPIRED/
UNKNOWN) but reports `filled_qty > 0`, the sleeve's tick loop and reconcile
both cleared live_order_id WITHOUT crediting the fill via _sleeve_on_fill.
The sleeve stayed in the pre-fill state (WAITING_FOR_BUY) forever while
Coinbase held the position — same bug family as 2026-07-12 reconcile-fill.

Fix (swing_leg.py): before clearing live_order_id in the CANCELLED/EXPIRED/
UNKNOWN branch, if filled > 0, credit the fill first via _sleeve_on_fill.

These tests verify:
  1. Tick loop credits a partial-fill BUY before clearing (state advances)
  2. Reconcile credits a partial-fill BUY before clearing on startup
  3. Zero-fill CANCELLED still clears cleanly (regression protection)
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


def test_tick_loop_credits_partial_fill_before_clear(tmp_path):
    """CANCELLED with filled_qty > 0 → sleeve MUST credit the fill before clearing."""
    trader, broker, _ = _make_trader(tmp_path)
    scs = trader._load_sleeves_cfg()
    sc = scs[0]
    ss = trader.s.sleeves[sc.id]

    # Arm the sleeve as if it placed a buy order
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "test-order-abc"

    # Mock the broker to return CANCELLED but with filled_qty=1
    # (Coinbase misreport: the fill actually happened but status is stale/wrong)
    broker.order_status = MagicMock(return_value={
        "status": "CANCELLED",
        "filled_qty": 1,
        "average_filled_price": 63.50,
    })

    # Verify _sleeve_on_fill gets called with the actual fill price
    on_fill = MagicMock()
    trader._sleeve_on_fill = on_fill

    trader._sleeve_step(sc, ss, last_price=63.50)

    # ASSERT: the fill was credited BEFORE live_order_id was cleared
    on_fill.assert_called_once()
    assert on_fill.call_args[0][2] == 63.50  # fill_price passed through
    # live_order_id cleared after credit
    assert ss.live_order_id is None
    # filled_qty reset to 0 after credit + clear
    assert ss.filled_qty == 0


def test_tick_loop_unknown_with_partial_fill_credits(tmp_path):
    """Same for UNKNOWN status — Coinbase transient error shouldn't lose the fill."""
    trader, broker, _ = _make_trader(tmp_path)
    scs = trader._load_sleeves_cfg()
    sc = scs[0]
    ss = trader.s.sleeves[sc.id]
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "test-order-def"

    broker.order_status = MagicMock(return_value={
        "status": "UNKNOWN",
        "filled_qty": 1,
        "average_filled_price": 63.75,
    })

    on_fill = MagicMock()
    trader._sleeve_on_fill = on_fill
    trader._sleeve_step(sc, ss, last_price=63.75)

    on_fill.assert_called_once()
    assert ss.live_order_id is None


def test_tick_loop_cancelled_zero_fill_still_clears(tmp_path):
    """Regression: CANCELLED with filled_qty=0 must still clear the id (no crediting)."""
    trader, broker, _ = _make_trader(tmp_path)
    scs = trader._load_sleeves_cfg()
    sc = scs[0]
    ss = trader.s.sleeves[sc.id]
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "test-order-ghi"

    broker.order_status = MagicMock(return_value={
        "status": "CANCELLED",
        "filled_qty": 0,
        "average_filled_price": None,
    })

    on_fill = MagicMock()
    trader._sleeve_on_fill = on_fill
    trader._sleeve_step(sc, ss, last_price=63.50)

    # No fill to credit — just clear
    on_fill.assert_not_called()
    assert ss.live_order_id is None
    assert ss.filled_qty == 0


def test_reconcile_credits_partial_fill_before_clear(tmp_path):
    """Startup reconcile: same partial-fill safety as the tick loop."""
    trader, broker, _ = _make_trader(tmp_path)
    scs = trader._load_sleeves_cfg()
    sc = scs[0]
    ss = trader.s.sleeves[sc.id]

    # Simulate bot restart: sleeve had a live_order_id from before shutdown
    ss.state = SleeveStateEnum.ARMED_BUY
    ss.live_order_id = "restart-order-jkl"

    # Broker reports CANCELLED with a filled_qty — the exchange filled it
    # while we were down, then some sweep marked it CANCELLED.
    broker.order_status = MagicMock(return_value={
        "status": "CANCELLED",
        "filled_qty": 1,
        "average_filled_price": 63.20,
    })
    # position_qty returns >= core so reconcile doesn't halt
    broker.position_qty = MagicMock(return_value=0)

    on_fill = MagicMock()
    trader._sleeve_on_fill = on_fill

    trader.reconcile()

    on_fill.assert_called_once()
    assert on_fill.call_args[0][2] == 63.20
    assert ss.live_order_id is None
