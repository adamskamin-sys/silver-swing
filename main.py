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
# Paper bot also serves the Lab tenant (dedicated $100k learning sandbox with
# theory-based preset strategies — mean_reversion, momentum, Bollinger, etc.).
# Disable by setting SWING_LAB_ENABLED=0 if you want to run the primary paper
# tenant alone. Lab tenant name is auto-derived: "adam-paper" → "adam-lab".
LAB_ENABLED = os.getenv("SWING_LAB_ENABLED", "1") == "1"
LAB_BALANCE = float(os.getenv("SWING_LAB_BALANCE", "100000.0"))
# When SWING_SYMBOL_FAMILY is set (e.g. "SLR", "AVE", "ETH"), the bot resolves
# it to the current front-month contract for that family on startup. That way
# a Coinbase auto-roll doesn't require an env var edit — the next redeploy
# picks up the new active contract automatically.
SYMBOL_FAMILY = os.getenv("SWING_SYMBOL_FAMILY", "").strip() or None


def _resolve_symbol(fallback: str) -> str:
    """If SWING_SYMBOL_FAMILY is set, resolve to that family's current
    front-month contract. Otherwise return the fixed SWING_SYMBOL."""
    if not SYMBOL_FAMILY:
        return fallback
    try:
        from broker import BrokerConfig, CoinbaseBroker
        from roll import resolve_front_month
        # product_id required by BrokerConfig but the client is product-agnostic
        client = CoinbaseBroker(BrokerConfig(product_id=fallback)).client
        resolved = resolve_front_month(
            type("_C", (), {"client": client})(),
            SYMBOL_FAMILY, fallback=fallback,
        )
        if resolved and resolved != fallback:
            _log(f"symbol family {SYMBOL_FAMILY!r} → resolved front-month {resolved} (fallback {fallback})")
        return resolved or fallback
    except Exception as e:
        _log(f"symbol family resolution failed ({type(e).__name__}: {e}) — using fallback {fallback}")
        return fallback


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


def _default_lab_config():
    """Lab tenant defaults: free-trading sandbox for learning theory-based
    strategies. core_qty=0 so the trader doesn't halt on reconcile when the
    Lab account starts flat with $100k. Abort bands intentionally wide so any
    tracked derivative fits without hand-tuning."""
    return {
        "core_qty": 0, "swing_qty": 0, "max_swing_qty": 10,
        "sell_px": 0, "buy_px": 0, "contract_size": 50,
        "margin_per_contract": 275.0, "scale_up_buffer_mult": 1.5,
        "fee_per_contract_roundtrip": 4.68,
        "abort_below": 0.0, "abort_above": 1e9,
        "fee_sanity_multiplier": 2.0,
        "sleeves": [],
    }


def _is_lab_tenant(tenant: str) -> bool:
    return tenant == _derive_lab_tenant(TENANT)


def _seed_config_if_missing(store, tenant: str, symbol: str) -> None:
    if store.get_config(tenant, symbol):
        _fixup_lab_config(store, tenant, symbol)
        return
    if _is_lab_tenant(tenant):
        store.put_config(tenant, symbol, _default_lab_config())
    else:
        store.put_config(tenant, symbol, _default_paper_config())


def _refresh_contract_spec_into_config(store, tenant: str, symbol: str) -> None:
    """Fetch the live Coinbase spec for this product and merge tick_size,
    contract_size, contract_expiry, and margin rates into the config. Runs on
    every _Track init so the dashboard can display the ACTUAL precision (e.g.
    0.00001 for a memecoin perp instead of a magnitude-inferred guess) and
    the Contract Info panel shows exact numbers from the exchange, not stale
    defaults. Failures are logged and swallowed — the bot must still boot if
    Coinbase is unreachable for a moment."""
    try:
        from broker import BrokerConfig, CoinbaseBroker
        cfg = dict(store.get_config(tenant, symbol) or {})
        broker = CoinbaseBroker(BrokerConfig(product_id=symbol))
        spec = broker.contract_spec()
        dirty = False
        for k in ("contract_size", "tick_size", "contract_expiry",
                  "intraday_margin_rate", "overnight_margin_rate"):
            v = spec.get(k)
            if v is not None and cfg.get(k) != v:
                cfg[k] = v
                dirty = True
        if dirty:
            store.put_config(tenant, symbol, cfg)
            _log(f"[{tenant}/{symbol}] spec refreshed: tick={spec.get('tick_size')}, "
                 f"size={spec.get('contract_size')}, expiry={spec.get('contract_expiry')}")
    except Exception as e:
        _log(f"[{tenant}/{symbol}] spec refresh skipped: {type(e).__name__}: {e}")


