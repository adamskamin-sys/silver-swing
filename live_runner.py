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
import log_config as _log_config; _log_config.install()  # quiet Cloudflare 502 HTML dumps + WS reconnect spam


TENANT = os.getenv("SWING_TENANT", "adam")
SYMBOL = os.getenv("SWING_SYMBOL", "SLR-27AUG26-CDE")
SYMBOL_FAMILY = os.getenv("SWING_SYMBOL_FAMILY", "").strip() or None

# Adam 2026-07-16: SWING_SYMBOL can now be "" or "NONE" to disable the
# primary-trader path entirely. Every product then runs as a non-primary
# track — collapses the primary/non-primary asymmetry per the
# project_silver_not_special memory rule. When PRIMARY_ENABLED=False:
#   - no primary broker / trader / feed
#   - no primary preflight, no primary reconcile, no primary step()
#   - main loop cadence uses time.sleep instead of primary feed poll
#   - non-primary tracks discover + tick normally
# Default (any real symbol) preserves current behavior. Kill switch:
# revert env var to the old SYMBOL value.
PRIMARY_ENABLED = bool(SYMBOL and SYMBOL.strip().upper() not in ("", "NONE"))
DATA_DIR = os.getenv("SWING_DATA_DIR", "data")
LOOP_INTERVAL_SECS = float(os.getenv("SWING_LOOP_INTERVAL", "0.05"))
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
# Long-horizon trend verdict refresh — daily candles for each held product,
# fed to trend_filter.long_trend_verdict. 6h cadence matches spec refresh:
# daily candles change once/day; 6h refresh gives us a fresh verdict well
# ahead of the 12h cache TTL. Env override for tests + tuning.
TREND_REFRESH_SECS = float(os.getenv("SWING_TREND_REFRESH_SECS", "21600.0"))
# Funding-rate sign-flip watcher poll cadence. 5 min balances freshness
# against funding change rate (perpetuals fund every 1h or 8h).
FUNDING_POLL_SECS = float(os.getenv("SWING_FUNDING_POLL_SECS", "300.0"))
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
# reconciliation_monitor cadence (2026-07-14 auditor artifact). Read-only
# defense — diffs exchange orders/positions against bot's sleeve state.
# 5 min is aggressive enough to catch a duplicate-orders or SLR-ghost
# class of bug within one tick window, cheap enough not to spam the notifier.
RECONCILIATION_INTERVAL_SECS = float(os.getenv("SWING_RECONCILIATION_SECS", "300.0"))
# Kill switch for the 2026-07-14 non-primary tick fix. Live worker used to
# tick ONLY the primary SYMBOL (SLR); sleeves on other held products (PT,
# HYP, XLP, etc.) went silent when the paper worker was suspended, missing
# take-profits and stop-losses. When set to 1 (default), the live worker
# creates a SwingTrader per held non-primary product and ticks each one
# with fresh marks from the __portfolio__ refresh (every 2s). Set to 0
# to revert to primary-only ticking if this new path misbehaves.
TICK_NON_PRIMARY = os.getenv("SWING_LIVE_TICK_NON_PRIMARY", "1") == "1"


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def _refresh_all_trend_verdicts(store, live_tenant: str) -> int:
    """Fetch daily candles for every held product on the live tenant
    and cache a fresh long-horizon trend verdict (Faber 200-day SMA +
    MOP 12-month TSM per trend_filter.py).

    2026-07-19 Option D-1: only refreshes when SWING_TREND_FILTER_ENABLED
    is on. When off, this is a no-op — no wasted Coinbase API calls.

    Runs on startup + every TREND_REFRESH_SECS. Bounded API cost: 1
    daily-candle call per held product per refresh (default 6h).

    Returns count of products whose verdict was refreshed.
    """
    try:
        import trend_filter as _tf
    except Exception:
        return 0
    if not _tf.long_trend_flag_enabled():
        return 0
    try:
        from broker import BrokerConfig, CoinbaseBroker
    except Exception:
        return 0
    # Pull the current portfolio snapshot to find held products
    pf = store.get_config(live_tenant, "__portfolio__") or {}
    derivs = pf.get("derivatives") or []
    refreshed = 0
    for d in derivs:
        pid = d.get("product_id")
        try:
            qty = float(d.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if not pid or qty == 0:
            continue
        try:
            b = CoinbaseBroker(BrokerConfig(product_id=pid))
            v = _tf.refresh_long_trend_verdict(b, store, live_tenant, pid)
            if v is not None:
                refreshed += 1
                _log(f"[trend] {pid} verdict: buy_ok={v.get('buy_ok')} "
                     f"tsm={v.get('tsm_sign')} gap={v.get('faber_gap')}")
        except Exception as e:
            _log(f"[trend] {pid} refresh failed: {type(e).__name__}: {e}")
    return refreshed


def _sweep_orphan_orders(store, live_tenant: str) -> int:
    """Cancel any Coinbase open order not referenced by any sleeve on the
    live tenant. Runs on boot + after every position-flat event.

    Two distinct classes covered:

      1. Orphan SELL (any product in cfg) — CHN + NER incident 2026-07-19.
         SELL orders left by a prior bot session flip the account SHORT
         after the primary sell closes position. HARD invariant per
         feedback_no_shorting.md.

      2. Orphan BUY that DUPLICATES a sleeve-owned BUY on the same product
         at similar price — HYP incident 2026-07-19 22:51 reconcile log
         showed 2 BUYs @ $60.17 on HYP-20DEC30-CDE (28bf7de3 orphan +
         552fbcab bot-owned). Both filling = oversize. A lone orphan BUY
         is left alone — could be a manual order the user just placed,
         not our business to cancel.

    Only sweeps orders whose product_id has an entry in the live tenant
    cfg (safety: never touches products we don't know about — could be
    hedges or manual positions the user placed via Coinbase UI).

    Returns count of orders canceled.
    """
    try:
        from broker import BrokerConfig, CoinbaseBroker
    except Exception as e:
        _log(f"[orphan-sweep] broker import failed: {e}")
        return 0

    # Build known-oid set from live tenant sleeves. Also keep a
    # product+side+price index of KNOWN orders so we can detect
    # orphan duplicates.
    known_oids: set[str] = set()
    known_by_pid_side: dict[tuple[str, str], list[str]] = {}
    try:
        for symbol in store.list_symbols(live_tenant):
            if symbol.startswith("__"):
                continue
            state = store.get_state(live_tenant, symbol) or {}
            for sid, ss in (state.get("sleeves") or {}).items():
                if not isinstance(ss, dict):
                    continue
                for k in ("live_order_id", "resting_stop_oid"):
                    v = ss.get(k)
                    if v:
                        known_oids.add(str(v))
                        # Track side/product for duplicate detection.
                        # resting_stop_oid → SELL. live_order_id side
                        # inferred from state.state (ARMED_SELL → SELL,
                        # ARMED_BUY → BUY).
                        if k == "resting_stop_oid":
                            side_of = "SELL"
                        else:
                            st = str(ss.get("state") or "").upper()
                            side_of = "SELL" if "SELL" in st else "BUY"
                        known_by_pid_side.setdefault((symbol, side_of), []).append(str(v))
            if state.get("live_order_id"):
                known_oids.add(str(state.get("live_order_id")))
                st = str(state.get("state") or "").upper()
                side_of = "SELL" if "SELL" in st else "BUY"
                known_by_pid_side.setdefault((symbol, side_of), []).append(str(state.get("live_order_id")))
    except Exception as e:
        _log(f"[orphan-sweep] oid collection failed: {e}")
        return 0

    # Seed BrokerConfig with any known product_id
    seed = os.getenv("SWING_SYMBOL", "SLR-27AUG26-CDE")
    try:
        b = CoinbaseBroker(BrokerConfig(product_id=seed))
        resp = b.client.list_orders(order_status=["OPEN"])
        raw = resp.to_dict() if hasattr(resp, "to_dict") else resp
    except Exception as e:
        _log(f"[orphan-sweep] list_orders failed: {e}")
        return 0

    orders = raw.get("orders") or []

    def _order_price(o: dict) -> float:
        cfg_o = o.get("order_configuration") or {}
        for shape in cfg_o.values():
            if isinstance(shape, dict):
                for k in ("limit_price", "stop_price", "price"):
                    v = shape.get(k)
                    if v:
                        try:
                            return float(v)
                        except (TypeError, ValueError):
                            pass
        return 0.0

    # Index open orders by (product, side) so we can detect duplicates.
    open_by_pid_side: dict[tuple[str, str], list[dict]] = {}
    for o in orders:
        pid = o.get("product_id")
        side = str(o.get("side") or "").upper()
        if pid and side:
            open_by_pid_side.setdefault((pid, side), []).append(o)

    canceled = 0
    for o in orders:
        oid = o.get("order_id") or o.get("id")
        side = str(o.get("side") or "").upper()
        pid = o.get("product_id")
        if not oid or oid in known_oids:
            continue
        # Guard: only touch products we know about (in cfg)
        try:
            cfg = store.get_config(live_tenant, pid) or {}
        except Exception:
            cfg = {}
        if not cfg.get("contract_size"):
            _log(f"[orphan-sweep] skip {pid}/{oid}: product not in cfg")
            continue

        cancel_reason = None
        if side == "SELL":
            # Adam 2026-07-20 RACE FIX: don't cancel SELL orders younger
            # than 5 minutes. Boot orphan sweep runs BEFORE all tracks
            # respawn + adopt their existing resting_stop_oids from state,
            # so a stop-limit just placed by the prior session (or by the
            # current session mid-Redis-save) reads as "orphan" and gets
            # cancelled. Race root cause: 5 stop-place events in Adam's
            # Coinbase fill history for HIGH + META (all cancelled seconds
            # after placement across successive Render deploys). The §3.8
            # short-risk that motivated the aggressive sweep is a MULTI-
            # HOUR concern — orders from prior bot sessions are hours+
            # old. Fresh orders (<300s) are almost certainly current
            # session's own stops.
            import datetime as _dt_orph
            _created_recent = False
            try:
                _ct = o.get("created_time")
                if _ct:
                    _ts = _dt_orph.datetime.fromisoformat(
                        str(_ct).replace("Z", "+00:00")).timestamp()
                    if (time.time() - _ts) < 300:
                        _created_recent = True
            except Exception:
                pass
            if _created_recent:
                _log(f"[orphan-sweep] SKIP recent SELL {pid}/{oid} "
                     f"(< 300s old — likely current-session race with state save)")
                continue
            # Any orphan SELL is a short-flip risk — cancel.
            cancel_reason = "would have flipped account short"
        elif side == "BUY":
            # Orphan BUY is only cancelable when it DUPLICATES a known
            # bot-owned BUY on the same product (fill-double = oversize).
            # A lone orphan BUY could be a manual user order; leave it.
            known_buys = known_by_pid_side.get((pid, "BUY"), [])
            all_open_buys = open_by_pid_side.get((pid, "BUY"), [])
            has_bot_owned_buy = any(str(x.get("order_id") or x.get("id")) in known_buys
                                     for x in all_open_buys)
            if has_bot_owned_buy:
                px = _order_price(o)
                cancel_reason = f"duplicate BUY vs bot-owned on {pid} (px={px})"
        if not cancel_reason:
            continue

        try:
            b2 = CoinbaseBroker(BrokerConfig(product_id=pid))
            b2.cancel(oid)
            canceled += 1
            _log(f"[orphan-sweep] CANCELED orphan {side} {pid}/{oid} "
                 f"({cancel_reason})")
        except Exception as e:
            _log(f"[orphan-sweep] cancel {pid}/{oid} failed: {e}")
    return canceled


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

    def place_market(self, side, qty):
        # 2026-07-14 problem-scout #4: pre-existing hole — without this,
        # SwingTrader._sleeve_market_sell / crash_guard / manual market
        # intents fell through __getattr__ to the real Coinbase client
        # and submitted REAL market orders even in dry-run mode. Amplified
        # by the non-primary tick fix which lets every held product's
        # sleeves reach market-order paths.
        self._counter += 1
        oid = f"dry-run-mkt-{self._counter}"
        # Try to get a reasonable fake fill price from contract_spec so
        # downstream _on_fill math (realized_pnl, cycles) uses something
        # near reality instead of 0.0.
        mark = 0.0
        try:
            spec = self._real.contract_spec()
            mark = float((spec or {}).get("current_price") or 0.0)
        except Exception:
            mark = 0.0
        self._fake_orders[oid] = {
            "side": side, "qty": qty, "price": mark,
            "status": "FILLED", "filled_qty": qty,
            "average_filled_price": mark,
        }
        _log(f"[DRY RUN] would market {side} {qty} @ ~{mark} → fake order {oid}")
        return oid

    def place_stop_limit(self, side, qty, stop_price, limit_price, client_order_id=None):
        # Adam 2026-07-15: DryRun stub for the ratchet-stop primitive.
        # Without this, calls fall through __getattr__ to the real client
        # and would submit REAL stop-limit orders in dry-run mode.
        self._counter += 1
        oid = f"dry-run-stop-{self._counter}"
        self._fake_orders[oid] = {
            "side": side, "qty": qty, "price": limit_price,
            "stop_price": stop_price, "limit_price": limit_price,
            "status": "OPEN", "filled_qty": 0,
        }
        _log(f"[DRY RUN] would place STOP_LIMIT {side} {qty} stop={stop_price} limit={limit_price} → fake order {oid}")
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
    from config_validator import validate_config, validate_market_bounds
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
    # Advisory: check abort bands vs current market price (non-blocking — does
    # NOT add to issues; won't prevent startup). Catches CHN-class bug where
    # abort_above=70 on a $3133 asset halts every tick. Logged prominently so
    # the operator sees it; a phone alert fires via notifier if available.
    try:
        _mark = coinbase.get_best_bid_ask() or {}
        _mid = (_mark.get("best_bid", 0) + _mark.get("best_ask", 0)) / 2
        if not _mid:
            _mid = float((coinbase.contract_spec() or {}).get("price") or 0)
        for _w in validate_market_bounds(cfg, _mid):
            _log(f"WARN preflight abort-band: {_w}")
            try:
                from alerting import Priority
                notifier.send("preflight: abort band misconfiguration", _w, Priority.WARN)
            except Exception:
                pass
    except Exception:
        pass

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

    # 2026-07-14 problem-scout #1: hard tenant guard. Non-primary trader
    # code below constructs SwingTrader(store, TENANT, pid) — if TENANT
    # doesn't end with '-live', reads/writes go to the wrong scope while
    # main.py's __portfolio__ lives under '{TENANT}-live'. Silent state
    # divergence + potential duplicate orders (same class as 2026-07-14
    # multi-writer incident). main.py already has this guard; parity.
    if not TENANT.endswith("-live"):
        _log(f"REFUSING: SWING_TENANT={TENANT!r} must end with '-live'. "
             f"Set SWING_TENANT=adam-live (or your equivalent) in Render env.")
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
    if PRIMARY_ENABLED and SYMBOL_FAMILY:
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

    if PRIMARY_ENABLED:
        _log(f"live_runner: mode={mode}, symbol={SYMBOL}, tenant={TENANT}"
             f"{' (family=' + SYMBOL_FAMILY + ')' if SYMBOL_FAMILY else ''}")
    else:
        _log(f"live_runner: mode={mode}, tenant={TENANT} — NO PRIMARY "
             f"(SWING_SYMBOL={SYMBOL!r}) — non-primary tracks only")

    store = make_store(DATA_DIR)
    log = make_trade_log(DATA_DIR)
    _log(f"store backend: {type(store).__name__}, trade log: {type(log).__name__}")
    ks = KillSwitch(store, TENANT)
    notifier = default_notifier()

    # Primary-symbol setup — skipped entirely when PRIMARY_ENABLED=False.
    # In no-primary mode: coinbase/broker/trader remain None. The main
    # loop below handles the None case.
    coinbase = None
    broker = None
    trader = None
    if PRIMARY_ENABLED:
        # Config auto-seed. Two variants:
        # - Dry-run: seed the full paper defaults (was already here).
        # - Real-money: seed a SLEEVES-ONLY config (swing_qty=0, primary
        #   disabled) so preflight passes without the operator having to
        #   pre-configure a primary strategy. Adam's fleet is sleeves-
        #   only (project_silver_not_special); the primary is legacy.
        #   Live-safe defaults are pulled from _seed_config_if_missing's
        #   fleet-wide rule (2026-07-15): core_qty=0, abort bands
        #   ATR-derived, swing_qty=0 disables primary trading.
        # 2026-07-19: real-money seed added to unbreak the crash loop
        # observed when a live tenant's primary config was stripped
        # to specs-only by auto-bootstrap.
        _existing_cfg = store.get_config(TENANT, SYMBOL) or {}
        if dry_run and not _existing_cfg:
            from main import _default_paper_config
            _log(f"dry-run: seeding default config for {TENANT}/{SYMBOL}")
            store.put_config(TENANT, SYMBOL, _default_paper_config())
        elif not dry_run:
            # Backfill any required primary fields the validator needs, with
            # live-safe values (swing_qty=0 means primary is disabled → no
            # bot-side primary trading, sleeves still work). Idempotent: won't
            # touch fields already set.
            from main import _seed_config_if_missing
            _seed_config_if_missing(store, TENANT, SYMBOL)
            # If STILL missing critical fields after seed (edge case where
            # the tenant has no ATR + no cfg at all — impossible on adam-
            # live after auto-bootstrap, but defensive), fill hard defaults
            # so preflight can pass. All conservative: swing disabled, wide
            # abort bands, minimal risk.
            _cfg2 = store.get_config(TENANT, SYMBOL) or {}
            # Hard defaults satisfy the validator's cross-field checks:
            #   buy_px < sell_px, abort_below < buy_px < abort_above,
            #   margin_per_contract > 0. Values are dummies — swing_qty=0
            #   means the primary strategy is disabled; these numbers never
            #   drive trades. But the validator doesn't know that so we
            #   have to pass its shape check.
            _hard_defaults = {
                "swing_qty": 0, "max_swing_qty": 1,
                "buy_px": 1.0, "sell_px": 2.0,
                "abort_below": 0.5, "abort_above": 1e9,
                "margin_per_contract": 1.0,
                "scale_up_buffer_mult": 1.5,
                "fee_per_contract_roundtrip": 0.01,
                "contract_size": 1.0,
                "core_qty": 0,
            }
            # A previous deploy of this seed path (before 6248f67) may
            # have written buy_px=sell_px=0.0 to Redis. Those pass the
            # "field present" check but fail the cross-field validator.
            # Overwrite invalid zeros too, not just missing fields.
            _invalid_zero_keys = {
                "buy_px", "sell_px",
                "margin_per_contract", "scale_up_buffer_mult",
                "contract_size", "abort_above",
            }
            _dirty = False
            for k, v in _hard_defaults.items():
                cur = _cfg2.get(k)
                missing = k not in _cfg2
                zero_invalid = (k in _invalid_zero_keys and cur == 0)
                if missing or zero_invalid:
                    _cfg2[k] = v
                    _dirty = True
            # Cross-field: if abort_below >= buy_px, snap it below.
            try:
                if float(_cfg2.get("abort_below", 0)) >= float(_cfg2.get("buy_px", 0)):
                    _cfg2["abort_below"] = float(_cfg2["buy_px"]) * 0.5
                    _dirty = True
            except (TypeError, ValueError):
                pass
            if _dirty:
                store.put_config(TENANT, SYMBOL, _cfg2)
                _log(f"[{TENANT}/{SYMBOL}] real-money: seeded hard defaults "
                     f"for primary fields (primary disabled, sleeves unaffected)")

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
    else:
        _log("no primary symbol — skipping primary broker/preflight/trader")

    # 2026-07-14 non-primary tick fix. Live worker previously only ticked
    # the primary SYMBOL — sleeves on other held products (PT, HYP, XLP,
    # etc.) went silent when the paper worker was suspended, missing take-
    # profits and stop-losses (root cause of missed PLAT sell + trail).
    # Cache one SwingTrader per non-primary product; each shares the tenant
    # kill switch + trade log but has its own CoinbaseBroker (broker is
    # tied to a product_id). Traders are created lazily on first tick.
    # 2026-07-14 full parity refactor. Each held non-primary product gets
    # its own {feed, trader, failure counter}. Structurally treats all
    # products equally (per project_silver_not_special.md) — the only
    # remaining "primary" concept is the boot-time SYMBOL that seeds the
    # dedicated WS feed above. All others get sub-second WS ticks too.
    class _NonPrimaryTrack:
        __slots__ = ("product_id", "feed", "trader",
                     "consecutive_step_failures", "last_step_ok_ts",
                     "last_tick_seen_ts", "spawn_ts", "tick_count",
                     "last_tick_attempt_ts", "last_tick_reason")
        def __init__(self, product_id, feed, trader):
            self.product_id = product_id
            self.feed = feed
            self.trader = trader
            self.consecutive_step_failures = 0
            # Adam 2026-07-15: init to 0 (was time.time()). Prior init to
            # spawn time made a Track that spawned but never stepped look
            # 'recently active' for the first 5 min. That defeated the
            # zombie check — a Track whose feed never produces a ticker
            # got a false 'alive' signal until 5 min after spawn.
            # Now: last_step_ok_ts stays 0 until step() actually succeeds.
            # The zombie check treats 0 as "infinite age" → detects
            # immediately. spawn_ts preserved for observability.
            self.last_step_ok_ts = 0.0
            self.last_tick_seen_ts = 0.0
            self.spawn_ts = time.time()
            # Adam 2026-07-19: cumulative tick count. Incremented only on
            # successful (non-halted, non-raising) step(). Diag reads this
            # via the periodic heartbeat to compute actual tick-rate per
            # Track — decoupled from sleeve-event count (which was
            # misleadingly used as a heartbeat before).
            self.tick_count = 0
            # Adam 2026-07-19: per-tick diagnostic. Every tick attempt
            # sets these — even the ones that skip. Lets us see WHY a
            # Track isn't advancing tick_count (feed_no_ticker,
            # step_halted, step_error, evicting, etc.).
            self.last_tick_attempt_ts = 0.0
            self.last_tick_reason = ""
        def close(self):
            try: self.feed.stop()
            except Exception: pass

    _non_primary_tracks: dict[str, "_NonPrimaryTrack"] = {}
    # problem-scout #3 (v2): cooldown between an eviction and a re-creation
    # attempt on the same product. Prevents infinite create-fail-evict
    # loops that would burn Coinbase auth handshakes and could rate-limit
    # us off the primary feed too.
    _non_primary_last_evict_ts: dict[str, float] = {}
    EVICT_COOLDOWN_SECS = float(os.getenv("SWING_EVICT_COOLDOWN_SECS", "900.0"))  # 15 min
    # Aggressive re-sync threshold: if a product's WS feed hasn't produced
    # a tick in this many seconds (and it's been alive that long),
    # tear down and restart the feed. Adam's 2026-07-14 rule: "catch up
    # + sync, never halt."
    FEED_STALE_THRESHOLD_SECS = float(os.getenv("SWING_FEED_STALE_SECS", "30.0"))
    # After N consecutive step failures on a track, evict + log a WARN so
    # a silently-broken trader doesn't sit forever pretending to work.
    STEP_FAILURE_EVICT_THRESHOLD = int(os.getenv("SWING_STEP_FAIL_EVICT", "10"))
    # Reconcile fills that predate this many hours are treated as stale
    # (clear the live_order_id, don't credit as fresh) — problem-scout #3.
    # 24h default covers "sleeve went silent overnight and orders may have
    # filled at Coinbase" without swallowing legitimate recent activity.
    STALE_HEARTBEAT_HOURS = float(os.getenv("SWING_STALE_HEARTBEAT_HOURS", "24.0"))

    def _clear_stale_sleeve_order_ids(product_id: str) -> None:
        """problem-scout #3 (v2, post-review): before creating a new trader,
        clear any live_order_ids on sleeves whose last activity is older
        than STALE_HEARTBEAT_HOURS. Prevents `_on_fill` from crediting a
        months-old FILLED order as a fresh cycle, which would pollute
        realized_pnl + cycles + trigger an expert-reanchor at a stale
        basis and possibly place a live-crossing buy.

        Field names verified against sleeves.py: sleeves have
        `armed_buy_since_ts`, not `last_heartbeat_ts` or `armed_at`.
        Parent SwingState has `last_heartbeat_ts` (updated on every
        _save_state) — use it as the "did this trader tick lately"
        signal when the sleeve has no armed_buy_since_ts."""
        try:
            st = store.get_state(TENANT, product_id) or {}
            sleeves = st.get("sleeves") or {}
            if not sleeves:
                return
            now_ts = time.time()
            cutoff = STALE_HEARTBEAT_HOURS * 3600
            parent_hb = float(st.get("last_heartbeat_ts") or 0)
            dirty = False
            for sid, ss in sleeves.items():
                if not ss.get("live_order_id"):
                    continue
                # Sleeve's own heartbeat first (when ARMED_BUY), else fall
                # back to the parent trader's heartbeat — if the whole
                # trader hasn't ticked lately, all sleeve state is stale.
                sleeve_hb = float(ss.get("armed_buy_since_ts") or 0)
                hb = sleeve_hb or parent_hb
                if hb and (now_ts - hb) > cutoff:
                    _log(f"[non-primary] {product_id}/{sid}: clearing stale "
                         f"live_order_id={ss['live_order_id']} "
                         f"({(now_ts - hb) / 3600:.1f}h old)")
                    ss["live_order_id"] = None
                    dirty = True
            if dirty:
                st["sleeves"] = sleeves
                store.put_state(TENANT, product_id, st)
        except Exception as e:
            _log(f"[non-primary] {product_id}: stale-heartbeat guard failed: "
                 f"{type(e).__name__}: {e}")

    def _get_or_create_non_primary_track(product_id: str):
        """Lazy-instantiate a per-product WebSocket feed + SwingTrader.
        Returns None if the product should be skipped (primary, reserved
        key, no config, kill switch active, in eviction cooldown, or
        creation error)."""
        if product_id == SYMBOL or product_id.startswith("__"):
            return None
        if product_id in _non_primary_tracks:
            return _non_primary_tracks[product_id]
        # problem-scout #3 (v2): eviction cooldown. If we evicted this
        # product recently, don't re-create it until the cooldown expires
        # — else a persistent per-product failure (bad config, delisted,
        # auth error) becomes an infinite create/fail/evict loop that
        # would burn Coinbase auth handshakes and could get us rate-
        # limited off the primary feed.
        last_evict = _non_primary_last_evict_ts.get(product_id, 0.0)
        if last_evict and (time.time() - last_evict) < EVICT_COOLDOWN_SECS:
            return None  # silent — we already logged the eviction
        # problem-scout #2: refuse creation with no config (SwingConfig
        # defaults are SLR-calibrated → wrong for other products).
        cfg = store.get_config(TENANT, product_id) or {}
        if not cfg:
            # Adam 2026-07-15: silent failure class — scanner-armed sleeves
            # (Option-B) create sleeve state without a top-level config.
            # Auto-recovery kept calling this function every 15s and getting
            # None back without any trade-log event, making it look like
            # "silent bug." Now we log the refusal + auto-seed a minimal
            # config from Coinbase specs when sleeves exist.
            state = store.get_state(TENANT, product_id) or {}
            sleeves_state = state.get("sleeves") or {}
            if sleeves_state:
                # Seed a minimal config from Coinbase specs. Better than
                # SLR-defaulted SwingConfig — pulls real tick_size,
                # contract_size, fees. Sleeves preserved.
                try:
                    seed_broker = CoinbaseBroker(BrokerConfig(product_id=product_id))
                    spec = seed_broker.contract_spec() or {}
                    _mrate = float(spec.get("intraday_margin_rate") or 0)
                    _cs    = float(spec.get("contract_size") or 1)
                    _cpx   = float(spec.get("current_price") or 0)
                    seeded = {
                        "product_id": product_id,
                        "tick_size": float(spec.get("tick_size") or 0.01),
                        "contract_size": _cs,
                        "margin_per_contract": round(_mrate * _cs * _cpx, 4) if _mrate > 0 and _cpx > 0 else 0,
                        "fee_per_contract_roundtrip": 0.5,   # conservative
                        "swing_qty": 0,                       # sleeves only, no primary
                        "core_qty": 0,                        # no protected core
                        "abort_above": 1e9,                   # bands off — sleeve controls (0 would halt on every tick)
                        "abort_below": 0,
                        "sleeves": [],                        # kept in state, not here
                    }
                    store.put_config(TENANT, product_id, seeded)
                    try:
                        log.record("non_primary_config_auto_seeded",
                                   tenant=TENANT, symbol=product_id,
                                   spec=spec, severity="warn",
                                   reason="sleeves exist but no top-level config; auto-seeded from Coinbase specs to enable spawn")
                    except Exception:
                        pass
                    _log(f"[non-primary] {product_id}: AUTO-SEEDED config "
                         f"from Coinbase specs (was missing; sleeves exist)")
                    cfg = seeded
                except Exception as _seed_err:
                    try:
                        log.record("non_primary_config_auto_seed_failed",
                                   tenant=TENANT, symbol=product_id,
                                   error=f"{type(_seed_err).__name__}: {_seed_err}",
                                   severity="critical",
                                   reason="cannot seed config; spawn will keep failing")
                    except Exception:
                        pass
                    _log(f"[non-primary] {product_id}: auto-seed FAILED: "
                         f"{type(_seed_err).__name__}: {_seed_err}")
                    return None
            else:
                # No sleeves either — genuinely nothing to spawn for
                try:
                    log.record("non_primary_spawn_refused_no_config",
                               tenant=TENANT, symbol=product_id,
                               reason="no top-level config AND no sleeve state",
                               severity="info")
                except Exception:
                    pass
                _log(f"[non-primary] {product_id}: no config, SKIPPING "
                     f"(configure via dashboard first)")
                return None
        # problem-scout #5: respect the kill switch before construction.
        try:
            if ks.is_active():
                _log(f"[non-primary] {product_id}: kill switch active, SKIPPING")
                return None
        except Exception:
            pass
        try:
            # problem-scout #3 (v2): clear stale sleeve order IDs BEFORE
            # reconcile so _sleeve_on_fill can't credit ancient fills as
            # fresh cycles (would pollute realized_pnl + cycles + trigger
            # an expert-reanchor at a stale basis).
            _clear_stale_sleeve_order_ids(product_id)
            prod_coinbase = CoinbaseBroker(BrokerConfig(product_id=product_id))
            prod_broker = DryRunBroker(prod_coinbase) if dry_run else prod_coinbase
            prod_trader = SwingTrader(prod_broker, store, TENANT, product_id,
                                      trade_log=log, kill_switch=ks, notifier=notifier)
            # problem-scout #4 (v2): DO NOT call normalize_primary_swing_qty
            # on non-primary traders. The normalizer HALTs on drift; for
            # non-primary products (which mostly run swing_qty=0 + sleeves
            # only), a HALT would silently freeze the sleeve overnight —
            # the opposite of the goal. Instead: LOG drift but don't act.
            # A human sees the log line and can decide whether to clamp.
            try:
                st = store.get_state(TENANT, product_id) or {}
                cfg_sq = int(cfg.get("swing_qty") or 0)
                st_sq = int(st.get("swing_qty") or 0)
                if st_sq != cfg_sq:
                    _log(f"[non-primary] {product_id} state.swing_qty={st_sq} "
                         f"drifted from config.swing_qty={cfg_sq} — LOGGING "
                         f"ONLY (not halting; manual clamp via dashboard "
                         f"if needed)")
            except Exception:
                pass
            # Initial reconcile (safe now — stale ids cleared above).
            try:
                prod_trader.reconcile()
            except Exception as e:
                _log(f"[non-primary] {product_id} initial reconcile failed: "
                     f"{type(e).__name__}: {e}")
            # Per-product WebSocket feed for sub-second ticks. Non-blocking
            # start; we don't wait_for_first_tick (would serialize boot
            # across 10+ products). If no tick has arrived on a given loop
            # iteration, that product simply skips this tick.
            prod_feed = LiveTickerFeed(product_id)
            try:
                prod_feed.start()
            except Exception as e:
                # problem-scout #8: don't leak a started feed on partial init.
                try: prod_feed.stop()
                except Exception: pass
                raise
            track = _NonPrimaryTrack(product_id, prod_feed, prod_trader)
            _non_primary_tracks[product_id] = track
            _log(f"[non-primary] track online: {product_id} (feed started)")
            return track
        except Exception as e:
            _log(f"[non-primary] {product_id} track creation failed: "
                 f"{type(e).__name__}: {e}")
            return None

    def _maybe_resync_stale_feed(track) -> None:
        """Adam's 2026-07-14 marks-in-sync rule: never halt on stale data
        — aggressively re-sync. If the feed hasn't produced a fresh tick
        in FEED_STALE_THRESHOLD_SECS (and the track has been alive long
        enough for that to be diagnostic, not startup lag), tear down
        and restart the feed.

        problem-scout #2 (v2): use time.time() when we see any ticker
        (rather than parsing t['ts'] which is an ISO string from Coinbase
        that float() can't parse — would silently ValueError every tick)."""
        try:
            t = track.feed.latest_ticker()
            now_ts = time.time()
            if t is not None:
                # Fresh tick received (any ticker at all counts as "not
                # stale") — mark the seen-at time as now.
                track.last_tick_seen_ts = now_ts
            reference_ts = track.last_tick_seen_ts or track.last_step_ok_ts
            # Adam 2026-07-19 FIX: fresh-boot Tracks have both timestamps=0,
            # producing reference_ts=0, age=HUGE, and the code below then
            # immediately tears down + rebuilds the WS feed on the very
            # first tick. If new_feed.start() blocks (WS handshake), the
            # tick loop hangs on the first Track and NEVER reaches later
            # tracks (or the last_tick_attempt_ts assignment). Use spawn_ts
            # as the baseline when both step/tick timestamps are 0 — gives
            # a new feed its full staleness window to actually connect
            # before we consider replacing it.
            if reference_ts == 0:
                reference_ts = float(track.spawn_ts or now_ts)
            age = now_ts - reference_ts
            if age > FEED_STALE_THRESHOLD_SECS:
                _log(f"[non-primary] {track.product_id}: feed stale "
                     f"({age:.1f}s), restarting")
                try: track.feed.stop()
                except Exception: pass
                new_feed = LiveTickerFeed(track.product_id)
                new_feed.start()
                track.feed = new_feed
                # Give the new feed one full staleness window before we'd
                # decide to restart it again — prevents restart-storm on
                # a persistently broken feed.
                track.last_tick_seen_ts = now_ts
        except Exception as e:
            _log(f"[non-primary] {track.product_id}: feed re-sync failed: "
                 f"{type(e).__name__}: {e}")

    # Adam 2026-07-15: Track health auto-recovery. Prior to this, if a Track
    # got evicted (STEP_FAILURE_EVICT_THRESHOLD or feed error) the eviction
    # cooldown blocked re-spawn for 15 min. AFTER cooldown, nothing kicked
    # off a new spawn attempt — the product just sat silent forever unless
    # a new sleeve got armed (which triggers spawn via the arm-time path)
    # or Render restarted. HYF sat dead 9+ hours in 2026-07-15 for exactly
    # this reason; PT (Platinum) same class.
    #
    # This periodic check walks state + portfolio, finds every product with
    # an ARMED sleeve OR a held position that DOESN'T have a live Track,
    # and force-attempts _get_or_create_non_primary_track. Respects the eviction
    # cooldown (won't hammer a persistently failing spawn). Logs
    # track_silent_detected on find + track_auto_respawn_attempted on
    # each spawn attempt so operator gets proactive visibility.
    _last_track_health_check_ts = [0.0]  # box so nested funcs can mutate
    # Adam 2026-07-15: tightened from 60s → 15s. In a fast-moving market a
    # newly-dead Track could miss fills during the detection gap. 15s is
    # short enough to bound the exposure while keeping the state-walk cost
    # low (typically <5ms per check on a small tenant).
    TRACK_HEALTH_INTERVAL_SECS = float(os.getenv(
        "SWING_TRACK_HEALTH_INTERVAL_SECS", "15.0"))
    # Critical-detection interval — for products with HELD POSITIONS
    # (unprotected money vs unprotected opportunity). If mark moves against
    # a held long and there's no Track, the stop can't fire. Check every
    # 5s or every tick — whichever is longer.
    TRACK_HEALTH_CRITICAL_INTERVAL_SECS = float(os.getenv(
        "SWING_TRACK_HEALTH_CRITICAL_SECS", "5.0"))
    _last_track_health_critical_ts = [0.0]

    def _maybe_recover_dead_tracks() -> None:
        now = time.time()
        # Adam 2026-07-19 force-respawn signal: operator writes a list of
        # product_ids to CONFIG `__force_respawn__` in Redis. This reader
        # runs at the TOP of every _maybe_recover_dead_tracks call (which
        # itself runs every 50ms tick), evicts + immediately re-spawns.
        # Bypasses BOTH the 15-min eviction cooldown AND the interval gate
        # below — spawn happens synchronously in this call, not delayed
        # to the next 5s critical tick.
        try:
            _sig = store.get_config(TENANT, "__force_respawn__") or {}
        except Exception as _sig_err:
            _sig = {}
            try:
                log.record("force_respawn_signal_read_failed",
                           tenant=TENANT, error=f"{type(_sig_err).__name__}: {_sig_err}",
                           severity="warn")
            except Exception:
                pass
        _pids = _sig.get("product_ids") if isinstance(_sig, dict) else None
        if _pids and isinstance(_pids, list):
            for _pid in _pids:
                if not isinstance(_pid, str) or not _pid:
                    continue
                # Log every step explicitly so operator can see in trade
                # log EXACTLY what happened per pid (not swallowed by try).
                _evict_ok = False
                _evict_err = ""
                try:
                    _evict_track(_pid, "operator force-respawn signal")
                    _evict_ok = True
                except Exception as _e:
                    _evict_err = f"{type(_e).__name__}: {_e}"
                _non_primary_last_evict_ts.pop(_pid, None)
                _zombie_streak_map = getattr(
                    _maybe_recover_dead_tracks, "_zombie_streak", {})
                _zombie_streak_map.pop(_pid, None)
                setattr(_maybe_recover_dead_tracks,
                        "_zombie_streak", _zombie_streak_map)
                # Adam 2026-07-19 REVERT: don't spawn synchronously here.
                # Synchronous spawn can block if broker/feed init hangs,
                # which would freeze the entire tick loop (no heartbeat,
                # no ticks). Now we just clear the cooldown; the DOWNSTREAM
                # interval-gated spawn (5s critical / 15s regular) picks up
                # from the normal recovery path. Trades ~5s of extra
                # respawn latency for main-loop safety.
                try:
                    log.record(
                        "track_force_respawn_signal_honored",
                        tenant=TENANT, symbol=_pid,
                        evict_ok=_evict_ok, evict_error=_evict_err,
                        reason=("operator wrote __force_respawn__ signal — "
                                "evicted + cleared cooldown; spawn deferred "
                                "to normal interval gate (avoid main-loop "
                                "block from long spawn)"),
                        severity=("info" if _evict_ok else "warn"),
                    )
                except Exception:
                    pass
            # Clear the signal after processing so it isn't re-run.
            try:
                store.put_config(TENANT, "__force_respawn__",
                                 {"product_ids": [], "cleared_ts": now,
                                  "last_processed_pids": _pids})
            except Exception as _clr_err:
                try:
                    log.record("force_respawn_signal_clear_failed",
                               tenant=TENANT,
                               error=f"{type(_clr_err).__name__}: {_clr_err}",
                               severity="warn")
                except Exception:
                    pass

        # Adam 2026-07-15: two-tier detection. Held-position dead Tracks
        # are money-at-risk (stop can't fire without a Track) so they check
        # every CRITICAL_INTERVAL (5s). Armed-sleeve dead Tracks are
        # missed-opportunity (annoying but not dangerous) so they check
        # every regular INTERVAL (15s).
        do_critical = (now - _last_track_health_critical_ts[0] >=
                       TRACK_HEALTH_CRITICAL_INTERVAL_SECS)
        do_regular = (now - _last_track_health_check_ts[0] >=
                      TRACK_HEALTH_INTERVAL_SECS)
        if not do_critical and not do_regular:
            return
        if do_critical:
            _last_track_health_critical_ts[0] = now
        if do_regular:
            _last_track_health_check_ts[0] = now
        # Discover products that SHOULD be tracked but aren't.
        # Split into (a) held-position (critical) and (b) armed-sleeve (regular).
        should_track_critical: set[str] = set()
        should_track_regular: set[str] = set()
        # Held positions first — always considered critical.
        # __portfolio__ is written to the CONFIG scope (store.put_config in
        # main.py) and structured as {"derivatives": [{product_id, qty, ...}]}.
        # Previously used get_STATE + wrong key iteration — always returned {}
        # so should_track_critical was always empty (2026-07-19 fix).
        try:
            pf = store.get_config(TENANT, "__portfolio__") or {}
            for deriv in (pf.get("derivatives") or []):
                sym = deriv.get("product_id")
                if not sym or sym.startswith("__") or sym == SYMBOL:
                    continue
                if float(deriv.get("qty") or 0) != 0:
                    should_track_critical.add(sym)
            # Adam 2026-07-20 SPOT: only iterate crypto[] for products with
            # notional > $10 (filters out sub-cent dust: 3.26e-10 BTC that
            # blew HEALTH: 5→20 dead in the prior iteration attempt). Real
            # holdings like META (23 tokens ≈ $101) + HIGH (5000 tokens
            # ≈ $118) DO need critical-track status so §3.6 stop-loss
            # coverage isn't gated by the recovery cadence. META held
            # unprotected for 40+ min at 1 spawn/cycle behind 7 futures.
            try:
                for spot in (pf.get("crypto") or []):
                    sym = spot.get("product_id")
                    if not sym or sym.startswith("__") or sym == SYMBOL:
                        continue
                    if float(spot.get("value_usd") or 0) >= 10.0:
                        should_track_critical.add(sym)
            except Exception:
                pass
        except Exception:
            pass
        # Armed sleeves — regular priority (unless product already in critical)
        # Adam 2026-07-15: per-product try/except (was outer wrapper). Outer
        # wrapper caused the "only AVE detected" bug — if get_state raised
        # for BIT (third in sorted order), the exception bailed the whole
        # loop and everything after BIT was skipped. Per-product try isolates
        # a bad state entry so it doesn't drop all following products.
        try:
            symbols_to_scan = list(store.list_symbols(TENANT))
        except Exception as _e:
            symbols_to_scan = []
            try:
                log.record("track_health_list_symbols_failed",
                           tenant=TENANT, error=str(_e), severity="warn")
            except Exception:
                pass
        for sym in symbols_to_scan:
            if sym.startswith("__"):
                continue
            if sym == SYMBOL:
                continue
            if sym in should_track_critical:
                continue  # already flagged
            try:
                st = store.get_state(TENANT, sym) or {}
                sleeves = st.get("sleeves") or {}
                for ss in sleeves.values():
                    sstate = str(ss.get("state") or "")
                    if sstate in ("ARMED_BUY", "ARMED_SELL"):
                        should_track_regular.add(sym)
                        break
            except Exception as _sym_err:
                # One bad product must not skip all following ones — log the
                # failure per-symbol so the operator sees WHICH product is
                # corrupt without losing discovery for everyone else.
                try:
                    log.record("track_health_discovery_failed_per_symbol",
                               tenant=TENANT, symbol=sym,
                               error=f"{type(_sym_err).__name__}: {_sym_err}",
                               severity="warn")
                except Exception:
                    pass
                continue
        # Merge into single set for spawn attempts, gated by which interval
        # actually fired this iteration.
        should_track: set[str] = set()
        if do_critical:
            should_track |= should_track_critical
        if do_regular:
            should_track |= should_track_regular

        # For each product that should be tracked but isn't: log + attempt.
        # Adam 2026-07-15: also detect ZOMBIE Tracks — in _non_primary_tracks
        # but not producing ticks (WS feed silent). If last_step_ok_ts is
        # older than ZOMBIE_THRESHOLD_SECS, force eviction + fresh spawn.
        # Prior version only detected products NOT in the dict, which
        # missed the case where boot spawned a Track whose feed then never
        # produced a tick (silent zombie).
        # 2026-07-19: bumped 300→600s after twitter_scanner-blocking incident.
        # Any pre-existing OR future main-loop stall of ~5 min would nuke
        # every non-primary Track simultaneously; 10 min gives more room.
        # Env override: SWING_ZOMBIE_THRESHOLD_SECS.
        ZOMBIE_THRESHOLD_SECS = float(os.getenv("SWING_ZOMBIE_THRESHOLD_SECS", "600.0"))
        # Adam 2026-07-20 BOTTLENECK FIX #2: cap synchronous spawns to
        # ONE per _maybe_recover_dead_tracks call. Prior code spawned
        # every silent product sequentially. With 3+ dead (MC, XLP, ZEC),
        # each spawn blocked ~5-30s on CoinbaseBroker.contract_spec fetch
        # + WS handshake, stacking to 15-90s+ per recovery cycle. Combined
        # with the feed-restart bottleneck fix (5c413cc), the tick sweep
        # STILL couldn't run because both spawns and restarts hogged the
        # main thread.
        #
        # Simple budget: at most ONE new spawn per call. Additional dead
        # tracks wait for the next call (5s critical / 15s regular). Loop
        # cadence returns to design for the healthy majority. Full recovery
        # spreads across N recovery cycles (N = number of dead) instead
        # of blocking one cycle indefinitely.
        _spawns_this_call = 0
        _MAX_SPAWNS_PER_CALL = 1
        for pid in sorted(should_track):
            existing = _non_primary_tracks.get(pid)
            if existing is not None:
                # Adam 2026-07-15: check ONLY last_step_ok_ts — NOT
                # last_tick_seen_ts. _maybe_resync_stale_feed bumps
                # last_tick_seen_ts = now every time it restarts a stale
                # feed, which happens every FEED_STALE_THRESHOLD_SECS
                # (60s default) regardless of whether the feed actually
                # produces tickers. Using max() of both = false 'alive'
                # signal when the feed keeps restarting but step() never
                # runs (no ticker → tick sweep `continue`s before step).
                #
                # last_step_ok_ts only advances when
                # _track.trader.step(...) returns without raising. That's
                # the ONLY reliable heartbeat that the Track is actually
                # doing productive work.
                last_ok = float(getattr(existing, "last_step_ok_ts", 0) or 0)
                age = now - last_ok if last_ok > 0 else float("inf")
                if age < ZOMBIE_THRESHOLD_SECS:
                    continue  # actively stepping — not a zombie
                # ZOMBIE detected — force evict + respawn
                try:
                    log.record(
                        "track_zombie_detected",
                        tenant=TENANT, symbol=pid,
                        last_step_ok_age_secs=round(age, 1),
                        threshold_secs=ZOMBIE_THRESHOLD_SECS,
                        reason="Track in _non_primary_tracks but no ticks / no successful step in threshold window — WS feed died silently",
                        severity="critical",
                    )
                except Exception:
                    pass
                # Force-evict. For the FIRST few consecutive zombie
                # evictions on a product, clear cooldown so respawn fires
                # immediately. After N consecutive zombie evictions
                # without a successful step in between, stop clearing —
                # let the 15-min cooldown throttle the loop (HYF was
                # respawning every ~20s = burning API). Something is
                # persistently broken; slow down + let operator diagnose.
                try:
                    _evict_track(pid, "zombie: no ticks in threshold window")
                    # Adam 2026-07-15 REVERTED: had time.sleep(3.0) here but
                    # it BLOCKED the main tick loop — if 5 products got
                    # zombied in one cycle, we slept 15s total, during which
                    # OTHER Tracks went stale and became zombies. Cascading
                    # zombification. Now zero-delay respawn; the rate limit
                    # (3 quick then 15-min cooldown) is sufficient throttle.
                    # Track how many times each product has been zombie-
                    # evicted in a row (no successful step between).
                    _zombie_streak = getattr(_maybe_recover_dead_tracks,
                                              "_zombie_streak", {})
                    _zombie_streak[pid] = _zombie_streak.get(pid, 0) + 1
                    setattr(_maybe_recover_dead_tracks, "_zombie_streak",
                            _zombie_streak)
                    ZOMBIE_STREAK_COOLDOWN_THRESHOLD = 3
                    if _zombie_streak[pid] <= ZOMBIE_STREAK_COOLDOWN_THRESHOLD:
                        # Clear cooldown — try again immediately (transient)
                        _non_primary_last_evict_ts.pop(pid, None)
                    else:
                        # Persistent zombie — respect the 15-min cooldown
                        try:
                            log.record(
                                "track_zombie_persistent_slowdown",
                                tenant=TENANT, symbol=pid,
                                streak=_zombie_streak[pid],
                                threshold=ZOMBIE_STREAK_COOLDOWN_THRESHOLD,
                                reason=("respawning immediately isn't fixing "
                                        "this product; letting 15-min cooldown "
                                        "throttle the retry loop"),
                                severity="critical",
                            )
                        except Exception:
                            pass
                except Exception:
                    pass
                # Fall through to the spawn-attempt path below.
            last_evict = _non_primary_last_evict_ts.get(pid, 0.0)
            cooldown_remaining = (max(0.0, EVICT_COOLDOWN_SECS - (now - last_evict))
                                   if last_evict else 0.0)
            try:
                log.record(
                    "track_silent_detected",
                    tenant=TENANT, symbol=pid,
                    reason=("product has armed sleeve or held position but "
                            "no live Track in _non_primary_tracks"),
                    cooldown_remaining_secs=round(cooldown_remaining, 1),
                    severity=("warn" if cooldown_remaining > 0 else "critical"),
                )
            except Exception:
                pass
            if cooldown_remaining > 0:
                # Armed-sleeve-only products: respect the full 900s cooldown
                # to prevent rate-limit bans from rapid create/fail/evict loops.
                # Held-position products (critical): 15 min of zero stop-loss
                # coverage is the worse outcome — use a 60s floor so we retry
                # every minute rather than waiting the full eviction cooldown.
                if pid not in should_track_critical:
                    continue
                CRITICAL_EVICT_COOLDOWN_SECS = 60.0
                if (now - last_evict) < CRITICAL_EVICT_COOLDOWN_SECS:
                    continue
                try:
                    log.record(
                        "track_critical_cooldown_override",
                        tenant=TENANT, symbol=pid,
                        full_cooldown_remaining_secs=round(cooldown_remaining, 1),
                        reason=("held position — bypassing full evict cooldown; "
                                "stop-loss coverage outweighs rate-limit risk"),
                        severity="warn",
                    )
                except Exception:
                    pass
            # Spawn budget guard: only ONE spawn per call for NON-CRITICAL
            # tracks (armed-sleeve-only, no held position). Critical tracks
            # (held positions, in `should_track_critical`) bypass the budget:
            # every minute they're not tracked is a minute they're without
            # exchange stop-loss protection (§3.6). Adam 2026-07-20: META
            # held 23 tokens for 40+ min with STOP LOSS: NOT PLACED because
            # META was queued behind 7 other dead tracks at 1 spawn/cycle.
            # Rate-limit risk on spawn burst is acceptable — Coinbase's WS
            # + REST allow parallel handshakes; contract_spec is cached.
            if (_spawns_this_call >= _MAX_SPAWNS_PER_CALL
                    and pid not in should_track_critical):
                try:
                    log.record(
                        "track_spawn_deferred_budget",
                        tenant=TENANT, symbol=pid,
                        reason=("spawn budget exhausted this cycle — will "
                                "retry next recovery call. Prevents main-tick "
                                "block from stacking spawns. (Non-critical "
                                "only; held-position products always spawn.)"),
                        severity="info",
                    )
                except Exception:
                    pass
                continue
            # Attempt recovery via the existing spawn path (handles all guards
            # + failure paths). We don't bypass its checks — if config missing
            # or spawn fails, _get_or_create_non_primary_track returns None + logs it.
            track = _get_or_create_non_primary_track(pid)
            _spawns_this_call += 1
            try:
                log.record(
                    "track_auto_respawn_attempted",
                    tenant=TENANT, symbol=pid,
                    success=(track is not None),
                    severity=("info" if track is not None else "warn"),
                    reason=("auto-recovery from silent-Track detection"),
                )
            except Exception:
                pass

    def _account_broker():
        """Return any broker suitable for account-level Coinbase API calls
        (futures_balance, list_open_orders, snapshot). These don't need a
        specific product_id but CoinbaseBroker's constructor requires one.

        Prefers the primary broker if enabled. Otherwise reuses the first
        available non-primary track's broker. Returns None if no broker
        is available yet (early boot in no-primary mode with no tracks
        discovered) — caller must handle None.
        """
        if coinbase is not None:
            return coinbase
        for _tr in _non_primary_tracks.values():
            b = getattr(getattr(_tr, "trader", None), "b", None)
            if b is not None:
                return b
        return None

    def _evict_track(product_id: str, reason: str) -> None:
        track = _non_primary_tracks.pop(product_id, None)
        if track is None:
            return
        try: track.close()
        except Exception: pass
        # Record eviction time for the cooldown check in the create path
        # (problem-scout #3 v2 — prevents infinite create/evict loops).
        _non_primary_last_evict_ts[product_id] = time.time()
        _log(f"[non-primary] {product_id}: EVICTED ({reason}) — cooldown "
             f"{int(EVICT_COOLDOWN_SECS)}s before re-create attempt")
        try:
            _health.record_error(store, "non_primary_track", TENANT,
                                 RuntimeError(f"{product_id}: {reason}"))
        except Exception:
            pass

    # Boot-time state coherence check — prevents the 2026-07-14 SLR class of bug
    # where runtime state.swing_qty drifts above config.swing_qty and gets stuck
    # re-arming an unwanted position after cancellation. Only clamps in provably
    # safe conditions (no live position tracked, not mid-cycle). Notifies CRIT.
    # Skipped in no-primary mode — no primary trader to normalize.
    if trader is not None:
        try:
            from boot_state_normalizer import normalize_primary_swing_qty
            _r = normalize_primary_swing_qty(trader, log=log, notifier=notifier)
            if _r["drifted"]:
                _log(f"boot state normalize: {_r['reason']}")
        except Exception as e:
            _log(f"WARN: boot_state_normalizer failed: {type(e).__name__}: {e}")

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

    # 2026-07-19: orphan-order sweep at boot. Prevents the CHN/NER class
    # where a prior bot session left resting SELL orders untracked; when
    # the primary sell fires and closes position, the orphans flip us
    # SHORT. Only touches SELL orders on products in cfg; never BUYs.
    try:
        from main import _derive_live_tenant
        live_tenant = _derive_live_tenant(TENANT)
        n_orphan = _sweep_orphan_orders(store, live_tenant)
        if n_orphan:
            _log(f"startup orphan sweep: canceled {n_orphan} orphan SELL order(s)")
    except Exception as e:
        _log(f"WARN: startup orphan sweep failed: {type(e).__name__}: {e}")

    # 2026-07-19 Option D-1: long-horizon trend verdict boot refresh.
    # No-op when SWING_TREND_FILTER_ENABLED is off (default). When on,
    # fetches daily candles for each held product + caches a verdict
    # so the sleeve trend gate has fresh data on first tick.
    try:
        from main import _derive_live_tenant
        live_tenant = _derive_live_tenant(TENANT)
        n_trend = _refresh_all_trend_verdicts(store, live_tenant)
        if n_trend:
            _log(f"startup trend refresh: {n_trend} product(s) verdicts cached")
    except Exception as e:
        _log(f"WARN: startup trend refresh failed: {type(e).__name__}: {e}")
    last_trend_refresh = time.time()
    # Offset the funding watcher by 60s so bot startup isn't dominated by it.
    last_funding_poll = time.time() - FUNDING_POLL_SECS + 60.0
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
    # ScannerWorker accepts a symbol as a "seed"; in no-primary mode there
    # isn't one — pass empty string, ScannerWorker treats it as unset.
    scanner_worker = ScannerWorker(store, os.getenv("REDIS_URL") or None,
                                   SYMBOL if PRIMARY_ENABLED else "")

    # Candle/backtest job servicer — dashboard queues candle-fetch and backtest
    # requests to Redis; a background thread here services them so the /api/
    # candles endpoint doesn't hang. This USED to run only inside run_paper_mode;
    # when Adam suspended silver-swing-bot-paper (2026-07-14), the dashboard
    # chart went silent (queued jobs, no consumer). Live is now the sole
    # consumer. Thread — no impact on the trader tick loop.
    if os.getenv("REDIS_URL"):
        try:
            import backtest_worker
            backtest_worker.start(os.getenv("REDIS_URL"))
            _log("backtest_worker: started (services /api/candles + /api/backtest)")
        except Exception as e:
            _log(f"WARN: backtest_worker failed to start: {type(e).__name__}: {e}")

    # Primary feed — skipped in no-primary mode. Cadence then comes from
    # a fixed sleep in the main loop instead of feed poll.
    feed = LiveTickerFeed(SYMBOL) if PRIMARY_ENABLED else None
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        _log("SIGINT received — shutting down")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        if feed is not None:
            feed.start()
            if not feed.wait_for_first_tick(timeout=FEED_READY_TIMEOUT):
                _log("no ticks — check WS + product_id")
                return 1
            _log("feed live — starting main loop")
        else:
            _log("no primary feed — starting main loop with sleep cadence")
        log.record("bot_started", mode=("dry_run" if dry_run else "live"),
                   tenant=TENANT, symbol=(SYMBOL if PRIMARY_ENABLED else None))
        if trader is not None:
            trader.reconcile()

        last_snapshot = 0.0
        last_reconcile = time.time()  # [crew:#4] startup reconcile just ran
        last_expert_guard = time.time()  # [crew] expert-params drift guard
        last_sentinel = time.time()  # [crew] risk_sentinel periodic scan
        last_reconciliation = time.time()  # [crew] reconciliation_monitor
        last_family_check = time.time()  # already resolved on startup
        while not stopping:
            if feed is not None:
                t = feed.latest_ticker()
                if t is None:
                    time.sleep(0.1)
                    continue
                # problem-scout #6 (v2): wrap the primary step so a transient
                # error (e.g. Coinbase 500 during order_status) doesn't crash
                # the loop and take down every non-primary sibling track with
                # it via process restart. Mirror the non-primary wrapper.
                try:
                    trader.step(t["price"])
                except Exception as e:
                    _log(f"[primary] {SYMBOL} step failed: {type(e).__name__}: {e}")
                    try:
                        _health.record_error(store, "primary_step", TENANT, e,
                                             trade_log=log)
                    except Exception:
                        pass
            else:
                # No-primary mode: no feed to poll for cadence. Sleep briefly
                # so we don't burn CPU, then fall through to non-primary
                # track ticking. LOOP_INTERVAL_SECS default is 0.05s.
                time.sleep(max(LOOP_INTERVAL_SECS, 0.05))
            now = time.time()
            # 2026-07-14 full parity: tick each non-primary track on the
            # SAME loop cadence as the primary, using each product's own
            # WS-ticker price. Sub-second reactions for all products.
            # Wrapped so one bad product can never take down siblings or
            # the primary loop. Failure counter evicts traders that fail
            # STEP_FAILURE_EVICT_THRESHOLD ticks in a row.
            if TICK_NON_PRIMARY:
                # Adam 2026-07-20 TIMING INSTRUMENTATION: measure per-section
                # duration so diag_tick_cadence can show WHERE the loop is
                # spending time. All fields stashed on _maybe_recover_dead_
                # tracks as attrs, picked up by the heartbeat below.
                import time as _timing
                _sec_ts = {}
                _sec_ts["iter_start"] = _timing.time()
                # Adam 2026-07-15: auto-recover any dead Tracks BEFORE the
                # tick sweep so a fresh spawn attempt goes through the tick
                # loop this iteration instead of waiting one more cycle.
                try:
                    _t0 = _timing.time()
                    _maybe_recover_dead_tracks()
                    _sec_ts["recovery_ms"] = int((_timing.time() - _t0) * 1000)
                except Exception as _rerr:
                    _sec_ts["recovery_ms"] = int((_timing.time() - _t0) * 1000)
                    _log(f"[non-primary] track health check failed: "
                         f"{type(_rerr).__name__}: {_rerr}")
                setattr(_maybe_recover_dead_tracks, "_last_iter_timing", _sec_ts)
                # Adam 2026-07-19: periodic tick-cadence heartbeat. Writes
                # each Track's tick_count + last_step_ok_ts + spawn_ts to
                # Redis every 5s so diag_tick_cadence.py can read them
                # cross-process. Includes prev_tick_count + prev_ts so the
                # reader can compute instant tick_rate (delta / interval)
                # in addition to lifetime average.
                try:
                    _hb_last = getattr(
                        _maybe_recover_dead_tracks, "_hb_last_ts", 0.0)
                    if now - _hb_last >= 5.0:
                        _hb_prev = getattr(
                            _maybe_recover_dead_tracks, "_hb_snap", {}) or {}
                        _hb_snap = {}
                        for _pid, _tr in _non_primary_tracks.items():
                            _prev = _hb_prev.get(_pid, {}) or {}
                            _hb_snap[_pid] = {
                                "tick_count": int(getattr(_tr, "tick_count", 0) or 0),
                                "last_step_ok_ts": float(_tr.last_step_ok_ts or 0),
                                "last_tick_seen_ts": float(_tr.last_tick_seen_ts or 0),
                                "spawn_ts": float(_tr.spawn_ts or 0),
                                "consecutive_step_failures":
                                    int(_tr.consecutive_step_failures or 0),
                                "prev_tick_count": int(_prev.get("tick_count", 0) or 0),
                                "prev_snap_ts": float(_prev.get("snap_ts", 0) or 0),
                                "snap_ts": now,
                                # Per-tick diagnostics — tells us WHY a
                                # Track isn't advancing tick_count
                                "last_tick_attempt_ts":
                                    float(getattr(_tr, "last_tick_attempt_ts", 0) or 0),
                                "last_tick_reason":
                                    str(getattr(_tr, "last_tick_reason", "") or ""),
                            }
                        try:
                            store.put_config(TENANT, "__track_heartbeat__",
                                             {"tracks": _hb_snap, "snap_ts": now,
                                              "loop_interval_secs": LOOP_INTERVAL_SECS})
                            setattr(_maybe_recover_dead_tracks, "_hb_snap", _hb_snap)
                            setattr(_maybe_recover_dead_tracks, "_hb_last_ts", now)
                        except Exception:
                            pass  # never fail-hard on heartbeat write
                except Exception:
                    pass
                _to_evict = []
                # Adam 2026-07-20 BOTTLENECK FIX: cap synchronous feed
                # restarts to ONE per tick sweep. Prior code called
                # _maybe_resync_stale_feed unconditionally for every
                # track. If 3+ feeds were stale simultaneously (MC + XLP +
                # ZEC), each restart blocked ~30s on the WS handshake →
                # 90s+ sequential block → entire fleet ticked at ~0.01 tps
                # (observed 92s between ticks across all 9 healthy tracks
                # per diag_tick_cadence output). Healthy majority held
                # hostage by the dead minority.
                #
                # Simple budget: track ages sorted by staleness; call
                # _maybe_resync_stale_feed for at most 1 track per sweep.
                # Additional stale feeds wait for the next sweep. Loop
                # cadence returns to ~50ms design for the healthy majority
                # while stale feeds recover one-per-tick.
                _now_sweep = now
                _stale_candidates = []  # (age, pid, track)
                _FEED_STALE_HINT_SECS = 45.0  # slightly under FEED_STALE_THRESHOLD_SECS
                for _pid, _track in list(_non_primary_tracks.items()):
                    _t_seen = float(getattr(_track, "last_tick_seen_ts", 0) or 0)
                    _t_ok = float(getattr(_track, "last_step_ok_ts", 0) or 0)
                    _t_spawn = float(getattr(_track, "spawn_ts", 0) or 0)
                    _ref = _t_seen or _t_ok or _t_spawn or _now_sweep
                    _age = _now_sweep - _ref
                    if _age > _FEED_STALE_HINT_SECS:
                        _stale_candidates.append((_age, _pid, _track))
                # Only restart the ONE oldest-stale feed this sweep.
                if _stale_candidates:
                    _stale_candidates.sort(reverse=True)
                    _oldest_age, _oldest_pid, _oldest_track = _stale_candidates[0]
                    try:
                        _maybe_resync_stale_feed(_oldest_track)
                    except Exception as _rsr:
                        _log(f"[non-primary] {_oldest_pid} single-restart "
                             f"attempt failed: {type(_rsr).__name__}: {_rsr}")
                for _pid, _track in list(_non_primary_tracks.items()):
                    _track.last_tick_attempt_ts = now
                    _tt = _track.feed.latest_ticker()
                    if _tt is None:
                        _track.last_tick_reason = "feed_no_ticker"
                        continue  # WS still spinning up, skip this iter
                    try:
                        _track.trader.step(float(_tt["price"]))
                        _track.consecutive_step_failures = 0
                        # Don't advance the zombie heartbeat for a HALTED trader.
                        # A halted step() returns immediately without doing work,
                        # so updating last_step_ok_ts would mask it from zombie
                        # detection forever. Without this guard, a halted track
                        # holds a WS connection indefinitely (2026-07-16 MEDIUM-9).
                        try:
                            _step_halted = (
                                getattr(getattr(_track.trader, "s", None),
                                        "state", None) == "HALTED"
                            )
                        except Exception:
                            _step_halted = False
                        if not _step_halted:
                            _track.last_step_ok_ts = now
                            _track.tick_count += 1
                            _track.last_tick_reason = "step_ok"
                        else:
                            _track.last_tick_reason = "step_halted"
                        # Adam 2026-07-15: successful step resets the
                        # zombie streak counter — a product that eventually
                        # starts ticking correctly shouldn't be penalized
                        # for prior zombie evictions.
                        try:
                            _zombie_streak = getattr(
                                _maybe_recover_dead_tracks,
                                "_zombie_streak", {})
                            if _pid in _zombie_streak:
                                del _zombie_streak[_pid]
                        except Exception:
                            pass
                    except Exception as e:
                        _track.consecutive_step_failures += 1
                        _track.last_tick_reason = f"step_error:{type(e).__name__}"
                        _log(f"[non-primary] {_pid} step failed "
                             f"({_track.consecutive_step_failures}/"
                             f"{STEP_FAILURE_EVICT_THRESHOLD}): "
                             f"{type(e).__name__}: {e}")
                        # Adam 2026-07-19: also record to trade log so
                        # diag_silent_product_events / diag_throttle_check
                        # can see step errors. Prior code only wrote to the
                        # process log which is invisible to diags.
                        try:
                            log.record(
                                "non_primary_step_failed",
                                tenant=TENANT, symbol=_pid,
                                error=f"{type(e).__name__}: {e}",
                                consecutive_failures=_track.consecutive_step_failures,
                                threshold=STEP_FAILURE_EVICT_THRESHOLD,
                                severity="warn",
                            )
                        except Exception:
                            pass
                        if _track.consecutive_step_failures >= STEP_FAILURE_EVICT_THRESHOLD:
                            _to_evict.append(_pid)
                for _pid in _to_evict:
                    _evict_track(_pid, f"{STEP_FAILURE_EVICT_THRESHOLD} consecutive step failures")
            # Periodic front-month recheck. If Coinbase has rolled the family,
            # halt with a clear reason so the next restart picks up the new
            # symbol. We don't hot-swap the WS feed mid-session (real risk of
            # state confusion between old and new orders) — restart is safer.
            if SYMBOL_FAMILY and now - last_family_check >= FAMILY_RECHECK_SECS:
                last_family_check = now
                try:
                    from roll import resolve_front_month
                    latest = resolve_front_month(coinbase, SYMBOL_FAMILY, fallback=SYMBOL)
                    # Stamp last_ok_ts BEFORE the roll-branch break. Auditor
                    # 2026-07-14 15:35: the daily audit uses last_ok_ts
                    # staleness as a liveness check; a clean roll must still
                    # stamp OK or front_month gets false-flagged as dead
                    # during a normal contract rotation.
                    _health.record_ok(store, "front_month", TENANT)
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
                if trader is not None:
                    try:
                        trader.reconcile()
                        _health.record_ok(store, "reconcile", TENANT)
                    except Exception as e:
                        _log(f"periodic reconcile failed: {type(e).__name__}: {e}")
                        _health.record_error(store, "reconcile", TENANT, e, trade_log=log)
                # 2026-07-14 non-primary reconcile — same guarantee the
                # primary gets: credit fills that happened outside the step
                # loop (manual trades on Coinbase, orders that filled between
                # ticks). Each product wrapped so one bad reconcile can never
                # take down siblings.
                if TICK_NON_PRIMARY:
                    for _pid, _tr in list(_non_primary_tracks.items()):
                        try:
                            _tr.trader.reconcile()
                        except Exception as e:
                            _log(f"[non-primary] {_pid} periodic reconcile "
                                 f"failed: {type(e).__name__}: {e}")
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
                # C-2 fix: margin sentinel wired into the live loop.
                # margin_sentinel.py had correct logic but was never called
                # here; live margin utilization and cluster liquidation
                # headroom were completely unmonitored. Runs every
                # SENTINEL_INTERVAL_SECS alongside risk_sentinel.
                try:
                    import margin_sentinel
                    _lt = TENANT  # TENANT is guardrailed to end with -live (line 304)
                    _pf = store.get_config(_lt, "__portfolio__") or {}
                    # Portfolio mark staleness check: _refresh_ts is written by
                    # refresh_portfolio_snapshot every PORTFOLIO_REFRESH_SECS (2s).
                    # A gap > 30s means the REST mark is stale — price-triggered
                    # actions (TP, trail, stop) may fire on a frozen price.
                    # Stale-mark alert moved to the 2s portfolio-refresh block
                    # (runs after its try/except) so it fires within seconds of
                    # a feed failure rather than waiting up to 300s here.
                    _ms_positions = []
                    for _d in (_pf.get("derivatives") or []):
                        _dpid = _d.get("product_id")
                        if not _dpid or not _d.get("mark"):
                            continue
                        _cfg_d = store.get_config(_lt, _dpid) or {}
                        _ms_positions.append({
                            "symbol": _dpid,
                            "side": _d.get("side", "BUY"),
                            "qty": float(_d.get("qty") or 0),
                            "avg_entry": float(_d.get("avg_entry") or 0),
                            "mark": float(_d.get("mark") or 0),
                            "contract_size": float(_cfg_d.get("contract_size") or 1),
                            "margin_per_contract": float(_cfg_d.get("margin_per_contract") or 0),
                            # Pass Coinbase's liquidation_price so margin_sentinel can
                            # compute headroom even for auto-seeded products that have
                            # margin_per_contract=0 in their config.
                            "liquidation_price": float(_d.get("liquidation_price") or 0),
                        })
                    if _ms_positions:
                        _acc_b = _account_broker()
                        if _acc_b is None:
                            _log("[margin-sentinel] skipped — no broker available "
                                 "for account balance call")
                            continue
                        _bal = _acc_b.futures_balance()
                        _cfm = 0.0
                        try:
                            _cfm = float((_bal.get("cfm_usd_balance") or {}).get("value") or 0)
                        except (TypeError, ValueError):
                            pass
                        _ms = margin_sentinel.margin_report(
                            _ms_positions, _cfm, warn_distance_pct=20.0)
                        for _a in (_ms.get("alerts") or []):
                            _log(f"[margin-sentinel] {_a['severity'].upper()}: {_a['detail']}")
                            try:
                                from alerting import Priority
                                _prio = Priority.CRIT if _a["severity"] == "critical" else Priority.HIGH
                                notifier.send("margin sentinel", _a["detail"], _prio)
                            except Exception:
                                pass
                        if _ms.get("blind_positions"):
                            _blind_syms = ", ".join(_ms["blind_positions"])
                            _log(f"[margin-sentinel] WARN: {len(_ms['blind_positions'])} position(s) "
                                 f"excluded from margin calc (no margin data): {_blind_syms}")
                        if _ms.get("alerts"):
                            try:
                                log.record(
                                    "margin_sentinel_alert", tenant=TENANT,
                                    utilization_pct=_ms.get("utilization_pct"),
                                    nearest_distance_to_liq_pct=_ms.get("nearest_distance_to_liq_pct"),
                                    verdict=_ms.get("verdict"),
                                    alerts=_ms.get("alerts"),
                                    blind_positions=_ms.get("blind_positions"),
                                    severity=("critical"
                                              if any(_a["severity"] == "critical"
                                                     for _a in _ms["alerts"])
                                              else "high"),
                                )
                            except Exception:
                                pass
                except Exception as _mse:
                    _log(f"margin_sentinel failed: {type(_mse).__name__}: {_mse}")
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
            # 2026-07-19 Option D-1: long-horizon trend verdict periodic
            # refresh. No-op when SWING_TREND_FILTER_ENABLED is off.
            if now - last_trend_refresh >= TREND_REFRESH_SECS:
                last_trend_refresh = now
                try:
                    from main import _derive_live_tenant
                    _lt = _derive_live_tenant(TENANT)
                    n_tr = _refresh_all_trend_verdicts(store, _lt)
                    if n_tr:
                        _log(f"periodic trend refresh: {n_tr} product(s) refreshed")
                except Exception as e:
                    _log(f"periodic trend refresh failed: {type(e).__name__}: {e}")
            # Portfolio circuit breaker — Van Tharp 'stop trading when things
            # go wrong'. Runs on the same cadence as snapshot so it can see
            # fresh mark prices when computing unrealized. Cheap: single
            # aggregation over all sleeves in this tenant.
            # [crew 2026-07-14] reconciliation_monitor — read-only defense.
            # Diffs Coinbase state (positions from __portfolio__ snapshot)
            # against bot sleeve state; flags duplicate orders, position
            # mismatches, stale entries. Never cancels — notifies + logs.
            # Would have caught today's SLR ghost automatically
            # (state.swing_qty=2 vs Coinbase position=1 → position_mismatch
            # critical). See reconciliation_monitor.py + AGENTS.md.
            if now - last_reconciliation >= RECONCILIATION_INTERVAL_SECS:
                last_reconciliation = now
                try:
                    import reconciliation_monitor as rmon
                    live_tenant = f"{TENANT}-live" if not TENANT.endswith("-live") else TENANT
                    pf = (store.get_config(live_tenant, "__portfolio__") or {})
                    exch_positions: dict[str, float] = {}
                    for d in (pf.get("derivatives") or []):
                        pid = d.get("product_id")
                        if pid:
                            exch_positions[pid] = abs(float(d.get("qty") or 0))
                    sleeves_data = []
                    try:
                        syms = store.list_symbols(live_tenant) or []
                    except Exception:
                        syms = []
                    for sym in syms:
                        if sym.startswith("__"):
                            continue
                        st = store.get_state(live_tenant, sym) or {}
                        cfg = store.get_config(live_tenant, sym) or {}
                        # PRIMARY row — the tenant's own swing strategy, if any.
                        # Includes core_qty in expected_position because core
                        # contracts ARE held on the exchange but no bot piece
                        # trades them. Without this, position_mismatch would
                        # false-alarm on every product with a core holding.
                        core_qty = int(cfg.get("core_qty") or 0)
                        prim_state_str = str(st.get("state") or "")
                        prim_qty = int(st.get("swing_qty") or 0) if prim_state_str == "ARMED_SELL" else 0
                        sleeves_data.append({
                            "symbol": sym,
                            "expected_position": prim_qty + core_qty,
                            "armed": prim_state_str in ("ARMED_SELL", "ARMED_BUY"),
                            "side": "SELL" if prim_state_str == "ARMED_SELL" else "BUY",
                            "state": prim_state_str,
                            "live_order_id": st.get("live_order_id"),
                            "armed_at": st.get("last_heartbeat_ts"),
                            "last_sale_px": st.get("last_sell_fill_price"),
                        })
                        # PER-SLEEVE rows — each sleeve holds its own qty of
                        # contracts when ARMED_SELL. Include so position_mismatch
                        # reflects the real total bot-managed position.
                        sleeve_states = st.get("sleeves") or {}
                        for s_cfg in (cfg.get("sleeves") or []):
                            sid = s_cfg.get("id")
                            s_st = (sleeve_states.get(sid) or {}) if sid else {}
                            s_state_str = str(s_st.get("state") or "")
                            s_qty = int(s_cfg.get("qty") or 0)
                            sleeves_data.append({
                                "symbol": sym,
                                "expected_position": s_qty if s_state_str == "ARMED_SELL" else 0,
                                "armed": s_state_str in ("ARMED_SELL", "ARMED_BUY"),
                                "side": "SELL" if s_state_str == "ARMED_SELL" else "BUY",
                                "state": s_state_str,
                                "live_order_id": s_st.get("live_order_id"),
                                # Adam 2026-07-20: include resting_stop_oid so
                                # reconciliation_monitor.check_orphans_and_missing
                                # can see it as a tracked order. Prior code only
                                # included live_order_id, so every real resting
                                # stop got reported as "orphan" — drowning
                                # genuine orphans in noise.
                                "resting_stop_oid": s_st.get("resting_stop_oid"),
                                "armed_at": s_st.get("armed_buy_since_ts"),
                                "last_sale_px": s_st.get("last_sell_fill_price"),
                            })
                    # Fetch open orders from Coinbase via broker.list_open_orders
                    # so reconciliation_monitor's duplicate_order + orphan_order
                    # checks have data to work with. Fail-safe: on any exception,
                    # pass empty and rely on position_mismatch + stale_entry.
                    open_orders_data: list[dict] = []
                    try:
                        _acc_b = _account_broker()
                        list_orders_fn = getattr(_acc_b, "list_open_orders", None) if _acc_b else None
                        if callable(list_orders_fn):
                            open_orders_data = list_orders_fn() or []
                    except Exception as _e:
                        _log(f"reconciliation_monitor: list_open_orders failed: {_e}")
                    # Build state-vs-config drift pairs — auditor 2026-07-14
                    # SLR-incident agenda item. Bot's runtime state.swing_qty
                    # can drift from config.swing_qty (e.g. after a scale-up
                    # or a config change made while bot was down). This check
                    # would have caught the SLR ghost automatically.
                    state_config_pairs = []
                    for sym in syms:
                        if sym.startswith("__"):
                            continue
                        st = store.get_state(live_tenant, sym) or {}
                        cfg = store.get_config(live_tenant, sym) or {}
                        state_config_pairs.append({
                            "symbol": sym,
                            "state_swing_qty": st.get("swing_qty"),
                            "config_swing_qty": cfg.get("swing_qty"),
                        })
                    findings = rmon.reconcile(
                        open_orders=open_orders_data,
                        exch_positions=exch_positions,
                        sleeves=sleeves_data,
                        now_ts=now,
                        state_config_pairs=state_config_pairs,
                    )
                    alert = rmon.format_alert(findings)
                    if alert:
                        _log(f"RECONCILIATION FINDINGS:\n{alert}")
                        try:
                            from alerting import Priority
                            crit = any(f.severity == "critical" for f in findings)
                            notifier.send("reconciliation_monitor",
                                          alert,
                                          Priority.HIGH if crit else Priority.NORMAL)
                        except Exception:
                            pass
                        # Also record to trade log so the daily audit sees it.
                        try:
                            for f in findings:
                                log.record(f"reconciliation_{f.kind}",
                                           severity=f.severity,
                                           symbol=f.symbol,
                                           detail=f.detail)
                        except Exception:
                            pass
                    # Auto-correct state_config_drift: when state.swing_qty >
                    # config.swing_qty AND exchange position = 0, the bot would
                    # keep re-arming to sell contracts it doesn't hold until Redis
                    # is manually fixed. Safe to zero out here — we only act when
                    # the exchange confirms no position (double-corroborated by
                    # position_mismatch=0 for that symbol). 2026-07-16 incident.
                    #
                    # SAFETY GATE: a Coinbase partial-response (HTTP 200 but one
                    # product missing from the derivatives list) sets _refresh_ok=False
                    # in main.refresh_portfolio_snapshot. If the snapshot is stale or
                    # incomplete, treat "absent from snapshot" as UNKNOWN — not zero.
                    # Skip both autocorrectors entirely to avoid clearing real positions.
                    _pf_ok = pf.get("_refresh_ok", True)  # True = never refreshed yet (fresh boot, fail-open once)
                    _pf_ts = float(pf.get("_refresh_ts") or 0)
                    _pf_age = (now - _pf_ts) if _pf_ts else None
                    _pf_fresh = _pf_ok and (_pf_age is None or _pf_age < PORTFOLIO_REFRESH_SECS * 10)
                    if not _pf_fresh:
                        _log(f"[reconcile-autocorrect] snapshot not fresh "
                             f"(_refresh_ok={_pf_ok}, age={_pf_age:.0f}s if known) — "
                             f"skipping autocorrect to avoid false ghost-clear")
                    try:
                        import state_autocorrect
                        drift_syms = {f.symbol for f in findings if f.kind == "state_config_drift"}
                        for _dsym in drift_syms:
                            _dst = store.get_state(live_tenant, _dsym) or {}
                            _dcfg = store.get_config(live_tenant, _dsym) or {}
                            _exch_qty = int(exch_positions.get(_dsym, 0) or 0)
                            _sym_present = _dsym in exch_positions
                            _ok, _new_sq, _why = state_autocorrect.should_autocorrect(
                                _dst, _dcfg, _exch_qty,
                                snapshot_fresh=_pf_fresh,
                                symbol_present_in_snapshot=_sym_present,
                            )
                            if not _ok:
                                if _why == "snapshot_stale_and_symbol_absent":
                                    _log(f"[reconcile-autocorrect] {_dsym}: skipping — "
                                         f"snapshot not fresh + symbol absent from exch")
                                continue
                            _old_sq = int(_dst.get("swing_qty") or 0)
                            _dst["swing_qty"] = _new_sq
                            store.put_state(live_tenant, _dsym, _dst)
                            # Also push a state_patch so the SwingTrader
                            # applies the correction to its in-memory
                            # self.s.swing_qty on the next tick — otherwise
                            # the trader's _save_state clobbers our Redis
                            # write with its stale in-memory value, and
                            # the autocorrect fires again every sweep
                            # (MC-17SEP26-CDE re-inflation loop, 2026-07-19).
                            try:
                                if hasattr(store, "put_state_patch"):
                                    store.put_state_patch(live_tenant, _dsym, {
                                        "top_level": {"swing_qty": _new_sq},
                                        "reason": (f"state_config_drift_autocorrect: "
                                                   f"swing_qty {_old_sq}→{_new_sq} "
                                                   f"(exch={_exch_qty}, cfg={_dcfg.get('swing_qty')})"),
                                        "ts": int(now),
                                    })
                            except Exception:
                                pass
                            _log(f"[reconcile-autocorrect] {_dsym}: state.swing_qty "
                                 f"{_old_sq}→{_new_sq} (exchange={_exch_qty}, "
                                 f"config={_dcfg.get('swing_qty')})")
                            try:
                                log.record("state_config_drift_autocorrected",
                                           tenant=TENANT, symbol=_dsym,
                                           old_swing_qty=_old_sq, new_swing_qty=_new_sq,
                                           exchange_qty=_exch_qty,
                                           config_swing_qty=_dcfg.get("swing_qty"),
                                           severity="warn")
                            except Exception:
                                pass
                    except Exception as _dce:
                        _log(f"[reconcile-autocorrect] failed: {type(_dce).__name__}: {_dce}")
                    # Auto-correct position_mismatch (ghost positions): exchange=0
                    # but state=ARMED_SELL with swing_qty>0 and no live order.
                    # Without this the primary loop spins on RuntimeError from
                    # no_short_check every tick (2026-07-16 SLVR/AVE/HYF incident).
                    # Only acts when exchange confirms 0 AND no live_order_id — a
                    # live order in flight means the fill might land any second.
                    try:
                        mismatch_syms = {f.symbol for f in findings if f.kind == "position_mismatch"}
                        for _msym in mismatch_syms:
                            # treat "not in exch_positions" as qty=0, but ONLY
                            # when the portfolio snapshot is confirmed fresh and
                            # complete. A Coinbase partial-200 could silently omit
                            # a real position — skip the clear in that case.
                            if not _pf_fresh and _msym not in exch_positions:
                                _log(f"[reconcile-autocorrect] {_msym}: skipping "
                                     f"ghost-clear (snapshot not fresh, absence is "
                                     f"unconfirmed)")
                                continue
                            _exch_qty = exch_positions.get(_msym, 0)
                            if _exch_qty != 0:
                                continue  # only ghosts (exchange confirmed 0)
                            _mst = store.get_state(live_tenant, _msym) or {}
                            if _mst.get("live_order_id"):
                                continue  # order in flight — don't touch
                            _mstate = str(_mst.get("state") or "").upper()
                            _msq = int(_mst.get("swing_qty") or 0)
                            if _mstate == "ARMED_SELL" and _msq > 0:
                                _mst["swing_qty"] = 0
                                _mst["state"] = "HALTED"
                                _mst["live_order_id"] = None
                                _mst["halt_reason"] = (
                                    f"ghost position auto-cleared: exchange=0 "
                                    f"but state=ARMED_SELL qty={_msq} (2026-07-16)"
                                )
                                store.put_state(live_tenant, _msym, _mst)
                                # Push state_patch so the trader's in-memory
                                # copy adopts the correction on the next tick
                                # instead of clobbering our Redis write.
                                # Only scalar fields — `state` is a State enum
                                # in memory; setattr with a raw string would
                                # break equality checks against State.HALTED.
                                # Setting swing_qty=0 + live_order_id=None is
                                # enough to stop the sell-vs-nothing loop; the
                                # trader will see the persisted HALTED state
                                # on its next full state load / restart.
                                try:
                                    if hasattr(store, "put_state_patch"):
                                        store.put_state_patch(live_tenant, _msym, {
                                            "top_level": {
                                                "swing_qty": 0,
                                                "live_order_id": None,
                                            },
                                            "reason": (f"position_mismatch_autocorrect: "
                                                       f"ghost clear, exch=0, "
                                                       f"was ARMED_SELL qty={_msq}"),
                                            "ts": int(now),
                                        })
                                except Exception:
                                    pass
                                _log(f"[reconcile-autocorrect] {_msym}: ghost cleared "
                                     f"(exchange=0, ARMED_SELL qty={_msq}→0, now HALTED)")
                                try:
                                    log.record("position_mismatch_autocorrected",
                                               tenant=TENANT, symbol=_msym,
                                               old_swing_qty=_msq, new_swing_qty=0,
                                               old_state="ARMED_SELL", new_state="HALTED",
                                               severity="warn")
                                except Exception:
                                    pass
                    except Exception as _mce:
                        _log(f"[reconcile-autocorrect] mismatch pass failed: "
                             f"{type(_mce).__name__}: {_mce}")
                    # Sleeve-level ghost auto-recover (2026-07-20).
                    # Adam: "I dont want to keep clearing ghost I dont want
                    # any more fucking ghosts." — feedback_no_ghost_sleeves.md
                    #
                    # Ghost sleeve = state.sleeves[sid].own_avg_entry is set
                    # (bot thinks it holds) but Coinbase reports position=0
                    # for that product. Two flavors we auto-fix here:
                    #   (a) HALTED sleeve with dead resting_stop_oid — the
                    #       halt-loop where Resume re-fires the credit path
                    #       against the stale oid and re-halts (SLR incident).
                    #   (b) ARMED_SELL sleeve with own_avg set + exchange=0 —
                    #       stop fired, primary credit path missed (missed
                    #       fill class).
                    #
                    # Auto-fix: reset sleeve to ARMED_BUY, clear own_avg +
                    # resting_stop_* + halt_reason, stamp armed_buy_since_ts.
                    # Preserves cycles/realized_pnl. Requires either:
                    #   - snapshot fresh AND symbol present with qty=0, or
                    #   - snapshot fresh AND symbol absent (confirmed zero)
                    # Skips when a buy is in flight (live_order_id set).
                    #
                    # Recovery of missed P&L is NOT attempted here (would
                    # require per-product broker calls in the reconcile
                    # loop). If a stop actually filled uncredited, Adam
                    # sees the "ghost auto-recovered w/ stale stop_oid"
                    # log line and can run diag_force_credit_cycle.py to
                    # backfill the specific cycle. Otherwise the reload-on-
                    # tick fix (83dd31b) makes the recovered state visible
                    # to the trader within ~1s.
                    try:
                        for _gsym in list(exch_positions.keys()) + list(
                            {f.symbol for f in findings
                             if f.kind in ("position_mismatch", "state_config_drift")}
                        ):
                            if _gsym.startswith("__"):
                                continue
                            if not _pf_fresh and _gsym not in exch_positions:
                                continue  # can't verify — snapshot stale
                            _gexch = int(exch_positions.get(_gsym, 0) or 0)
                            if _gexch != 0:
                                continue  # real position — not a ghost candidate
                            _gst = store.get_state(live_tenant, _gsym) or {}
                            _sleeves = _gst.get("sleeves") or {}
                            if not isinstance(_sleeves, dict) or not _sleeves:
                                continue
                            _dirty_ids: list[str] = []
                            for _sid, _ss in list(_sleeves.items()):
                                if not isinstance(_ss, dict):
                                    continue
                                _own_avg = _ss.get("own_avg_entry")
                                _has_avg = _own_avg not in (None, 0, 0.0)
                                _sstate = str(_ss.get("state") or "").upper()
                                _dead_stop = (_sstate == "HALTED"
                                              and _ss.get("resting_stop_oid"))
                                _ghost_hold = (_has_avg
                                               and _sstate == "ARMED_SELL")
                                if not (_has_avg or _dead_stop or _ghost_hold):
                                    continue
                                if _ss.get("live_order_id"):
                                    continue  # buy in flight — wait
                                _prev_avg = _own_avg
                                _prev_state = _sstate
                                _prev_oid = _ss.get("resting_stop_oid")
                                _prev_halt = _ss.get("halt_reason")
                                if _prev_halt:
                                    _ss["_prev_halt_reason"] = _prev_halt
                                if _prev_avg is not None:
                                    _ss["_prev_ghost_own_avg"] = _prev_avg
                                _ss["state"] = "ARMED_BUY"
                                _ss["own_avg_entry"] = None
                                _ss["resting_stop_oid"] = None
                                _ss["resting_stop_px"] = None
                                _ss["resting_stop_stage"] = None
                                _ss["halt_reason"] = None
                                _ss["armed_buy_since_ts"] = now
                                _sleeves[_sid] = _ss
                                _dirty_ids.append(_sid)
                                _log(f"[reconcile-autocorrect] {_gsym}/{_sid}: "
                                     f"sleeve ghost auto-recovered "
                                     f"(exch=0, was state={_prev_state} "
                                     f"own_avg={_prev_avg} stop_oid={_prev_oid})")
                                try:
                                    log.record("sleeve_ghost_auto_recovered",
                                               tenant=TENANT, symbol=_gsym,
                                               sleeve_id=_sid,
                                               prev_state=_prev_state,
                                               prev_own_avg=_prev_avg,
                                               prev_stop_oid=_prev_oid,
                                               prev_halt_reason=_prev_halt,
                                               severity="warn",
                                               reason=("bot state said held "
                                                       "but exchange=0; auto-"
                                                       "cleared per feedback_"
                                                       "no_ghost_sleeves"))
                                except Exception:
                                    pass
                            if _dirty_ids:
                                _gst["sleeves"] = _sleeves
                                store.put_state(live_tenant, _gsym, _gst)
                                # reload-on-tick (swing_leg._reload_sleeves_
                                # from_redis) picks this up within ~1s;
                                # no state_patch needed for sleeves.
                    except Exception as _gce:
                        _log(f"[reconcile-autocorrect] sleeve ghost pass failed: "
                             f"{type(_gce).__name__}: {_gce}")
                    _health.record_ok(store, "reconciliation_monitor", TENANT)
                except Exception as e:
                    _log(f"reconciliation_monitor failed: {type(e).__name__}: {e}")
                    _health.record_error(store, "reconciliation_monitor", TENANT, e, trade_log=log)

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
                # Hoist live_tenant before try so it is in scope for the
                # drift and stale-mark checks that run after the except.
                live_tenant = f"{TENANT}-live" if not TENANT.endswith("-live") else TENANT
                try:
                    from main import refresh_portfolio_snapshot
                    n_refreshed = refresh_portfolio_snapshot(store, live_tenant)
                    if n_refreshed > 0:
                        pass  # silent success — logging every 30s is noisy
                    # refresh_portfolio_snapshot already records
                    # portfolio_snapshot_error internally + updates its own
                    # snapshot flags (see main.py:261). We add the health
                    # record here for the wrapper site itself.
                    _health.record_ok(store, "portfolio_refresh", TENANT)
                    # 2026-07-14 full parity — discovery only here. Each
                    # non-primary product's actual tick happens on the outer
                    # loop cadence (below) using its own WS feed, not the
                    # portfolio poll. This block just ensures a track exists
                    # for every currently held product.
                    if TICK_NON_PRIMARY:
                        snap = store.get_config(live_tenant, "__portfolio__") or {}
                        for deriv in (snap.get("derivatives") or []):
                            pid = deriv.get("product_id")
                            if not pid or pid == SYMBOL or pid.startswith("__"):
                                continue
                            _get_or_create_non_primary_track(pid)
                except Exception as e:
                    _log(f"portfolio refresh failed: {type(e).__name__}: {e}")
                    # NOTE: no trade_log= arg here. refresh_portfolio_snapshot
                    # (main.py:261) already records `portfolio_snapshot_error`
                    # to the trade log on failure. Adding a second
                    # `portfolio_refresh_error` event double-counts in the
                    # auditor's safety-event tally and splits one failure
                    # across two event names. We keep the __health__ scope
                    # write so cockpit chip + daily audit still see the
                    # failure. (Auditor fix-on-top 2026-07-14 15:35.)
                    _health.record_error(store, "portfolio_refresh", TENANT, e)
                # C-1 fix: WS-vs-REST mark drift detection moved outside the
                # refresh try/except so it runs every 2s regardless of whether
                # refresh_portfolio_snapshot succeeded. Reads whatever mark is
                # currently in the store — the stale-mark check below will flag
                # if that data is too old. Never halts trading (re-sync only).
                try:
                    _pf_drift = store.get_config(live_tenant, "__portfolio__") or {}
                    for _dd in (_pf_drift.get("derivatives") or []):
                        _dpid = _dd.get("product_id")
                        _rest_mark = float(_dd.get("mark") or 0)
                        if not _dpid or _rest_mark <= 0:
                            continue
                        _ws_price = None
                        if _dpid == SYMBOL:
                            _wt = feed.latest_ticker()
                            if _wt:
                                _ws_price = float(_wt.get("price") or 0)
                        elif _dpid in _non_primary_tracks:
                            _wt = _non_primary_tracks[_dpid].feed.latest_ticker()
                            if _wt:
                                _ws_price = float(_wt.get("price") or 0)
                        if not _ws_price or _ws_price <= 0:
                            continue
                        _drift = abs(_ws_price - _rest_mark) / _rest_mark
                        if _drift > 0.01:
                            _log(f"[mark-drift] {_dpid}: WS={_ws_price:.4f} "
                                 f"REST={_rest_mark:.4f} drift={_drift*100:.2f}% "
                                 f"— force re-sync")
                            try:
                                log.record(
                                    "mark_drift_detected", tenant=TENANT,
                                    symbol=_dpid, ws_price=_ws_price,
                                    rest_mark=_rest_mark,
                                    drift_pct=round(_drift * 100, 3),
                                    severity="critical")
                            except Exception:
                                pass
                            try:
                                from alerting import Priority
                                notifier.send(
                                    f"mark drift {_dpid}",
                                    f"{_dpid}: WS {_ws_price:.4f} vs REST "
                                    f"{_rest_mark:.4f} ({_drift*100:.1f}% apart) "
                                    f"— restarting WS feed",
                                    Priority.CRIT,
                                )
                            except Exception:
                                pass
                            if _dpid == SYMBOL:
                                try:
                                    feed.stop()
                                    feed.start()
                                    _log(f"[mark-drift] primary feed {_dpid} restarted")
                                except Exception as _fe:
                                    _log(f"[mark-drift] primary feed restart failed: {_fe}")
                            elif _dpid in _non_primary_tracks:
                                _maybe_resync_stale_feed(_non_primary_tracks[_dpid])
                except Exception as _dme:
                    _log(f"[mark-drift] check failed: {type(_dme).__name__}: {_dme}")
                # C-1 + C-2 fix: stale-mark check runs every 2s here, not every
                # 300s in the sentinel. Also fixes the C-2 boot bug: _refresh_ts=0
                # (never refreshed) is now treated as infinite age instead of
                # silently skipped via the old `if _pf_refresh_ts else None` guard.
                try:
                    _pf_stale = store.get_config(live_tenant, "__portfolio__") or {}
                    _pf_ts = float(_pf_stale.get("_refresh_ts") or 0)
                    _pf_mark_age = now - _pf_ts if _pf_ts > 0 else float("inf")
                    if _pf_mark_age > 30:
                        _stale_msg = (
                            "__portfolio__ mark has never been refreshed"
                            if _pf_ts == 0
                            else f"__portfolio__ mark is {_pf_mark_age:.0f}s stale (> 30s)"
                        )
                        _log(f"[mark-stale] CRIT: {_stale_msg}")
                        try:
                            from alerting import Priority
                            notifier.send("portfolio mark stale", _stale_msg, Priority.CRIT)
                        except Exception:
                            pass
                        last_portfolio_refresh = 0  # force re-fetch next tick
                except Exception as _mse:
                    _log(f"[mark-stale] check failed: {type(_mse).__name__}: {_mse}")
            # Funding sign-flip watcher — every 5 min. Cheap (reads snapshot
            # cache, no external API). Emits shadow signals into the Signals
            # tab when a perp's funding rate crosses zero.
            if now - last_funding_poll >= FUNDING_POLL_SECS:
                last_funding_poll = now
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
            # Primary-symbol snapshot — only when primary is enabled. In
            # no-primary mode, each non-primary track owns its own snapshot
            # write (put_snapshot called by SwingTrader.step).
            if trader is not None and feed is not None and now - last_snapshot >= SNAPSHOT_INTERVAL:
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
        if feed is not None:
            try: feed.stop()
            except Exception: pass
        # 2026-07-14 full parity: cleanly close every per-product WS feed
        # on shutdown so we don't leak connections back to Coinbase across
        # deploys/restarts.
        for _pid, _tr in list(_non_primary_tracks.items()):
            try: _tr.close()
            except Exception: pass
        log.record("bot_stopped", mode=("dry_run" if dry_run else "live"))
    return 0


if __name__ == "__main__":
    sys.exit(run())
