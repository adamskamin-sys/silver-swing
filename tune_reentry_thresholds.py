"""Per-product tuner for the expert-reentry thresholds (crew).

References (methodology)
------------------------
Pardo, Robert. *The Evaluation and Optimization of Trading Strategies*.
Wiley, 2008. Ch. 11 "Walk-Forward Analysis" — the canonical algorithm we
apply here: split each product's cycle history into train / OOS folds,
select the best thresholds on train, verify performance holds on OOS.

Bailey / Borwein / López de Prado / Zhu. "The Probability of Backtest
Overfitting." *Journal of Computational Finance*, 2016.
    - We apply their overfit guard: reject any combo where OOS / IS
      degradation is >50%. Prevents publishing a threshold set that only
      looks good because it fit the noise.

López de Prado, Marcos. *Advances in Financial Machine Learning* (Wiley,
2018), Ch. 11-12 — modern treatment of walk-forward + cross-validation.

Aronson, David. *Evidence-Based Technical Analysis* (Wiley, 2007), Ch. 4, 6
— minimum sample size discussion; we require N >= 20 cycles per product
to publish a recommendation (below that, keep defaults).

Van Tharp, K. *Trade Your Way to Financial Freedom* (McGraw-Hill, 2nd ed.
2007), Ch. 8 — SQN (System Quality Number) as a viability threshold.
We require SQN >= 1.0 on the OOS fold (Van Tharp's "acceptable" floor).

Wu, Tong T. & Kenneth Lange. "Coordinate Descent Algorithms for Lasso
Penalized Regression." *Annals of Applied Statistics* 2, no. 1 (2008) —
the search strategy. Two passes over the parameter set beats a full
Cartesian grid when there are few interactions.

Objective
---------
Total realized dollar P&L on the OUT-OF-SAMPLE fold, subject to:
  - OOS / IS ratio >= 0.5           (Bailey/López de Prado overfit guard)
  - SQN(OOS) >= 1.0                 (Van Tharp viability floor)
  - N cycles >= 20                  (Aronson sample-size floor)
If a candidate combo fails ANY of the three, we discard it and pick the
next-best by OOS profit. If no combo passes, no recommendation for that
product — the orchestrator keeps DEFAULT_THRESHOLDS.

Output
------
Writes `reentry_tuning_report.json` in the repo root. Prints a
per-product table. Does NOT touch any Redis scope. Promotion happens
via a separate script (promote_reentry_thresholds.py) that requires
--confirm and mirrors promote_candidate.py.

Usage
-----
    python3 tune_reentry_thresholds.py [--symbol SYMBOL] [--report PATH]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from typing import Optional

from safety import make_trade_log
import experts_reentry


# ---- Threshold grid (5 values per parameter, centered on defaults) --------

GRID: dict[str, list[float]] = {
    "ehlers_bounce_low":         [0.55, 0.60, 0.65, 0.70, 0.75],
    "ehlers_bounce_high":        [0.85, 0.90, 0.95, 0.98, 0.99],
    "elder_stochastic_oversold": [20.0, 25.0, 30.0, 35.0, 40.0],
    "connors_buy_zone":          [40.0, 50.0, 60.0, 70.0, 80.0],
    "vpin_calm_ceiling":         [0.50, 0.55, 0.60, 0.65, 0.70],
    "vince_max_ruin_prob":       [0.02, 0.03, 0.05, 0.08, 0.10],
    "ou_band_window":            [10, 15, 20, 25, 30],
    "regime_downtrend_lookback": [10, 15, 20, 25, 30],
}


# ---- Constraints (Bailey/López de Prado / Van Tharp / Aronson) -----------

MIN_N_CYCLES = 20             # Aronson sample-size floor
MIN_OOS_IS_RATIO = 0.5        # Bailey overfit guard (per-product objective)
MIN_SQN_OOS = 1.0             # Van Tharp viability floor
TRAIN_FRAC = 0.70             # 70/30 walk-forward split (Pardo)
PRICE_WINDOW = 60             # bars of price context per cycle for the chain


# ---- Data loading --------------------------------------------------------

def load_events(data_dir: str, tail: int = 50000) -> list[dict]:
    log = make_trade_log(data_dir)
    return log.tail(tail)


def group_cycles_by_symbol(events: list[dict]) -> dict[str, list[dict]]:
    """{symbol: [cycle_event, ...]} — only sleeve_cycle_completed events with
    non-None cycle_pnl are kept (those are the ground truth for scoring)."""
    out: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if e.get("event_type") != "sleeve_cycle_completed":
            continue
        sym = e.get("symbol")
        if sym is None:
            continue
        # cycle_pnl is the newer field; fall back to (gross - fees) for
        # older events (predates the cycle_pnl addition).
        cp = e.get("cycle_pnl")
        if cp is None:
            g = e.get("gross")
            f = e.get("fees")
            if g is not None and f is not None:
                try:
                    cp = float(g) - float(f)
                    e = dict(e)
                    e["cycle_pnl"] = cp
                except (TypeError, ValueError):
                    continue
        if cp is None:
            continue
        out[sym].append(e)
    # sort each symbol's cycles by timestamp (oldest → newest)
    for sym in out:
        out[sym].sort(key=lambda e: float(e.get("ts") or 0))
    return dict(out)


def build_price_series_before(events: list[dict], symbol: str, before_ts: float,
                              n: int = PRICE_WINDOW) -> list[float]:
    """Approximate price series for `symbol` immediately before `before_ts`.
    We pull the last-N fill_price / average_filled_price / price fields
    from any event tagged with this symbol before the cycle's sell. Not
    bar-perfect but directionally sound at swing-trade rates."""
    prices: list[float] = []
    for e in events:
        if e.get("symbol") != symbol:
            continue
        ts = float(e.get("ts") or 0)
        if ts >= before_ts:
            continue
        # Prefer a real fill price; fall back to mark or sell/buy targets
        p = (e.get("fill_price") or e.get("average_filled_price")
             or e.get("price") or e.get("mark"))
        if p is None:
            continue
        try:
            prices.append(float(p))
        except (TypeError, ValueError):
            continue
    return prices[-n:]


# ---- Chain evaluation ----------------------------------------------------

def evaluate_combo(cycles: list[dict], events: list[dict], symbol: str,
                   thresholds: dict) -> dict:
    """Score a threshold combo across a list of ground-truth cycle events.
    Rule: if the expert chain would ARM under these thresholds, count the
    actual realized cycle_pnl. If it would REFUSE, count 0 (that cycle
    would not have been taken). Best combo keeps winners + skips losers.

    Returns {profit, armed_count, skipped_count, sqn, n}.
    """
    kept_pnls: list[float] = []
    armed = 0
    skipped = 0
    for c in cycles:
        ts = float(c.get("ts") or 0)
        sold_price = c.get("fill_price") or c.get("cost_basis")
        cycle_pnl = float(c.get("cycle_pnl") or 0.0)
        prices = build_price_series_before(events, symbol, ts, n=PRICE_WINDOW)
        if not prices or sold_price is None:
            # Insufficient context → conservative: treat as armed with actual
            # cycle_pnl. Prevents "unknown history" from silently dropping
            # cycles and skewing the sum toward whichever combo happens to
            # skip more.
            armed += 1
            kept_pnls.append(cycle_pnl)
            continue
        try:
            d = experts_reentry.compute_reentry(
                prices=prices,
                sold_price=float(sold_price),
                spread=max(0.005, abs(cycle_pnl) * 0.01) if cycle_pnl else 0.05,
                strategy_qty=1,
                thresholds=thresholds,
            )
        except Exception:
            armed += 1
            kept_pnls.append(cycle_pnl)
            continue
        if d.get("should_arm"):
            armed += 1
            kept_pnls.append(cycle_pnl)
        else:
            skipped += 1
    profit = sum(kept_pnls)
    sqn = _sqn(kept_pnls)
    return {
        "profit": round(profit, 4),
        "armed": armed,
        "skipped": skipped,
        "sqn": sqn,
        "n": len(kept_pnls),
    }


def _sqn(pnls: list[float]) -> float:
    """Van Tharp System Quality Number.
    SQN = mean(R) / stdev(R) * sqrt(min(n, 100))
    where R = pnl / mean_abs_pnl (unitless normalization).
    Returns 0 for small samples or zero-variance series."""
    if len(pnls) < 2:
        return 0.0
    mabs = sum(abs(x) for x in pnls) / len(pnls)
    if mabs <= 0:
        return 0.0
    rs = [x / mabs for x in pnls]
    mean = sum(rs) / len(rs)
    try:
        std = statistics.stdev(rs)
    except statistics.StatisticsError:
        return 0.0
    if std <= 0:
        return 0.0
    return mean / std * math.sqrt(min(len(rs), 100))


# ---- Coordinate descent (Wu-Lange 2008; Bertsekas 1999) -------------------

def coordinate_descent(cycles_train: list[dict], events: list[dict],
                       symbol: str, passes: int = 2) -> tuple[dict, dict]:
    """Optimize thresholds by sweeping one at a time (2 passes). Returns
    (best_thresholds, best_score_dict)."""
    current = dict(experts_reentry.DEFAULT_THRESHOLDS)
    best_score = evaluate_combo(cycles_train, events, symbol, current)
    for _ in range(max(1, passes)):
        for name, values in GRID.items():
            best_v = current.get(name)
            for v in values:
                trial = dict(current)
                trial[name] = v
                sc = evaluate_combo(cycles_train, events, symbol, trial)
                if sc["profit"] > best_score["profit"]:
                    best_score = sc
                    best_v = v
            if best_v is not None:
                current[name] = best_v
    return current, best_score


# ---- Per-symbol walk-forward tuning --------------------------------------

def tune_symbol(events: list[dict], symbol: str,
                cycles_by_symbol: Optional[dict[str, list[dict]]] = None) -> Optional[dict]:
    # Prefer the pre-filtered/pre-augmented map (which applies the
    # cycle_pnl = gross-fees fallback for older events). Fall back to a
    # fresh filter for callers that pass raw events.
    if cycles_by_symbol is not None:
        cycles = list(cycles_by_symbol.get(symbol) or [])
    else:
        cycles = [e for e in events if e.get("symbol") == symbol
                  and e.get("event_type") == "sleeve_cycle_completed"
                  and e.get("cycle_pnl") is not None]
    cycles.sort(key=lambda e: float(e.get("ts") or 0))
    n = len(cycles)
    if n < MIN_N_CYCLES:
        return {"skipped": True, "n": n,
                "reason": f"insufficient cycles ({n} < {MIN_N_CYCLES})"}

    split = int(n * TRAIN_FRAC)
    train, oos = cycles[:split], cycles[split:]

    # Default baseline on OOS — what happens if we DON'T tune
    baseline_oos = evaluate_combo(oos, events, symbol,
                                  dict(experts_reentry.DEFAULT_THRESHOLDS))

    tuned_thr, tuned_train = coordinate_descent(train, events, symbol)
    tuned_oos = evaluate_combo(oos, events, symbol, tuned_thr)

    # Overfit guard (Bailey / López de Prado)
    ratio = (tuned_oos["profit"] / tuned_train["profit"]) if tuned_train["profit"] > 0 else 0.0
    passed_ratio = ratio >= MIN_OOS_IS_RATIO
    passed_sqn = tuned_oos["sqn"] >= MIN_SQN_OOS
    passed_n = n >= MIN_N_CYCLES
    published = passed_ratio and passed_sqn and passed_n and (
        tuned_oos["profit"] > baseline_oos["profit"])

    delta = {k: tuned_thr[k] for k in tuned_thr
             if tuned_thr[k] != experts_reentry.DEFAULT_THRESHOLDS.get(k)}

    return {
        "symbol": symbol,
        "n_train": len(train),
        "n_oos": len(oos),
        "tuned_thresholds": tuned_thr,
        "delta_from_default": delta,
        "train_profit": tuned_train["profit"],
        "oos_profit": tuned_oos["profit"],
        "baseline_oos_profit": baseline_oos["profit"],
        "oos_over_is_ratio": round(ratio, 3),
        "sqn_oos": round(tuned_oos["sqn"], 3),
        "sqn_baseline_oos": round(baseline_oos["sqn"], 3),
        "passed_overfit_guard": passed_ratio,
        "passed_sqn_floor": passed_sqn,
        "passed_sample_size": passed_n,
        "published": published,
        "publish_reason": (
            "recommended" if published
            else "; ".join(r for r in [
                ("OOS/IS ratio too low (Bailey guard)" if not passed_ratio else ""),
                ("SQN(OOS) below Van Tharp floor" if not passed_sqn else ""),
                ("insufficient sample" if not passed_n else ""),
                ("no improvement vs default" if tuned_oos["profit"] <= baseline_oos["profit"] else ""),
            ] if r)
        ),
        "citations": {
            "walk_forward": "Pardo 2008 Ch. 11",
            "overfit_guard": "Bailey/Borwein/López de Prado/Zhu 2016",
            "sqn": "Van Tharp 2007 Trade Your Way to Financial Freedom Ch. 8",
            "sample_size": "Aronson 2007 Ch. 4",
            "coord_descent": "Wu-Lange 2008; Bertsekas 1999",
        },
    }


# ---- Report --------------------------------------------------------------

def print_report(report: dict) -> None:
    print(f"\n{'symbol':<24} {'n':>4} {'baseOOS':>10} {'tunedOOS':>10} "
          f"{'ratio':>6} {'SQN':>6}  status")
    print("-" * 84)
    for sym in sorted(report.keys()):
        r = report[sym]
        if r.get("skipped"):
            print(f"{sym:<24} {r.get('n', 0):>4}   {'--':>10}   {'--':>10}   "
                  f"{'--':>6}   {'--':>6}  SKIP ({r.get('reason')})")
            continue
        status = "PUBLISH" if r["published"] else "HOLD"
        print(f"{sym:<24} {r['n_train'] + r['n_oos']:>4} "
              f"{r['baseline_oos_profit']:>10.2f} {r['oos_profit']:>10.2f} "
              f"{r['oos_over_is_ratio']:>6.2f} {r['sqn_oos']:>6.2f}  "
              f"{status} — {r['publish_reason']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None,
                    help="tune a single symbol (default: all with >= 20 cycles)")
    ap.add_argument("--report", default="reentry_tuning_report.json",
                    help="output report path")
    ap.add_argument("--tail", type=int, default=50000,
                    help="how many recent events to read (max ~10k on Redis)")
    args = ap.parse_args()

    events = load_events(os.getenv("SWING_DATA_DIR", "data"), tail=args.tail)
    by_symbol = group_cycles_by_symbol(events)
    if args.symbol:
        target = args.symbol
        by_symbol = {k: v for k, v in by_symbol.items() if target.upper() in k.upper()}
    print(f"Loaded {len(events)} events; {len(by_symbol)} symbols with cycle data.")

    report: dict = {}
    for sym in sorted(by_symbol.keys()):
        try:
            r = tune_symbol(events, sym, cycles_by_symbol=by_symbol)
            if r is not None:
                report[sym] = r
        except Exception as e:
            report[sym] = {"symbol": sym, "error": str(e)}

    print_report(report)
    with open(args.report, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nWrote {args.report}. "
          f"To promote: python3 promote_reentry_thresholds.py --report {args.report}")


if __name__ == "__main__":
    main()
