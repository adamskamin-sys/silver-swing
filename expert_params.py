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


def compute_efficiency_ratio(candles: list, period: int = 20) -> float:
    """Kaufman's Efficiency Ratio.

    ER = |close(t) - close(t-N)| / sum(|close(i) - close(i-1)|)

    Range [0, 1]:
      1.0 = pure trend (every tick moves in the same direction, no whipsaw)
      0.5 = typical mixed regime
      0.0 = pure noise (net movement zero despite lots of intra-period motion)

    Source: Perry J. Kaufman — "Trading Systems and Methods" (5th ed. 2013);
    the flagship contribution his Adaptive Moving Average (AMA) is built on.
    ER is the CANONICAL Kaufman mechanism for detecting whether current price
    action is trending vs mean-reverting.

    Application (2026-07-19 Adam): replace the arbitrary 25% crypto bump
    on stop/trail multipliers with real Kaufman ER modulation. When ER is
    low (noisy), stops widen dynamically so we don't get shaken out by
    noise. When ER is high (trending clean), stops tighten to canonical
    Van Tharp/Turtle levels.

    Returns 1.0 (neutral — no widening) when insufficient data, so callers
    that don't yet have candles fall back to canonical multipliers.
    """
    if not candles or len(candles) < period + 1:
        return 1.0

    def _close(c):
        if hasattr(c, "close"):
            return float(c.close or 0)
        return float(c.get("close") or 0)

    closes = [_close(c) for c in candles if _close(c) > 0]
    if len(closes) < period + 1:
        return 1.0

    tail = closes[-(period + 1):]
    net = abs(tail[-1] - tail[0])
    gross = sum(abs(tail[i] - tail[i - 1]) for i in range(1, len(tail)))
    if gross <= 0:
        return 1.0
    return max(0.0, min(1.0, net / gross))


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
        # 2026-07-19: crypto bump REMOVED. The former 25-50% arbitrary bump
        # is now driven dynamically by Kaufman's Efficiency Ratio via
        # compute_efficiency_ratio() + expert_params(er=...) — replaces a
        # HOUSE number with a citable canonical mechanism. When ER is low
        # (crypto is typically ER≈0.4-0.6), distances widen automatically;
        # when crypto has a clean trend day (rare, ER≈0.7-0.9), they
        # tighten toward canonical Van Tharp/Turtle levels.
        "trail_x_atr": 2.0,          # Turtle 2N
        "stop_x_atr": 2.0,           # Van Tharp 1R
        "activation_offset_x_atr": 0.5,  # Le Beau breakout buffer
        "ratchet_x_atr": 3.0,        # Le Beau chandelier
        "ratchet_activation_x_atr": 0.5,  # Van Tharp — wait for 0.5R gain
        "reanchor_x_atr": 1.0,       # HOUSE (per re-entry-after-contraction concept)
        "buy_trail_x_atr": 0.5,      # Le Beau confirmation buffer
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


def er_modulation_enabled() -> bool:
    """Master switch. Default OFF per 2026-07-19 backtest-referee NO-GO:
    the sensitivity constant hasn't been grid-search-validated on OOS
    data, and enabling it changes the numbers the expert_guard sees
    without also updating what the guard compares against — resulting
    in a drift-alert storm.

    Enable path (do all of these first):
      1. Grid-search `sensitivity ∈ {0.25, 0.35, 0.50, 0.65, 0.80}` on
         30+ days of SLR + NOL + BTC-PERP via expert_tuner-style backtest
      2. Feed the grid to tuning_overfit_report; require verdict !=
         LIKELY_OVERFIT AND positive edge on all three products
      3. Thread `er` through expert_guard._current_atr AND the five
         other callers (avg_down_advisor, expert_tuner,
         run_champion_challenger, run_go_live_check, reversal) so
         guard expected values match actual config
      4. Set SWING_ER_MODULATION_ENABLED=1

    Once flipped ON, expert_params(pid, atr, er) modulates by
    er_modulation(er) as designed. While OFF, ER is computed + reported
    for observability (dashboard, logs) but does NOT scale distances.
    """
    import os as _os
    return _os.getenv("SWING_ER_MODULATION_ENABLED", "0").lower() in ("1", "true", "yes", "on")


def er_modulation(er: float, sensitivity: float = 0.5) -> float:
    """Volatility widening multiplier from Kaufman Efficiency Ratio.

    Returns 1.0 (no widening) at ER=1 (pure trend), up to (1 + sensitivity)
    at ER=0 (pure noise). Linear interpolation between.

    sensitivity=0.5 gives:
      ER=1.0 (clean trend)    → 1.00 × canonical (Van Tharp/Turtle base)
      ER=0.7 (mild trend)     → 1.15 × canonical  (+15%)
      ER=0.5 (mixed / crypto) → 1.25 × canonical  (+25%, matches old bump)
      ER=0.3 (noisy crypto)   → 1.35 × canonical  (+35%)
      ER=0.0 (pure whipsaw)   → 1.50 × canonical  (+50% ceiling)

    Empirically matches the retired 25% crypto bump on average while
    ADAPTING to actual regime instead of hardcoding by asset class. In a
    genuine crypto trend day, ER climbs and stops tighten. In a chop day,
    ER falls and stops widen. Same mechanism runs on metals/energy/equity
    — they naturally sit at higher ER, so they see less widening.
    """
    er_clamped = max(0.0, min(1.0, er))
    return 1.0 + sensitivity * (1.0 - er_clamped)


def expert_params(product_id: str, atr: float, er: float = 1.0) -> dict[str, float]:
    """Compute all strategy parameters for a product from its ATR + ER.

    Returns actual dollar values (not multipliers). Every value is derived
    from real per-product volatility × published expert multipliers × ER
    modulation. Silver at ATR $0.09 vs oil at ATR $0.42 vs BTC at ATR $850
    all get properly scaled numbers.

    er: Kaufman Efficiency Ratio in [0, 1]. Callers who don't yet compute
        ER pass 1.0 (default) → no modulation, canonical multipliers.
    """
    m = multipliers_for(product_id)
    # Feature-flagged: while off, ignore ER and return canonical multipliers
    # so expert_guard's canonical `expected` matches the actual config.
    # Reported for observability regardless.
    mod = er_modulation(er) if er_modulation_enabled() else 1.0
    return {
        "atr": atr,
        "efficiency_ratio": round(er, 4),
        "er_modulation": round(mod, 4),
        "er_modulation_enabled": er_modulation_enabled(),
        "asset_class": asset_class_of(product_id),
        "trail_distance": round(atr * m["trail_x_atr"] * mod, 4),
        "stop_loss_distance": round(atr * m["stop_x_atr"] * mod, 4),
        "trail_activation_offset": round(atr * m["activation_offset_x_atr"] * mod, 4),
        "ratchet_distance": round(atr * m["ratchet_x_atr"] * mod, 4),
        "ratchet_activation": round(atr * m["ratchet_activation_x_atr"] * mod, 4),
        "reanchor_threshold": round(atr * m["reanchor_x_atr"] * mod, 4),
        "buy_trail_distance": round(atr * m.get("buy_trail_x_atr", 0.5) * mod, 4),
        "multipliers": m,
    }
