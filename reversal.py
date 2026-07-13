"""Evidence-based position reversal (crew).

When enabled per-sleeve, this decides whether to FLIP the net position
(long<->short) instead of only selling back to flat. Coinbase nets to one
position, so a "reversal" is a flip through flat, not a hedge.

The signal is built from the foremost trend / trend-reversal literature, with a
regime gate that is MANDATORY — reversing in chop is the fastest way to get
shredded (Kaminski-Lo 2014):

  1. REGIME GATE (Kaminski-Lo; Hurst): only ever flip in a confirmed TREND.
     In mean-revert / chop, never reverse. This gate alone is the difference
     between "captures downtrends" and "donates in a range".
  2. DONCHIAN BREAK (Turtle System, Dennis/Faith): a new N-period extreme
     against the current position is THE canonical trend-reversal trigger.
     20 periods = System 1 (default), 55 = System 2 (slower).
  3. TIME-SERIES MOMENTUM SIGN (Moskowitz-Ooi-Pedersen 2012; Faber 2007): the
     lookback return / price-vs-long-MA must have flipped against the position.
  4. ATR CONFIRMATION BUFFER (Le Beau ~0.5xATR): the break must exceed the
     trigger by a volatility buffer so noise doesn't flip you.

A reversal fires only when: regime is trending AND the opposite-side Donchian
break is confirmed by the ATR buffer AND the TSM sign agrees. All three must
point the same way. Read-only signal; the strategy executes the flip.
"""

from __future__ import annotations

from typing import Optional

import regime as _regime
try:
    from expert_params import compute_atr as _compute_atr
except Exception:  # pragma: no cover
    _compute_atr = None


DEFAULT_REVERSAL_CONFIG = {
    "reversal_enabled": False,      # OPT-IN per sleeve — off by default
    "donchian_period": 20,          # Turtle System 1
    "tsm_lookback": 50,             # ~ momentum lookback (bars)
    "atr_buffer_x": 0.5,            # Le Beau breakout confirmation buffer
    "require_regime_trend": True,   # Kaminski-Lo gate — do NOT disable lightly
}


def _closes(candles):
    out = []
    for c in candles or []:
        v = getattr(c, "close", None)
        if v is None and isinstance(c, dict):
            v = c.get("close")
        if v is not None:
            out.append(float(v))
    return out


def donchian(candles, period: int):
    """(upper, lower) = highest high / lowest low over the last `period` bars
    (excluding the current bar, so a NEW extreme on the current bar can trigger)."""
    hs, ls = [], []
    for c in candles or []:
        h = getattr(c, "high", None) if not isinstance(c, dict) else c.get("high")
        l = getattr(c, "low", None) if not isinstance(c, dict) else c.get("low")
        if h is not None and l is not None:
            hs.append(float(h)); ls.append(float(l))
    if len(hs) < period + 1:
        return None, None
    window_h = hs[-(period + 1):-1]
    window_l = ls[-(period + 1):-1]
    return max(window_h), min(window_l)


def tsm_sign(closes, lookback: int) -> int:
    """+1 if the lookback return is up, -1 if down, 0 if flat/insufficient."""
    if len(closes) < lookback + 1:
        return 0
    r = closes[-1] - closes[-1 - lookback]
    return 1 if r > 0 else (-1 if r < 0 else 0)


