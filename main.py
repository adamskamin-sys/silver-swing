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
        if _is_lab_tenant(tenant):
            _seed_lab_comparison_sleeves(store, tenant, symbol)
        return
    if _is_lab_tenant(tenant):
        store.put_config(tenant, symbol, _default_lab_config())
        _seed_lab_comparison_sleeves(store, tenant, symbol)
    else:
        store.put_config(tenant, symbol, _default_paper_config())


def _seed_lab_comparison_sleeves(store, tenant: str, symbol: str) -> None:
    """One-time bootstrap: seed the Lab tenant with 5 sleeves running Models
    A–E side-by-side, 2 contracts each. Clears any pre-existing sleeves so
    the comparison always starts fresh with just A–E.

    Skips re-seeding if the sleeves list already contains any of the
    'Model X' sleeves — prevents wiping user's mid-run comparison on every
    bot restart. To force a re-seed, delete the existing sleeves first via
    the dashboard.

    Anchor pricing uses the last snapshot mark if available, else falls
    back to 61.5 (roughly current silver at build time). Prices adjust on
    the sleeve's first arm anyway — they're just starting points.
    """
    cfg = dict(store.get_config(tenant, symbol) or {})
    existing = cfg.get("sleeves") or []
    already_seeded = any(str(s.get("name", "")).startswith("Model ") for s in existing)
    if already_seeded:
        return
    snap = store.get_snapshot(tenant, symbol) or {}
    mark = float(snap.get("last_mark") or 61.5)
    # Common defaults for all 5 sleeves
    spread = 0.20  # buy/sell centered on mark with $0.20 spread → nets ~$10 at qty=2
    base = {
        "qty": 2,
        "reanchor_threshold": 2.0,
        "buy_px": round(mark - spread / 2, 3),
        "sell_px": round(mark + spread / 2, 3),
        "trail_trigger": round(mark + spread / 2, 3),
        "trail_distance": 0.15,
        "trail_activation_px": round(mark + spread / 2 + 0.10, 3),
        "hybrid_delay_secs": 5,
        "max_qty": 5,
        "scale_up_buffer_mult": 1.5,
        "stop_loss_px": round(mark - spread / 2 - 1.50, 3),
        "stop_loss_qty_mode": "all",
        "stop_loss_qty_custom": 0,
    }
    # Preset collapse (2026-07-13): Models C/D/E removed — every feature they
    # embodied (microstructure gates, news blackout, book imbalance) is now
    # a per-sleeve toggle available on Model B. Lab auto-seed now creates one
    # canonical Model B sleeve; A/B experiments should be run as sibling
    # sleeves with specific toggles flipped, not as separate named models.
    models = [
        {**base, "id": "model_b", "name": "Model B — Defensive Plus",
         "exit_mode": "hybrid",
         "stop_loss_enabled": True, "stop_loss_ratchet_enabled": True,
         "stop_loss_ratchet_distance": 1.5, "stop_loss_ratchet_activation": 0.5,
         "stop_loss_reanchor_on_trigger": True, "stop_loss_max_consecutive": 3,
         "reentry_mode": "volatility", "reentry_range_contraction": 0.5,
         "reentry_min_wait_secs": 30,
         "reanchor_threshold": 0.75,
         "accumulate_enabled": True},
    ]
    cfg["sleeves"] = models
    store.put_config(tenant, symbol, cfg)
    _log(f"[{tenant}/{symbol}] seeded Lab with Model B (canonical expert-stack sleeve)")


def _attach_expert_params(broker, portfolio_snap: dict) -> None:
    """For every derivative in the snapshot, compute ATR from recent 5-min
    candles and attach Layer-1 expert params (asset-class multipliers ×
    ATR). Also attaches Layer-2 tuned params if a cached tuning exists.
    Best-effort — failures per product don't stall the caller."""
    from datetime import datetime, timedelta, timezone
    from backtest import fetch_candles
    from expert_params import compute_atr, expert_params

    derivs = portfolio_snap.get("derivatives") or []
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=24)  # 24h of 5-min = ~288 candles, ample for ATR-14
    for d in derivs:
        pid = d.get("product_id")
        if not pid:
            continue
        try:
            candles = fetch_candles(broker.client, pid, start, end, granularity="FIVE_MINUTE")
            atr = compute_atr(candles, period=14)
            if atr > 0:
                d["atr"] = round(atr, 5)
                d["expert_params"] = expert_params(pid, atr)
        except Exception:
            pass


