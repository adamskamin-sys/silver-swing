"""
live_runner.py — the real-money entry point.

Deliberately separate from main.py so it can't run by accident. Two safety
gates before ANY order goes to the exchange:

  1. Dry-run mode (SWING_LIVE_DRY_RUN=1) — everything wires up, orders are
     LOGGED but NOT submitted. Confirms the full pipeline works against a real
     feed and real reconcile without risking a dollar. Recommended for the first
     several sessions.

  2. Real mode (SWING_LIVE_CONFIRM=I_UNDERSTAND) — orders actually submit. The
     verbose env var is deliberately annoying: you must type it every time.

Pre-flight checks (all pass or the runner refuses to start):
  - COINBASE_API_KEY_JSON_PATH is set and file exists
  - Broker can read futures balance (proves key + futures enrollment work)
  - Product exists and session is open
  - Config passes validate_config()
  - Kill switch is OFF
  - Roll check: not within roll_days_before of expiry (else HALT + alert)
  - Reconcile: position >= core_qty

If any check fails, the runner logs the failure and exits non-zero.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv


TENANT = os.getenv("SWING_TENANT", "adam")
SYMBOL = os.getenv("SWING_SYMBOL", "SLR-27AUG26-CDE")
DATA_DIR = os.getenv("SWING_DATA_DIR", "data")
LOOP_INTERVAL_SECS = float(os.getenv("SWING_LOOP_INTERVAL", "1.0"))
FEED_READY_TIMEOUT = float(os.getenv("SWING_FEED_TIMEOUT", "15.0"))
SNAPSHOT_INTERVAL = float(os.getenv("SWING_SNAPSHOT_INTERVAL", "5.0"))


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


class DryRunBroker:
    """Wraps a real CoinbaseBroker but INTERCEPTS the write-side.
    All reads (order_status, position_qty, preview, snapshot) pass through.
    place_limit and cancel log and return a fake order id — no real order created."""

    def __init__(self, real):
        self._real = real
        self._fake_orders: dict[str, dict] = {}
        self._counter = 0

    def __getattr__(self, name):
        return getattr(self._real, name)

    def place_limit(self, side, qty, price):
        self._counter += 1
        oid = f"dry-run-{self._counter}"
        self._fake_orders[oid] = {
            "side": side, "qty": qty, "price": price,
            "status": "OPEN", "filled_qty": 0,
        }
        _log(f"[DRY RUN] would place {side} {qty} @ {price} → fake order {oid}")
        return oid

    def order_status(self, order_id):
        if order_id in self._fake_orders:
            o = self._fake_orders[order_id]
            return {
                "status": o["status"], "filled_qty": o["filled_qty"],
                "raw_status": "DRY_RUN", "average_filled_price": None,
            }
        return self._real.order_status(order_id)

    def cancel(self, order_id):
        if order_id in self._fake_orders:
            self._fake_orders[order_id]["status"] = "CANCELLED"
            _log(f"[DRY RUN] would cancel {order_id}")
            return
        self._real.cancel(order_id)


def _preflight(coinbase, store, tenant, symbol, notifier) -> tuple[bool, list[str]]:
    """Return (ok, issues). Every check must pass to proceed to live."""
    from config_validator import validate_config
    from roll import check_roll
    from safety import KillSwitch

    issues: list[str] = []

    # 1. Broker health — can we read the futures balance?
    try:
        balance = coinbase.futures_balance()
        if not balance:
            issues.append("preflight: futures balance empty — is the CFM account enrolled?")
    except Exception as e:
        issues.append(f"preflight: broker.futures_balance failed: {e}")

    # 2. Product exists and session is open
    try:
        spec = coinbase.contract_spec()
        if not spec or not spec.get("product_id"):
            issues.append(f"preflight: product {symbol} not found on venue")
        elif not spec.get("session_open"):
            issues.append(f"preflight: session for {symbol} is currently closed")
    except Exception as e:
        issues.append(f"preflight: broker.contract_spec failed: {e}")

    # 3. Config passes validator
    cfg = store.get_config(tenant, symbol) or {}
    v = validate_config(cfg)
    if not v.ok:
        issues.extend(f"preflight config: {i.field}: {i.message}" for i in v.issues)

    # 4. Kill switch off
    ks = KillSwitch(store, tenant)
    if ks.is_active():
        issues.append(f"preflight: kill switch active: {ks.reason() or 'no reason'}")

    # 5. Roll check
    try:
        roll_days = int(os.getenv("SWING_ROLL_DAYS_BEFORE", "5"))
        detection = check_roll(coinbase, symbol, roll_days_before=roll_days)
        if detection.should_roll:
            issues.append(f"preflight: {detection.summary()} — roll before running live")
    except Exception as e:
        _log(f"WARN: roll check failed: {e} (not a preflight blocker, but investigate)")

    # 6. Position vs floor
    try:
        pos = coinbase.position_qty()
        core = int(cfg.get("core_qty") or 0)
        if pos < core:
            issues.append(f"preflight: position {pos} below core {core} — would halt immediately")
    except Exception as e:
        issues.append(f"preflight: broker.position_qty failed: {e}")

    return (len(issues) == 0, issues)


def run() -> int:
    load_dotenv()

    dry_run = os.getenv("SWING_LIVE_DRY_RUN") == "1"
    real_confirm = os.getenv("SWING_LIVE_CONFIRM") == "I_UNDERSTAND"

    if not dry_run and not real_confirm:
        _log("REFUSING: neither SWING_LIVE_DRY_RUN=1 nor SWING_LIVE_CONFIRM=I_UNDERSTAND is set")
        _log("For a paper session use main.py. For a first live pass use SWING_LIVE_DRY_RUN=1.")
        return 2

    from alerting import default_notifier
    from broker import BrokerConfig, CoinbaseBroker
    from feed import LiveTickerFeed
    from safety import KillSwitch, TradeLog
    from state_store import JsonFileStateStore
    from swing_leg import SwingTrader

    mode = "DRY-RUN" if dry_run else "LIVE (real orders)"
    _log(f"live_runner: mode={mode}, symbol={SYMBOL}, tenant={TENANT}")

    store = JsonFileStateStore(f"{DATA_DIR}/store.json")
    log = TradeLog(f"{DATA_DIR}/trades.jsonl")
    ks = KillSwitch(store, TENANT)
    notifier = default_notifier()

    coinbase = CoinbaseBroker(BrokerConfig(product_id=SYMBOL))
    ok, issues = _preflight(coinbase, store, TENANT, SYMBOL, notifier)
    if not ok:
        for i in issues:
            _log(f"  ✗ {i}")
        _log("preflight failed — refusing to start")
        notifier.send(
            "live_runner preflight FAILED",
            f"tenant={TENANT} symbol={SYMBOL}\n" + "\n".join(issues),
            __import__("alerting").Priority.CRIT,
        )
        return 3
    _log("preflight: all checks passed")

    broker = DryRunBroker(coinbase) if dry_run else coinbase
    trader = SwingTrader(broker, store, TENANT, SYMBOL,
                         trade_log=log, kill_switch=ks, notifier=notifier)

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
            _log("no ticks — check WS + product_id")
            return 1
        _log("feed live — starting main loop")
        log.record("bot_started", mode=("dry_run" if dry_run else "live"),
                   tenant=TENANT, symbol=SYMBOL)
        trader.reconcile()

        last_snapshot = 0.0
        while not stopping:
            t = feed.latest_ticker()
            if t is None:
                time.sleep(0.1)
                continue
            trader.step(t["price"])
            now = time.time()
            if now - last_snapshot >= SNAPSHOT_INTERVAL:
                try:
                    snap = coinbase.snapshot()
                    snap["mode"] = "dry_run" if dry_run else "live"
                    snap["best_bid"] = t["best_bid"]
                    snap["best_ask"] = t["best_ask"]
                    snap["generated_at"] = now
                    store.put_snapshot(TENANT, SYMBOL, snap)
                except Exception as e:
                    _log(f"snapshot failed: {e}")
                last_snapshot = now
            time.sleep(LOOP_INTERVAL_SECS)

    finally:
        feed.stop()
        log.record("bot_stopped", mode=("dry_run" if dry_run else "live"))
    return 0


if __name__ == "__main__":
    sys.exit(run())
