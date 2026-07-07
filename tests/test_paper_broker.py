"""Tests for PaperBroker. Uses the empirical SLR-27AUG26-CDE numbers as the default cost model."""

import pytest

from paper_broker import PaperBroker, PaperConfig


# Empirical SLR-27AUG26-CDE spec, from scratch/verify_access.py runs on 2026-07-06
SLR_CFG = PaperConfig(
    product_id="SLR-27AUG26-CDE",
    contract_size=50.0,
    tick_size=0.005,
    fee_per_fill=2.34,
    margin_per_contract=275.0,
    starting_balance=10_000.0,
)


# ---- Order lifecycle --------------------------------------------------------


def test_place_limit_registers_open_order():
    b = PaperBroker(SLR_CFG)
    oid = b.place_limit("SELL", 2, 65.0)
    assert oid.startswith("paper-")
    assert oid in b.open_orders
    assert b.order_status(oid)["status"] == "OPEN"


def test_place_limit_bad_side_raises():
    b = PaperBroker(SLR_CFG)
    with pytest.raises(ValueError):
        b.place_limit("SHORT", 1, 65.0)


def test_cancel_is_idempotent():
    b = PaperBroker(SLR_CFG)
    oid = b.place_limit("SELL", 2, 65.0)
    b.cancel(oid)
    b.cancel(oid)  # should not raise
    assert b.order_status(oid)["status"] == "CANCELLED"


def test_order_status_unknown_id():
    b = PaperBroker(SLR_CFG)
    assert b.order_status("nope")["status"] == "UNKNOWN"


# ---- Fill simulation --------------------------------------------------------


def test_sell_limit_fills_when_bid_crosses():
    b = PaperBroker(SLR_CFG)
    oid = b.place_limit("SELL", 2, 65.0)
    b.tick(best_bid=64.99, best_ask=65.00)  # bid not yet at 65 → no fill
    assert b.order_status(oid)["status"] == "OPEN"
    b.tick(best_bid=65.00, best_ask=65.01)  # bid touches limit → fills
    assert b.order_status(oid)["status"] == "FILLED"
    assert b.order_status(oid)["filled_qty"] == 2


def test_buy_limit_fills_when_ask_crosses():
    b = PaperBroker(SLR_CFG)
    oid = b.place_limit("BUY", 2, 63.0)
    b.tick(best_bid=63.01, best_ask=63.02)  # no fill
    assert b.order_status(oid)["status"] == "OPEN"
    b.tick(best_bid=62.99, best_ask=63.00)  # ask at limit → fills
    assert b.order_status(oid)["status"] == "FILLED"


def test_tick_returns_fills_this_tick():
    b = PaperBroker(SLR_CFG)
    oid1 = b.place_limit("SELL", 1, 65.0)
    oid2 = b.place_limit("SELL", 1, 66.0)
    fills = b.tick(best_bid=65.5, best_ask=65.6)
    assert len(fills) == 1
    assert fills[0].order_id == oid1
    assert b.order_status(oid2)["status"] == "OPEN"


# ---- Position + realized P&L (LONG scenarios — matches Adam's actual strategy) ---


def test_buy_opens_long_position():
    b = PaperBroker(SLR_CFG)
    b.place_limit("BUY", 12, 58.44)
    b.tick(best_bid=58.44, best_ask=58.44)
    assert b.position_qty() == 12
    assert b.position.avg_entry == 58.44


def test_sell_closes_long_and_realizes_pnl():
    """Adam's swing: sold 2 at higher, rebuys 2 at lower, banks the diff."""
    b = PaperBroker(SLR_CFG)
    # Enter long 12 at $58.44 (matches Adam's actual position)
    b.place_limit("BUY", 12, 58.44)
    b.tick(best_bid=58.44, best_ask=58.44)
    # Sell 2 at $65 (the swing)
    b.place_limit("SELL", 2, 65.0)
    b.tick(best_bid=65.0, best_ask=65.01)
    # Realized P&L on the 2 sold: (65 - 58.44) * 50 * 2 = $656
    assert b.realized_pnl == pytest.approx(656.0)
    assert b.position_qty() == 10  # 12 - 2
    assert b.position.avg_entry == 58.44  # unchanged; still holding the original 10


def test_full_swing_cycle_returns_position_to_starting():
    """Sell 2 → rebuy 2 = position back to 12, realized P&L on the gap."""
    b = PaperBroker(SLR_CFG)
    b.place_limit("BUY", 12, 58.44); b.tick(58.44, 58.44)
    b.place_limit("SELL", 2, 65.0); b.tick(65.0, 65.01)
    b.place_limit("BUY", 2, 63.0); b.tick(62.99, 63.0)
    assert b.position_qty() == 12
    # First close: (65 - 58.44) * 50 * 2 = 656
    # Rebuy raises avg_entry: (10*58.44 + 2*63.0)/12 = 59.20
    assert b.position.avg_entry == pytest.approx((10 * 58.44 + 2 * 63.0) / 12)
    # Only the sell realized P&L; the rebuy re-adds inventory
    assert b.realized_pnl == pytest.approx(656.0)


