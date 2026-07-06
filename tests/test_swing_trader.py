"""Tests for the retrofitted SwingTrader using PaperBroker as the injected broker.

Covers: state persistence via StateStore, kill-switch pause, config refresh mid-run,
reconcile-on-startup, floor guard, fee gate, full sell-then-buy cycle, scale-up.
"""

import pytest

from paper_broker import PaperBroker, PaperConfig
from safety import KillSwitch, TradeLog, reconcile as safety_reconcile
from state_store import JsonFileStateStore
from swing_leg import State, SwingConfig, SwingTrader


TENANT = "adam"
SYMBOL = "SLR-27AUG26-CDE"

# Empirical config
def make_paper_broker(starting_balance=100_000.0):
    return PaperBroker(PaperConfig(
        product_id=SYMBOL,
        contract_size=50.0,
        tick_size=0.005,
        fee_per_fill=2.34,
        margin_per_contract=275.0,
        starting_balance=starting_balance,
    ))


def preload_position(broker: PaperBroker, qty: int, entry: float) -> None:
    """Fast-forward the paper broker to a specific existing position."""
    broker.place_limit("BUY", qty, entry)
    broker.tick(entry, entry)


def default_config_dict():
    return {
        "core_qty": 10,
        "swing_qty": 2,
        "max_swing_qty": 5,
        "sell_px": 65.0,
        "buy_px": 63.0,
        "contract_size": 50,
        "margin_per_contract": 275.0,
        "scale_up_buffer_mult": 1.5,
        "fee_per_contract_roundtrip": 4.68,
        "abort_below": 60.0,
        "abort_above": 70.0,
        "fee_sanity_multiplier": 2.0,
    }


def make_trader(tmp_path, broker=None, config=None, with_log=True, with_ks=True):
    store = JsonFileStateStore(tmp_path / "store.json")
    store.put_config(TENANT, SYMBOL, config or default_config_dict())
    if broker is None:
        broker = make_paper_broker()
        preload_position(broker, 12, 58.44)
    log = TradeLog(tmp_path / "trades.jsonl") if with_log else None
    ks = KillSwitch(store, TENANT) if with_ks else None
    return SwingTrader(broker, store, TENANT, SYMBOL, trade_log=log, kill_switch=ks), broker, log, ks, store


# ---- config / state loading -------------------------------------------------


def test_loads_config_from_store(tmp_path):
    store = JsonFileStateStore(tmp_path / "store.json")
    store.put_config(TENANT, SYMBOL, {"core_qty": 8, "swing_qty": 3, "sell_px": 70.0, "buy_px": 68.0})
    broker = make_paper_broker()
    preload_position(broker, 11, 65.0)
    trader = SwingTrader(broker, store, TENANT, SYMBOL)
    assert trader.cfg.core_qty == 8
    assert trader.cfg.swing_qty == 3
    assert trader.cfg.sell_px == 70.0


def test_defaults_when_no_config(tmp_path):
    store = JsonFileStateStore(tmp_path / "store.json")
    broker = make_paper_broker()
    preload_position(broker, 12, 58.44)
    trader = SwingTrader(broker, store, TENANT, SYMBOL)
    assert trader.cfg.core_qty == 10
    assert trader.cfg.margin_per_contract == 275.0  # empirical default, not 1000.0


def test_state_persists_across_instances(tmp_path):
    trader1, broker, _, _, store = make_trader(tmp_path)
    trader1.reconcile()
    trader1.step(63.5)
    # New instance, same store
    trader2 = SwingTrader(broker, store, TENANT, SYMBOL)
    assert trader2.s.state == trader1.s.state
    assert trader2.s.live_order_id == trader1.s.live_order_id


# ---- reconcile --------------------------------------------------------------


def test_reconcile_ok_when_at_or_above_core(tmp_path):
    trader, broker, log, _, _ = make_trader(tmp_path)  # position starts at 12, core is 10
    trader.reconcile()
    assert trader.s.state == State.ARMED_SELL
    events = list(log.events())
    assert any(e["event_type"] == "reconciled" for e in events)


