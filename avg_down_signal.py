"""Average-down GREEN LIGHT — notification only, never executes (crew).

Averaging down (adding to a losing long) is broadly WARNED AGAINST by the canon
(Van Tharp, Jim Paul, O'Neil, the Turtles): in a downtrend it's how a small loss
becomes a blow-up. So this signal does NOT cheer "it's cheaper, buy more." It
lights green ONLY in the one narrow case the literature actually supports —
disciplined *scaling into a range near support*, which has positive expected
value, versus reckless *adding to a loser in a trend*, which has negative EV.

Green requires ALL of (a conjunction, not a vibe):
  1. MEAN-REVERTING regime, not a downtrend. Ornstein-Uhlenbeck (Ernie Chan):
     in a range the further below the mean, the higher the expected snap-back,
     so a lower unit has +EV. In a TREND the same add is a falling knife (-EV).
     (regime.classify_regime via Kaufman ER / Hurst.)
  2. Price DOWN near the channel FLOOR / support, not merely below cost.
     (channel_finder: at/below the buy band, above the floor.)
  3. CALM flow — not a toxic liquidation cascade, not a dead-cat bounce. Never
     add into forced selling. (crash_guard VPIN/OFI; not 'crash' severity.)
  4. Volatility SETTLED, not mid-drop. (channel_finder.stabilized.)
  5. Actually below YOUR average (it's an average-DOWN) and you have MARGIN
     headroom within position limits (define max exposure first — Jim Paul).

Any hard disqualifier (downtrend, active crash, no margin, mid-break) => RED.
All green conditions met => GREEN. Partial => AMBER (watch, not yet).

Read-only: returns a light + reasons. The human pulls the trigger.
"""

from __future__ import annotations

from typing import Optional

import regime as _regime
import channel_finder as _channel
import crash_guard as _crash


def _returns(prices):
    return [(prices[i] - prices[i - 1]) / prices[i - 1]
            for i in range(1, len(prices)) if prices[i - 1]]


DEFAULT_AVGDOWN_CONFIG = {
    "vpin_calm": 0.60,        # VPIN below this = flow calm enough to add
    "min_below_avg_frac": 0.0,  # require price at least this % below cost (0 = any)
}


def average_down_signal(prices, ms: Optional[dict] = None, ofi: Optional[float] = None,
                        position_avg: Optional[float] = None, last_price: Optional[float] = None,
                        have_margin: bool = True, atr: Optional[float] = None,
                        cfg: Optional[dict] = None) -> dict:
    """Return the average-down light for a long position.

    prices        : recent closes (the sleeve/product window).
    ms            : microstructure snapshot (vpin/ofi/...) if available.
    position_avg  : current long average entry (cost basis).
    last_price    : current mark (defaults to last close).
    have_margin   : True if adding one more contract fits margin/position limits.
    Returns {light:'green'|'amber'|'red', ok, reasons:[...], checks:{...}}.
    """
    c = {**DEFAULT_AVGDOWN_CONFIG, **(cfg or {})}
    ps = [float(p) for p in (prices or []) if p is not None]
    if len(ps) < 24:
        return {"light": "amber", "ok": False, "reasons": ["insufficient history"], "checks": {}}
    last = float(last_price) if last_price is not None else ps[-1]

    reg = _regime.classify_regime([{"close": p} for p in ps])  # classify_regime wants candles
    ch = _channel.find_channel(ps, atr=atr)
    vpin = None
    severity = "none"
    if ms:
        try:
            a = _crash.crash_assessment(ms, _returns(ps), "LONG", {"guard_enabled": True})
            severity = a.get("severity", "none")
        except Exception:
            severity = "none"
        vpin = ms.get("vpin") if isinstance(ms, dict) else None

    center = ch.get("center")
    floor = ch.get("floor")
    buy_band = ch.get("buy_px")

    # individual checks
    is_mean_revert = reg.get("regime") == "mean_revert"
    is_trend_down = reg.get("regime") == "trend" and center is not None and last < center
    # "near the floor" = at/below the buy band (lower zone of the channel). A
    # fresh new low BELOW the whole range is a breakdown, already caught as a
    # hard RED via mid_break, so no brittle exact-floor comparison here.
    near_floor = buy_band is not None and last <= float(buy_band)
    calm = severity != "crash" and (vpin is None or float(vpin) < c["vpin_calm"])
    stabilized = bool(ch.get("stabilized"))
    mid_break = bool(ch.get("broke")) and not stabilized
    below_avg = position_avg is not None and last < float(position_avg) * (1.0 - float(c["min_below_avg_frac"]))
    margin_ok = bool(have_margin)

    checks = {
        "mean_revert": is_mean_revert, "near_floor": near_floor, "calm": calm,
        "stabilized": stabilized, "below_avg": bool(below_avg), "margin_ok": margin_ok,
        "regime": reg.get("regime"), "vpin": round(float(vpin), 3) if vpin is not None else None,
        "center": center, "floor": floor, "buy_band": buy_band,
    }

    reasons = []
    # hard disqualifiers -> RED
    if is_trend_down:
        reasons.append("DOWNTREND — adding is a falling knife (Van Tharp/Jim Paul: don't average down in a trend)")
    if severity == "crash":
        reasons.append("active liquidation cascade — never add into forced selling")
    if mid_break:
        reasons.append("mid-drop, volatility not settled — wait for the all-clear")
    if not margin_ok:
        reasons.append("no margin headroom / would breach position limits")
    if reasons:
        return {"light": "red", "ok": False, "reasons": reasons, "checks": checks}

    # all green conditions. Note: 'stabilized' (vol contracting) is NOT required
    # here — a steady range never "contracts"; mid-crash is already blocked as a
    # hard RED via mid_break above, which is the real "don't add mid-drop" guard.
    green = (is_mean_revert and near_floor and calm and below_avg and margin_ok)
    if green:
        return {"light": "green", "ok": True,
                "reasons": [f"mean-reverting range, price {last:.4f} at the floor "
                            f"(center {center}, floor {floor}), flow calm, below your "
                            f"avg {position_avg} — disciplined scale-in near support"],
                "checks": checks}

    # otherwise amber — say what's missing
    missing = []
    if not is_mean_revert: missing.append(f"regime is {reg.get('regime')}, not mean-reverting")
    if not near_floor: missing.append("price not yet near the channel floor")
    if not below_avg: missing.append("not below your average (nothing to average down)")
    if not calm: missing.append("flow not calm enough (VPIN elevated)")
    return {"light": "amber", "ok": False,
            "reasons": ["conditions not all met: " + "; ".join(missing)], "checks": checks}