# ---- Fees -------------------------------------------------------------------


def test_fee_is_deducted_per_fill():
    b = PaperBroker(SLR_CFG)
    b.place_limit("BUY", 2, 63.0); b.tick(63.0, 63.0)
    assert b.fees_paid == pytest.approx(2 * 2.34)
    assert b.balance == pytest.approx(10_000.0 - 4.68)


def test_full_cycle_fee_matches_spec_fee_math():
    """Round-trip on 2 contracts: $4.68 per contract = $9.36 total. Matches spec §5A math."""
    b = PaperBroker(SLR_CFG)
    b.place_limit("BUY", 12, 58.44); b.tick(58.44, 58.44)  # fees: 12 * 2.34
    b.place_limit("SELL", 2, 65.0); b.tick(65.0, 65.0)     # fees: 2 * 2.34
    b.place_limit("BUY", 2, 63.0); b.tick(63.0, 63.0)      # fees: 2 * 2.34
    # Total: 16 fills × $2.34
    assert b.fees_paid == pytest.approx(16 * 2.34)


# ---- Unrealized + equity ----------------------------------------------------


def test_unrealized_pnl_tracks_mark():
    b = PaperBroker(SLR_CFG)
    b.place_limit("BUY", 12, 58.44); b.tick(58.44, 58.44)
    b.tick(best_bid=62.79, best_ask=62.81)  # mark = 62.80
    # (62.80 - 58.44) * 50 * 12 = $2,616 — matches Adam's actual dashboard
    assert b.unrealized_pnl() == pytest.approx((62.80 - 58.44) * 50 * 12)


def test_equity_includes_realized_and_unrealized():
    b = PaperBroker(SLR_CFG)
    b.place_limit("BUY", 12, 58.44); b.tick(58.44, 58.44)
    b.tick(62.79, 62.81)
    # equity = balance (10k - fees) + realized (0 so far) + unrealized
    fees = 12 * 2.34
    unrealized = (62.80 - 58.44) * 50 * 12
    assert b.equity() == pytest.approx(10_000.0 - fees + 0 + unrealized)


# ---- Drawdown ---------------------------------------------------------------


def test_high_water_mark_and_drawdown_track_equity():
    b = PaperBroker(SLR_CFG)
    b.place_limit("BUY", 1, 60.0); b.tick(60.0, 60.0)
    b.tick(65.0, 65.0)  # equity way up
    high = b.high_water_mark
    b.tick(50.0, 50.0)  # equity crash
    assert b.max_drawdown > 0
    assert b.max_drawdown == pytest.approx(high - b.equity(), abs=1e-6)


# ---- Margin call ------------------------------------------------------------


def test_margin_call_halts_broker():
    # Tiny starting balance so any position triggers margin call
    cfg = PaperConfig(
        product_id="X",
        contract_size=50.0,
        tick_size=0.005,
        fee_per_fill=2.34,
        margin_per_contract=1000.0,
        starting_balance=100.0,  # not enough to hold 1 contract
    )
    b = PaperBroker(cfg)
    b.place_limit("BUY", 1, 60.0)
    b.tick(60.0, 60.0)
    assert b._halted
    assert "margin call" in b._halt_reason


def test_halted_broker_rejects_new_orders():
    cfg = PaperConfig(
        product_id="X", contract_size=50.0, tick_size=0.005,
        fee_per_fill=2.34, margin_per_contract=1000.0, starting_balance=100.0,
    )
    b = PaperBroker(cfg)
    b.place_limit("BUY", 1, 60.0); b.tick(60.0, 60.0)
    with pytest.raises(RuntimeError, match="halted"):
        b.place_limit("SELL", 1, 65.0)


def test_halt_cancels_open_orders():
    cfg = PaperConfig(
        product_id="X", contract_size=50.0, tick_size=0.005,
        fee_per_fill=2.34, margin_per_contract=1000.0, starting_balance=100.0,
    )
    b = PaperBroker(cfg)
    resting = b.place_limit("SELL", 1, 999.0)  # unfillable, just resting
    b.place_limit("BUY", 1, 60.0)               # this fill triggers margin call
    b.tick(60.0, 60.0)
    assert b._halted
    assert b.order_status(resting)["status"] == "CANCELLED"


# ---- Slippage ---------------------------------------------------------------


