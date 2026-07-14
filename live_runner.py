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

import health as _health  # background-job health tracker; never-raise


TENANT = os.getenv("SWING_TENANT", "adam")
SYMBOL = os.getenv("SWING_SYMBOL", "SLR-27AUG26-CDE")
SYMBOL_FAMILY = os.getenv("SWING_SYMBOL_FAMILY", "").strip() or None
DATA_DIR = os.getenv("SWING_DATA_DIR", "data")
LOOP_INTERVAL_SECS = float(os.getenv("SWING_LOOP_INTERVAL", "1.0"))
FEED_READY_TIMEOUT = float(os.getenv("SWING_FEED_TIMEOUT", "15.0"))
SNAPSHOT_INTERVAL = float(os.getenv("SWING_SNAPSHOT_INTERVAL", "5.0"))
# How often (seconds) to re-check the front-month contract when family mode
# is active. Once/hour is plenty — expiries move on multi-week cadences.
FAMILY_RECHECK_SECS = float(os.getenv("SWING_FAMILY_RECHECK_SECS", "3600.0"))
# How often (seconds) to re-pull contract_size, tick_size, and per-fill fees
# from Coinbase for EVERY product in the store. Coinbase adjusts fees (Adam's
# 30d volume tier shifts), contract specs occasionally change (roll cycles),
# and any product whose config was seeded with wrong defaults stays wrong
# until we overwrite it. 6h is a fine tradeoff: 4 refreshes/day, negligible
# Coinbase API budget, and no product can drift for more than 6h.
SPEC_REFRESH_SECS = float(os.getenv("SWING_SPEC_REFRESH_SECS", "21600.0"))
# Twitter shadow scanner poll interval. 5 min balances freshness against
# Nitter instance rate limits and RSS parse cost. Env override for tests.
TWITTER_POLL_SECS = float(os.getenv("SWING_TWITTER_POLL_SECS", "300.0"))
# Tick recorder pruning cadence + retention. Runs once per hour on the
# main loop. keep_days=7 caps total disk at roughly ~1GB across ~15
# symbols — bump SWING_TICK_KEEP_DAYS if a Render persistent disk is
# configured and you want longer retention for training data.
TICK_PRUNE_INTERVAL_SECS = float(os.getenv("SWING_TICK_PRUNE_INTERVAL_SECS", "3600.0"))
TICK_KEEP_DAYS = int(os.getenv("SWING_TICK_KEEP_DAYS", "7"))
# [crew:#4] How often to re-run reconcile() DURING the session. Previously
# reconcile ran once at startup and never again, so any drift between the bot's
# believed state and the exchange (an order filled/cancelled outside the step
# loop, a manual trade, position slipping below core) went undetected for the
# whole uptime — potentially days on Render. Re-running it periodically credits
# missed fills and halts on a core breach while the session is live. 60s default.
RECONCILE_INTERVAL_SECS = float(os.getenv("SWING_RECONCILE_INTERVAL_SECS", "60.0"))
# Adam durable rule (2026-07-13): refresh marks for ALL tracked products,
# not just the primary. Prior behavior called broker.portfolio_snapshot()
# only at startup — every non-primary product's mark stayed frozen for the
# rest of the session, corrupting unrealized display, portfolio circuit
# breaker aggregate math, and Carver risk-contribution reads. 30s is a
# safe cadence: negligible Coinbase API cost, aggressive enough that stale
# marks never lag by more than ~30s. Env override for tuning.
PORTFOLIO_REFRESH_SECS = float(os.getenv("SWING_PORTFOLIO_REFRESH_SECS", "2.0"))
# [crew] How often to verify the live config is still tracking the EXPERT params
# (expert_params × Layer-2 tuned multipliers). Alerts if silver's actual
# trail/stop/reanchor levels have drifted off the expert data. Read-only. 5 min.
EXPERT_GUARD_INTERVAL_SECS = float(os.getenv("SWING_EXPERT_GUARD_SECS", "300.0"))
SENTINEL_INTERVAL_SECS = float(os.getenv("SWING_SENTINEL_SECS", "300.0"))


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def _refresh_all_specs(store) -> int:
    """Pull fresh contract_size/tick_size/fees from Coinbase for EVERY product
    in EVERY tenant's config, and merge into the stored config. Runs once on
    startup and periodically thereafter. Returns count of refreshes attempted.

    Why: bot-live used to only guarantee spec freshness for its own primary
    symbol (SWING_SYMBOL). Every OTHER product Adam holds a strategy on
    (attached via the dashboard, force-included in the scanner) kept whatever
    contract_size was originally seeded — often wrong for nano/micro futures
    (BIT stored as 0.04 instead of 0.01, silver-defaults for oil products,
    etc.). Result: slider says '$10 net', but the sleeve produces $1.24
    because the modal computes spread with the wrong contract_size.

    Failures are logged and swallowed. One bad product must never block the
    refresh sweep for the other 20.
    """
    from main import _refresh_contract_spec_into_config  # reuse the paper logic
    tenants = store.list_tenants()
    refreshed = 0
    for tenant in tenants:
        for symbol in store.list_symbols(tenant):
            if symbol.startswith("__"):
                continue  # namespace / meta keys, not products
            try:
                _refresh_contract_spec_into_config(store, tenant, symbol)
                refreshed += 1
            except Exception as e:
                _log(f"[spec-refresh] {tenant}/{symbol} FAILED: {type(e).__name__}: {e}")
    return refreshed


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
        # Stale dry-run id persisted from a prior process (state lives in
        # Redis, this instance's _fake_orders dict does not). Treat as
        # CANCELLED so reconcile clears it and the strategy re-arms cleanly.
        # Without this, we'd forward the fake id to Coinbase and 400.
        if str(order_id).startswith("dry-run-"):
            _log(f"[DRY RUN] stale order id {order_id} from prior session — treating as CANCELLED")
            return {
                "status": "CANCELLED", "filled_qty": 0,
                "raw_status": "DRY_RUN_STALE", "average_filled_price": None,
            }
        return self._real.order_status(order_id)

    def cancel(self, order_id):
        if order_id in self._fake_orders:
            self._fake_orders[order_id]["status"] = "CANCELLED"
            _log(f"[DRY RUN] would cancel {order_id}")
            return
        if str(order_id).startswith("dry-run-"):
            _log(f"[DRY RUN] stale order id {order_id} from prior session — noop cancel")
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
    from safety import KillSwitch, make_trade_log
    from state_store import make_store
    from swing_leg import SwingTrader

    mode = "DRY-RUN" if dry_run else "LIVE (real orders)"

    # Family mode: resolve current front-month contract before anything else.
    # Lets a Coinbase auto-roll survive a redeploy without touching env vars.
    global SYMBOL
    if SYMBOL_FAMILY:
        try:
            from roll import resolve_front_month
            probe = CoinbaseBroker(BrokerConfig(product_id=SYMBOL))
            resolved = resolve_front_month(probe, SYMBOL_FAMILY, fallback=SYMBOL)
            if resolved and resolved != SYMBOL:
                _log(f"family={SYMBOL_FAMILY!r} → resolved front-month {resolved} (was {SYMBOL})")
                SYMBOL = resolved
            else:
                _log(f"family={SYMBOL_FAMILY!r} → still {SYMBOL}")
        except Exception as e:
            _log(f"family resolution failed ({type(e).__name__}: {e}) — using fallback {SYMBOL}")

    _log(f"live_runner: mode={mode}, symbol={SYMBOL}, tenant={TENANT}"
         f"{' (family=' + SYMBOL_FAMILY + ')' if SYMBOL_FAMILY else ''}")

    store = make_store(DATA_DIR)
    log = make_trade_log(DATA_DIR)
    _log(f"store backend: {type(store).__name__}, trade log: {type(log).__name__}")
    ks = KillSwitch(store, TENANT)
    notifier = default_notifier()

    # In dry-run only, seed a default config if the tenant/symbol has no
    # config yet — otherwise the preflight validator rejects the run before
    # the operator can configure via dashboard (chicken-and-egg on first
    # deploy). Real-money mode still requires the config to be pre-set:
    # we don't want defaults touching production.
    if dry_run and not store.get_config(TENANT, SYMBOL):
        from main import _default_paper_config
        _log(f"dry-run: seeding default config for {TENANT}/{SYMBOL}")
        store.put_config(TENANT, SYMBOL, _default_paper_config())

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

    # Sync EVERY product's contract_size + fees from Coinbase before the
    # trader takes its first step. Without this, dashboard modals and slider
    # math for non-primary products (BIT, NOL, XLP, everything else) run
    # against whatever was seeded — often wrong for nano futures. Runs again
    # every SPEC_REFRESH_SECS in the main loop so specs stay honest.
    try:
        n = _refresh_all_specs(store)
        _log(f"startup spec refresh: {n} product(s) refreshed against Coinbase truth")
    except Exception as e:
        _log(f"WARN: startup spec refresh failed: {type(e).__name__}: {e}")
    last_spec_refresh = time.time()
    # Offset the first twitter poll by 60s so bot startup isn't dominated by
    # a slow RSS fetch across ~15 handles.
    last_twitter_poll = time.time() - TWITTER_POLL_SECS + 60.0
    # Set to now so the FIRST refresh fires PORTFOLIO_REFRESH_SECS after
    # startup (the initial refresh already happened in _sync_live_portfolio).
    last_portfolio_refresh = time.time()
    # Offset tick pruning by 15 min from startup so it doesn't compete with
    # the first snapshot / scanner run.
    last_tick_prune = time.time() - TICK_PRUNE_INTERVAL_SECS + 900.0

    # Scanner tick shared with paper mode — keeps Edit Strategy tiles fresh
    # even when bot-paper isn't running (Adam retired it). Reuses the same
    # cadence (30s floor, 15 min auto) and force_include semantics.
    from scanner_worker import ScannerWorker
    scanner_worker = ScannerWorker(store, os.getenv("REDIS_URL") or None, SYMBOL)

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
        last_reconcile = time.time()  # [crew:#4] startup reconcile just ran
        last_expert_guard = time.time()  # [crew] expert-params drift guard
        last_sentinel = time.time()  # [crew] risk_sentinel periodic scan
        last_family_check = time.time()  # already resolved on startup
        while not stopping:
            t = feed.latest_ticker()
            if t is None:
                time.sleep(0.1)
                continue
            trader.step(t["price"])
            now = time.time()
            # Periodic front-month recheck. If Coinbase has rolled the family,
            # halt with a clear reason so the next restart picks up the new
            # symbol. We don't hot-swap the WS feed mid-session (real risk of
            # state confusion between old and new orders) — restart is safer.
            if SYMBOL_FAMILY and now - last_family_check >= FAMILY_RECHECK_SECS:
                last_family_check = now
                try:
                    from roll import resolve_front_month
                    latest = resolve_front_month(coinbase, SYMBOL_FAMILY, fallback=SYMBOL)
                    if latest and latest != SYMBOL:
                        msg = (f"front-month rolled: {SYMBOL} → {latest}. "
                               "Restarting to pick up new contract.")
                        _log(msg)
                        log.record("front_month_rolled",
                                   old_symbol=SYMBOL, new_symbol=latest, family=SYMBOL_FAMILY)
                        try:
                            from alerting import Priority
                            notifier.send("front-month rolled", msg, Priority.HIGH)
                        except Exception:
                            pass
                        stopping = True
                        break
                    _health.record_ok(store, "front_month", TENANT)
                except Exception as e:
                    _log(f"front-month recheck failed ({type(e).__name__}: {e})")
                    _health.record_error(store, "front_month", TENANT, e, trade_log=log)
            scanner_worker.tick()
            # [crew:#4] Periodic reconcile — trust the exchange, not memory.
            # Credits fills that happened outside the step loop and halts on a
            # core breach. Wrapped so a transient broker/API error never takes
            # the loop down; the next tick retries.
            if now - last_reconcile >= RECONCILE_INTERVAL_SECS:
                last_reconcile = now
                try:
                    trader.reconcile()
                    _health.record_ok(store, "reconcile", TENANT)
                except Exception as e:
                    _log(f"periodic reconcile failed: {type(e).__name__}: {e}")
                    _health.record_error(store, "reconcile", TENANT, e, trade_log=log)
            # [crew] Expert-params drift guard — is the live config still using
            # the expert data (expert_params x tuned multipliers)? Alerts on
            # drift. Read-only; a transient failure never stops the loop.
            if now - last_expert_guard >= EXPERT_GUARD_INTERVAL_SECS:
                last_expert_guard = now
                try:
                    import expert_guard
                    reports = expert_guard.run_guard(
                        store, TENANT, store.list_symbols(TENANT),
                        notifier=notifier, trade_log=log)
                    drifted = [r["symbol"] for r in reports if r.get("drifts")]
                    if drifted:
                        _log(f"expert_guard: DRIFT on {drifted} — alerted")
                    _health.record_ok(store, "expert_guard", TENANT)
                except Exception as e:
                    _log(f"expert_guard failed: {type(e).__name__}: {e}")
                    _health.record_error(store, "expert_guard", TENANT, e, trade_log=log)
            if now - last_sentinel >= SENTINEL_INTERVAL_SECS:
                last_sentinel = now
                try:
                    import risk_sentinel
                    risk_sentinel.run_sentinel(store, TENANT, log, now, notifier=notifier)
                    _health.record_ok(store, "risk_sentinel", TENANT)
                except Exception as e:
                    _log(f"risk_sentinel failed: {type(e).__name__}: {e}")
                    _health.record_error(store, "risk_sentinel", TENANT, e, trade_log=log)
            # Periodic sweep so no product's contract_size/fees can silently
            # drift for more than SPEC_REFRESH_SECS (6h default).
            if now - last_spec_refresh >= SPEC_REFRESH_SECS:
                last_spec_refresh = now
                try:
                    n = _refresh_all_specs(store)
                    _log(f"periodic spec refresh: {n} product(s) refreshed")
                    _health.record_ok(store, "spec_refresh", TENANT)
                except Exception as e:
                    _log(f"periodic spec refresh failed: {type(e).__name__}: {e}")
                    _health.record_error(store, "spec_refresh", TENANT, e, trade_log=log)
            # Portfolio circuit breaker — Van Tharp 'stop trading when things
            # go wrong'. Runs on the same cadence as snapshot so it can see
            # fresh mark prices when computing unrealized. Cheap: single
            # aggregation over all sleeves in this tenant.
            if now - last_snapshot >= SNAPSHOT_INTERVAL:
                try:
                    import portfolio_risk
                    change = portfolio_risk.tick(store, TENANT, trade_log=log)
                    if change:
                        _log(f"portfolio_risk: {change.get('kind')} — "
                             f"drawdown {change.get('drawdown_pct', 0):.1f}% "
                             f"(${change.get('drawdown_dollars', 0):.2f})")
                    _health.record_ok(store, "portfolio_risk_tick", TENANT)
                except Exception as e:
                    _log(f"portfolio_risk tick failed: {type(e).__name__}: {e}")
                    _health.record_error(store, "portfolio_risk_tick", TENANT, e, trade_log=log)
            # Twitter shadow scanner — polls a curated watchlist, detects
            # would-block / would-alert signals, evaluates outcomes at
            # 1h/6h/24h. Runs every TWITTER_POLL_SECS. SHADOW ONLY: the
            # module has a hardcoded EXECUTE_TRADES=False; nothing in this
            # loop passes a Twitter signal to any order path. Adam's ask:
            # "give it a try but don't execute any trades with it. I want
            # to see if it works first."
            # Portfolio refresh: Adam durable rule 2026-07-13. Refresh marks
            # for ALL tracked products every PORTFOLIO_REFRESH_SECS. Prior
            # behavior called broker.portfolio_snapshot() only at startup;
            # PT and every other non-primary product's mark stayed frozen,
            # corrupting the dashboard's unrealized display, the aggregate
            # circuit-breaker math, and Carver risk-contribution reads.
            if now - last_portfolio_refresh >= PORTFOLIO_REFRESH_SECS:
                last_portfolio_refresh = now
                try:
                    from main import refresh_portfolio_snapshot
                    live_tenant = f"{TENANT}-live"
                    n_refreshed = refresh_portfolio_snapshot(store, live_tenant)
                    if n_refreshed > 0:
                        pass  # silent success — logging every 30s is noisy
                    # refresh_portfolio_snapshot already records
                    # portfolio_snapshot_error internally + updates its own
                    # snapshot flags (see main.py:261). We add the health
                    # record here for the wrapper site itself.
                    _health.record_ok(store, "portfolio_refresh", TENANT)
                except Exception as e:
                    _log(f"portfolio refresh failed: {type(e).__name__}: {e}")
                    _health.record_error(store, "portfolio_refresh", TENANT, e, trade_log=log)
            if now - last_twitter_poll >= TWITTER_POLL_SECS:
                last_twitter_poll = now
                try:
                    import twitter_scanner
                    telem = twitter_scanner.tick(store, TENANT)
                    if telem.get("signals_new", 0) or telem.get("outcomes_updated", 0):
                        _log(f"twitter_scanner: {telem}")
                except Exception as e:
                    _log(f"twitter_scanner tick failed: {type(e).__name__}: {e}")
                # Funding sign-flip watcher rides the same cadence — every
                # 5 min. Cheap (reads snapshot cache, no external API), so
                # no additional throttle needed. Emits shadow signals into
                # the Signals tab when a perp's funding rate crosses zero.
                try:
                    import funding_watcher
                    ftelem = funding_watcher.tick(store, TENANT)
                    if ftelem.get("flips_detected", 0):
                        _log(f"funding_watcher: {ftelem}")
                except Exception as e:
                    _log(f"funding_watcher tick failed: {type(e).__name__}: {e}")
            # Tick-recorder pruning: drop tick directories older than
            # TICK_KEEP_DAYS. Bounded disk consumption on Render's
            # ephemeral (or persistent) volume.
            if now - last_tick_prune >= TICK_PRUNE_INTERVAL_SECS:
                last_tick_prune = now
                try:
                    from tick_recorder import prune_old_ticks
                    n = prune_old_ticks(keep_days=TICK_KEEP_DAYS)
                    if n:
                        _log(f"tick_recorder pruned {n} old day-directories")
                except Exception as e:
                    _log(f"tick prune failed: {type(e).__name__}: {e}")
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
