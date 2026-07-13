"""Find a NEW swing channel after a significant drop (crew).

Swing trading = trading a channel (buy the floor, sell the ceiling). A dramatic
drop is a STRUCTURAL BREAK: the old channel is dead and you must re-estimate a
new one from post-drop data. The literature's recipe, in three steps:

  1. DETECT THE BREAK. The recent level has shifted down beyond noise.
     Bai-Perron (2003) multiple structural breaks; Hamilton (1989) Markov
     regime-switching; Adams-MacKay (2007) Bayesian online change-point. Here:
     a robust CUSUM-style test — recent mean vs the earlier mean, scaled by
     volatility. A break is when |shift| >> sigma.
  2. WAIT FOR THE NEW REGIME TO STABILIZE. Engle ARCH / Bollerslev GARCH:
     volatility clusters after a crash, so a range measured mid-cascade is
     noise. Only estimate once recent vol has contracted vs the drop's vol.
     (cascade_state supplies this all-clear in the live loop.)
  3. ESTIMATE THE NEW CHANNEL from post-drop data only:
       center = Kaufman-Efficiency-Ratio-weighted adaptive mean (KAMA idea):
                in a still-trending tape lean to the latest price; in a ranging
                tape lean to the mean. Converges to the new oscillation center.
       width  = k * ATR (Keltner) or k * stdev (Bollinger) on post-drop bars.
       floor / ceiling = Donchian low / high of the post-drop window (Turtle).
     Buy target sits near the floor, sell target near the ceiling — the swing.

Read-only, pure. Feed it the recent closes (post-drop window) + optional ATR;
it returns the new channel + suggested buy/sell targets for the reanchor.
"""

from __future__ import annotations

from statistics import mean, pstdev
from typing import Optional


DEFAULT_CHANNEL_CONFIG = {
    "half_window": 20,        # bars per half for the break test / estimation
    "break_sigma": 2.0,       # |mean shift| >= this * sigma = a structural break
    "width_k": 1.5,           # channel half-width = k * ATR (or k * stdev if no ATR)
    "er_period": 10,          # Kaufman Efficiency Ratio lookback for the adaptive center
    "target_frac": 0.8,       # place buy/sell this fraction of the way to floor/ceiling
    "min_bars": 12,           # need at least this many bars to say anything
}


def efficiency_ratio(prices: list[float], period: int) -> Optional[float]:
    """Kaufman ER: |net change| / sum(|bar changes|) over `period`. 1 = clean
    directional move, ~0 = choppy. Drives how fast the center adapts."""
    if len(prices) < period + 1:
        return None
    window = prices[-(period + 1):]
    net = abs(window[-1] - window[0])
    noise = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
    if noise <= 0:
        return None
    return net / noise


def _atr_proxy(prices: list[float], n: int) -> float:
    """Close-to-close ATR proxy (mean abs bar move) over the last n bars."""
    w = prices[-(n + 1):] if len(prices) > n else prices
    if len(w) < 2:
        return 0.0
    return mean(abs(w[i] - w[i - 1]) for i in range(1, len(w)))


def find_channel(prices: list[float], atr: Optional[float] = None,
                 cfg: Optional[dict] = None) -> dict:
    """Estimate the new swing channel from a post-drop close series.

    Returns {broke, stabilized, center, upper, lower, floor, ceiling, width,
             buy_px, sell_px, efficiency_ratio, reason}. `broke` True means a
             structural down-shift is detected; `stabilized` True means recent
             vol has contracted enough to trust the estimate. Both True is the
             green light to re-anchor onto (buy_px, sell_px)."""
    c = {**DEFAULT_CHANNEL_CONFIG, **(cfg or {})}
    ps = [float(p) for p in (prices or []) if p is not None]
    hw = int(c["half_window"])
    if len(ps) < max(int(c["min_bars"]), hw + 2):
        return {"broke": False, "stabilized": False, "reason": "insufficient history",
                "center": ps[-1] if ps else None}

    recent = ps[-hw:]
    earlier = ps[-2 * hw:-hw] if len(ps) >= 2 * hw else ps[:-hw]

    recent_mean = mean(recent)
    earlier_mean = mean(earlier) if earlier else recent_mean
    # volatility of the earlier (drop) window vs recent (candidate-new) window
    earlier_sd = pstdev(earlier) if len(earlier) >= 2 else 0.0
    recent_sd = pstdev(recent) if len(recent) >= 2 else 0.0

    # ---- 1. structural break: level shifted DOWN beyond noise ---------------
    shift = recent_mean - earlier_mean
    scale = earlier_sd if earlier_sd > 0 else (recent_sd if recent_sd > 0 else abs(earlier_mean) * 1e-6 or 1.0)
    broke_down = shift < 0 and abs(shift) >= c["break_sigma"] * scale

    # ---- 2. new-regime stabilized: recent vol contracted vs the drop's vol ---
    stabilized = (earlier_sd <= 0) or (recent_sd <= 0.8 * earlier_sd)

    # ---- 3. estimate the new channel from post-drop (recent) bars -----------
    er = efficiency_ratio(ps, int(c["er_period"]))
    # adaptive center (KAMA idea): still-directional -> latest price; ranging -> mean
    a = er if er is not None else 0.3
    center = a * ps[-1] + (1 - a) * recent_mean

    unit = float(atr) if (atr and atr > 0) else _atr_proxy(ps, hw)
    half_width = float(c["width_k"]) * unit if unit > 0 else recent_sd
    floor = min(recent)                      # Donchian low of the new window
    ceiling = max(recent)                    # Donchian high

    upper = center + half_width
    lower = center - half_width
    # keep the band inside the realized post-drop range (don't invent levels)
    lower = max(lower, floor)
    upper = min(upper, ceiling) if ceiling > center else center + half_width

    # buy near the floor, sell near the ceiling — target_frac of the way out
    tf = float(c["target_frac"])
    buy_px = center - tf * (center - lower)
    sell_px = center + tf * (upper - center)

    return {
        "broke": bool(broke_down),
        "stabilized": bool(stabilized),
        "center": round(center, 6),
        "upper": round(upper, 6),
        "lower": round(lower, 6),
        "floor": round(floor, 6),
        "ceiling": round(ceiling, 6),
        "width": round(half_width, 6),
        "buy_px": round(buy_px, 6),
        "sell_px": round(sell_px, 6),
        "efficiency_ratio": round(er, 3) if er is not None else None,
        "shift": round(shift, 6),
        "reason": (
            "structural down-shift detected; " if broke_down else "no significant break; ") + (
            "new regime stabilized — re-anchor onto buy_px/sell_px"
            if stabilized else "vol still elevated — wait before re-anchoring"),
    }