def test_slippage_costs_buyer_more():
    cfg = PaperConfig(
        product_id="X", contract_size=50.0, tick_size=0.005,
        fee_per_fill=2.34, margin_per_contract=275.0, starting_balance=10_000.0,
        slippage_ticks=2.0,
    )
    b = PaperBroker(cfg)
    oid = b.place_limit("BUY", 1, 63.0)
    b.tick(62.99, 63.00)
    fill_price = b.order_status(oid)["average_filled_price"]
    assert fill_price == pytest.approx(63.0 + 2 * 0.005)  # 2 ticks worse


def test_slippage_costs_seller_less():
    cfg = PaperConfig(
        product_id="X", contract_size=50.0, tick_size=0.005,
        fee_per_fill=2.34, margin_per_contract=275.0, starting_balance=10_000.0,
        slippage_ticks=2.0,
    )
    b = PaperBroker(cfg)
    oid = b.place_limit("SELL", 1, 65.0)
    b.tick(65.0, 65.01)
    fill_price = b.order_status(oid)["average_filled_price"]
    assert fill_price == pytest.approx(65.0 - 2 * 0.005)  # 2 ticks worse


# ---- Snapshot ---------------------------------------------------------------


def test_snapshot_contains_expected_fields():
    b = PaperBroker(SLR_CFG)
    b.place_limit("BUY", 2, 63.0); b.tick(63.0, 63.0)
    snap = b.snapshot()
    for key in (
        "starting_balance", "balance", "realized_pnl", "unrealized_pnl", "equity",
        "fees_paid", "position_qty", "position_avg_entry", "margin_used",
        "available_margin", "high_water_mark", "max_drawdown", "open_orders",
        "fills", "halted", "halt_reason", "last_mark",
    ):
        assert key in snap
    assert snap["position_qty"] == 2
    assert snap["fills"] == 1
    assert snap["fees_paid"] == pytest.approx(2 * 2.34)


def test_snapshot_available_margin_reflects_used():
    b = PaperBroker(SLR_CFG)
    b.place_limit("BUY", 2, 63.0); b.tick(63.0, 63.0)
    snap = b.snapshot()
    assert snap["margin_used"] == pytest.approx(2 * 275.0)
    assert snap["available_margin"] == pytest.approx(snap["equity"] - snap["margin_used"])


# ---- Lot consumption priority (multi-strategy attribution) ------------------


def _buy_tagged(b, qty, price, strategy_id):
    """Helper: place a BUY tagged with a strategy_id and fill it."""
    b.set_pending_source("strategy", strategy_id=strategy_id)
    b.place_limit("BUY", qty, price)
    b.tick(price, price)  # fills the limit


def test_untagged_sell_prefers_unassigned_lots_over_tagged():
    """A strategy consuming lots should NOT steal another strategy's tagged
    lots when unassigned lots are available. Regression: previously the
    fallback used global FIFO, letting a newer strategy silently drain an
    older strategy's cost basis."""
    b = PaperBroker(SLR_CFG)
    b.tick(60.0, 60.0)  # prime last_mark so BUY can fill
    # Untagged inherited lot first (older ts) at basis 60.0
    b.set_pending_source("manual")
    b.place_limit("BUY", 2, 60.0)
    b.tick(60.0, 60.0)
    # Strategy s1's own tagged lot at basis 61.0 (newer ts)
    _buy_tagged(b, 2, 61.0, "s1")
    assert b.position.qty == 4
    # Strategy s2 sells 2 — should consume the UNTAGGED lot (basis 60.0),
    # leaving s1's tagged 61.0 lot intact for its own future sell.
    b.set_pending_source("strategy", strategy_id="s2")
    b.place_limit("SELL", 2, 62.0)
    b.tick(62.0, 62.0)
    assert b.position.qty == 2
    remaining = b.lots
    assert len(remaining) == 1
    assert remaining[0].strategy_id == "s1"
    assert remaining[0].entry_price == pytest.approx(61.0)


def test_sell_still_falls_through_to_other_tagged_lots_as_last_resort():
    """If there are no untagged lots left, a sell should still be able to
    consume another strategy's tagged lots — the guard is priority, not a hard
    barrier. Otherwise a strategy could sell more than its own inventory when
    everything is tagged."""
    b = PaperBroker(SLR_CFG)
    b.tick(60.0, 60.0)
    _buy_tagged(b, 2, 60.0, "s1")
    _buy_tagged(b, 2, 61.0, "s2")
    # No untagged lots exist. s2 sells 3, forcing the last-resort branch to
    # consume 1 of s1's tagged lots after exhausting its own 2.
    b.set_pending_source("strategy", strategy_id="s2")
    b.place_limit("SELL", 3, 62.0)
    b.tick(62.0, 62.0)
    assert b.position.qty == 1
    assert b.lots[0].strategy_id == "s1"
