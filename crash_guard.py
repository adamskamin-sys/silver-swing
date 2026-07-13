"""Crash / liquidation-cascade guard (crew).

Adam's #1 loss driver: a crash he can't exit in time — married to a long while
other bots/algos and forced-liquidation cascades run the price down. This is the
DECISION LAYER that turns the microstructure sensors already in microstructure.py
(VPINEstimator, TradeTapeOFI, KylesLambda, OrderBookImbalance, AggressorRun) into
an action, using the best-evidenced signals for exactly this event:

  - VPIN / flow toxicity  — Easley, Lopez de Prado & O'Hara (2012). Spiked
    BEFORE the 2010 Flash Crash. The canonical "a cascade is starting" signal.
  - Order Flow Imbalance  — Cont, Kukanov & Stoikov (2014). Extreme one-sided
    aggressive flow = the book being run over.
  - Book depletion / OBI  — Cartea-Jaimungal. Bids vanishing into forced sells.
  - Kyle's lambda         — Kyle (1985). Price impact per unit volume spikes
    when liquidity evaporates (a crash is a liquidity event).
  - Jump detection        — Lee & Mykland (2008). Separates a REAL jump/crash
    from ordinary volatility so we don't panic-exit on noise (added here; the
    only piece microstructure.py didn't already have).

PHILOSOPHY — two layers, deliberately asymmetric:
  1. DEFENSIVE (well-evidenced): when a toxic cascade runs AGAINST the position,
     FLATTEN immediately at market — bypassing the normal trailing stop, which
     is too slow for a gap-through. This is the piece that fixes "couldn't get
     out in time." On by default when the guard is enabled.
  2. OFFENSIVE (higher risk, opt-in, must be VALIDATED): additionally FLIP into
     the cascade to ride the continuation. Cascades V-snap-back, so this is
     gated behind its own flag and belongs in paper + the go-live gauntlet
     before real size.

Read-only: returns an assessment + recommended action. The strategy executes.
"""

from __future__ import annotations

import math
from statistics import mean
from typing import Optional


DEFAULT_GUARD_CONFIG = {
    "guard_enabled": False,     # master per-sleeve opt-in
    "flip_enabled": False,      # OFFENSIVE: also flip short into the crash (opt-in)
    "vpin_crash": 0.75,         # VPIN toxicity above this (their vpin_max default 0.7)
    "ofi_extreme": 0.70,        # |normalized OFI| this strong = one-sided run
    "obi_extreme": 0.65,        # |order-book imbalance| this strong = depletion
    "kyle_spike_x": 3.0,        # Kyle's lambda vs its baseline
    "jump_sigma": 4.0,          # Lee-Mykland jump statistic threshold
    "min_signals_crash": 3,     # this many sensors agreeing = CRASH
    "min_signals_warn": 2,      # this many = WARNING
}


def jump_stat(returns: list[float]) -> Optional[float]:
    """Lee-Mykland jump statistic for the latest return: |r_t| scaled by local
    realized volatility estimated from BIPOWER variation (robust to the jump
    itself). |L| >> 1 means the last move is a statistical jump, not normal vol."""
    r = [float(x) for x in returns if x is not None]
    if len(r) < 20:
        return None
    window = r[-20:]
    # bipower variation ~ integrated variance, robust to a single jump
    bpv = (math.pi / 2.0) * mean(abs(window[i]) * abs(window[i - 1]) for i in range(1, len(window)))
    sigma = math.sqrt(bpv) if bpv > 0 else 0.0
    if sigma <= 0:
        return None
    return abs(r[-1]) / sigma


def _get(ms: dict, *keys):
    for k in keys:
        if k in ms and ms[k] is not None:
            return ms[k]
    return None


def crash_assessment(ms: dict, recent_returns: list[float], position_side: str,
                     cfg: Optional[dict] = None) -> dict:
    """Assess crash/cascade risk from the microstructure snapshot + recent
    returns, relative to the current position.

    ms: MicrostructureFilter.snapshot()-style dict. Reads (flexible keys):
        vpin; trade_ofi_60s / ofi; obi / order_book_imbalance;
        kyle_lambda + kyle_lambda_baseline; aggressor_run.
    position_side: 'LONG' | 'SHORT' | 'FLAT'.

    Returns {"severity": 'none'|'warning'|'crash', "direction": 'DOWN'|'UP'|None,
             "action": 'HOLD'|'FLATTEN'|'FLATTEN_AND_FLIP', "score", "fired",
             "reason"}.
    """
    c = {**DEFAULT_GUARD_CONFIG, **(cfg or {})}
    side = str(position_side or "FLAT").upper()
    if not c.get("guard_enabled"):
        return {"severity": "none", "direction": None, "action": "HOLD",
                "score": 0, "fired": [], "reason": "crash guard disabled for this sleeve"}

    vpin = _get(ms, "vpin")
    ofi = _get(ms, "trade_ofi_60s", "ofi", "trade_ofi")
    obi = _get(ms, "obi", "order_book_imbalance")
    kyle = _get(ms, "kyle_lambda")
    kyle_base = _get(ms, "kyle_lambda_baseline", "kyle_lambda_avg")
    arun = _get(ms, "aggressor_run", "aggressor_run_len")
    jump = jump_stat(recent_returns)

    fired = []
    # direction votes: negative OFI / bid-depleted OBI / negative last return = DOWN
    down_votes = up_votes = 0

    if vpin is not None and float(vpin) >= c["vpin_crash"]:
        fired.append(f"VPIN {float(vpin):.2f} toxic")
    if ofi is not None and abs(float(ofi)) >= c["ofi_extreme"]:
        fired.append(f"OFI {float(ofi):+.2f} one-sided")
        if float(ofi) < 0:
            down_votes += 1
        else:
            up_votes += 1
    if obi is not None and abs(float(obi)) >= c["obi_extreme"]:
        fired.append(f"OBI {float(obi):+.2f} depleted")
        if float(obi) < 0:
            down_votes += 1
        else:
            up_votes += 1
    if kyle is not None and kyle_base and float(kyle_base) > 0 and float(kyle) >= c["kyle_spike_x"] * float(kyle_base):
        fired.append(f"Kyle-λ {float(kyle)/float(kyle_base):.1f}x (liquidity gone)")
    if jump is not None and jump >= c["jump_sigma"]:
        fired.append(f"jump {jump:.1f}σ")
    if arun is not None and float(arun) >= 5:
        fired.append(f"aggressor run {int(float(arun))}")

    # last-return direction as a tiebreak
    if recent_returns:
        last = recent_returns[-1]
        if last < 0:
            down_votes += 1
        elif last > 0:
            up_votes += 1
    direction = "DOWN" if down_votes > up_votes else ("UP" if up_votes > down_votes else None)

    score = len(fired)
    if score >= c["min_signals_crash"]:
        severity = "crash"
    elif score >= c["min_signals_warn"]:
        severity = "warning"
    else:
        severity = "none"

    action = "HOLD"
    if severity == "crash" and direction and side in ("LONG", "SHORT"):
        against = (direction == "DOWN" and side == "LONG") or (direction == "UP" and side == "SHORT")
        if against:
            action = "FLATTEN_AND_FLIP" if c.get("flip_enabled") else "FLATTEN"

    return {
        "severity": severity,
        "direction": direction,
        "action": action,
        "score": score,
        "fired": fired,
        "reason": (f"{severity.upper()} {direction or ''}: " + "; ".join(fired)
                   if fired else "no cascade signature"),
        "flip_to": ("SHORT" if direction == "DOWN" else "LONG") if action == "FLATTEN_AND_FLIP" else None,
    }