def should_reverse(candles, current_side: str, cfg: Optional[dict] = None) -> dict:
    """Decide whether to flip the position.

    current_side: 'LONG' | 'SHORT' | 'FLAT'.
    Returns {"reverse": bool, "to_side": 'LONG'|'SHORT'|None, "reason": str,
             "signals": {...}} — read-only.
    """
    c = {**DEFAULT_REVERSAL_CONFIG, **(cfg or {})}
    side = str(current_side or "FLAT").upper()

    if not c.get("reversal_enabled"):
        return {"reverse": False, "to_side": None, "reason": "reversal disabled for this sleeve"}
    if side not in ("LONG", "SHORT"):
        return {"reverse": False, "to_side": None, "reason": "no open position to reverse"}

    closes = _closes(candles)
    if len(closes) < max(c["donchian_period"], c["tsm_lookback"]) + 2:
        return {"reverse": False, "to_side": None, "reason": "insufficient history"}

    reg = _regime.classify_regime(candles)
    atr = _compute_atr(candles, 14) if _compute_atr else 0.0
    buf = (atr or 0.0) * float(c["atr_buffer_x"])
    upper, lower = donchian(candles, int(c["donchian_period"]))
    price = closes[-1]
    tsm = tsm_sign(closes, int(c["tsm_lookback"]))

    signals = {
        "regime": reg.get("regime"), "hurst": reg.get("hurst"),
        "donchian_upper": upper, "donchian_lower": lower,
        "price": price, "atr_buffer": round(buf, 6), "tsm_sign": tsm,
    }

    # Mandatory regime gate.
    if c.get("require_regime_trend") and reg.get("regime") != "trend":
        return {"reverse": False, "to_side": None,
                "reason": f"regime is {reg.get('regime')} — only reverse in a confirmed trend (Kaminski-Lo)",
                "signals": signals}

    if upper is None or lower is None:
        return {"reverse": False, "to_side": None, "reason": "no Donchian channel", "signals": signals}

    # LONG -> SHORT: price breaks a NEW N-period LOW by the ATR buffer AND TSM turned down.
    if side == "LONG":
        broke_down = price < (lower - buf)
        if broke_down and tsm <= 0:
            return {"reverse": True, "to_side": "SHORT",
                    "reason": f"trend flip DOWN: {int(c['donchian_period'])}-bar low broken by >{c['atr_buffer_x']}xATR, TSM negative, regime=trend",
                    "signals": signals}
        return {"reverse": False, "to_side": None,
                "reason": "long trend intact (no confirmed down-break)", "signals": signals}

    # SHORT -> LONG: price breaks a NEW N-period HIGH by the ATR buffer AND TSM turned up.
    broke_up = price > (upper + buf)
    if broke_up and tsm >= 0:
        return {"reverse": True, "to_side": "LONG",
                "reason": f"trend flip UP: {int(c['donchian_period'])}-bar high broken by >{c['atr_buffer_x']}xATR, TSM positive, regime=trend",
                "signals": signals}
    return {"reverse": False, "to_side": None,
            "reason": "short trend intact (no confirmed up-break)", "signals": signals}


# ── Cascade detector: "join the liquidation run" ────────────────────────────
# When forced liquidations / clustered stops get hit, price gaps one way on a
# climax of RANGE + VOLUME expansion, and the move self-reinforces (liquidations
# beget liquidations as price runs the next tier of stops/liq levels). Coinbase
# gives no direct liquidation feed, so we infer the cascade from its
# microstructure SIGNATURE: a range-and-volume climax bar in one direction,
# optionally confirmed by extreme one-sided order-flow imbalance
# (Cont-Kukanov-Stoikov 2014; Easley-Lopez de Prado-O'Hara flow toxicity).
#
# HIGH RISK: this is momentum-chasing into a violent move — the cascade can
# EXHAUST and snap back (you catch the exact bottom/top). The caller MUST use a
# tight stop, small size, and paper-test it. This is a tactical FAST trigger,
# distinct from the slow trend-flip above.

CASCADE_CONFIG = {
    "cascade_enabled": False,   # separate opt-in from trend reversals
    "lookback": 30,
    "range_x": 3.0,             # current true-range >= 3x the recent average
    "vol_x": 3.0,               # current volume >= 3x the recent average
    "ofi_min": 0.6,             # |OFI| this strong confirms one-sided forced flow
    "min_bar_move_pct": 0.5,    # the climax bar itself moved at least this %
}


def _tr(candles):
    trs = []
    prev_c = None
    for c in candles:
        h = float(c.get("high")) if isinstance(c, dict) else float(c.high)
        l = float(c.get("low")) if isinstance(c, dict) else float(c.low)
        cl = float(c.get("close")) if isinstance(c, dict) else float(c.close)
        t = h - l
        if prev_c is not None:
            t = max(t, abs(h - prev_c), abs(l - prev_c))
        trs.append(t)
        prev_c = cl
    return trs


