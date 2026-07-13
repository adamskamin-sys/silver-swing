"""Cascade lifecycle + signal-based re-entry timing (crew).

A liquidation cascade is not one event — it's CRASH -> dead-cat BOUNCE ->
(EXHAUSTION) -> recovery OR a SECOND LEG down. Re-entering into the bounce is a
trap: short-term reversal (Lehmann 1990) is real but short-lived, and long-memory
order flow (Lillo-Farmer 2004) + volatility clustering (Engle ARCH / Bollerslev
GARCH) say the selling and the elevated vol usually aren't done. So the right
"waiting period" is SIGNAL-BASED, not a fixed clock: wait until the microstructure
NORMALIZES — VPIN subsides AND realized volatility CONTRACTS.

Feed a rolling series of observations (price, vpin, vol, ofi); this returns:
  - phase: CALM / CRASHING / BOUNCE / EXHAUSTION
  - reentry_ok: True only on a measured all-clear (toxicity + vol subsided)
  - second_leg_risk: True when price is bouncing but flow is still toxic/one-sided
Read-only. It measures the trajectory, not a single snapshot.
"""

from __future__ import annotations

from statistics import mean
from typing import Optional


DEFAULT_CFG = {
    "crash_vpin": 0.75,        # VPIN at/above this = toxic cascade underway
    "reentry_vpin": 0.55,      # must fall below this before re-entry is allowed
    "vol_contract_ratio": 0.8, # recent vol <= this * peak-window vol = contracting
    "half_window": 8,          # bars per half (recent vs earlier)
    "min_calm_bars": 5,        # bars of normalized flow required for the all-clear
}


def _series(observations, key):
    out = []
    for o in observations or []:
        v = o.get(key)
        if v is not None:
            try: out.append(float(v))
            except (TypeError, ValueError): pass
        else:
            out.append(None)
    return out


def assess(observations, cfg: Optional[dict] = None) -> dict:
    """observations: list (oldest -> newest) of dicts with at least 'vpin' and
    'price'; optional 'vol' (or ATR) and 'ofi'. Returns the cascade lifecycle
    assessment + a signal-based re-entry decision."""
    c = {**DEFAULT_CFG, **(cfg or {})}
    hw = int(c["half_window"])
    n = len(observations or [])
    if n < hw + 2:
        return {"phase": "unknown", "reentry_ok": True, "second_leg_risk": False,
                "reason": "insufficient history"}

    vpin = [v for v in _series(observations, "vpin") if v is not None]
    price = [p for p in _series(observations, "price") if p is not None]
    vol = [v for v in _series(observations, "vol") if v is not None]
    ofi_series = [o for o in _series(observations, "ofi") if o is not None]

    if not vpin or not price:
        return {"phase": "unknown", "reentry_ok": True, "second_leg_risk": False,
                "reason": "no vpin/price"}

    vpin_now = vpin[-1]
    vpin_peak = max(vpin)
    recent_price = price[-min(hw, len(price)):]
    earlier_price = price[-2 * hw:-hw] if len(price) >= 2 * hw else price[:-hw]
    price_now = price[-1]
    price_recent_low = min(recent_price)

    # volatility contraction: recent vol vs the peak-window vol
    vol_contracting = True
    vol_ratio = None
    if len(vol) >= 2 * hw:
        recent_vol = mean(vol[-hw:])
        peak_vol = max(mean(vol[i:i + hw]) for i in range(0, len(vol) - hw + 1))
        vol_ratio = (recent_vol / peak_vol) if peak_vol > 0 else 1.0
        vol_contracting = vol_ratio <= c["vol_contract_ratio"]

    crashed = vpin_peak >= c["crash_vpin"]
    price_up_recent = price_now > price_recent_low * 1.001 and (
        not earlier_price or price_now > min(earlier_price))
    ofi_now = ofi_series[-1] if ofi_series else None
    flow_still_toxic = vpin_now >= c["reentry_vpin"]
    flow_still_selling = ofi_now is not None and ofi_now < -0.3

    # phase
    if vpin_now >= c["crash_vpin"]:
        phase = "crashing"
    elif crashed and price_up_recent and (flow_still_toxic or flow_still_selling):
        phase = "bounce"                       # dead-cat: price up but flow toxic
    elif crashed and vpin_now < c["reentry_vpin"] and vol_contracting:
        phase = "exhaustion"                   # normalizing — the all-clear building
    else:
        phase = "calm"

    # signal-based re-entry all-clear
    calm_bars = 0
    for v in reversed(vpin):
        if v < c["reentry_vpin"]:
            calm_bars += 1
        else:
            break
    reentry_ok = (vpin_now < c["reentry_vpin"] and vol_contracting
                  and calm_bars >= c["min_calm_bars"])

    second_leg_risk = phase == "bounce"

    return {
        "phase": phase,
        "reentry_ok": reentry_ok,
        "second_leg_risk": second_leg_risk,
        "vpin_now": round(vpin_now, 3),
        "vpin_peak": round(vpin_peak, 3),
        "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
        "calm_bars": calm_bars,
        "reason": {
            "crashing": "toxic cascade underway — do NOT re-enter, defensive only",
            "bounce": "dead-cat bounce: price up but flow still toxic/one-sided — a SECOND LEG is likely; wait, and this is where you short the continuation",
            "exhaustion": "toxicity + vol subsiding — all-clear building; confirm a few more calm bars",
            "calm": "normalized — safe to operate",
            "unknown": "insufficient data",
        }[phase],
    }