_last_tuner_run_ts: dict[str, float] = {}
_TUNER_INTERVAL_SECS = 24 * 3600  # daily


def _maybe_run_tuner(store, live_tenant: str) -> None:
    """Layer 2: run the grid-search tuner over the last 30 days for each Live
    derivative. Slow (~5-15s per product) so only runs once per day. Results
    written to '__tuned_params__' on the live tenant; the modal reads them
    and prefers them over Layer 1 literature defaults."""
    import time as _t
    now = _t.time()
    last = _last_tuner_run_ts.get(live_tenant, 0.0)
    if now - last < _TUNER_INTERVAL_SECS:
        return
    _last_tuner_run_ts[live_tenant] = now
    try:
        from broker import BrokerConfig, CoinbaseBroker
        from expert_tuner import tune_products
        # Only tune derivatives we're actually holding on Live.
        pf = store.get_config(live_tenant, "__portfolio__") or {}
        pids = [d.get("product_id") for d in (pf.get("derivatives") or []) if d.get("product_id")]
        if not pids:
            return
        _log(f"[{live_tenant}] tuner: grid-searching best multipliers over 30d for {pids}")
        seed = os.getenv("SWING_SYMBOL", "SLR-27AUG26-CDE")
        client = CoinbaseBroker(BrokerConfig(product_id=seed)).client
        tuned = tune_products(client, pids, days=30)
        store.put_config(live_tenant, "__tuned_params__", tuned)
        _log(f"[{live_tenant}] tuner: done, tuned {len(tuned)} products")
    except Exception as e:
        _log(f"[{live_tenant}] tuner failed: {type(e).__name__}: {e}")


def _derive_live_tenant(paper_tenant: str) -> str:
    """adam-paper → adam-live. Anything else gets '-live' appended."""
    if paper_tenant.endswith("-paper"):
        return paper_tenant[: -len("-paper")] + "-live"
    return f"{paper_tenant}-live"


def _default_live_holding_config(product_id: str, kind: str) -> dict:
    """Minimal config for an auto-discovered Live-tenant holding. Zero swing_qty
    so no strategy fires until user attaches one. Core=0 so no floor guard
    complaints. Wide abort bands. tick_size/contract_size get overwritten by
    _refresh_contract_spec_into_config on next spec refresh."""
    return {
        "core_qty": 0, "swing_qty": 0, "max_swing_qty": 10,
        "sell_px": 0.0, "buy_px": 0.0,
        "contract_size": 1.0 if kind == "spot" else 50.0,
        "tick_size": 0.005,
        "margin_per_contract": 0.0 if kind == "spot" else 275.0,
        "scale_up_buffer_mult": 1.5,
        "fee_per_contract_roundtrip": 0.0,
        "abort_below": 0.0, "abort_above": 1e9,
        "fee_sanity_multiplier": 2.0,
        "asset_kind": kind,   # "futures" or "spot"
        "sleeves": [],
    }


