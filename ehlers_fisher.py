"""ehlers_fisher.py — John Ehlers' Fisher Transform.

Reference: John F. Ehlers, "Cybernetic Analysis for Stocks and Futures"
(Wiley, 2004), Ch. 1 "The Fisher Transform." Also *Rocket Science for
Traders* (2001) and *Cycle Analytics for Traders* (2013).

Purpose
-------
The Fisher Transform converts a bounded input series into an unbounded
one with an approximately Gaussian distribution, MASSIVELY amplifying
signals at price extremes. Where standard oscillators (RSI, Stochastic)
saturate near their upper/lower bounds, Fisher goes to ±infinity, so
turning points become dramatically visible.

Formula
-------
Given a "value" v in [-1, +1]:
    Fisher(v) = 0.5 * ln((1 + v) / (1 - v))

Standard pipeline:
    1. Normalize recent price range to [-1, +1] over N bars.
    2. Feed through the Fisher transform.
    3. Signal on Fisher crossing its 1-bar lag (turning point detected).

Trigger crossovers of Fisher and its previous value flag CYCLE INFLECTION
POINTS earlier than RSI/Stoch would (per Ehlers, 2004 Ch. 1 figures 1.4-
1.7 comparing signal-to-noise).

Usage in the expert stack
-------------------------
- **Entry inflection:** Fisher crossing above its previous value near a
  recent low = high-EV mean-reversion buy candidate.
- **Exit inflection:** Fisher crossing below its previous value near a
  recent high = high-EV sell candidate.
- **Complements Ehlers cycle_phase (from ehlers.py):** cycle_phase tells
  you WHERE in the cycle you are (0..1); Fisher Transform tells you IF
  a turn has occurred (sharp crossover signal).

Fail-safe: returns None on insufficient data.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence


DEFAULT_PERIOD = 10


def fisher_transform(prices: Sequence[float],
                     period: int = DEFAULT_PERIOD) -> Optional[dict]:
    """Compute the Fisher Transform value and its 1-bar lag.

    Args:
        prices: sequence of recent closes. Need >= period + 2 for a value.
        period: normalization window (default 10, per Ehlers).

    Returns:
        {
            "fisher": <current fisher value>,
            "fisher_prev": <fisher value 1 bar ago>,
            "crossover": "up" | "down" | "none",
            "reason": <short interpretation>,
        }
        or None if insufficient data.

    Signals:
        * crossover="up" (fisher > fisher_prev, and prev was <= 0) →
          strong mean-reversion BUY signal
        * crossover="down" (fisher < fisher_prev, and prev was >= 0) →
          strong mean-reversion SELL signal
    """
    ps = [float(p) for p in (prices or []) if p is not None]
    if len(ps) < period + 2:
        return None

    # Ehlers' pipeline: normalize each price to [-1, +1] over the last N bars,
    # smooth the normalized value, then apply the Fisher transform.
    values = []
    for i in range(len(ps) - period, len(ps)):
        window = ps[max(0, i - period + 1):i + 1]
        if len(window) < 2:
            values.append(0.0)
            continue
        lo = min(window)
        hi = max(window)
        rng = hi - lo
        if rng <= 0:
            values.append(0.0)
        else:
            # Normalize to [-1, +1]
            norm = 2 * ((ps[i] - lo) / rng) - 1.0
            values.append(norm)

    # Smooth normalized values with 5-bar EMA-ish (Ehlers uses 5-bar WMA);
    # apply Fisher transform. Clip to avoid ln(0) or ln(negative).
    def _fisher(v: float) -> float:
        vc = max(-0.999, min(0.999, v))
        return 0.5 * math.log((1 + vc) / (1 - vc))

    # Recursive smoother, standard Ehlers form: v_smooth = 0.33 * v + 0.67 * v_prev
    if len(values) < 2:
        return None
    smooth = values[0]
    fishers = []
    for v in values[1:]:
        smooth = 0.33 * v + 0.67 * smooth
        fishers.append(_fisher(smooth))

    if len(fishers) < 2:
        return None

    fisher_now = fishers[-1]
    fisher_prev = fishers[-2]

    # Detect crossover
    if fisher_now > fisher_prev and fisher_prev <= 0:
        crossover = "up"
        reason = (f"Fisher crossed up from {fisher_prev:.3f} to {fisher_now:.3f} "
                  "— mean-reversion buy inflection detected")
    elif fisher_now < fisher_prev and fisher_prev >= 0:
        crossover = "down"
        reason = (f"Fisher crossed down from {fisher_prev:.3f} to {fisher_now:.3f} "
                  "— mean-reversion sell inflection detected")
    else:
        crossover = "none"
        reason = f"Fisher {fisher_now:.3f} (prev {fisher_prev:.3f}) — no crossover"

    return {
        "fisher": round(fisher_now, 4),
        "fisher_prev": round(fisher_prev, 4),
        "crossover": crossover,
        "reason": reason,
    }
