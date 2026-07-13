"""Almgren-Chriss (2000) — optimal execution / position slicing.

Almgren-Chriss, "Optimal Execution of Portfolio Transactions" (JFE 2000):
splitting a large order into slices trades market impact (bigger slices
hurt) against timing risk (waiting longer lets price move against you).
The optimal schedule minimizes a mean-variance objective:

    minimize  E[cost] + risk_aversion × Var[cost]

Under linear-impact assumptions the closed-form solution is exponential:
slice sizes decay over the execution horizon. For our regime (1-5
contracts per arm, seconds not minutes of horizon), the exponential
form collapses to something close to "front-load the first slice."

We use Kyle's λ (already computed in microstructure.py) as the impact
parameter. When λ is high (illiquid), slicing helps more. When λ is
low (liquid), slicing is unnecessary — one shot is fine.

Applied opt-in per sleeve. Only fires for qty > 1 — single-contract
arms have nothing to slice.

Practical caveats:
  - Coinbase's fee model rewards MAKER over TAKER, so slicing multiple
    market orders is fee-expensive. This module assumes maker orders
    with post_only, which our sleeves already run.
  - Cancel-and-replace latency (~100ms) limits how fast we can execute
    a schedule. Our floor is 500ms per slice.
"""

from __future__ import annotations

import math
from typing import Optional


DEFAULT_URGENCY_SECS = 30.0  # total horizon to complete the order
MIN_SLICE_INTERVAL_SECS = 0.5


def optimal_slice_schedule(
    total_qty: int,
    urgency_secs: float = DEFAULT_URGENCY_SECS,
    kyle_lambda: Optional[float] = None,
    risk_aversion: float = 1.0,
) -> list[tuple[float, int]]:
    """Return list of (delay_secs_from_start, qty) tuples that sum to total_qty.

    Almgren-Chriss closed-form under linear temporary + permanent impact.
    We simplify to:
      - N slices where N ≈ ceil(urgency_secs / max_slice_interval)
      - Exponential decay weights (front-loaded when λ is high)
      - Integer qty rounding with remainder assigned to first slice

    For qty <= 1 or λ near zero, returns [(0.0, total_qty)] — single shot.
    """
    total_qty = int(total_qty)
    if total_qty <= 1:
        return [(0.0, total_qty)]
    if urgency_secs < MIN_SLICE_INTERVAL_SECS:
        return [(0.0, total_qty)]
    # No impact → no reason to slice
    if kyle_lambda is None or kyle_lambda <= 0:
        return [(0.0, total_qty)]

    # Slice count: cap by qty (can't have more slices than contracts)
    max_slices = max(1, int(urgency_secs // MIN_SLICE_INTERVAL_SECS))
    n = min(total_qty, max_slices, 5)  # cap at 5 slices — diminishing returns
    if n <= 1:
        return [(0.0, total_qty)]

    # Weights: front-loaded exponential with rate proportional to λ × risk_aversion.
    # When rate=0, weights are uniform. When rate is large, first slice dominates.
    rate = min(3.0, max(0.5, float(kyle_lambda) * 1000.0 * risk_aversion))
    weights = [math.exp(-rate * i / n) for i in range(n)]
    total_w = sum(weights)
    slice_qtys = [int(round(w * total_qty / total_w)) for w in weights]
    # Fix rounding to sum exactly to total_qty
    diff = total_qty - sum(slice_qtys)
    slice_qtys[0] += diff
    # Ensure no zero-qty slices (they'd be no-ops)
    slice_qtys = [max(1, q) for q in slice_qtys]
    # Re-normalize if rounding pushed us over
    over = sum(slice_qtys) - total_qty
    if over > 0:
        for i in range(len(slice_qtys) - 1, -1, -1):
            take = min(over, slice_qtys[i] - 1)
            slice_qtys[i] -= take
            over -= take
            if over == 0:
                break
    # Timing: uniform over the horizon
    step = urgency_secs / n
    schedule = [(i * step, slice_qtys[i]) for i in range(n) if slice_qtys[i] > 0]
    return schedule


def should_slice(qty: int, kyle_lambda: Optional[float],
                 min_qty_to_slice: int = 2) -> bool:
    """Cheap gate the caller can use to skip the schedule computation."""
    if qty < min_qty_to_slice:
        return False
    if kyle_lambda is None or kyle_lambda <= 0:
        return False
    return True