def _fixup_lab_config(store, tenant: str, symbol: str) -> None:
    """One-time migration for Lab configs that were seeded before this fix
    landed — they inherited the primary paper defaults (core_qty=10) so a
    fresh $100k Lab account halted immediately at reconcile with 'position 0
    below core 10'. Lower core_qty + widen abort bands so the Lab actually
    behaves as a learning sandbox. Only touches lab tenants; primary paper /
    live configs are never rewritten by this function."""
    if not _is_lab_tenant(tenant):
        return
    cfg = store.get_config(tenant, symbol) or {}
    dirty = False
    if int(cfg.get("core_qty") or 0) > 0:
        cfg["core_qty"] = 0
        dirty = True
    if float(cfg.get("abort_below") or 0) > 0:
        cfg["abort_below"] = 0.0
        dirty = True
    if float(cfg.get("abort_above") or 0) < 1e6:
        cfg["abort_above"] = 1e9
        dirty = True
    if dirty:
        store.put_config(tenant, symbol, cfg)
        _log(f"[{tenant}/{symbol}] lab config migrated: core_qty→0, abort bands widened")


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


class _Track:
    """One tracked symbol = one broker + trader + feed. Lifecycle is bounded:
    open() creates + starts everything, close() reverses it. main_loop pumps
    ticks through step() every iteration. All state (broker, trader, feed) is
    owned by this object so adding/removing symbols at runtime is contained."""

    def __init__(self, store, log, ks, tenant: str, symbol: str, starting_balance: float):
        from feed import LiveTickerFeed
        from microstructure import MicrostructureFilter
        from paper_broker import PaperBroker, PaperConfig
        from swing_leg import SwingTrader

        _seed_config_if_missing(store, tenant, symbol)
        _refresh_contract_spec_into_config(store, tenant, symbol)
        self.tenant = tenant
        self.symbol = symbol
        self.store = store
        self.log = log

        self.broker = PaperBroker(PaperConfig(
            product_id=symbol,
            contract_size=50.0, tick_size=0.005,
            fee_per_fill=2.34, margin_per_contract=275.0,
            starting_balance=starting_balance,
        ))
        persisted = store.get_paper_state(tenant, symbol)
        if persisted:
            self.broker.restore_from_state_dict(persisted)
            _log(f"[{symbol}] restored: qty={self.broker.position.qty}, "
                 f"balance=${self.broker.balance:,.2f}, "
                 f"realized=${self.broker.realized_pnl:+,.2f}")
        else:
            _log(f"[{symbol}] paper starts flat")

        # Microstructure signals only run for the primary tenant's primary
        # symbol so the lab tenant stays a clean testbed for theory strategies
        # without the primary's HFT signal gating leaking in. Adding a Lab-only
        # microstructure toggle is a follow-up if we want it.
        is_primary_track = (symbol == SYMBOL and tenant == TENANT)
        self.ms = MicrostructureFilter() if is_primary_track else None
        if self.ms and not self.ms.any_enabled():
            self.ms = None

        self.trader = SwingTrader(self.broker, store, tenant, symbol,
                                  trade_log=log, kill_switch=ks, microstructure=self.ms)
        self.feed = LiveTickerFeed(
            symbol,
            subscribe_l2=(self.ms.needs_l2() if self.ms else False),
            subscribe_trades=(self.ms.needs_trades() if self.ms else False),
            on_l2_snapshot=(self.ms.on_l2_snapshot if self.ms else None),
            on_l2_update=(self.ms.on_l2_update if self.ms else None),
            on_trade=(self.ms.on_trade if self.ms else None),
        )
        self.last_snapshot_ts = 0.0
        self.reconciled = False

    def start(self, feed_ready_timeout: float) -> bool:
        self.feed.start()
        if not self.feed.wait_for_first_tick(timeout=feed_ready_timeout):
            _log(f"[{self.symbol}] no ticks within {feed_ready_timeout}s — skipping")
            self.feed.stop()
            return False
        self.trader.reconcile()
        self.reconciled = True
        return True

    def step(self, now: float, snapshot_interval: float) -> None:
        t = self.feed.latest_ticker()
        if t is None:
            return
        self.broker.tick(t["best_bid"], t["best_ask"])
        if self.ms is not None:
            self.ms.on_ticker(t["best_bid"], t["best_ask"], t["price"])
        set_range = getattr(self.broker, "set_external_day_range", None)
        if callable(set_range):
            set_range(t.get("high_24h"), t.get("low_24h"))
        self.trader.step(t["price"])
        if now - self.last_snapshot_ts >= snapshot_interval:
            snap = self.broker.snapshot()
            snap["mode"] = "paper"
            snap["product_id"] = self.symbol
            snap["best_bid"] = t["best_bid"]
            snap["best_ask"] = t["best_ask"]
            snap["generated_at"] = now
            if self.ms is not None:
                snap["microstructure"] = self.ms.snapshot()
            self.store.put_snapshot(self.tenant, self.symbol, snap)
            self.store.put_paper_state(self.tenant, self.symbol,
                                       self.broker.to_state_dict())
            self.last_snapshot_ts = now

    def close(self) -> None:
        try:
            self.feed.stop()
        except Exception:
            pass


