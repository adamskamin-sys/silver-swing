"""How well are the experts doing? Do we have real edge?

Adam 2026-07-15: honest assessment of whether the strategy is
making money AND whether the edge is real (statistically) vs noise.

Pulls from three sources:
  * sleeve_cycle_completed events — cycle_pnl, gross, fees, slippage
  * resting_stop_filled_credited events — direct-credited cycles
  * per-sleeve state — realized_pnl, cycles, recent_cycle_pnls

Reports:
  [1] Portfolio-level $/day + total realized + cycles/day
  [2] Per-sleeve breakdown (best/worst)
  [3] Statistical edge check (t-test on cycle_pnl vs zero)
  [4] Regime check: last 7d vs prior 7d — decay/improvement?
  [5] Fee drag: fees as % of gross
  [6] Actionable verdict

Read-only. Usage:
    python3 diag_expert_edge.py                    # all products, all history
    python3 diag_expert_edge.py PRODUCT_ID         # one product only
"""
from __future__ import annotations
import math
import os
import sys
import time
from collections import defaultdict


def _fmt_money(x) -> str:
    try:
        return f"${x:>10.2f}"
    except Exception:
        return "$    —"


def _welch_p_value_gt_zero(sample: list[float]) -> float:
    """One-sample t-test: is the mean > 0 with statistical significance?

    Returns approx p-value from a two-tailed normal approximation of the
    t-distribution. Adequate for n>=30 (central limit theorem); crude for
    smaller samples but at least indicative.

    p < 0.05 = "edge is real" (mean cycle_pnl is significantly > 0)
    p < 0.20 = "some evidence of edge but noisy"
    p >= 0.20 = "coinflip; no reliable edge"
    """
    if not sample or len(sample) < 3:
        return 1.0
    n = len(sample)
    mean = sum(sample) / n
    if mean <= 0:
        return 1.0  # negative mean = no positive edge, don't need p
    var = sum((x - mean) ** 2 for x in sample) / (n - 1)
    if var <= 0:
        return 0.0 if mean > 0 else 1.0  # perfectly constant + positive = infinite edge
    sem = math.sqrt(var / n)
    t = mean / sem
    # Convert t to approx p (one-sided) using normal approx.
    # p = 1 - Phi(t) where Phi is standard normal CDF.
    # erf-based approximation.
    p = 0.5 * (1.0 - math.erf(t / math.sqrt(2)))
    return max(min(p, 1.0), 0.0)


def _verdict_for_edge(pnl_per_day: float, p_value: float,
                       cycles: int, avg_per_cycle: float) -> str:
    """Actionable verdict based on the numbers."""
    if cycles < 20:
        return (f"INSUFFICIENT DATA — only {cycles} cycles. "
                f"Need 30+ for a reliable read. Keep running.")
    if pnl_per_day <= 0:
        return (f"❌ LOSING — average ${pnl_per_day:.2f}/day across {cycles} cycles. "
                f"Halt fresh sleeves and diagnose before scaling.")
    if p_value >= 0.20:
        return (f"⚠️ NOISE — ${pnl_per_day:.2f}/day but p={p_value:.3f} "
                f"means this could be coinflip. Keep sample size growing "
                f"before scaling.")
    if p_value >= 0.05:
        return (f"🟡 EDGE UNCERTAIN — ${pnl_per_day:.2f}/day, p={p_value:.3f}. "
                f"Some evidence but not statistically significant. Continue "
                f"as-is; don't scale size until p<0.05.")
    return (f"✅ REAL EDGE — ${pnl_per_day:.2f}/day across {cycles} cycles, "
            f"p={p_value:.3f}. Statistically significant. Scale carefully.")


