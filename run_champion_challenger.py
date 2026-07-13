#!/usr/bin/env python3
"""Run the champion-challenger evaluator on REAL candles. Local only — needs
your Coinbase credentials + your live config. READ-ONLY: never trades or edits.

Usage (from the repo root):
    # sweep every tracked product on the tenant
    python3 run_champion_challenger.py --days 30

    # or scope to one symbol via CLI flag (NOT via SWING_SYMBOL env — that
    # env var is set on the Render bot service for the live loop, and if the
    # sweep honored it, it would silently collapse to that one product)
    python3 run_champion_challenger.py --symbol SLR-27AUG26-CDE --days 30

What it does per symbol:
  - reads the LIVE config as the "champion"
  - builds two trail-distance challenger variants (tighter / wider)
  - fetches the last N days of 5-min candles
  - evaluates every config OUT-OF-SAMPLE via walk-forward
  - prints a conservative promotion recommendation (usually: keep champion)

Note: this assumes SwingConfig keys match expert_tuner._cfg_for_grid
(trail_distance, contract_size, margin_per_contract, ...). If a key differs on
your setup, adjust the two spots marked [ADJUST].
"""

import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone


def _evaluate_one(symbol: str, champion: dict, coinbase, days: int,
                  wide_grid: bool = False):
    """Walk-forward CC for a single symbol. Returns (ok, report_or_msg)."""
    from backtest import fetch_candles, run_backtest
    from paper_broker import PaperConfig
    from expert_tuner import _make_trader_factory
    from expert_params import compute_atr
    import champion_challenger as cc

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    try:
        candles = fetch_candles(coinbase.client, symbol, start, end, granularity="FIVE_MINUTE")
    except Exception as e:
        return False, f"fetch_candles error: {type(e).__name__}: {e}"
    if len(candles) < 200:
        return False, f"only {len(candles)} candles (need >= 200)"
    atr = compute_atr(candles, 14)

    spec = {}
    try:
        spec = coinbase.contract_spec() or {}
    except Exception:
        pass
    tick = float(champion.get("tick_size") or spec.get("price_increment") or 0.005)
    # contract_size: prefer champion cfg → spec from Coinbase → 50 last resort.
    # Sleeve-heavy configs often lack top-level contract_size; spec has it for
    # every product (verified in broker.contract_spec:349).
    contract_size = float(champion.get("contract_size") or spec.get("contract_size") or 50)
    paper_cfg = PaperConfig(
        product_id=symbol,
        contract_size=contract_size,
        tick_size=tick,
        fee_per_fill=float(champion.get("fee_per_contract_roundtrip", 4.68)) / 2.0,
        margin_per_contract=float(champion.get("margin_per_contract", 275.0)),
        starting_balance=100_000.0,
        slippage_ticks=1.0,
    )

    # Adam 2026-07-13: variants must touch BOTH the top-level primary
    # SwingConfig fields AND every sleeve's fields — sleeves are where the
    # real trading happens on Adam's live setup, and they carry their own
    # sell_px / buy_px / trail_distance. Only scaling the primary yielded
    # identical backtest metrics across all configs. Scale both bands
    # (sell/buy) and trail — one variant hits every exit_mode.
    seed_price = candles[len(candles) // 2].close

    def _scale_node(node: dict, mult: float) -> None:
        # sell/buy band around the mid                             [ADJUST]
        sell = node.get("sell_px"); buy = node.get("buy_px")
        if sell is not None and buy is not None:
            s = float(sell); b = float(buy)
            if s > b:
                mid = (s + b) / 2.0
                half = ((s - b) / 2.0) * mult
                node["sell_px"] = round(mid + half, 6)
                node["buy_px"] = round(mid - half, 6)
        # trailing-stop distance                                    [ADJUST]
        td = node.get("trail_distance")
        if td is not None:
            node["trail_distance"] = round(float(td) * mult, 6)

    def _variant(mult: float) -> dict:
        c = copy.deepcopy(champion)
        _scale_node(c, mult)
        for s in (c.get("sleeves") or []):
            if isinstance(s, dict):
                _scale_node(s, mult)
        return c

    # Default grid: coarse tighter/wider (3-point). --wide-grid: 6-point
    # response curve (0.75, 0.90, 1.10, 1.25, 1.40 + champion) so we can see
    # if "wider is better" is monotonic or peaks somewhere. ~2× runtime.
    if wide_grid:
        configs = {
            "champion": champion,
            "0.75x": _variant(0.75),
            "0.90x": _variant(0.90),
            "1.10x": _variant(1.10),
            "1.25x": _variant(1.25),
            "1.40x": _variant(1.40),
        }
    else:
        configs = {
            "champion": champion,
            "tighter": _variant(0.75),
            "wider": _variant(1.25),
        }

    def run_fn(cfg, cs):
        _, factory = _make_trader_factory(cfg, symbol, seed_price)
        return run_backtest(factory, paper_cfg, cs)

    try:
        report = cc.evaluate_challengers(
            candles, configs, run_fn,
            champion="champion", n_splits=4, embargo=5, min_edge_pct=10.0,
        )
    except Exception as e:
        return False, f"evaluate_challengers error: {type(e).__name__}: {e}"
    report["_candles"] = len(candles)
    report["_atr"] = round(atr, 6)
    return True, report


def _list_tracked_symbols(store, tenant: str) -> list[str]:
    """Every non-meta symbol in the tenant. We don't require product_id/
    contract_size at the top level — sleeve-heavy configs put those inside
    the sleeve dicts. Per-symbol backtest failures surface in the SWEEP
    SUMMARY (status='error') rather than being silently dropped, which is
    the honest way to expose configs that need attention.
    Adam 2026-07-13: filter was too strict; adam-live had 18 products but
    only 1 (SLR) had contract_size at the config root."""
    out = []
    try:
        symbols = store.list_symbols(tenant) or []
    except Exception:
        symbols = []
    for sym in symbols:
        if sym.startswith("__"):
            continue
        out.append(sym)
    return sorted(set(out))


def main() -> int:
    days = 30
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])

    # Only scope to a single symbol if EXPLICITLY passed via --symbol.
    # SWING_SYMBOL env var is set on the Render bot service for the live loop,
    # and if we honor it here, the sweep silently collapses to that one product
    # (Adam's 18-symbol adam-live tenant → "1 symbol(s)" runs). CC needs its
    # own opt-in flag.
    single_symbol = None
    if "--symbol" in sys.argv:
        single_symbol = sys.argv[sys.argv.index("--symbol") + 1]
    wide_grid = "--wide-grid" in sys.argv
    data_dir = os.getenv("SWING_DATA_DIR", "data")

    from state_store import make_store
    from broker import BrokerConfig, CoinbaseBroker

    store = make_store(data_dir)

    # Adam 2026-07-13: "every tracked product and every future product."
    # LIVE contracts live under the "-live" suffixed tenant (e.g., "adam-live"),
    # NOT the bare "adam" tenant (which is paper). Auto-discover the live
    # tenant so `run_champion_challenger.py` without env-vars does the right
    # thing. Env SWING_TENANT overrides for edge cases.
    # Always print tenant + symbol diagnostics up top — makes it obvious what
    # the sweep is (and isn't) seeing. Adam 2026-07-13 kept getting "1 symbol"
    # for reasons we couldn't diagnose without this visibility.
    all_tenants = list(store.list_tenants() or [])
    print(f"[diag] tenants in store: {all_tenants}")

    env_tenant = os.getenv("SWING_TENANT")
    if env_tenant:
        tenant = env_tenant
        print(f"[diag] SWING_TENANT override: {tenant!r}")
    else:
        live_tenants = [t for t in all_tenants if t.endswith("-live")]
        if live_tenants:
            tenant = live_tenants[0]
        else:
            tenant = "adam"
        print(f"[diag] auto-picked tenant: {tenant!r}")

    raw_symbols = list(store.list_symbols(tenant) or [])
    print(f"[diag] {tenant}: raw list_symbols count = {len(raw_symbols)}")
    print(f"[diag] {tenant}: raw list_symbols = {raw_symbols}")

    if single_symbol:
        symbols = [single_symbol]
    else:
        symbols = _list_tracked_symbols(store, tenant)
        print(f"[diag] {tenant}: after _list_tracked_symbols filter = {len(symbols)} symbols")
        if not symbols:
            print(f"No tracked products for tenant {tenant!r}. "
                  f"Try SWING_TENANT=<tenant> or SWING_SYMBOL=<product>.")
            return 1

    print(f"Champion-challenger sweep: {len(symbols)} symbol(s), {days}d each")
    print("=" * 72)

    summary = []
    for i, symbol in enumerate(symbols, 1):
        champion = store.get_config(tenant, symbol)
        if not champion:
            print(f"\n[{i}/{len(symbols)}] {symbol}: no config; skip")
            summary.append({"symbol": symbol, "status": "no_config"})
            continue
        print(f"\n[{i}/{len(symbols)}] {symbol} — fetching {days}d candles ...")
        try:
            coinbase = CoinbaseBroker(BrokerConfig(product_id=symbol))
        except Exception as e:
            print(f"  broker init failed: {type(e).__name__}: {e}")
            summary.append({"symbol": symbol, "status": "broker_error", "error": str(e)})
            continue
        ok, report = _evaluate_one(symbol, champion, coinbase, days, wide_grid=wide_grid)
        if not ok:
            print(f"  {report}")
            summary.append({"symbol": symbol, "status": "error", "error": report})
            continue
        print(f"  {report['_candles']} candles, ATR(14) = {report['_atr']}")
        print(json.dumps(
            {k: v for k, v in report.items() if not k.startswith("_")},
            indent=2, default=str,
        ))
        print(f"  >>> {report.get('note')}")
        # Pull the best challenger for the summary table so Adam can see
        # WHERE the edge (if any) came from without opening the full JSON.
        challengers = (report.get("challengers") or {})
        best_name, best_return, best_worst = None, None, None
        champ_return = report.get("champion_metrics", {}).get("oos_mean_return")
        champ_worst = report.get("champion_metrics", {}).get("oos_worst_fold")
        for name, m in challengers.items():
            if name == "champion":
                continue
            r = m.get("oos_mean_return")
            if r is None:
                continue
            if best_return is None or r > best_return:
                best_return, best_name, best_worst = r, name, m.get("oos_worst_fold")
        edge_pct = None
        if isinstance(champ_return, (int, float)) and isinstance(best_return, (int, float)):
            if champ_return > 0:
                edge_pct = round((best_return - champ_return) / champ_return * 100.0, 1)
            elif best_return > champ_return:
                edge_pct = 999.0
        summary.append({
            "symbol": symbol,
            "status": "ok",
            "champion_return": champ_return,
            "champion_worst": champ_worst,
            "best_challenger": best_name,
            "best_return": best_return,
            "best_worst": best_worst,
            "edge_pct": edge_pct,
            "recommend_promote": report.get("recommend_promote"),
        })

    _print_summary_table(summary)
    return 0