def _discover_tracked_symbols(store, tenant: str, primary_symbol: str) -> list[str]:
    """Any (tenant, symbol) with a config block is a tracked symbol. Primary
    always leads. list_symbols may include entries the tenant created via
    /api/track-symbol without setting SWING_SYMBOL for them."""
    try:
        found = store.list_symbols(tenant) or []
    except Exception as e:
        _log(f"discover_tracked_symbols failed: {type(e).__name__}: {e}")
        found = []
    # Primary first, others alphabetical after — order matters for consistent
    # log output but no functional dependency.
    extras = [s for s in found if s and s != primary_symbol]
    return [primary_symbol] + sorted(extras)


def _derive_lab_tenant(paper_tenant: str) -> str:
    """Convert a paper tenant name to its lab counterpart. adam-paper → adam-lab.
    Anything else gets '-lab' appended so the naming stays predictable."""
    if paper_tenant.endswith("-paper"):
        return paper_tenant[: -len("-paper")] + "-lab"
    return f"{paper_tenant}-lab"


def _tenant_balance(tenant: str) -> float:
    """Balance a tenant starts new tracks with. Lab uses $100k; other tenants
    use SWING_PAPER_BALANCE."""
    lab_tenant = _derive_lab_tenant(TENANT)
    if tenant == lab_tenant:
        return LAB_BALANCE
    return float(os.getenv("SWING_PAPER_BALANCE", "100000.0"))


