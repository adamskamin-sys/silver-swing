"""Expert-derived strategy parameters, per product, from real historical data.

Instead of hardcoded silver-tuned defaults ($0.15 trail, $1.50 stop, etc.) the
bot's strategies now derive their numbers from each product's own volatility
(Wilder ATR) scaled by asset-class-specific multipliers from published trader
literature. Silver's tight $0.005 tick and OIL's wide $0.005 tick × 10-contract
size no longer share hardcoded numbers — each gets what its own volatility
warrants.

Provenance — verified against primary sources (2026 evidence review). Each
technique is tagged [CANONICAL] (correctly-cited foremost source), [CONVENTION]
(a reasonable round number, NOT an empirically-proven optimum), or [CORRECTED]
(a prior misattribution now fixed).

  - [CANONICAL] Wilder ATR (Wilder 1978, "New Concepts in Technical Trading
    Systems") — the volatility unit. 14 periods, Wilder smoothing (a=1/N).
    Volatility-scaling is the single best-evidenced idea in the trend
    literature (see EVIDENCE below), so ATR-as-unit is the most defensible
    choice in this whole framework.
  - [CANONICAL] Turtle System (Dennis / Faith, "Way of the Turtle"): N = 20-day
    ATR; 2N stop. NOTE the bot uses the 2N stop but NOT the Turtle's canonical
    Donchian 20/55-day breakout ENTRIES — a known gap (see ROADMAP).
  - [CANONICAL] Le Beau / Lucas ("Technical Traders Guide to Computer Analysis
    of the Futures Markets", 1992): Chandelier = HighestHigh(22) - 3×ATR(22).
    Our chandelier=3.0×ATR matches the source. The ~0.5×ATR breakout buffer is
    [CONVENTION] (echoes the Turtle 1/2 N), not a crisp Le Beau rule.
  - [CANONICAL] Van Tharp ("Trade Your Way to Financial Freedom"): the 1R /
    R-multiple risk unit. His volatility stops run ~2.7-3.4x 10-day ATR, so a
    "~2-3xATR stop" is a fair paraphrase. The 2.0xATR stop itself is
    [CONVENTION] (Turtle 2N), sensible but not empirically optimal.
  - [CORRECTED] Re-entry after a volatility/pullback contraction. This was
    previously mis-cited as "Van Tharp SafeZone." SafeZone is actually
    Alexander Elder's ("Come Into My Trading Room", 2002) and is built on
    Directional-Movement penetrations x a 2-3 coefficient — NOT a "1xATR
    contraction." Our reanchor_x_atr=1.0 is therefore a HOUSE RULE inspired by
    the re-enter-after-contraction concept, not Elder's or Tharp's mechanism.
    Implement Elder's real DM-based rule or keep this as an explicit house rule
    (do not attribute it to Tharp).
  - [CORRECTED] Crypto band widening. Kaufman's real, canonical contribution is
    the Efficiency Ratio and the PRINCIPLE that bands/stops should be
    volatility-proportional (they self-widen as vol rises) — his major works
    predate crypto and prescribe no "25-33% wider" number. Since ATR ALREADY
    auto-widens for crypto, the extra fixed bump risks double-counting; treat
    it as a HOUSE adjustment justified by crypto's lower Efficiency Ratio /
    higher noise, not a Kaufman prescription.
  - [CANONICAL] Ederington-Lee (J.Finance 1993) / Andersen-Bollerslev
    (J.Finance 1998): scheduled macro releases drive concentrated, short-lived
    volatility spikes — basis for news-blackout windows (~15 min before/~30 after).

EVIDENCE the underlying approach works (peer-reviewed):
  - Time-series momentum / trend: Moskowitz, Ooi & Pedersen (2012, JFE),
    "Time Series Momentum" — 58 futures, pooled t~4.34; vol-scaled.
  - Cross-sectional momentum: Jegadeesh & Titman (1993, J.Finance) — ~1%/mo.
  - Trend over a century: Hurst, Ooi & Pedersen (2017, JPM / AQR).
  - Vol targeting for SIZING: Harvey et al. (2018, JPM); Moreira-Muir (2017).
  - Stops, honestly: Kaminski & Lo (2014) — stops add value ONLY under
    momentum/positive autocorrelation at monthly+ horizons, and HURT under
    mean-reversion/random-walk. Han-Zhou-Zhu (2016): a stop doubled momentum's
    Sharpe by truncating the left tail. Clare et al. (2013): adding a fixed
    stop to a trend system that already exits on trend-change can HURT ("a
    change of trend is the best stop loss"). => the chandelier (trend) exit is
    the primary; treat the 2xATR stop as a catastrophic floor, not the main exit.

ROADMAP — foremost evidence-backed techniques currently MISSING (see the
crew review): (1) a time-series-momentum / 200-day-SMA trend ENTRY filter
(MOP 2012, Faber 2007) — the biggest gap, and it's what makes stops work;
(2) volatility-targeted position SIZING (Turtle "Unit"; Harvey 2018);
(3) Donchian 20/55-day breakout entries (Turtle) in place of the bare 0.5xATR
buffer; (4) a regime filter (Faber). Do NOT per-asset-tune the multipliers on
short backtests (overfitting — Bailey/Lopez de Prado); keep round conventional
numbers and sensitivity-test.

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
        "reanchor_x_atr": 1.0,      # HOUSE RULE (re-enter after ~1xATR contraction).
                                    # NOT "Van Tharp SafeZone" — SafeZone is Elder's
                                    # DM-based rule. See module docstring [CORRECTED].
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
        # Crypto: wider bands for 24/7 markets + higher noise. This ~25-33%
        # bump is a HOUSE adjustment justified by crypto's lower Efficiency
        # Ratio, NOT a Kaufman-prescribed number — and note ATR already
        # auto-widens for crypto, so this stacks on top. See docstring [CORRECTED].
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