def test_reconcile_halts_when_below_core(tmp_path):
    broker = make_paper_broker()
    preload_position(broker, 8, 58.44)  # below core of 10
    trader, _, log, _, _ = make_trader(tmp_path, broker=broker)
    trader.reconcile()
    assert trader.s.state == State.HALTED
    events = list(log.events())
    assert any(e["event_type"] == "reconcile_halt" for e in events)


def test_reconcile_clears_stale_order_id(tmp_path):
    """If we thought an order was live but it's actually gone, clear the tracker."""
    trader, broker, _, _, _ = make_trader(tmp_path)
    trader.s.live_order_id = "ghost-order-id"
    trader.reconcile()
    # PaperBroker treats unknown IDs as UNKNOWN -> cleared
    assert trader.s.live_order_id is None


# ---- floor guard ------------------------------------------------------------


def test_arm_sell_blocked_by_floor(tmp_path):
    """If a sell would breach the core, HALT instead."""
    broker = make_paper_broker()
    preload_position(broker, 10, 58.44)  # exactly at core; sell would breach
    # core_qty=10, swing_qty=2 by default → 10 - 2 = 8 < core 10 → HALT
    trader, _, log, _, _ = make_trader(tmp_path, broker=broker)
    trader.reconcile()
    trader.step(63.5)
    assert trader.s.state == State.HALTED


# ---- kill switch ------------------------------------------------------------


def test_kill_switch_pauses_without_halting(tmp_path):
    trader, _, log, ks, _ = make_trader(tmp_path)
    trader.reconcile()
    ks.activate("pausing for testing")
    trader.step(63.5)
    # No arming should have happened
    assert trader.s.live_order_id is None
    # And state stays ARMED_SELL — kill switch is a pause, not a halt
    assert trader.s.state == State.ARMED_SELL
    events = list(log.events())
    assert any(e["event_type"] == "kill_switch_pause" for e in events)


def test_kill_switch_cleared_resumes(tmp_path):
    trader, _, _, ks, _ = make_trader(tmp_path)
    trader.reconcile()
    ks.activate("test pause")
    trader.step(63.5)
    assert trader.s.live_order_id is None
    ks.clear(cleared_by="test")
    trader.step(63.5)
    # Now should arm a sell
    assert trader.s.live_order_id is not None


# ---- abort guards -----------------------------------------------------------


def test_abort_above_halts_when_price_runs():
    """If ARMED_SELL and price ran past abort_above, HALT — don't chase."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        from pathlib import Path
        trader, _, _, _, _ = make_trader(Path(td))
        trader.reconcile()
        trader.step(75.0)  # abort_above is 70
        assert trader.s.state == State.HALTED


def test_abort_below_halts_when_price_craters(tmp_path):
    trader, _, _, _, _ = make_trader(tmp_path)
    trader.reconcile()
    # Force into ARMED_BUY manually
    trader.s.state = State.ARMED_BUY
    trader.step(55.0)  # abort_below is 60
    assert trader.s.state == State.HALTED


# ---- full swing cycle -------------------------------------------------------


def test_sell_fill_transitions_to_buy_leg(tmp_path):
    trader, broker, _, _, _ = make_trader(tmp_path)
    trader.reconcile()
    trader.step(63.5)  # arms SELL at 65
    assert trader.s.state == State.ARMED_SELL
    assert trader.s.live_order_id is not None

    broker.tick(65.0, 65.01)  # cross the sell limit → fill
    trader.step(63.5)          # step sees FILLED, transitions

    assert trader.s.state == State.ARMED_BUY
    assert trader.s.last_sell_qty == 2


def test_full_cycle_realizes_pnl_and_counts(tmp_path):
    trader, broker, log, _, _ = make_trader(tmp_path)
    trader.reconcile()

    trader.step(63.5)               # arm SELL at 65
    broker.tick(65.0, 65.0)         # fill sell
    trader.step(63.5)               # process fill, transition to ARMED_BUY

    trader.step(63.5)               # arm BUY at 63
    broker.tick(62.99, 63.0)        # fill buy
    trader.step(63.0)               # process fill, cycle completes

    assert trader.s.state == State.ARMED_SELL
    assert trader.s.cycles == 1
    # Realized P&L per strategy math: (65 - 63) × 50 × 2 - $4.68 × 2 = 200 - 9.36 = $190.64
    assert trader.s.realized_pnl == pytest.approx(200.0 - 9.36)
    events = list(log.events())
    assert any(e["event_type"] == "cycle_completed" for e in events)


# ---- scale-up ---------------------------------------------------------------


def test_scale_up_when_profit_covers_margin(tmp_path):
    trader, broker, _, _, _ = make_trader(tmp_path)
    trader.reconcile()
    # Fake enough realized P&L to fund one contract's margin × 1.5 buffer = $412.50
    trader.s.realized_pnl = 500.0
    trader.s.state = State.ARMED_BUY  # scale-up happens on buy leg
    trader.step(63.5)
    assert trader.s.swing_qty == 3  # was 2 → 3


def test_no_scale_up_without_profit(tmp_path):
    trader, _, _, _, _ = make_trader(tmp_path)
    trader.reconcile()
    trader.s.state = State.ARMED_BUY
    trader.step(63.5)
    assert trader.s.swing_qty == 2  # unchanged


# ---- fee gate ---------------------------------------------------------------


def test_fee_gate_passes_at_normal_commission(tmp_path):
    """PaperBroker doesn't have preview_order, so the gate passes through — good default."""
    trader, _, _, _, _ = make_trader(tmp_path)
    trader.reconcile()
    trader.step(63.5)
    assert trader.s.live_order_id is not None