def main() -> None:
    product_filter = sys.argv[1] if len(sys.argv) > 1 else None
    tenant = "adam-live"

    print("=" * 118)
    print(f"EXPERT EDGE AUDIT — tenant={tenant}"
          + (f"  product={product_filter}" if product_filter else " · all products"))
    print("=" * 118)

    # Load trade log for cycle events
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    except Exception as e:
        print(f"\n✗ trade log load failed: {e}")
        return

    all_cycles = []  # {ts, symbol, sleeve_id, cycle_pnl, gross, fees, slippage}
    for e in log.events():
        if not isinstance(e, dict):
            continue
        et = str(e.get("event_type") or "")
        sym = str(e.get("symbol") or "")
        if product_filter and sym != product_filter:
            continue
        if et == "sleeve_cycle_completed":
            all_cycles.append({
                "ts": float(e.get("ts") or 0),
                "symbol": sym,
                "sleeve_id": str(e.get("sleeve_id") or ""),
                "cycle_pnl": float(e.get("cycle_pnl") or 0),
                "gross": float(e.get("gross") or 0),
                "fees": float(e.get("fees") or 0),
                "slippage_dollars": float(e.get("slippage_dollars") or 0),
            })
        elif et == "resting_stop_filled_credited":
            profit = e.get("profit")
            if profit is not None:
                all_cycles.append({
                    "ts": float(e.get("ts") or 0),
                    "symbol": sym,
                    "sleeve_id": str(e.get("sleeve_id") or ""),
                    "cycle_pnl": float(profit or 0),
                    "gross": float(profit or 0),  # net = gross for resting-stop credit
                    "fees": 0.0,
                    "slippage_dollars": 0.0,
                })

    if not all_cycles:
        print(f"\n· No completed cycles found in the trade log yet.")
        print(f"  Bot may be young or trade log rotated.")
        return

    all_cycles.sort(key=lambda x: x["ts"])
    first_ts = all_cycles[0]["ts"]
    last_ts = all_cycles[-1]["ts"]
    days_running = max((last_ts - first_ts) / 86400.0, 1e-6)
    total_cycles = len(all_cycles)
    total_pnl = sum(c["cycle_pnl"] for c in all_cycles)
    total_gross = sum(c["gross"] for c in all_cycles)
    total_fees = sum(c["fees"] for c in all_cycles)
    total_slip = sum(c["slippage_dollars"] for c in all_cycles)
    avg_per_cycle = total_pnl / total_cycles
    cycles_per_day = total_cycles / days_running
    pnl_per_day = total_pnl / days_running
    win_cycles = sum(1 for c in all_cycles if c["cycle_pnl"] > 0)
    win_rate = win_cycles / total_cycles

    print(f"\n[1] PORTFOLIO-LEVEL — since first completed cycle:")
    print(f"    Days running:        {days_running:.1f}")
    print(f"    Total cycles:        {total_cycles}")
    print(f"    Total realized:      {_fmt_money(total_pnl)}")
    print(f"    Total gross:         {_fmt_money(total_gross)}")
    print(f"    Total fees paid:     {_fmt_money(total_fees)}")
    print(f"    Fee drag on gross:   "
          f"{(100.0 * total_fees / total_gross) if total_gross > 0 else 0:.1f}%")
    print(f"    Slippage $:          {_fmt_money(total_slip)}")
    print(f"    Cycles/day:          {cycles_per_day:.2f}")
    print(f"    Avg $/cycle:         {_fmt_money(avg_per_cycle)}")
    print(f"    Win rate:            {win_rate * 100:.1f}%  ({win_cycles}/{total_cycles})")
    print(f"    Realized $/day:      {_fmt_money(pnl_per_day)}")

    # Statistical edge check across ALL cycles
    p_val = _welch_p_value_gt_zero([c["cycle_pnl"] for c in all_cycles])
    print(f"\n[2] STATISTICAL EDGE CHECK (one-sample t-test, mean > 0):")
    print(f"    p-value:  {p_val:.4f}   (lower = stronger evidence of real edge)")
    print(f"    Verdict:  {_verdict_for_edge(pnl_per_day, p_val, total_cycles, avg_per_cycle)}")

    # Per-sleeve breakdown
    per_sleeve = defaultdict(lambda: {"n": 0, "pnl": 0.0, "gross": 0.0,
                                       "fees": 0.0, "wins": 0,
                                       "pnl_list": []})
    for c in all_cycles:
        k = (c["symbol"], c["sleeve_id"])
        s = per_sleeve[k]
        s["n"] += 1
        s["pnl"] += c["cycle_pnl"]
        s["gross"] += c["gross"]
        s["fees"] += c["fees"]
        s["pnl_list"].append(c["cycle_pnl"])
        if c["cycle_pnl"] > 0:
            s["wins"] += 1

    print(f"\n[3] PER-SLEEVE BREAKDOWN (sorted by total realized):")
    print(f"    {'SYMBOL':22s} {'SLEEVE':14s} {'N':>4s} {'REALIZED':>12s} "
          f"{'AVG $/CYC':>12s} {'WIN %':>8s} {'p-value':>10s}")
    print(f"    {'-' * 90}")
    for (sym, sid), s in sorted(per_sleeve.items(), key=lambda kv: -kv[1]["pnl"]):
        p = _welch_p_value_gt_zero(s["pnl_list"])
        wr = s["wins"] / s["n"] if s["n"] else 0
        avg = s["pnl"] / s["n"] if s["n"] else 0
        print(f"    {sym:22s} {sid:14s} {s['n']:>4d} "
              f"{_fmt_money(s['pnl']):>12s} {_fmt_money(avg):>12s} "
              f"{wr * 100:>7.1f}% {p:>10.4f}")

    # Regime check: last 7 days vs prior 7 days
    print(f"\n[4] EDGE DECAY / IMPROVEMENT — last 7 days vs prior 7 days:")
    now = time.time()
    last_7d = [c for c in all_cycles if c["ts"] >= now - 7 * 86400]
    prior_7d = [c for c in all_cycles if now - 14 * 86400 <= c["ts"] < now - 7 * 86400]
    def slice_stats(cycles):
        n = len(cycles)
        if n == 0:
            return (0, 0.0, 0.0, 0.0)
        pnl = sum(c["cycle_pnl"] for c in cycles)
        wins = sum(1 for c in cycles if c["cycle_pnl"] > 0)
        return (n, pnl, pnl / max(1, n), wins / max(1, n))
    n1, pnl1, avg1, wr1 = slice_stats(last_7d)
    n0, pnl0, avg0, wr0 = slice_stats(prior_7d)
    print(f"    {'':30s} {'CYCLES':>10s} {'REALIZED':>12s} "
          f"{'AVG $/CYC':>12s} {'WIN %':>8s}")
    print(f"    {'Last 7d:':30s} {n1:>10d} {_fmt_money(pnl1):>12s} "
          f"{_fmt_money(avg1):>12s} {wr1 * 100:>7.1f}%")
    print(f"    {'Prior 7d (day 8-14):':30s} {n0:>10d} {_fmt_money(pnl0):>12s} "
          f"{_fmt_money(avg0):>12s} {wr0 * 100:>7.1f}%")
    if n0 > 5 and n1 > 5:
        delta = avg1 - avg0
        if delta > 0.5 * abs(avg0) if avg0 else False:
            print(f"    → IMPROVING (avg $/cycle up ${delta:+.2f})")
        elif delta < -0.5 * abs(avg0) if avg0 else False:
            print(f"    → DECAYING (avg $/cycle down ${delta:+.2f}) — worth investigating")
        else:
            print(f"    → STABLE (avg $/cycle Δ ${delta:+.2f})")

    # Bottom line
    print(f"\n[5] BOTTOM LINE:")
    projected_year = pnl_per_day * 252  # trading-day count
    projected_month = pnl_per_day * 21
    print(f"    Extrapolated (naive): ${projected_month:.2f}/month · "
          f"${projected_year:.2f}/year at current rate")
    print(f"    {_verdict_for_edge(pnl_per_day, p_val, total_cycles, avg_per_cycle)}")
    if total_fees > 0 and total_gross > 0:
        fee_drag_pct = 100.0 * total_fees / total_gross
        if fee_drag_pct > 30:
            print(f"    ⚠️  FEE DRAG HIGH: {fee_drag_pct:.1f}% of gross went to fees. "
                  f"Consider wider spreads or maker-only tighter fills.")

    print("=" * 118)


if __name__ == "__main__":
    main()
