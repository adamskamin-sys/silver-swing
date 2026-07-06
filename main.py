"""
main.py — bot entry point. Wires feed → broker → trader → loop.

Modes (via SWING_MODE env var):
  paper       (default) — LiveTickerFeed + PaperBroker. Real feed, simulated fills.
                          Safe to run: nothing reaches Coinbase's order path.
  backtest    — no feed; runs the backtest engine over a candle window.
  live        — LiveTickerFeed + CoinbaseBroker. REAL ORDERS. Only invoke
                deliberately; refuses to run without SWING_LIVE_CONFIRM=I_UNDERSTAND set.

Config comes from StateStore under (tenant_id, symbol). If none is present,
seeds a default block from broker.contract_spec() so the bot boots cleanly
on a fresh install without a dashboard.

Ctrl-C is handled cleanly — cancels open orders on the paper broker, closes
the WS feed, saves final state, records a shutdown event in the trade log.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv


TENANT = os.getenv("SWING_TENANT", "adam")
SYMBOL = os.getenv("SWING_SYMBOL", "SLR-27AUG26-CDE")
DATA_DIR = os.getenv("SWING_DATA_DIR", "data")
LOOP_INTERVAL_SECS = float(os.getenv("SWING_LOOP_INTERVAL", "1.0"))
FEED_READY_TIMEOUT = float(os.getenv("SWING_FEED_TIMEOUT", "15.0"))


def _default_paper_config():
    """Empirical SLR-27AUG26-CDE values (spec §3A). Used when the store has no config."""
    return {
        "core_qty": 10, "swing_qty": 2, "max_swing_qty": 5,
        "sell_px": 65.0, "buy_px": 63.0, "contract_size": 50,
        "margin_per_contract": 275.0, "scale_up_buffer_mult": 1.5,
        "fee_per_contract_roundtrip": 4.68,
        "abort_below": 60.0, "abort_above": 70.0,
        "fee_sanity_multiplier": 2.0,
    }


def _seed_config_if_missing(store, tenant: str, symbol: str) -> None:
    if store.get_config(tenant, symbol):
        return
    store.put_config(tenant, symbol, _default_paper_config())


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def _mirror_live_position_into_paper(paper, product_id: str) -> bool:
    """Query real Coinbase (read-only) and preload the paper broker with the
    same position at the same avg entry. Returns True if a position was
    mirrored, False if flat or query failed. Paper starts flat on any error.
    """
    try:
        from broker import BrokerConfig, CoinbaseBroker
        live = CoinbaseBroker(BrokerConfig(product_id=product_id))
        qty = live.position_qty()
        if qty == 0:
            _log("live position: flat. paper starts flat.")
            return False
        resp = live.client.list_futures_positions()
        positions = (resp.to_dict() if hasattr(resp, "to_dict") else resp).get("positions") or []
        avg_entry = None
        for p in positions:
            if p.get("product_id") == product_id:
                avg_entry = float(p.get("avg_entry_price") or 0)
                break
        if not avg_entry or avg_entry <= 0:
            _log(f"live position {qty} exists but avg_entry unavailable — paper starts flat")
            return False
        _log(f"mirroring live position into paper: {qty} @ ${avg_entry:.4f}")
        paper.set_pending_source("mirror")
        paper.place_limit("BUY" if qty > 0 else "SELL", abs(qty), avg_entry)
        paper.tick(avg_entry, avg_entry)
        return True
    except Exception as e:
        _log(f"could not query live position ({type(e).__name__}: {e}) — paper starts flat")
        return False


def run_paper_mode() -> int:
    """Live feed → PaperBroker → SwingTrader. Real market prices, simulated fills.
    Safe: no path to Coinbase's order endpoint."""
    from feed import LiveTickerFeed
    from paper_broker import PaperBroker, PaperConfig
    from safety import KillSwitch, TradeLog
    from state_store import JsonFileStateStore
    from swing_leg import SwingTrader

    _log(f"paper mode: symbol={SYMBOL}, tenant={TENANT}")

    store = JsonFileStateStore(f"{DATA_DIR}/store.json")
    log = TradeLog(f"{DATA_DIR}/trades.jsonl")
    ks = KillSwitch(store, TENANT)
    _seed_config_if_missing(store, TENANT, SYMBOL)

    # Paper account balance from env or default
    starting_balance = float(os.getenv("SWING_PAPER_BALANCE", "100000.0"))
    paper = PaperBroker(PaperConfig(
        product_id=SYMBOL,
        contract_size=50.0,
        tick_size=0.005,
        fee_per_fill=2.34,
        margin_per_contract=275.0,
        starting_balance=starting_balance,
    ))
    _log(f"paper broker seeded: balance=${starting_balance:,.2f}")

    # Default: paper is fully isolated (no mirror) so a fresh run gives a fresh
    # sandbox — no real-account cost basis leaks in. Set SWING_PAPER_MIRROR_LIVE=1
    # to explicitly opt in (useful for "practice managing my current position").
    if os.getenv("SWING_PAPER_MIRROR_LIVE", "0") == "1":
        _mirror_live_position_into_paper(paper, SYMBOL)
    else:
        _log("paper starts flat (mirror disabled — set SWING_PAPER_MIRROR_LIVE=1 to enable)")

    trader = SwingTrader(paper, store, TENANT, SYMBOL, trade_log=log, kill_switch=ks)

    _log(f"connecting to WS feed (waiting up to {FEED_READY_TIMEOUT}s for first tick)...")
    feed = LiveTickerFeed(SYMBOL)
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        _log("SIGINT received — shutting down")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        feed.start()
        if not feed.wait_for_first_tick(timeout=FEED_READY_TIMEOUT):
            _log("no ticks received within timeout — check the WS or product_id")
            return 1
        _log("feed live — starting main loop")
        log.record("bot_started", mode="paper", tenant=TENANT, symbol=SYMBOL,
                   starting_balance=starting_balance)
        trader.reconcile()

        snapshot_interval = float(os.getenv("SWING_SNAPSHOT_INTERVAL", "5.0"))
        last_snapshot = 0.0
        while not stopping:
            t = feed.latest_ticker()
            if t is None:
                time.sleep(0.1)
                continue
            paper.tick(t["best_bid"], t["best_ask"])
            # Forward exchange-provided 24h high/low if the feed shipped them.
            set_range = getattr(paper, "set_external_day_range", None)
            if callable(set_range):
                set_range(t.get("high_24h"), t.get("low_24h"))
            trader.step(t["price"])
            now = time.time()
            if now - last_snapshot >= snapshot_interval:
                snap = paper.snapshot()
                snap["mode"] = "paper"
                snap["product_id"] = SYMBOL
                snap["best_bid"] = t["best_bid"]
                snap["best_ask"] = t["best_ask"]
                snap["generated_at"] = now
                store.put_snapshot(TENANT, SYMBOL, snap)
                last_snapshot = now
            time.sleep(LOOP_INTERVAL_SECS)

    finally:
        feed.stop()
        log.record("bot_stopped", mode="paper", final_snapshot=paper.snapshot())
        _log(f"final paper snapshot: {paper.snapshot()}")
    return 0


