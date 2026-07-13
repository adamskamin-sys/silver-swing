"""Go-live gauntlet (crew).

One gate that a strategy or parameter change must clear before it touches real
capital. It chains the discipline agents into a single GO / NO-GO:

  1. OVERFIT   — backtest_integrity.tuning_overfit_report on the tuning grid
                 (skipped if no grid supplied). LIKELY_OVERFIT => NO-GO.
  2. TAIL      — stress_test.stress_report on the candidate. Any uncaught
                 blowup => NO-GO.
  3. OOS EDGE  — champion_challenger.evaluate_challengers, candidate vs the live
                 champion, walk-forward. Candidate must NOT be worse than the
                 champion out-of-sample (and to PROMOTE, must robustly beat it).

Strategy plumbing is injected via run_fn(cfg, candles) -> BacktestResult (same
contract as champion_challenger / stress_test). Read-only.
"""

from __future__ import annotations

from typing import Callable, Optional

import backtest_integrity as bi
import stress_test as st
import champion_challenger as cc


def gauntlet(candidate_cfg: dict, champion_cfg: dict, candles: list,
             run_fn: Callable, tuning_grid: Optional[list] = None,
             base_window: int = 200) -> dict:
    """Run the full go-live gauntlet. Returns a GO/NO-GO memo."""
    blockers = []
    checks = {}

    # 1. Overfit referee (only if a tuning grid was supplied)
    if tuning_grid:
        ref = bi.tuning_overfit_report(tuning_grid)
        checks["overfit"] = ref
        if ref["verdict"] == "LIKELY_OVERFIT":
            blockers.append(f"OVERFIT: tuning winner is a fragile spike ({ref['gap_to_second_pct']}% above #2)")
    else:
        checks["overfit"] = {"verdict": "SKIPPED", "note": "no tuning grid supplied"}

    # 2. Tail / stress red-team
    base = candles[-base_window:] if len(candles) > base_window else candles
    stress = st.stress_report(candidate_cfg, run_fn, base)
    checks["stress"] = stress
    if stress.get("blowups"):
        blockers.append(f"TAIL: uncaught blowups in {', '.join(stress['blowups'])}")

    # 3. Out-of-sample edge vs champion
    cc_report = cc.evaluate_challengers(
        candles, {"champion": champion_cfg, "candidate": candidate_cfg},
        run_fn, champion="champion", n_splits=4, embargo=5, min_edge_pct=10.0,
    )
    checks["oos"] = cc_report
    cand = (cc_report.get("challengers") or {}).get("candidate", {})
    champ = cc_report.get("champion_metrics", {})
    worse_oos = (cand.get("oos_mean_return", 0) < champ.get("oos_mean_return", 0)) or \
                (cand.get("oos_worst_fold", 0) < champ.get("oos_worst_fold", 0))
    if worse_oos:
        blockers.append("OOS: candidate is worse than the live champion out-of-sample")

    promotable = cc_report.get("recommend_promote") == "candidate"

    if blockers:
        verdict = "NO-GO"
    elif promotable:
        verdict = "GO-PROMOTE"      # clears the gauntlet AND robustly beats champion
    else:
        verdict = "GO-HOLD"          # safe to run, but no proven edge over champion

    return {
        "verdict": verdict,
        "blockers": blockers,
        "checks": checks,
        "summary": {
            "NO-GO": "Do NOT ship — " + ("; ".join(blockers) if blockers else ""),
            "GO-PROMOTE": "Clears overfit + tail + beats champion OOS — safe to promote (start small).",
            "GO-HOLD": "Safe to run at small size, but it does NOT beat your champion OOS — keep the champion unless it earns promotion.",
        }[verdict],
    }
