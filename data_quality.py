"""Data-quality / feed-integrity checks (crew).

Garbage in, garbage out. A single bad candle corrupts the ATR, which corrupts
every expert-derived level, which corrupts every buy/sell price downstream. This
validates a candle series before it's trusted to compute ATR or drive a
backtest, and flags the classic feed pathologies:
  - crossed/invalid candles (high < low, non-positive prices)
  - time gaps (missing candles) and non-monotonic timestamps
  - duplicate timestamps
  - stale prints (long runs of identical closes = a frozen feed)
  - outliers (a single candle jumping many sigma from its neighbours)

Read-only. Returns a report; a caller decides whether to trust the data.
"""

from __future__ import annotations

from statistics import mean, pstdev
from typing import Optional


def _fields(c):
    if isinstance(c, dict):
        return (c.get("ts"), c.get("open"), c.get("high"), c.get("low"), c.get("close"))
    return (getattr(c, "ts", None), getattr(c, "open", None), getattr(c, "high", None),
            getattr(c, "low", None), getattr(c, "close", None))


def check_candles(candles, expected_step: Optional[float] = None,
                  stale_run: int = 6, outlier_sigma: float = 8.0) -> dict:
    """Validate a candle series (oldest -> newest)."""
    n = len(candles or [])
    issues = []
    if n == 0:
        return {"ok": False, "n": 0, "issues": [{"kind": "empty", "detail": "no candles"}]}

    prev_ts = None
    steps = []
    closes = []
    for i, c in enumerate(candles):
        ts, o, h, l, cl = _fields(c)
        try:
            ts = float(ts); o = float(o); h = float(h); l = float(l); cl = float(cl)
        except (TypeError, ValueError):
            issues.append({"kind": "unparseable", "index": i})
            continue
        if min(o, h, l, cl) <= 0:
            issues.append({"kind": "non_positive_price", "index": i, "ts": ts})
        if h < l:
            issues.append({"kind": "crossed_candle", "index": i, "ts": ts, "detail": f"high {h} < low {l}"})
        if not (l <= o <= h and l <= cl <= h):
            issues.append({"kind": "ohlc_out_of_range", "index": i, "ts": ts})
        if prev_ts is not None:
            dt = ts - prev_ts
            if dt <= 0:
                issues.append({"kind": "non_monotonic_ts", "index": i, "ts": ts})
            else:
                steps.append(dt)
        prev_ts = ts
        closes.append(cl)

    # infer the cadence if not given, then flag gaps
    step = expected_step
    if step is None and steps:
        step = _mode(steps)
    if step:
        gaps = sum(1 for dt in steps if dt > step * 1.5)
        if gaps:
            issues.append({"kind": "time_gaps", "count": gaps, "detail": f"{gaps} gaps > 1.5x the {step:.0f}s cadence"})

    # stale run of identical closes
    run = 1
    longest = 1
    for i in range(1, len(closes)):
        run = run + 1 if closes[i] == closes[i - 1] else 1
        longest = max(longest, run)
    if longest >= stale_run:
        issues.append({"kind": "stale_prints", "detail": f"{longest} identical closes in a row (frozen feed?)"})

    # outliers: a candle-to-candle return many sigma from the norm
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1]]
    if len(rets) >= 10:
        m, sd = mean(rets), pstdev(rets)
        if sd > 0:
            outliers = [i for i, r in enumerate(rets) if abs(r - m) > outlier_sigma * sd]
            if outliers:
                issues.append({"kind": "outlier_prints", "count": len(outliers),
                               "detail": f"{len(outliers)} candle(s) > {outlier_sigma}sigma — check for a bad print"})

    critical = {"crossed_candle", "non_positive_price", "non_monotonic_ts", "unparseable"}
    has_critical = any(i["kind"] in critical for i in issues)
    return {
        "ok": not has_critical,
        "trustworthy_for_atr": len(issues) == 0,
        "n": n,
        "inferred_step_secs": step,
        "issues": issues,
        "verdict": ("BAD DATA — do not trust for ATR/levels" if has_critical
                    else ("clean" if not issues else "usable but flagged — review issues")),
    }


def _mode(values):
    from collections import Counter
    return Counter(round(v) for v in values).most_common(1)[0][0]