def run_backtest_mode() -> int:
    """Backtest over a fixed window. Configure via SWING_BACKTEST_DAYS."""
    from backtest import fetch_candles, run_backtest
    from broker import CoinbaseBroker, BrokerConfig
    from paper_broker import PaperConfig
    from safety import TradeLog
    from state_store import JsonFileStateStore
    from swing_leg import SwingTrader

    from datetime import timedelta

    days = int(os.getenv("SWING_BACKTEST_DAYS", "7"))
    granularity = os.getenv("SWING_BACKTEST_GRAN", "FIVE_MINUTE")
    _log(f"backtest mode: {days}d @ {granularity}, symbol={SYMBOL}")

    coinbase = CoinbaseBroker(BrokerConfig(product_id=SYMBOL))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    _log(f"fetching candles {start.isoformat()} → {end.isoformat()}...")
    candles = fetch_candles(coinbase.client, SYMBOL, start, end, granularity=granularity)
    _log(f"loaded {len(candles)} candles")

    store = JsonFileStateStore(f"{DATA_DIR}/backtest_store.json")
    log = TradeLog(f"{DATA_DIR}/backtest_trades.jsonl")
    _seed_config_if_missing(store, TENANT, SYMBOL)

    def factory(broker):
        return SwingTrader(broker, store, TENANT, SYMBOL, trade_log=log)

    starting_balance = float(os.getenv("SWING_PAPER_BALANCE", "100000.0"))
    result = run_backtest(factory, PaperConfig(
        product_id=SYMBOL, contract_size=50.0, tick_size=0.005,
        fee_per_fill=2.34, margin_per_contract=275.0,
        starting_balance=starting_balance,
    ), candles)
    _log(result.summary())
    return 0


def run_live_mode() -> int:
    """Delegate to live_runner.py — the safety-gated real-money entry point.

    live_runner enforces preflight (broker health, product session, config
    validation, kill switch, roll check, position vs floor) and refuses to
    start on any failure. Requires SWING_LIVE_DRY_RUN=1 (fake orders) or
    SWING_LIVE_CONFIRM=I_UNDERSTAND (real orders) to run at all.
    """
    from live_runner import run as run_live
    return run_live()


def main() -> int:
    load_dotenv()
    mode = os.getenv("SWING_MODE", "paper").lower()
    if mode == "paper":
        return run_paper_mode()
    if mode == "backtest":
        return run_backtest_mode()
    if mode == "live":
        return run_live_mode()
    _log(f"unknown SWING_MODE={mode!r}. valid: paper | backtest | live")
    return 2


if __name__ == "__main__":
    sys.exit(main())