def run_paper_mode() -> int:
    """Live feed → PaperBroker → SwingTrader, per (tenant, symbol) tracked.
    Real market prices, simulated fills. Safe: no path to Coinbase's order
    endpoint. Serves the primary paper tenant plus the Lab tenant (auto-derived
    from primary) so users can experiment with theory-based strategies in an
    isolated $100k sandbox."""
    from safety import KillSwitch, make_trade_log
    from state_store import make_store

    global SYMBOL
    SYMBOL = _resolve_symbol(SYMBOL)

    tenants: list[str] = [TENANT]
    if LAB_ENABLED:
        lab_tenant = _derive_lab_tenant(TENANT)
        if lab_tenant != TENANT:
            tenants.append(lab_tenant)
    _log(f"paper mode: primary={SYMBOL}, tenants={tenants}"
         f"{' (family=' + SYMBOL_FAMILY + ')' if SYMBOL_FAMILY else ''}")

    store = make_store(DATA_DIR)
    log = make_trade_log(DATA_DIR)
    _log(f"store backend: {type(store).__name__}, trade log: {type(log).__name__}")

    # One KillSwitch per tenant — pausing the lab shouldn't pause primary.
    kill_switches: dict[str, "KillSwitch"] = {t: KillSwitch(store, t) for t in tenants}

    # Start backtest worker if Redis is wired. Dashboard pushes jobs onto a
    # queue; this thread runs them here (where Python + Coinbase creds live)
    # and writes results back for the dashboard to poll. See backtest_worker.py.
    if os.getenv("REDIS_URL"):
        import backtest_worker
        backtest_worker.start(os.getenv("REDIS_URL"))

    # tracks keyed by (tenant, symbol) so multi-tenant + multi-symbol is a flat
    # iteration in the main loop. Each track owns its own broker + trader +
    # feed — no cross-tenant state sharing.
    tracks: dict[tuple[str, str], _Track] = {}
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        _log("SIGINT received — shutting down")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    # Seed initial tracks: for the primary tenant, use SYMBOL as the primary;
    # for the lab tenant, use SYMBOL too so there's always at least one card
    # visible in the lab on first boot. Both then pick up extras from the store.
    for tenant in tenants:
        balance = _tenant_balance(tenant)
        initial_symbols = _discover_tracked_symbols(store, tenant, SYMBOL)
        _log(f"[{tenant}] tracking {len(initial_symbols)} symbol(s) at boot: {initial_symbols}")
        for sym in initial_symbols:
            track = _Track(store, log, kill_switches[tenant], tenant, sym, balance)
            if track.start(FEED_READY_TIMEOUT):
                tracks[(tenant, sym)] = track
            else:
                track.close()

    if not tracks:
        _log("no tracks came up — check the WS or product_ids")
        return 1

    # Mirror-live opt-in still only applies to the PRIMARY tenant's primary
    # symbol — the lab is intentionally sandboxed away from real positions.
    primary_track = tracks.get((TENANT, SYMBOL))
    if (primary_track and primary_track.broker.position.qty == 0
            and os.getenv("SWING_PAPER_MIRROR_LIVE", "0") == "1"):
        _mirror_live_position_into_paper(primary_track.broker, SYMBOL)

    log.record("bot_started", mode="paper", tenants=tenants,
               tracks=[f"{t}:{s}" for (t, s) in tracks.keys()])

    try:
        snapshot_interval = float(os.getenv("SWING_SNAPSHOT_INTERVAL", "5.0"))
        scanner_interval = float(os.getenv("SWING_SCANNER_INTERVAL", "60.0"))
        symbol_discover_interval = float(os.getenv("SWING_SYMBOL_DISCOVER_INTERVAL", "10.0"))
        last_scanner = 0.0
        last_discover = 0.0
        _coinbase_for_scanner = None
        redis_url = os.getenv("REDIS_URL")

        while not stopping:
            now = time.time()

            # Hot-add newly-tracked symbols across ALL tenants — dashboard-added
            # symbols come online without a restart. Runs across tenants so a
            # user's "Track this symbol" click in the Lab tab picks up too.
            if now - last_discover >= symbol_discover_interval:
                for tenant in tenants:
                    current = set(_discover_tracked_symbols(store, tenant, SYMBOL))
                    existing = {s for (t, s) in tracks if t == tenant}
                    balance = _tenant_balance(tenant)
                    for sym in current - existing:
                        _log(f"[{tenant}] hot-adding new tracked symbol: {sym}")
                        track = _Track(store, log, kill_switches[tenant], tenant, sym, balance)
                        if track.start(FEED_READY_TIMEOUT):
                            tracks[(tenant, sym)] = track
                        else:
                            track.close()
                last_discover = now

            for track in list(tracks.values()):
                track.step(now, snapshot_interval)

            if redis_url and now - last_scanner >= scanner_interval:
                try:
                    from scanner import fetch_and_rank, write_ranking_to_redis
                    if _coinbase_for_scanner is None:
                        from broker import BrokerConfig, CoinbaseBroker
                        _coinbase_for_scanner = CoinbaseBroker(
                            BrokerConfig(product_id=SYMBOL)
                        ).client
                    ranking = fetch_and_rank(_coinbase_for_scanner, top_n=10)
                    write_ranking_to_redis(redis_url, ranking, generated_at=now)
                except Exception as e:
                    _log(f"scanner refresh failed: {type(e).__name__}: {e}")
                last_scanner = now

            time.sleep(LOOP_INTERVAL_SECS)

    finally:
        for track in tracks.values():
            track.close()
        log.record("bot_stopped", mode="paper", tracks=[f"{t}:{s}" for (t, s) in tracks.keys()])
        for (tenant, sym), track in tracks.items():
            _log(f"[{tenant}/{sym}] final: {track.broker.snapshot()}")
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