def cascade_signal(candles, ofi: Optional[float] = None, cfg: Optional[dict] = None) -> dict:
    """Detect a likely stop/liquidation cascade to JOIN. Returns
    {"cascade": bool, "direction": 'DOWN'|'UP'|None, "reason", "signals"}."""
    c = {**CASCADE_CONFIG, **(cfg or {})}
    if not c.get("cascade_enabled"):
        return {"cascade": False, "direction": None, "reason": "cascade trigger disabled"}
    lb = int(c["lookback"])
    if len(candles or []) < lb + 2:
        return {"cascade": False, "direction": None, "reason": "insufficient history"}

    last = candles[-1]
    o = float(last.get("open")) if isinstance(last, dict) else float(last.open)
    cl = float(last.get("close")) if isinstance(last, dict) else float(last.close)
    trs = _tr(candles)
    avg_tr = sum(trs[-lb - 1:-1]) / lb if lb else 0.0
    range_ratio = (trs[-1] / avg_tr) if avg_tr > 0 else 0.0

    vols = []
    for x in candles:
        v = x.get("volume") if isinstance(x, dict) else getattr(x, "volume", None)
        vols.append(float(v) if v is not None else None)
    have_vol = all(v is not None for v in vols[-lb - 1:])
    vol_ratio = 0.0
    if have_vol:
        avg_v = sum(vols[-lb - 1:-1]) / lb if lb else 0.0
        vol_ratio = (vols[-1] / avg_v) if avg_v > 0 else 0.0

    bar_move_pct = ((cl - o) / o * 100.0) if o else 0.0
    direction = "DOWN" if cl < o else "UP"

    range_ok = range_ratio >= c["range_x"]
    vol_ok = (vol_ratio >= c["vol_x"]) if have_vol else True   # if no volume data, don't block on it
    move_ok = abs(bar_move_pct) >= c["min_bar_move_pct"]
    ofi_ok = True
    if ofi is not None:
        ofi_ok = abs(ofi) >= c["ofi_min"] and ((ofi < 0) == (direction == "DOWN"))

    signals = {"range_ratio": round(range_ratio, 2), "vol_ratio": round(vol_ratio, 2),
               "bar_move_pct": round(bar_move_pct, 3), "ofi": ofi, "have_volume": have_vol}
    fired = range_ok and vol_ok and move_ok and ofi_ok
    if not fired:
        return {"cascade": False, "direction": None,
                "reason": "no cascade signature (need range+volume climax in one direction)",
                "signals": signals}
    return {"cascade": True, "direction": direction,
            "reason": (f"CASCADE {direction}: range {range_ratio:.1f}x, "
                       f"vol {vol_ratio:.1f}x, bar {bar_move_pct:+.1f}%"
                       + (f", OFI {ofi:+.2f}" if ofi is not None else "")
                       + " — forced-flow climax; join with a TIGHT stop + small size"),
            "signals": signals}


def decide(candles, current_side: str, cfg: Optional[dict] = None,
           ofi: Optional[float] = None) -> dict:
    """Unified flip decision. Checks the FAST cascade trigger first (join a
    liquidation run into its direction), then the SLOW trend-flip. Returns a
    reversal decision tagged with which trigger fired."""
    side = str(current_side or "FLAT").upper()
    casc = cascade_signal(candles, ofi=ofi, cfg=cfg)
    if casc.get("cascade") and side in ("LONG", "SHORT"):
        to = "SHORT" if casc["direction"] == "DOWN" else "LONG"
        if to != side:   # only flip if the cascade runs AGAINST our position
            return {"reverse": True, "to_side": to, "trigger": "cascade",
                    "reason": casc["reason"], "signals": casc.get("signals")}
    trend = should_reverse(candles, side, cfg)
    trend["trigger"] = "trend" if trend.get("reverse") else None
    return trend


# ── Telemetry: how many reversals, and the P&L attributable to them ──────────
# The strategy should emit event_type="position_reversed" on each flip, and tag
# the realized-P&L event that CLOSES a reversal-entered leg with via_reversal=True
# (and gross=<realized>). These read those events; they never compute a flip.

def reversal_stats(events) -> dict:
    """Count reversals and sum the realized P&L attributable to reversal-entered
    legs, overall and per sleeve/source."""
    count = 0
    total_pnl = 0.0
    by_source: dict[str, dict] = {}
    for e in events:
        et = str(e.get("event_type") or "")
        if et == "position_reversed":
            count += 1
            src = str(e.get("sleeve_name") or e.get("sleeve_id") or "primary")
            by_source.setdefault(src, {"reversals": 0, "pnl": 0.0})["reversals"] += 1
        if e.get("via_reversal") and isinstance(e.get("gross"), (int, float)):
            g = float(e["gross"])
            total_pnl += g
            src = str(e.get("sleeve_name") or e.get("sleeve_id") or "primary")
            by_source.setdefault(src, {"reversals": 0, "pnl": 0.0})["pnl"] += g
    for s in by_source.values():
        s["pnl"] = round(s["pnl"], 2)
    return {
        "reversals": count,
        "reversal_pnl": round(total_pnl, 2),
        "by_source": by_source,
        "verdict": ("reversals are net-positive" if total_pnl > 0
                    else ("no reversals yet" if count == 0 else "reversals are net-negative — review the signal / regime gate")),
    }
