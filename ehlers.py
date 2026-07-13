"""John Ehlers cycle-phase detection for re-entry timing (crew).

References
----------
Ehlers, John F. *Cybernetic Analysis for Stocks and Futures: Cutting-Edge
DSP Technology to Improve Your Trading*. Wiley, 2004.
    - Ch. 5 "The Homodyne Discriminator" — dominant-cycle period estimation.
    - Ch. 7 "SineWave Indicator" — cycle phase for entry/exit timing.

Ehlers, John F. *Cycle Analytics for Traders: Advanced Technical Trading
Concepts*. Wiley, 2013.
    - Refined Hilbert-transform filters, HP roofing.

Purpose
-------
Gate re-entry to the bottom of the price cycle. In a mean-reverting regime,
buying mid-drop is a falling knife; buying at cycle trough has the highest
EV. `in_bounce_zone(prices)` returns True when the current dominant-cycle
phase places us in Ehlers' Sinewave "buy zone" — past the acceleration
phase of the drop, before the actual turn is confirmed.

Notes on the math
-----------------
The Homodyne Discriminator (Cybernetic Analysis eq. 5-1..5-9) uses a 4-tap
Hilbert transformer to derive InPhase (I) and Quadrature (Q) components of
the detrended price. Phase = atan2(Q, I). Delta phase over consecutive bars
implies instantaneous frequency; averaging yields the dominant cycle period.

We follow the canonical derivation but keep the smoother compact — Ehlers'
6-tap WMA (Cybernetic Analysis Ch. 2) is sufficient at swing-trading
sample rates (>= 1 bar/minute).
"""
from __future__ import annotations

import math
from typing import Optional, Sequence


# -- Ehlers 6-tap WMA smoother (Cybernetic Analysis Ch. 2 eq. 2-4) ----------

_WMA_WEIGHTS = (1.0, 2.0, 3.0, 3.0, 2.0, 1.0)
_WMA_NORM = sum(_WMA_WEIGHTS)


def _smooth(prices: Sequence[float]) -> list[float]:
    """Ehlers 6-tap symmetric WMA. Reduces sample noise before the
    discriminator without introducing group delay."""
    out: list[float] = []
    w = _WMA_WEIGHTS
    n = len(w)
    for i in range(len(prices)):
        if i < n - 1:
            out.append(float(prices[i]))
            continue
        acc = 0.0
        for k in range(n):
            acc += float(prices[i - k]) * w[k]
        out.append(acc / _WMA_NORM)
    return out


# -- Hilbert transformer (Cybernetic Analysis eq. 5-3) ---------------------

def _hilbert_iq(smoothed: Sequence[float]) -> tuple[list[float], list[float]]:
    """Ehlers' 4-tap Hilbert quadrature. Returns (I, Q) aligned to input.
    Detrend is a 3-bar centered difference; I is that detrend, Q is the
    Hilbert transform, both windowed to keep them 90° out of phase."""
    n = len(smoothed)
    if n < 8:
        return [], []
    detrend: list[float] = [0.0] * n
    for i in range(6, n):
        # Ehlers Cyb.An. eq. 5-3: 4-tap FIR
        detrend[i] = (0.0962 * smoothed[i]
                      + 0.5769 * smoothed[i - 2]
                      - 0.5769 * smoothed[i - 4]
                      - 0.0962 * smoothed[i - 6])
    # I leads detrend by 3 bars; Q is the detrended series itself.
    I: list[float] = [0.0] * n
    Q: list[float] = [0.0] * n
    for i in range(9, n):
        I[i] = detrend[i - 3]
        Q[i] = (0.0962 * detrend[i]
                + 0.5769 * detrend[i - 2]
                - 0.5769 * detrend[i - 4]
                - 0.0962 * detrend[i - 6])
    return I, Q


# -- Dominant cycle period (Homodyne Discriminator, eq. 5-4..5-9) ----------

def dominant_period(prices: Sequence[float],
                    min_p: int = 6, max_p: int = 50) -> Optional[float]:
    """Estimate dominant cycle period. Returns None if history too short
    or discriminator can't resolve."""
    if len(prices) < 40:
        return None
    smoothed = _smooth(prices)
    I, Q = _hilbert_iq(smoothed)
    if not I:
        return None
    # Instantaneous phase per bar, atan2(Q, I). Delta phase = frequency.
    phases: list[float] = []
    for i in range(len(I)):
        if I[i] == 0.0 and Q[i] == 0.0:
            continue
        phases.append(math.atan2(Q[i], I[i]))
    if len(phases) < 8:
        return None
    deltas: list[float] = []
    for i in range(1, len(phases)):
        d = phases[i - 1] - phases[i]  # positive for advancing phase
        # unwrap
        while d > math.pi:
            d -= 2 * math.pi
        while d < -math.pi:
            d += 2 * math.pi
        # We care about magnitude — direction is regime, handled elsewhere.
        d = abs(d)
        if d > 1e-6:
            deltas.append(d)
    if not deltas:
        return None
    # Median of the last window is more robust than the mean (Ehlers 2013).
    tail = deltas[-min(len(deltas), 16):]
    tail_sorted = sorted(tail)
    med = tail_sorted[len(tail_sorted) // 2]
    period = 2 * math.pi / med
    return max(float(min_p), min(float(max_p), period))


# -- Cycle phase / SineWave zones (Cybernetic Analysis Ch. 7) --------------

def cycle_phase(prices: Sequence[float]) -> Optional[float]:
    """Position within the current cycle in [0, 1].
    0.0 = cycle TOP, 0.5 = zero-crossing (neutral), 1.0 = cycle BOTTOM.
    Uses Ehlers' Sinewave convention: phase = atan2(Q, I) mapped to [0, 1]."""
    smoothed = _smooth(prices)
    I, Q = _hilbert_iq(smoothed)
    if not I:
        return None
    # Find the last non-zero pair.
    for i in range(len(I) - 1, -1, -1):
        if I[i] != 0.0 or Q[i] != 0.0:
            theta = math.atan2(Q[i], I[i])
            # atan2 ∈ [-π, π]. Ehlers' Sinewave: cycle TOP at +cos peak,
            # BOTTOM at -cos peak. We want BOTTOM = 1.0, so:
            # theta = 0        → cycle TOP        → phase 0
            # theta = ±π       → cycle BOTTOM     → phase 1
            # phase = |theta| / π gives that mapping.
            return abs(theta) / math.pi
    return None


def in_bounce_zone(prices: Sequence[float],
                   low: float = 0.65, high: float = 0.95) -> bool:
    """True if cycle phase sits in the bounce zone — past mid-drop
    acceleration (falling knife territory), before the cycle actually
    turns (which is where lagging indicators would confirm).
    Defaults match Ehlers' Sinewave crossover bands from Cyb.An. Ch. 7."""
    ph = cycle_phase(prices)
    if ph is None:
        return False
    return low <= ph <= high


def assess(prices: Sequence[float]) -> dict:
    """Diagnostic snapshot — dominant period, phase, and gate verdict."""
    period = dominant_period(prices)
    phase = cycle_phase(prices)
    bounce = phase is not None and 0.65 <= phase <= 0.95
    return {
        "dominant_period": period,
        "cycle_phase": phase,
        "in_bounce_zone": bounce,
        "citation": "Ehlers 2004 Cybernetic Analysis Ch. 5, 7",
    }