def _classify(row: dict) -> str:
    """Health flag for the summary table:
      🟢 healthy — traded, positive OOS mean, worst-fold within 2× mean
      🟡 borderline — traded, positive OOS mean but noisy (worst-fold > 2× mean)
      🔴 at-risk — traded, negative OOS mean OR promotion recommended (act)
      ⚪ idle — didn't trade (all metrics 0)
      ❌ error — status='error' / 'no_config' / 'broker_error'
    """
    st = row.get("status")
    if st != "ok":
        return "❌"
    r = row.get("champion_return") or 0.0
    w = row.get("champion_worst") or 0.0
    if row.get("recommend_promote"):
        return "🔴 promote"
    if r == 0.0 and w == 0.0:
        return "⚪ idle"
    if r < 0:
        return "🔴 losing"
    if r > 0 and abs(w) > 2.0 * r:
        return "🟡 noisy"
    return "🟢 healthy"


def _fmt_money(v) -> str:
    if v is None:
        return "     —"
    try:
        return f"{v:+8.2f}"
    except Exception:
        return f"{v!s:>8}"


def _print_summary_table(summary: list) -> None:
    print("\n" + "=" * 100)
    print("SWEEP SUMMARY")
    print("=" * 100)
    # Rank by champion return desc so highest-earning products are on top.
    ranked = sorted(summary, key=lambda r: (r.get("champion_return") or 0.0), reverse=True)
    header = f"{'FLAG':<14} {'SYMBOL':<22} {'CHAMP $':>9} {'WORST $':>9} {'BEST':<8} {'CHALL $':>9} {'CHALL WORST':>12} {'EDGE %':>8}"
    print(header)
    print("-" * len(header))
    for row in ranked:
        flag = _classify(row)
        edge = row.get("edge_pct")
        edge_s = f"{edge:+7.1f}" if isinstance(edge, (int, float)) else "     —"
        print(f"{flag:<14} {row['symbol']:<22} "
              f"{_fmt_money(row.get('champion_return')):>9} "
              f"{_fmt_money(row.get('champion_worst')):>9} "
              f"{(row.get('best_challenger') or '—'):<8} "
              f"{_fmt_money(row.get('best_return')):>9} "
              f"{_fmt_money(row.get('best_worst')):>12} "
              f"{edge_s:>8}")
    # Buckets
    buckets = {"🔴 promote": [], "🔴 losing": [], "🟡 noisy": [],
               "🟢 healthy": [], "⚪ idle": [], "❌": []}
    for row in ranked:
        buckets[_classify(row)].append(row["symbol"])
    print("\n=== By verdict ===")
    for k in ("🔴 promote", "🔴 losing", "🟡 noisy", "🟢 healthy", "⚪ idle", "❌"):
        if buckets[k]:
            print(f"  {k}: {buckets[k]}")
    # Actionable list
    promo = [r["symbol"] for r in ranked if r.get("recommend_promote")]
    print(f"\nPromotable: {promo if promo else 'none — keep all champions'}")


if __name__ == "__main__":
    sys.exit(main())
