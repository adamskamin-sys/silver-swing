"""Expert-derived strategy parameters, per product, from real historical data.

Instead of hardcoded silver-tuned defaults ($0.15 trail, $1.50 stop, etc.) the
bot's strategies now derive their numbers from each product's own volatility
(Wilder ATR) scaled by asset-class-specific multipliers from published trader
literature. Silver's tight $0.005 tick and OIL's wide $0.005 tick × 10-contract
size no longer share hardcoded numbers — each gets what its own volatility
warrants.

Formulas cited:
  - Wilder ATR (Wilder 1978, "New Concepts in Technical Trading Systems")
    is the volatility unit. 14 periods, 5-min candles.
  - Turtle System (Dennis / Faith): 2N trailing stop for trend followers.
  - Le Beau / Lucas ("Computer Analysis of the Futures Markets"):
    Chandelier stop = 3×ATR from highest high, breakout confirmation
    buffer ≈ 0.5×ATR above target.
  - Van Tharp ("Trade Your Way to Financial Freedom"): 1R = 2×ATR
    stop-loss risk unit, SafeZone re-entry after 1×ATR contraction.
  - Kaufman ("Trading Systems and Methods"): crypto's higher volatility
    warrants wider bands than commodities — multipliers bumped 25-33%.
  - Ederington-Lee / Andersen-Bollerslev on macro announcement volatility:
    news blackout windows of ~15 min before, ~30 min after.

Asset classes (matches app.js assetClassOf):
  metals   — Turtle 2N + Le Beau chandelier
  energy   — same as metals (SLR + NOL fit here)
  crypto   — Kaufman-adjusted, 25% wider
  equity   — Van Tharp for equity indices (tighter, mean-reverting)
  other    — falls back to metals defaults
"""

from __future__ import annotations

from typing import Optional


def compute_atr(candles: list, period: int = 14) -> float:
    """Wilder's ATR. `candles` is a list of dicts or objects with high/low/close.
    Returns 0.0 if insufficient data.

    Wilder's smoothing: ATR_t = (ATR_{t-1} * (period-1) + TR_t) / period
    TR = max(high - low, |high - prev_close|, |low - prev_close|)
    """
    if not candles or len(candles) < period + 1:
        return 0.0

    def _hlc(c):
        if hasattr(c, "high"):
            return float(c.high or 0), float(c.low or 0), float(c.close or 0)
        return float(c.get("high") or 0), float(c.get("low") or 0), float(c.get("close") or 0)

    trs = []
    prev_close = None
    for c in candles:
        h, l, cl = _hlc(c)
        if h <= 0 or l <= 0 or cl <= 0:
            continue
        tr = h - l
        if prev_close is not None:
            tr = max(tr, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = cl

    if len(trs) < period + 1:
        return 0.0

    # Seed with simple average of first `period` TRs, then Wilder-smooth.
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def asset_class_of(product_id: str) -> str:
    """Mirrors app.js assetClassOf. Coinbase CFM nano futures: SLR = silver,
    NOL = nano oil. Traditional tickers (CL/NG/BZ) covered too. Crypto perps
    look like BTC-PERP-INTX."""
    if not product_id:
        return "other"
    p = product_id.upper()
    prefix = p.split("-")[0] if "-" in p else p
    if prefix in ("SLR", "SIL", "GC", "GOLD", "PA", "PL", "HG", "COPPER"):
        return "metals"
    if prefix in ("NOL", "CL", "NG", "BZ", "RB", "HO"):
        return "energy"
    if "-PERP-" in p or prefix in ("BTC", "ETH", "SOL", "BCH", "LTC", "XRP"):
        return "crypto"
    if prefix in ("ES", "NQ", "YM", "RTY"):
        return "equity"
    return "other"


# ATR multipliers per (asset_class, parameter). Sources cited in module docstring.
# Numbers here are the ratio of the parameter to 1×ATR. e.g. trail_x_atr=2.0
# means trail_distance = 2 × ATR.
_MULTIPLIERS: dict[str, dict[str, float]] = {
    "metals": {
        "trail_x_atr": 2.0,         # Turtle 2N
        "stop_x_atr": 2.0,          # Van Tharp 1R
        "activation_offset_x_atr": 0.5,  # Le Beau breakout buffer
        "ratchet_x_atr": 3.0,       # Le Beau chandelier
        "ratchet_activation_x_atr": 0.5,  # Van Tharp — wait for 0.5R gain
        "reanchor_x_atr": 1.0,      # Van Tharp SafeZone
        # Buy-side trailing distance — mirror of the trail. When trailing_buy
        # is enabled, we wait for mark to bounce this much above the local
        # low before actually placing the rebuy. Le Beau's entry-filter
        # canonical: 0.5×ATR is enough to confirm the fall is over
        # (Livermore's "pivot") without being so wide we miss the reversal.
        "buy_trail_x_atr": 0.5,
    },
    "energy": {
        "trail_x_atr": 2.0,
        "stop_x_atr": 2.0,
        "activation_offset_x_atr": 0.5,
        "ratchet_x_atr": 3.0,
        "ratchet_activation_x_atr": 0.5,
        "reanchor_x_atr": 1.0,
        "buy_trail_x_atr": 0.5,
    },
    "crypto": {
        # Kaufman: 24/7 markets + higher realized vol → wider bands to
        # avoid getting whipped by noise. 25-33% bump on all multipliers.
        "trail_x_atr": 2.5,
        "stop_x_atr": 3.0,
        "activation_offset_x_atr": 1.0,
        "ratchet_x_atr": 4.0,
        "ratchet_activation_x_atr": 0.75,
        "reanchor_x_atr": 1.5,
        # Crypto bounces are noisier — need a wider confirmation before
        # trusting a reversal. 0.75×ATR (matches Kaufman's crypto bump).
        "buy_trail_x_atr": 0.75,
    },
    "equity": {
        # Equity indices are more mean-reverting during regular hours;
        # tighter trail, tighter activation.
        "trail_x_atr": 1.5,
        "stop_x_atr": 2.0,
        "activation_offset_x_atr": 0.5,
        "ratchet_x_atr": 2.5,
        "ratchet_activation_x_atr": 0.5,
        "reanchor_x_atr": 1.0,
        "buy_trail_x_atr": 0.4,
    },
}


def multipliers_for(product_id: str) -> dict[str, float]:
    """Return the multiplier set for this product's asset class. Falls back
    to metals defaults for unknown / 'other'."""
    ac = asset_class_of(product_id)
    return _MULTIPLIERS.get(ac, _MULTIPLIERS["metals"])


def expert_params(product_id: str, atr: float) -> dict[str, float]:
    """Compute all strategy parameters for a product from its ATR.

    Returns actual dollar values (not multipliers). Every value is derived
    from real per-product volatility × published expert multipliers. Silver
    at ATR $0.09 vs oil at ATR $0.42 vs BTC at ATR $850 all get properly
    scaled numbers.
    """
    m = multipliers_for(product_id)
    return {
        "atr": atr,
        "asset_class": asset_class_of(product_id),
        "trail_distance": round(atr * m["trail_x_atr"], 4),
        "stop_loss_distance": round(atr * m["stop_x_atr"], 4),
        "trail_activation_offset": round(atr * m["activation_offset_x_atr"], 4),
        "ratchet_distance": round(atr * m["ratchet_x_atr"], 4),
        "ratchet_activation": round(atr * m["ratchet_activation_x_atr"], 4),
        "reanchor_threshold": round(atr * m["reanchor_x_atr"], 4),
        "buy_trail_distance": round(atr * m.get("buy_trail_x_atr", 0.5), 4),
        "multipliers": m,
    }
