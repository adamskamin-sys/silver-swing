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
                     "last_tick_seen_ts", "spawn_ts")
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
    FEED_STALE_THRESHOLD_SECS = float(os.getenv("SWING_FEED_STALE_SECS", "60.0"))
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
                    seeded = {
                        "product_id": product_id,
                        "tick_size": float(spec.get("tick_size") or 0.01),
                        "contract_size": float(spec.get("contract_size") or 1),
                        "fee_per_contract_roundtrip": 0.5,   # conservative
                        "swing_qty": 0,                       # sleeves only, no primary
                        "core_qty": 0,                        # no protected core
                        "abort_above": 0,                     # bands off — sleeve controls
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
    # and force-attempts _make_non_primary_track. Respects the eviction
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
        # Held positions first — always considered critical
        try:
            pf = store.get_state(TENANT, "__portfolio__") or {}
            for sym, snap in pf.items():
                if sym.startswith("__") or sym == SYMBOL:
                    continue
                if isinstance(snap, dict) and float(snap.get("position_qty") or 0) != 0:
                    should_track_critical.add(sym)
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
        ZOMBIE_THRESHOLD_SECS = 300.0  # 5 min without a SUCCESSFUL step = zombie
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
                # Force-evict. Cooldown gets set by _evict_track, but we
                # want the IMMEDIATE respawn attempt to fire (not wait 15
                # min) since we don't know yet whether respawn will fail.
                # Cooldown protection is for repeated step-failure evictions
                # (create-fail-evict loops); a zombie eviction is different
                # — the current Track is proven dead, we want to try again
                # right now. If respawn ALSO fails, that failure sets its
                # own cooldown via the spawn error path.
                try:
                    _evict_track(pid, "zombie: no ticks in threshold window")
                    # Clear the just-set cooldown so the fall-through spawn
                    # attempt below actually fires this cycle.
                    _non_primary_last_evict_ts.pop(pid, None)
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
                continue  # respect the cooldown; try again next health cycle
            # Attempt recovery via the existing spawn path (handles all guards
            # + failure paths). We don't bypass its checks — if config missing
            # or spawn fails, _make_non_primary_track returns None + logs it.
            track = _make_non_primary_track(pid)
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
        last_reconciliation = time.time()  # [crew] reconciliation_monitor
        last_family_check = time.time()  # already resolved on startup
        while not stopping:
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
            now = time.time()
            # 2026-07-14 full parity: tick each non-primary track on the
            # SAME loop cadence as the primary, using each product's own
            # WS-ticker price. Sub-second reactions for all products.
            # Wrapped so one bad product can never take down siblings or
            # the primary loop. Failure counter evicts traders that fail
            # STEP_FAILURE_EVICT_THRESHOLD ticks in a row.
            if TICK_NON_PRIMARY:
                # Adam 2026-07-15: auto-recover any dead Tracks BEFORE the
                # tick sweep so a fresh spawn attempt goes through the tick
                # loop this iteration instead of waiting one more cycle.
                try:
                    _maybe_recover_dead_tracks()
                except Exception as _rerr:
                    _log(f"[non-primary] track health check failed: "
                         f"{type(_rerr).__name__}: {_rerr}")
                _to_evict = []
                for _pid, _track in list(_non_primary_tracks.items()):
                    # Aggressive re-sync if the feed has gone quiet.
                    _maybe_resync_stale_feed(_track)
                    _tt = _track.feed.latest_ticker()
                    if _tt is None:
                        continue  # WS still spinning up, skip this iter
                    try:
                        _track.trader.step(float(_tt["price"]))
                        _track.consecutive_step_failures = 0
                        _track.last_step_ok_ts = now
                    except Exception as e:
                        _track.consecutive_step_failures += 1
                        _log(f"[non-primary] {_pid} step failed "
                             f"({_track.consecutive_step_failures}/"
                             f"{STEP_FAILURE_EVICT_THRESHOLD}): "
                             f"{type(e).__name__}: {e}")
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
                                "armed_at": s_st.get("armed_buy_since_ts"),
                                "last_sale_px": s_st.get("last_sell_fill_price"),
                            })
                    # Fetch open orders from Coinbase via broker.list_open_orders
                    # so reconciliation_monitor's duplicate_order + orphan_order
                    # checks have data to work with. Fail-safe: on any exception,
                    # pass empty and rely on position_mismatch + stale_entry.
                    open_orders_data: list[dict] = []
                    try:
                        list_orders_fn = getattr(coinbase, "list_open_orders", None)
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
                try:
                    from main import refresh_portfolio_snapshot
                    # Match the reconcile guard on line 495: if TENANT already
                    # ends with '-live' (Adam's live worker has SWING_TENANT=
                    # adam-live), do NOT append another '-live' — else every
                    # refresh writes to the ghost 'adam-live-live' scope while
                    # the dashboard reads 'adam-live' and stays frozen at the
                    # startup snapshot. Root cause of 2026-07-14 stale-mark
                    # incident that missed PLAT sell + trail.
                    live_tenant = f"{TENANT}-live" if not TENANT.endswith("-live") else TENANT
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
