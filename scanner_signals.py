"""Entry-signal scoring for the scanner (crew).

Applies the expert regime + microstructure stack to CANDIDATE contracts at
discovery time, so the scanner ranks what to enter and flags what to avoid —
instead of only ranking by swing frequency / spread. Each candidate gets a
recommendation:

  - TREND-ENTER   : clean trend (high Kaufman Efficiency Ratio), non-toxic — good
                    for a trend-following / momentum entry.
  - SWING-OK      : ranging / mean-reverting and calm — fine for the buy-low /
                    sell-high swing the bot is built around.
  - CASCADE-SHORT : a crash is happening on this product RIGHT NOW — a momentum-
                    short opportunity to "join the run" (actionable only if the
                    offensive flip is enabled; otherwise it's an AVOID for longs).
  - AVOID         : toxic flow (high VPIN) or chop — don't catch the falling
                    knife, don't get whipsawed.

Read-only. Composes regime.classify_regime + crash_guard.crash_assessment +
reversal.cascade_signal. Feed a candidate's candles (+ its microstructure
snapshot if available) and it returns the call.
"""

from __future__ import annotations

from typing import Optional

import regime as _regime
import crash_guard as _crash
import reversal as _reversal


def _returns(candles):
    cs = []
    for c in candles or []:
        v = c.get("close") if isinstance(c, dict) else getattr(c, "close", None)
        if v is not None:
            cs.append(float(v))
    return [(cs[i] - cs[i - 1]) / cs[i - 1] for i in range(1, len(cs)) if cs[i - 1]]


def entry_assessment(candles, ms: Optional[dict] = None, ofi: Optional[float] = None,
                     cfg: Optional[dict] = None) -> dict:
    """Classify a candidate contract for entry. `ms` = its MicrostructureFilter
    snapshot (vpin/ofi/obi/...) if available; `ofi` an optional order-flow value."""
    c = cfg or {}
    reg = _regime.classify_regime(candles)
    er = reg.get("efficiency_ratio")

    crash = (_crash.crash_assessment(ms or {}, _returns(candles), "FLAT",
                                     {**c, "guard_enabled": True})
             if ms else {"severity": "none", "direction": None})
    casc = _reversal.cascade_signal(candles, ofi=ofi, cfg={**c, "cascade_enabled": True})

    vpin = (ms or {}).get("vpin")
    toxic = vpin is not None and float(vpin) >= float(c.get("vpin_avoid", 0.70))
    cascading = crash.get("severity") == "crash" or casc.get("cascade")
    direction = crash.get("direction") or casc.get("direction")

    if cascading and direction == "DOWN":
        rec, reason = "CASCADE-SHORT", "crash in progress — short-momentum opportunity (join only if flip enabled)"
    elif cascading and direction == "UP":
        rec, reason = "AVOID", "up-cascade / squeeze in progress — don't chase a long into it"
    elif toxic:
        rec, reason = "AVOID", f"toxic flow (VPIN {float(vpin):.2f}) — don't enter into forced flow"
    elif reg["regime"] == "trend" and er is not None and er >= 0.40:
        rec, reason = "TREND-ENTER", f"clean trend (ER {er}) — trend-following entry"
    elif reg["regime"] == "mean_revert":
        rec, reason = "SWING-OK", "ranging / mean-reverting — suits the buy-low/sell-high swing"
    elif reg["regime"] == "chop":
        rec, reason = "AVOID", "choppy / no regime — whipsaw risk"
    else:
        rec, reason = "SWING-OK", "calm / neutral"

    # A 0..1 entry-quality score for ranking (higher = more attractive to enter now).
    q = 0.5
    if rec == "TREND-ENTER":
        q = min(1.0, 0.6 + (er or 0) * 0.4)
    elif rec == "SWING-OK":
        q = 0.55
    elif rec == "CASCADE-SHORT":
        q = 0.7   # attractive, but only for the offensive flip path
    elif rec == "AVOID":
        q = 0.15

    return {
        "recommendation": rec,
        "reason": reason,
        "entry_quality": round(q, 3),
        "regime": reg["regime"],
        "efficiency_ratio": er,
        "vol_state": reg.get("vol_state"),
        "toxic": toxic,
        "cascading": bool(cascading),
        "direction": direction,
    }


def rank_candidates(candidates, cfg: Optional[dict] = None) -> list[dict]:
    """candidates: list of {"symbol", "candles", "ms"?, "ofi"?}. Returns each
    with its entry_assessment attached, sorted best-to-enter first (AVOID last)."""
    out = []
    for cand in candidates:
        a = entry_assessment(cand.get("candles"), cand.get("ms"), cand.get("ofi"), cfg)
        out.append({"symbol": cand.get("symbol"), **a})
    out.sort(key=lambda r: r["entry_quality"], reverse=True)
    return out