def _sync_live_portfolio(store, live_tenant: str) -> list[str]:
    """Pull the user's real Coinbase portfolio (all futures positions + all
    non-zero spot balances) and upsert each holding as a tracked symbol on the
    Live tenant. Watchlist behavior: once added, a symbol persists in the store
    even after the position closes on Coinbase, so the user can re-enter
    without re-adding it.

    Preserves any existing config for a known symbol (user-added sleeves,
    tweaked prices, etc.) — only creates fresh entries for new holdings.

    Returns the list of upserted symbols. Empty list on failure.
    """
    try:
        from broker import BrokerConfig, CoinbaseBroker
        # product_id required by BrokerConfig but list_all_holdings is
        # product-agnostic; the seed symbol is only used to construct the client.
        seed = os.getenv("SWING_SYMBOL", "SLR-27AUG26-CDE")
        broker = CoinbaseBroker(BrokerConfig(product_id=seed))
        holdings = broker.list_all_holdings()
    except Exception as e:
        _log(f"[{live_tenant}] live portfolio sync failed: {type(e).__name__}: {e}")
        return []

    upserted: list[str] = []
    for h in holdings:
        pid = h.get("product_id")
        kind = h.get("kind") or "futures"
        if not pid:
            continue
        existing = store.get_config(live_tenant, pid)
        if not existing:
            cfg = _default_live_holding_config(pid, kind)
            store.put_config(live_tenant, pid, cfg)
            upserted.append(pid)
        # Refresh actual contract specs from Coinbase every sync so tick_size
        # / contract_size / margin / round-trip fee reflect THIS product
        # (silver=50 contracts, oil=10 contracts, etc.), not the silver-based
        # defaults in _default_live_holding_config. Runs on both new upserts
        # AND existing entries so a holding added when defaults were wrong
        # gets its correct specs on the next sync. Futures only — spot has no
        # contract spec.
        if kind == "futures":
            try:
                _refresh_contract_spec_into_config(store, live_tenant, pid)
            except Exception as e:
                _log(f"[{live_tenant}/{pid}] spec refresh skipped: {type(e).__name__}: {e}")
        # Force core_qty=0 on every Live holding — Live is a portfolio mirror,
        # not a swing bot, so there's no "protected core" to defend. Old
        # configs seeded before this rule still had core_qty=10 stuck, which
        # blocked every sleeve add with 'available 0 (position N - core N)'.
        try:
            cur = dict(store.get_config(live_tenant, pid) or {})
            if int(cur.get("core_qty") or 0) != 0:
                cur["core_qty"] = 0
                store.put_config(live_tenant, pid, cur)
        except Exception:
            pass

    # Also compute + persist the structured portfolio snapshot so the Live
    # tab can render the Coinbase-style Cash / Derivatives / Crypto view.
    # Stored under a reserved symbol '__portfolio__'; the dashboard skips
    # this key in the regular card render loop.
    try:
        snap = broker.portfolio_snapshot()
        # Attach per-product expert params derived from real ATR. Layer 1:
        # ATR × asset-class multipliers from published trader literature.
        _attach_expert_params(broker, snap)
        store.put_config(live_tenant, "__portfolio__", snap)
    except Exception as e:
        _log(f"[{live_tenant}] portfolio_snapshot skipped: {type(e).__name__}: {e}")

    if upserted:
        _log(f"[{live_tenant}] added {len(upserted)} live holdings to portfolio: {upserted}")
    return upserted


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

        # Fetch the real per-fill commission via preview_order — BOTH sides.
        # Prior versions only previewed the SELL side and doubled it. That
        # assumes buy_fee == sell_fee, which is *usually* true on CFM but not
        # guaranteed (fee-tier changes, product-specific carve-outs). Preview
        # each side, store them separately, and derive round-trip as the sum.
        # Coinbase adjusts these occasionally (silver dropped $2.34 → $2.32/fill
        # on 2026-07-07) and Adam's fee tier can shift as his 30d volume grows
        # — hardcoding any single value silently drifts against reality.
        try:
            # Derive limit prices near current market — extreme values
            # (0.001 / 999999.99) get rejected by Coinbase's tick/price bands
            # for some products (PT at $1640, etc.) which causes preview_order
            # to fail silently, leaving stored fees at $0. Use market ±50%
            # instead: far enough from mid to guarantee the preview isn't a
            # crossing order that would fill, close enough that Coinbase
            # accepts it. Snap to the product's tick grid so preview doesn't
            # reject on precision either.
            live_price = float(spec.get("current_price") or 0.0)
            live_tick = float(spec.get("tick_size") or 0.0)

            def _snap(px: float) -> float:
                if live_tick <= 0:
                    return round(px, 4)
                return round(round(px / live_tick) * live_tick, 8)

            def _preview_side_commission(side: str) -> float:
                if side == "BUY":
                    buy_px = _snap(live_price * 0.5) if live_price > 0 else 0.001
                    preview = broker.client.preview_limit_order_gtc_buy(
                        product_id=symbol, base_size="1", limit_price=str(buy_px),
                    )
                else:
                    sell_px = _snap(live_price * 1.5) if live_price > 0 else 999999.99
                    preview = broker.client.preview_limit_order_gtc_sell(
                        product_id=symbol, base_size="1", limit_price=str(sell_px),
                    )
                pd = preview.to_dict() if hasattr(preview, "to_dict") else preview
                return float(pd.get("commission_total") or 0.0)

            per_fill_buy = _preview_side_commission("BUY")
            per_fill_sell = _preview_side_commission("SELL")
            if per_fill_buy > 0 or per_fill_sell > 0:
                # If only one side previews cleanly, fall back to 2× that side
                # so we still get *some* accurate value rather than dropping to
                # the stale default.
                if per_fill_buy <= 0:
                    per_fill_buy = per_fill_sell
                if per_fill_sell <= 0:
                    per_fill_sell = per_fill_buy
                round_trip = round(per_fill_buy + per_fill_sell, 4)
                per_fill_avg = round((per_fill_buy + per_fill_sell) / 2.0, 4)
                changed = (
                    cfg.get("fee_per_contract_roundtrip") != round_trip
                    or cfg.get("fee_per_fill_buy") != round(per_fill_buy, 4)
                    or cfg.get("fee_per_fill_sell") != round(per_fill_sell, 4)
                )
                if changed:
                    cfg["fee_per_contract_roundtrip"] = round_trip
                    cfg["fee_per_fill_buy"] = round(per_fill_buy, 4)
                    cfg["fee_per_fill_sell"] = round(per_fill_sell, 4)
                    cfg["fee_per_fill_empirical"] = per_fill_avg
                    dirty = True
        except Exception as fee_err:
            _log(f"[{tenant}/{symbol}] fee refresh skipped: {type(fee_err).__name__}: {fee_err}")

        if dirty:
            store.put_config(tenant, symbol, cfg)
            _log(f"[{tenant}/{symbol}] spec refreshed: tick={spec.get('tick_size')}, "
                 f"size={spec.get('contract_size')}, expiry={spec.get('contract_expiry')}, "
                 f"buy_fee=${cfg.get('fee_per_fill_buy')}, "
                 f"sell_fee=${cfg.get('fee_per_fill_sell')}, "
                 f"round_trip_fee=${cfg.get('fee_per_contract_roundtrip')}")
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
        self.is_live = (tenant == _derive_live_tenant(TENANT))

        if self.is_live:
            # REAL MONEY path. CoinbaseBroker places actual orders and reads
            # position/balance directly from Coinbase — no simulated state.
            from broker import BrokerConfig, CoinbaseBroker
            self.broker = CoinbaseBroker(BrokerConfig(product_id=symbol))
            _log(f"[REAL MONEY] {tenant}/{symbol} — CoinbaseBroker attached "
                 f"(pos={self.broker.position.qty}, avg=${self.broker.position.avg_entry:.4f})")
        else:
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
        # Tick recorder — writes every ticker/trade/L2 to disk when
        # SWING_TICK_RECORDING=1 for future backtest / ML training. No-op
        # when the env var is unset (default).
        self.tick_recorder = None
        try:
            from tick_recorder import TickRecorder, enabled as _tr_enabled
            if _tr_enabled():
                self.tick_recorder = TickRecorder(symbol)
        except Exception:
            self.tick_recorder = None
        # Wire the aggressor-run shadow-signal callback. Fires from
        # MicrostructureFilter.on_trade when the run detector crosses its
        # threshold. Shadow ONLY: emit_aggressor_run_signal only writes to
        # the shadow log — never touches the broker. Guarded in
        # tape_shadow.py via EXECUTE_TRADES=False + a test suite that
        # asserts no broker imports in that module.
        if self.ms is not None:
            try:
                from tape_shadow import emit_aggressor_run_signal
                def _on_agg(crossing, _filter, _store=store, _t=tenant, _s=symbol):
                    try:
                        emit_aggressor_run_signal(
                            _store, _t, _s, crossing,
                            baseline_mark=float(crossing.get("price") or 0.0),
                        )
                    except Exception:
                        pass
                self.ms.on_aggressor_run_crossing = _on_agg
            except Exception as e:
                _log(f"tape_shadow wiring failed: {type(e).__name__}: {e}")

        self.trader = SwingTrader(self.broker, store, tenant, symbol,
                                  trade_log=log, kill_switch=ks, microstructure=self.ms)
        # Fan out feed callbacks: MicrostructureFilter first, then tick
        # recorder (if enabled). This way tick recording works even for
        # symbols/tenants that don't have an active MS filter.
        def _fanout_l2_snapshot(bids, asks):
            if self.ms is not None: self.ms.on_l2_snapshot(bids, asks)
            if self.tick_recorder is not None: self.tick_recorder.on_l2_snapshot(bids, asks)
        def _fanout_l2_update(side, price, sz):
            if self.ms is not None: self.ms.on_l2_update(side, price, sz)
            if self.tick_recorder is not None: self.tick_recorder.on_l2_update(side, price, sz)
        def _fanout_trade(price, size, side, ts=None):
            if self.ms is not None: self.ms.on_trade(price, size, side, ts)
            if self.tick_recorder is not None: self.tick_recorder.on_trade(price, size, side, ts)
        needs_l2 = (self.ms is not None and self.ms.needs_l2()) or (self.tick_recorder is not None)
        needs_trades = (self.ms is not None and self.ms.needs_trades()) or (self.tick_recorder is not None)
        self.feed = LiveTickerFeed(
            symbol,
            subscribe_l2=needs_l2,
            subscribe_trades=needs_trades,
            on_l2_snapshot=(_fanout_l2_snapshot if needs_l2 else None),
            on_l2_update=(_fanout_l2_update if needs_l2 else None),
            on_trade=(_fanout_trade if needs_trades else None),
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
        if self.tick_recorder is not None:
            self.tick_recorder.on_ticker(t["best_bid"], t["best_ask"], t["price"])
        set_range = getattr(self.broker, "set_external_day_range", None)
        if callable(set_range):
            set_range(t.get("high_24h"), t.get("low_24h"))
        self.trader.step(t["price"])
        if now - self.last_snapshot_ts >= snapshot_interval:
            snap = self.broker.snapshot()
            snap["mode"] = "live" if self.is_live else "paper"
            snap["product_id"] = self.symbol
            snap["best_bid"] = t["best_bid"]
            snap["best_ask"] = t["best_ask"]
            snap["generated_at"] = now
            if self.ms is not None:
                snap["microstructure"] = self.ms.snapshot()
            self.store.put_snapshot(self.tenant, self.symbol, snap)
            if not self.is_live:
                self.store.put_paper_state(self.tenant, self.symbol,
                                           self.broker.to_state_dict())
            self.last_snapshot_ts = now

    def close(self) -> None:
        try:
            self.feed.stop()
        except Exception:
            pass
        if self.tick_recorder is not None:
            try:
                self.tick_recorder.close()
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
    # Skip control keys — __account_kill_switch__, __portfolio__ etc. are not
    # real Coinbase products; treating them as symbols causes 404s from
    # Coinbase's get_product endpoint every discover tick.
    extras = [s for s in found
              if s and s != primary_symbol and not s.startswith("__")]
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

    # Live engine: opt-in, DOUBLE-gated. SWING_LIVE_ENGINE=1 enables the wiring
    # AND SWING_LIVE_CONFIRM=I_UNDERSTAND acknowledges real orders will place
    # on Coinbase. Missing either flag runs paper/lab only.
    live_tenant = _derive_live_tenant(TENANT)
    live_engine_enabled = (
        os.getenv("SWING_LIVE_ENGINE", "0") == "1"
        and os.getenv("SWING_LIVE_CONFIRM") == "I_UNDERSTAND"
    )
    if os.getenv("SWING_LIVE_ENGINE", "0") == "1" and not live_engine_enabled:
        _log("SWING_LIVE_ENGINE=1 but SWING_LIVE_CONFIRM!=I_UNDERSTAND — Live engine will NOT run")
    if live_engine_enabled and live_tenant != TENANT and live_tenant not in tenants:
        tenants.append(live_tenant)

    _log(f"paper mode: primary={SYMBOL}, tenants={tenants}"
         f"{' (family=' + SYMBOL_FAMILY + ')' if SYMBOL_FAMILY else ''}"
         f"{' [REAL MONEY LIVE ENGINE ACTIVE]' if live_engine_enabled else ''}")

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

    # Boot-time live portfolio sync must run BEFORE track seeding when the Live
    # engine is enabled, so the seed loop finds the correct product_ids on the
    # Live tenant. When Live engine is off, the sync still runs (dashboard-only
    # visibility mode), just after track seeding.
    if live_engine_enabled:
        live_syms = _sync_live_portfolio(store, live_tenant)
        # DO NOT wipe existing state on boot. That was destroying halt state,
        # ratcheted stop-loss HWM, hybrid delay windows, and consecutive-stop
        # counters on every deploy — un-halting sleeves and re-triggering
        # protective sells that had already been suppressed. Preserve state;
        # dead live_order_ids get cleared organically on the next tick when
        # the sleeve polls order_status and sees CANCELLED/EXPIRED/UNKNOWN.
        for sym in live_syms:
            cfg = store.get_config(live_tenant, sym) or {}
            n_sleeves = len(cfg.get("sleeves") or [])
            _log(f"[REAL MONEY] {live_tenant}/{sym} — {n_sleeves} sleeve(s) attached (state preserved)")

    # Seed initial tracks: for the primary tenant, use SYMBOL as the primary;
    # for the lab tenant, use SYMBOL too so there's always at least one card
    # visible in the lab on first boot. Both then pick up extras from the store.
    for tenant in tenants:
        balance = _tenant_balance(tenant)
        if tenant == live_tenant:
            # Live tenant tracks exactly what Coinbase reports it holds. Reserved
            # store keys (__portfolio__, __tuned_params__, etc.) are not products.
            initial_symbols = [s for s in (store.list_symbols(tenant) or [])
                               if not s.startswith("__")]
        else:
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

    # If Live engine is off, sync just for dashboard visibility. When Live is
    # on, we already synced above (before seeding) so this is skipped.
    if not live_engine_enabled:
        _sync_live_portfolio(store, live_tenant)

    log.record("bot_started", mode="paper", tenants=tenants,
               tracks=[f"{t}:{s}" for (t, s) in tracks.keys()])

    try:
        snapshot_interval = float(os.getenv("SWING_SNAPSHOT_INTERVAL", "5.0"))
        # Minimum interval between scans — even if the user hammers the tab.
        # Scanner is on-demand now, but 30s floor keeps us under API limits
        # if two browsers open at once or someone clicks refresh in a loop.
        scanner_interval = float(os.getenv("SWING_SCANNER_INTERVAL", "30.0"))
        # Auto-refresh interval: even without a user clicking refresh, run
        # a scan every N seconds so the Edit modal always has fresh BEST
        # tiles. Default 900s (15 min) — balances Coinbase API budget
        # against staleness. Set to 0 to disable auto-run.
        scanner_auto_interval = float(os.getenv("SWING_SCANNER_AUTO_INTERVAL", "900.0"))
        last_scanner_auto = 0.0
        symbol_discover_interval = float(os.getenv("SWING_SYMBOL_DISCOVER_INTERVAL", "10.0"))
        # 15s = near real-time refresh of the Live tab's portfolio view
        # (positions, marks, specs). Set higher via SWING_LIVE_PORTFOLIO_INTERVAL
        # if you hit Coinbase rate limits.
        # 3s cadence: fast enough that the Live drill-down modal's price bar
        # feels real-time (client polls every 5s). At 3s × ~3 open positions,
        # Coinbase get_product runs ~1 req/sec — well under the 30 req/sec limit.
        live_portfolio_interval = float(os.getenv("SWING_LIVE_PORTFOLIO_INTERVAL", "3.0"))
        last_scanner = 0.0
        last_discover = 0.0
        last_live_portfolio = 0.0
        _coinbase_for_scanner = None
        redis_url = os.getenv("REDIS_URL")

        while not stopping:
            now = time.time()

            # Hot-add newly-tracked symbols across ALL tenants — dashboard-added
            # symbols come online without a restart. Runs across tenants so a
            # user's "Track this symbol" click in the Lab tab picks up too.
            if now - last_discover >= symbol_discover_interval:
                for tenant in tenants:
                    if tenant == live_tenant:
                        current = set(s for s in (store.list_symbols(tenant) or [])
                                      if not s.startswith("__"))
                    else:
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

            # Periodic live portfolio refresh — picks up newly-opened positions
            # on the real Coinbase account and adds them to the Live tab.
            # Existing entries are preserved (watchlist behavior).
            if now - last_live_portfolio >= live_portfolio_interval:
                _sync_live_portfolio(store, live_tenant)
                _maybe_run_tuner(store, live_tenant)
                last_live_portfolio = now

            # Scanner runs on TWO triggers:
            #   1. User-requested refresh (dashboard click sets Redis flag)
            #   2. Auto-refresh every SWING_SCANNER_AUTO_INTERVAL seconds so
            #      Edit modal BEST tiles stay fresh without a click. Costs
            #      ~30 candle fetches/scan; at 15 min default that's ~120/hr
            #      — well within Coinbase's budget.
            # Both paths are rate-limited by scanner_interval (30s floor).
            if redis_url and now - last_scanner >= scanner_interval:
                try:
                    from scanner import (
                        fetch_and_rank, write_ranking_to_redis,
                        check_and_clear_refresh_request,
                    )
                    requested = check_and_clear_refresh_request(redis_url)
                    auto_due = (scanner_auto_interval > 0
                                and now - last_scanner_auto >= scanner_auto_interval)
                    if requested or auto_due:
                        if _coinbase_for_scanner is None:
                            from broker import BrokerConfig, CoinbaseBroker
                            _coinbase_for_scanner = CoinbaseBroker(
                                BrokerConfig(product_id=SYMBOL)
                            ).client
                        # Force-include every product Adam has an active
                        # strategy on so the Edit modal's "Recommended spreads"
                        # tiles always populate. Without this, low-24h-range
                        # products (that Adam is still actively swinging) drop
                        # off the top-N and their modal shows "no scanner data".
                        forced = set()
                        try:
                            for t in store.list_tenants():
                                for sym in store.list_symbols(t):
                                    if sym.startswith("__"):
                                        continue
                                    cfg = store.get_config(t, sym) or {}
                                    if cfg.get("sleeves") or cfg.get("swing_qty"):
                                        forced.add(sym)
                        except Exception as e:
                            _log(f"scanner: force-include gather failed: {type(e).__name__}: {e}")
                        # Also force-include every product Adam actually holds
                        # a live futures position in — even without a strategy
                        # attached yet. Newly-bought contracts (via scanner-buy
                        # or Coinbase directly) don't have sleeves yet, so
                        # without this they'd drop out of the scan and the
                        # Add Strategy modal would open with no tiles.
                        try:
                            resp = _coinbase_for_scanner.list_futures_positions()
                            positions = (resp.to_dict() if hasattr(resp, "to_dict") else resp).get("positions") or []
                            for p in positions:
                                pid = p.get("product_id")
                                if pid:
                                    forced.add(pid)
                        except Exception as e:
                            _log(f"scanner: live position gather failed: {type(e).__name__}: {e}")
                        # Also honor explicit include list(s) from the dashboard
                        # (Add Strategy modal for a brand-new product without
                        # an existing sleeve). Uses a Redis set so successive
                        # Scan-Now clicks accumulate instead of overwriting.
                        # Consumed once per scan (deleted after read).
                        try:
                            import redis as _redis
                            _r = _redis.from_url(redis_url)
                            members = _r.smembers("silver-swing:scanner:refresh_include_set") or set()
                            for m in members:
                                pid = m.decode() if isinstance(m, bytes) else str(m)
                                pid = pid.strip()
                                if pid:
                                    forced.add(pid)
                            if members:
                                _r.delete("silver-swing:scanner:refresh_include_set")
                            # Legacy key from earlier a47a094 deploy — read + delete
                            # once so any stale requests still count.
                            legacy = _r.get("silver-swing:scanner:refresh_include")
                            if legacy:
                                legacy = legacy.decode() if isinstance(legacy, bytes) else legacy
                                for pid in str(legacy).split(","):
                                    pid = pid.strip()
                                    if pid:
                                        forced.add(pid)
                                _r.delete("silver-swing:scanner:refresh_include")
                        except Exception as e:
                            _log(f"scanner: refresh_include read failed: {type(e).__name__}: {e}")
                        trigger = "user request" if requested else "auto interval"
                        _log(f"scanner: running one scan ({trigger}, force_include={sorted(forced)})")
                        ranking = fetch_and_rank(
                            _coinbase_for_scanner, top_n=10,
                            force_include=list(forced),
                        )
                        write_ranking_to_redis(redis_url, ranking, generated_at=now)
                        last_scanner = now
                        if auto_due and not requested:
                            last_scanner_auto = now
                        elif requested:
                            # User request also counts as a fresh auto tick.
                            last_scanner_auto = now
                except Exception as e:
                    _log(f"scanner refresh failed: {type(e).__name__}: {e}")
                    last_scanner = now  # back off on repeated failure

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
