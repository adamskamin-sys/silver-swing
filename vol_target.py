"""Volatility-targeted position sizing — Harvey (2018 JPM) canonical.

Option D-2 from 2026-07-19 expert-source refactor plan. Reduces
drawdowns by scaling positions inversely to realized volatility.

The canonical Harvey mechanism:
    scale = target_vol / realized_vol
    contracts = base_qty × min(scale, max_scale)

When realized vol > target, positions shrink → smaller loss on the
next tick. When realized vol < target, positions can grow up to
max_scale × base — but capped to avoid over-leveraging in eerily
calm regimes (which precede vol spikes).

Sources (peer-reviewed):
  - Harvey, Hoyle, Korgaonkar, Rattray, Sargaison, Van Hemert (2018 JPM),
    "The Impact of Volatility Targeting" — empirical demonstration that
    vol-targeting improves risk-adjusted returns across asset classes,
    primarily by REDUCING drawdown magnitude and duration.
  - Moreira, Muir (2017 J.Finance), "Volatility-Managed Portfolios" —
    same result on equity factors; vol targeting is not just a hedge-
    fund folk technique.
  - Faith (Way of the Turtle) — Turtle "Unit" is a per-market
    volatility-adjusted position size. Same mechanism at a coarser
    grain (uses N-day ATR instead of EWMA).

Compared to the existing risk_budget.py (Carver Systematic Trading):
  - Carver: sizes each sleeve to a per-sleeve daily $vol budget
    using ATR × contract_size (snapshot vol).
  - Harvey: sizes to portfolio-level realized vol using EWMA of
    returns (smoother, more responsive to regime shifts).

This module ships Harvey's version because that's what the peer-
reviewed evidence in Harvey 2018 specifically endorses.

Feature-flagged OFF by default. When on, gates the sleeve's arm-time
qty computation. Enable path documented in flag_enabled() docstring.
"""
from __future__ import annotations

import math
import os
from typing import Optional


DEFAULT_TARGET_ANNUAL_VOL = 0.15   # 15% annualized — moderate risk budget
DEFAULT_LAMBDA = 0.94              # EWMA decay — RiskMetrics standard
DEFAULT_MAX_SCALE = 2.0            # Cap upside scaling — prevents over-leverage
DEFAULT_MIN_SCALE = 0.25           # Floor — don't shrink below 1/4 of base


def flag_enabled() -> bool:
    """Master switch. Off by default per 2026-07-19 backtest-referee
    discipline. When off, this module returns base_qty unchanged.

    Enable path (do all of these first):
      1. Backtest 30-90d against control on adam-live products.
         Metric: max drawdown reduction (should decrease meaningfully),
         net $/day (should not decrease materially). Harvey 2018 shows
         a small return give-up but 20-40% drawdown reduction.
      2. Verify size clamping — no sleeve should ever exceed cfg.qty ×
         DEFAULT_MAX_SCALE (default 2×), and never below cfg.qty ×
         DEFAULT_MIN_SCALE (default 0.25×).
      3. Verify base_qty is never 0 on flag-off day (regression against
         accidental hard-zeroing).
      4. Set SWING_VOL_TARGET_ENABLED=1
    """
    return os.getenv("SWING_VOL_TARGET_ENABLED", "0").lower() in ("1", "true", "yes", "on")


def compute_realized_vol(returns: list[float], lam: float = DEFAULT_LAMBDA) -> Optional[float]:
    """EWMA realized volatility on a return series (log or arithmetic).

    Uses RiskMetrics-style EWMA:
        var_t = lambda × var_{t-1} + (1 - lambda) × r_t^2

    Seed with the sample variance of the earliest few returns.
    Returns annualized sigma assuming daily returns and 252 trading
    days. Callers using intra-day returns should scale externally
    (sqrt(bars_per_year / 252)).

    None on insufficient data (< 5 samples).
    """
    if not returns or len(returns) < 5:
        return None
    seed_n = min(5, len(returns) // 2)
    var = sum(r * r for r in returns[:seed_n]) / seed_n
    for r in returns[seed_n:]:
        var = lam * var + (1 - lam) * (r * r)
    if var <= 0:
        return None
    daily_sigma = math.sqrt(var)
    # Assume input is daily returns; annualize by sqrt(252). Callers with
    # different bar frequencies should convert to daily before calling.
    return daily_sigma * math.sqrt(252.0)


def size_scale(realized_vol: Optional[float],
               target_vol: float = DEFAULT_TARGET_ANNUAL_VOL,
               max_scale: float = DEFAULT_MAX_SCALE,
               min_scale: float = DEFAULT_MIN_SCALE) -> float:
    """Harvey 2018 scaling factor: target / realized, clamped.

    Returns 1.0 (no scaling) when realized_vol is None (insufficient
    data — permissive fail-open matches every other filter in the
    codebase).

    Clamped to [min_scale, max_scale] so vol collapse can't blow up
    position size and vol spike can't zero it out entirely.
    """
    if realized_vol is None or realized_vol <= 0:
        return 1.0
    raw = target_vol / realized_vol
    return max(min_scale, min(max_scale, raw))


def adjusted_qty(base_qty: int,
                 returns: list[float],
                 target_vol: float = DEFAULT_TARGET_ANNUAL_VOL,
                 lam: float = DEFAULT_LAMBDA) -> int:
    """Apply Harvey vol-target sizing to a base contract qty.

    Returns int(base_qty × size_scale), floored at 1 (never zero —
    that's a halt decision, not a sizing decision).

    When flag is OFF (default), returns base_qty unchanged. This is
    the sole integration seam — callers pass base_qty and get back
    an adjusted qty without any awareness of the underlying mechanism.
    """
    if not flag_enabled():
        return int(base_qty)
    if base_qty <= 0:
        return int(base_qty)
    rv = compute_realized_vol(returns, lam=lam)
    scale = size_scale(rv, target_vol=target_vol)
    scaled = int(round(base_qty * scale))
    return max(1, scaled)
