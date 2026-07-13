"""Stress / red-team engine (crew).

You trade crypto perps — the fattest tails in finance. This runs the strategy
through the scenarios that actually end accounts, to find WHERE it blows up
before real size does. Two kinds:
  - synthetic shocks constructed from a base candle window: a gap-through the
    stop, a volatility explosion, a liquidity hole (limit never fills), and a
    frozen feed (flat prints).
  - historical carnage: pass real candles from LUNA / FTX / COVID-March windows.

Strategy plumbing is INJECTED (same pattern as champion_challenger): pass
`run_fn(cfg, candles) -> BacktestResult`. Read-only; it simulates, never trades.
"""

from __future__ import annotations

from typing import Callable, Optional


def _c(ts, o, h, l, cl, v=0.0):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": cl, "volume": v}


def _last_close(candles) -> float:
    c = candles[-1]
    return float(getattr(c, "close", None) if not isinstance(c, dict) else c.get("close"))


def _ts_of(c):
    return float(getattr(c, "ts", None) if not isinstance(c, dict) else c.get("ts"))


def synthetic_scenarios(base_candles, drop_pct: float = 0.30) -> dict:
    """Build stressed candle sets appended to a base window. Returns
    {scenario_name: candles} (candles as dicts, accepted by backtest.Candle-shaped
    consumers / run_fn that takes dict candles)."""
    if not base_candles:
        return {}
    base = [c if isinstance(c, dict) else _c(_ts_of(c), c.open, c.high, c.low, c.close, getattr(c, "volume", 0))
            for c in base_candles]
    px = _last_close(base)
    t0 = base[-1]["ts"]
    step = (base[-1]["ts"] - base[-2]["ts"]) if len(base) > 1 else 300

    def seq(prices):
        out = []
        prev = px
        for i, p in enumerate(prices):
            hi = max(prev, p)
            lo = min(prev, p)
            out.append(_c(t0 + step * (i + 1), prev, hi, lo, p))
            prev = p
        return out

    gap = px * (1 - drop_pct)
    scenarios = {
        # instantaneous gap straight through any stop, then it stays down
        "gap_through": base + seq([gap, gap * 0.99, gap * 1.01, gap]),
        # volatility explosion: violent whipsaw around a downtrend
        "vol_spike": base + seq([px * 0.9, px * 1.05, px * 0.85, px * 1.0, px * 0.8]),
        # liquidity hole: price teleports down with no intermediate prints to fill on
        "liquidity_hole": base + seq([gap, gap, gap]),
        # frozen feed: identical prints (stale) — does anything key off a moving mark?
        "frozen_feed": base + seq([px, px, px, px, px]),
        # slow bleed: relentless one-way grind (trend-follower's friend, mean-reverter's grave)
        "slow_bleed": base + seq([px * (1 - 0.03 * k) for k in range(1, 8)]),
    }
    return scenarios


def stress_report(cfg, run_fn: Callable, base_candles,
                  historical: Optional[dict] = None, drop_pct: float = 0.30) -> dict:
    """Run one config through every synthetic scenario (+ any historical windows
    passed as {name: candles}) and report the worst outcomes / blowups."""
    scenarios = synthetic_scenarios(base_candles, drop_pct=drop_pct)
    if historical:
        scenarios.update(historical)
    results = {}
    worst = None
    for name, candles in scenarios.items():
        try:
            res = run_fn(cfg, candles)
            ret = float(getattr(res, "total_return", 0.0))
            mdd = float(getattr(res, "max_drawdown", 0.0))
            halted = bool(getattr(res, "halted", False))
            reason = getattr(res, "halt_reason", None)
        except Exception as e:
            ret, mdd, halted, reason = None, None, None, f"ERROR: {type(e).__name__}: {e}"
        row = {"return": None if ret is None else round(ret, 2),
               "max_drawdown": None if mdd is None else round(mdd, 2),
               "halted": halted, "halt_reason": reason,
               "blowup": (ret is not None and ret < 0 and not halted)}
        results[name] = row
        if ret is not None and (worst is None or ret < worst[1]):
            worst = (name, ret)

    blowups = [n for n, r in results.items() if r["blowup"]]
    survived_unhalted = [n for n, r in results.items()
                         if r.get("return") is not None and not r["halted"] and r["return"] >= 0]
    return {
        "scenarios": results,
        "worst_scenario": worst[0] if worst else None,
        "worst_return": round(worst[1], 2) if worst else None,
        "blowups": blowups,               # lost money AND the guards didn't halt = a hole
        "clean_survivals": survived_unhalted,
        "verdict": ("HOLES FOUND: " + ", ".join(blowups)) if blowups
                   else "no uncaught blowups — guards halted or absorbed every scenario",
    }
