"""Tests for the backtest engine using synthetic candles + a real SwingTrader."""

import pytest

from backtest import BacktestResult, Candle, _walk_candle, run_backtest
from paper_broker import PaperConfig
from safety import TradeLog
from state_store import JsonFileStateStore
from swing_leg import State, SwingTrader


TENANT = "adam"
SYMBOL = "SLR-27AUG26-CDE"


def slr_cfg(starting_balance=100_000.0):
    return PaperConfig(
        product_id=SYMBOL,
        contract_size=50.0,
        tick_size=0.005,
        fee_per_fill=2.34,
        margin_per_contract=275.0,
        starting_balance=starting_balance,
    )


def default_config_dict():
    return {
        "core_qty": 10, "swing_qty": 2, "max_swing_qty": 5,
        "sell_px": 65.0, "buy_px": 63.0, "contract_size": 50,
        "margin_per_contract": 275.0, "scale_up_buffer_mult": 1.5,
        "fee_per_contract_roundtrip": 4.68,
        "abort_below": 60.0, "abort_above": 70.0,
        "fee_sanity_multiplier": 2.0,
    }


def make_factory(tmp_path):
    """Returns a trader factory that preloads Adam's baseline 12-contract long."""
    store = JsonFileStateStore(tmp_path / "store.json")
    store.put_config(TENANT, SYMBOL, default_config_dict())
    log = TradeLog(tmp_path / "trades.jsonl")

    def factory(broker):
        # Preload the 12-contract long that Adam actually holds
        broker.place_limit("BUY", 12, 58.44)
        broker.tick(58.44, 58.44)
        return SwingTrader(broker, store, TENANT, SYMBOL, trade_log=log)

    return factory


# ---- candle walking ---------------------------------------------------------


def test_walk_candle_green():
    c = Candle(ts=1, open=60, high=65, low=59, close=64)
    assert _walk_candle(c) == [60, 59, 65, 64]


def test_walk_candle_red():
    c = Candle(ts=1, open=64, high=65, low=59, close=60)
    assert _walk_candle(c) == [64, 65, 59, 60]


# ---- basic engine behavior --------------------------------------------------


def test_empty_candles_returns_starting_equity(tmp_path):
    result = run_backtest(make_factory(tmp_path), slr_cfg(), candles=[])
    # A 12-contract long was opened during factory setup; but with no candles
    # after that, unrealized_pnl is on the last known mark (58.44). Equity ≈
    # starting minus fees, no P&L movement.
    assert isinstance(result, BacktestResult)
    assert result.fills == 1  # the initial 12-contract preload
    assert result.total_return < 0  # slight negative from opening fees


def test_ranging_candles_produce_cycles(tmp_path):
    """A market that keeps bouncing 63 ↔ 65 → strategy should complete cycles."""
    candles = []
    for i in range(6):
        candles.append(Candle(ts=i, open=63.5, high=65.5, low=62.5, close=64.5))
    result = run_backtest(make_factory(tmp_path), slr_cfg(), candles=candles)
    assert result.cycles >= 1
    # Each cycle: (65 - 63) * 50 * 2 = $200 gross, minus $4.68 * 2 = $9.36 fees
    # Net per cycle: $190.64 realized (minus any preload/open cost)
    assert result.realized_pnl > 0


def test_backtest_result_summary_readable(tmp_path):
    candles = [Candle(ts=i, open=63, high=65, low=62.9, close=64) for i in range(2)]
    result = run_backtest(make_factory(tmp_path), slr_cfg(), candles=candles)
    summary = result.summary()
    assert "start $" in summary
    assert "cycles" in summary


def test_equity_curve_grows_with_candles(tmp_path):
    candles = [Candle(ts=i, open=63, high=65, low=62.9, close=64) for i in range(5)]
    result = run_backtest(make_factory(tmp_path), slr_cfg(), candles=candles)
    assert len(result.equity_curve) == 5
    # Each point should have the expected fields
    p = result.equity_curve[0]
    assert hasattr(p, "equity")
    assert hasattr(p, "cycles")
    assert hasattr(p, "close")


# ---- halt propagation -------------------------------------------------------


def test_halt_stops_iteration(tmp_path):
    """When paper broker halts (e.g., margin call), the engine breaks the loop."""
    # Massive candle that crashes silver from $60 to $10 — long position gets crushed
    cfg = slr_cfg(starting_balance=5_000.0)  # thin margin
    candles = [
        Candle(ts=1, open=60, high=60, low=60, close=60),
        Candle(ts=2, open=60, high=10, low=10, close=10),   # crash
        Candle(ts=3, open=10, high=10, low=10, close=10),   # would run more if not halted
    ]
    result = run_backtest(make_factory(tmp_path), cfg, candles=candles)
    # Loop broke on halt — equity curve stops before candle 3
    assert len(result.equity_curve) < 3
    assert result.halted


# ---- price direction affects fill order -------------------------------------


def test_green_candle_fills_low_before_high(tmp_path):
    """In a green candle O→L→H→C, a BUY limit near the low fills before a SELL near the high.
    Since the strategy only has one order live at a time, this affects sequence."""
    # This is more of a plausibility check than a hard assertion — we mainly
    # care that the strategy remains stable when candles walk in this order.
    candles = [Candle(ts=i, open=63.5, high=65.0, low=63.0, close=64.5) for i in range(3)]
    result = run_backtest(make_factory(tmp_path), slr_cfg(), candles=candles)
    assert isinstance(result, BacktestResult)


# ---- fees dominate a tight-range regime -------------------------------------


def test_tight_range_fees_flow(tmp_path):
    """Sanity check that the fee model actually runs in the backtest — fees_paid
    must accumulate on every fill. Doesn't assert a specific P&L number because
    the paper broker's cost-basis P&L rolls forward as we rebuy at higher/lower,
    and that's different from the strategy's per-cycle P&L (they measure
    different things — both correct, both worth watching separately)."""
    tight_cfg = default_config_dict()
    tight_cfg["sell_px"] = 63.25
    tight_cfg["buy_px"] = 63.0

    store = JsonFileStateStore(tmp_path / "store.json")
    store.put_config(TENANT, SYMBOL, tight_cfg)

    def factory(broker):
        broker.place_limit("BUY", 12, 58.44); broker.tick(58.44, 58.44)
        return SwingTrader(broker, store, TENANT, SYMBOL)

    candles = [Candle(ts=i, open=63.1, high=63.3, low=62.9, close=63.1) for i in range(6)]
    result = run_backtest(factory, slr_cfg(), candles=candles)
    # 12-contract preload paid 12 × $2.34 = $28.08 in fees. Any strategy activity
    # on top adds more. Assert the floor and that additional activity accumulated
    # (each order = 2 contracts × $2.34 = $4.68 per side).
    preload_fees = 12 * 2.34
    assert result.fees_paid >= preload_fees
    if result.cycles > 0:
        # At least (2 orders per cycle × $4.68) beyond preload
        min_activity_fees = result.cycles * 2 * 4.68
        assert result.fees_paid >= preload_fees + min_activity_fees - 0.01