def test_fee_gate_halts_on_abnormal_commission(tmp_path):
    """Wire up a broker mock with an inflated preview and confirm the gate halts."""
    class FeeBlowoutBroker:
        def __init__(self, real_broker):
            self.real = real_broker
        def place_limit(self, side, qty, price):
            return self.real.place_limit(side, qty, price)
        def order_status(self, oid):
            return self.real.order_status(oid)
        def cancel(self, oid):
            self.real.cancel(oid)
        def position_qty(self):
            return self.real.position_qty()
        def preview_order(self, side, qty, price):
            # Expected per-side fee for 2 contracts = 2 × $2.34 = $4.68
            # Return 10× that to trigger the sanity ceiling (>2×)
            return {"commission_total": 46.80}

    real = make_paper_broker()
    preload_position(real, 12, 58.44)
    blowout = FeeBlowoutBroker(real)
    from state_store import JsonFileStateStore as S
    from safety import TradeLog as L, KillSwitch as K
    store = S(tmp_path / "store.json")
    store.put_config(TENANT, SYMBOL, default_config_dict())
    trader = SwingTrader(blowout, store, TENANT, SYMBOL, trade_log=L(tmp_path / "trades.jsonl"))
    trader.reconcile()
    trader.step(63.5)
    assert trader.s.state == State.HALTED


# ---- trade log --------------------------------------------------------------


def test_trade_log_captures_lifecycle(tmp_path):
    trader, broker, log, _, _ = make_trader(tmp_path)
    trader.reconcile()
    trader.step(63.5)
    broker.tick(65.0, 65.0); trader.step(63.5)
    events = [e["event_type"] for e in log.events()]
    assert "reconciled" in events
    assert "order_placed" in events
    assert "order_filled" in events


# ---- dashboard-style config refresh -----------------------------------------


def test_config_refresh_mid_run(tmp_path):
    """A dashboard write to config should take effect on the next step()."""
    trader, _, _, _, store = make_trader(tmp_path)
    trader.reconcile()
    assert trader.cfg.sell_px == 65.0
    # Dashboard changes the sell target
    cfg = default_config_dict()
    cfg["sell_px"] = 72.0
    store.put_config(TENANT, SYMBOL, cfg)
    trader.step(63.5)
    assert trader.cfg.sell_px == 72.0


# ---- safety.reconcile against SwingTrader's broker --------------------------


def test_safety_reconcile_works_against_paper_broker(tmp_path):
    """Belt-and-suspenders: the Reconciler helper works alongside SwingTrader."""
    trader, broker, _, _, _ = make_trader(tmp_path)
    trader.reconcile()
    result = safety_reconcile(broker, believed_position=12)
    assert result.ok
    bad = safety_reconcile(broker, believed_position=99)
    assert not bad.ok
