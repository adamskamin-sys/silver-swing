"""Dynamic Kelly-fraction sizing.

Van Tharp (Trade Your Way, ch. 14): full Kelly is theoretically optimal
but has ~50% expected drawdown. Half-Kelly halves the drawdown; quarter-
Kelly (0.25×) is what most professional systems actually run.

Ralph Vince (The Handbook of Portfolio Mathematics) extends Kelly to
non-binary outcomes; the formula reduces to:

    f* = p × W - q × L
         --------------
              W × L

where p = win probability, q = 1-p, W = avg win, L = avg loss (both as
positive numbers). We compute p, W, L from the sleeve's recent_cycle_pnls
history (the same list that drives loss-streak auto-disable).

Applied as a multiplier on cfg.qty:
    effective_qty = round(cfg.qty × min(kelly_fraction, kelly_f*))

Safety rails:
  - Never scales UP beyond cfg.qty (only equal or down)
  - Ignored while cycles < min_cycles (default 8) — sample too small
  - Ignored when p, W, L can't be estimated cleanly
  - Kelly ceiling capped at kelly_fraction (default 0.25 = quarter Kelly)
"""

from __future__ import annotations

from typing import Optional


DEFAULT_MIN_CYCLES = 8
DEFAULT_KELLY_FRACTION = 0.25  # Van Tharp quarter-Kelly


def compute_kelly_multiplier(
    recent_cycle_pnls: list,
    kelly_fraction: float = DEFAULT_KELLY_FRACTION,
    min_cycles: int = DEFAULT_MIN_CYCLES,
) -> Optional[float]:
    """Return a size multiplier in (0, 1] based on the sleeve's cycle history.

    Returns None (caller should use full cfg.qty) when insufficient data or
    no meaningful estimate can be made. Never returns > 1.0 — Kelly can be
    used to size DOWN a static allocation but this module refuses to
    size up (leverage/margin risk is the caller's responsibility).
    """
    if not recent_cycle_pnls or len(recent_cycle_pnls) < min_cycles:
        return None
    wins = [p for p in recent_cycle_pnls if p > 0]
    losses = [-p for p in recent_cycle_pnls if p < 0]  # losses as positive
    if not wins or not losses:
        # All wins or all losses — Kelly undefined. If all wins, safe to
        # use full size (return 1.0). If all losses, sleeve should already
        # have been auto-disabled by loss-streak logic.
        if wins and not losses:
            return 1.0
        return None
    n = len(recent_cycle_pnls)
    p = len(wins) / n
    q = 1.0 - p
    W = sum(wins) / len(wins)
    L = sum(losses) / len(losses)
    if W <= 0 or L <= 0:
        return None
    # Vince's formula: f* = (p*W - q*L) / (W*L). Can go negative when the
    # sleeve is a net loser — treat that as "don't trade at all" (0.0) so
    # the caller can decide to halt.
    kelly_star = (p * W - q * L) / (W * L)
    kelly_star = max(0.0, kelly_star)
    # Cap at kelly_fraction (safety). Never return > 1.0 so we can only
    # size DOWN existing allocation, never up.
    return min(1.0, kelly_star * kelly_fraction)


def size_from_qty(cfg_qty: int, multiplier: Optional[float]) -> int:
    """Apply the multiplier to cfg_qty. Rounds to nearest int with a floor
    of 1 (never zero — that would deactivate the sleeve; the caller should
    use halt logic for that instead)."""
    if multiplier is None:
        return int(cfg_qty)
    return max(1, int(round(cfg_qty * multiplier)))
