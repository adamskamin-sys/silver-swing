"""Short-leg cover logic for the offensive reversal (crew).

Once the crash guard flips a sleeve long -> short, THIS decides when to cover
(buy back to flat). It is the decision brain for Adam's ZEC test rule:

    "Exit the short when it looks like I'm going to lose the realized gains.
     As a test, make sure I at least break even."

So the design protects the banked realized P&L first, profit second. Three
exits, whichever fires first, all measured on the SHORT leg (a short profits
when price FALLS below entry):

  1. PROTECT-REALIZED / BREAK-EVEN (the rule). Track the best (lowest) price the
     short has reached. Once it has been favorable by `arm_x_atr`, arm a lock:
     cover if profit gives back past `lock_frac` of the peak — but never let the
     lock sit below break-even (entry). So a short that ever became profitable
     can NOT turn into a loss. Mirrors stop_loss_protect_realized for longs
     (Elder break-even stop; Van Tharp profit-protection).
  2. HARD STOP (inverted). A short's stop sits ABOVE entry. Cover if price rises
     `stop_x_atr` past entry. And when protecting realized, the dollar loss is
     ALSO capped at the realized already banked — you never give back more than
     you made, so the whole experiment stays >= break-even. (Van Tharp 1R;
     Kaminski-Lo: a failed cascade-short is a regime failure, cut it.)
  3. TRAILING (Chandelier / Le Beau, inverted). Ride a continuation: cover if
     price retraces `trail_x_atr` up off the lowest price seen.

Read-only: returns {action, reason, ...}; the sleeve executes the cover.
`atr` may be omitted — then *_x_atr are treated as absolute price distances.
"""

from __future__ import annotations

from typing import Optional


DEFAULT_SHORT_CONFIG = {
    "protect_realized": True,   # cap loss at banked realized; never give it back
    "arm_x_atr": 0.75,          # short must be this far in profit before break-even arms
    "lock_frac": 0.5,           # once armed, keep at least this fraction of peak profit
    "stop_x_atr": 1.5,          # hard inverted stop: price this far ABOVE entry -> cover
    "trail_x_atr": 1.0,         # cover if price retraces this far up off the low
    "trail_enabled": True,
}


def short_unrealized(entry: float, last_price: float, qty: int, contract_size: float) -> float:
    """Dollar P&L of a short: profit when price falls below entry."""
    return (float(entry) - float(last_price)) * float(contract_size) * abs(int(qty))


def _dist(x_atr: float, atr: Optional[float], entry: float) -> float:
    """Resolve an x_atr multiple to a price distance. With ATR, distance =
    x_atr * ATR. Without ATR, fall back to x_atr as a FRACTION of entry price
    (so it scales across a $500 ZEC vs a $65 metal contract)."""
    if atr and atr > 0:
        return float(x_atr) * float(atr)
    return float(x_atr) * abs(float(entry))  # x_atr read as a price fraction


def cover_decision(entry: float, last_price: float,
                   qty: int = 1, contract_size: float = 1.0,
                   realized_banked: float = 0.0,
                   peak_low: Optional[float] = None,
                   atr: Optional[float] = None,
                   cfg: Optional[dict] = None) -> dict:
    """Decide whether to COVER a short.

    entry           : short entry price.
    last_price      : current mark.
    qty             : short size (contracts).
    contract_size   : $ per point per contract.
    realized_banked : realized P&L already locked on this sleeve BEFORE the flip
                      (what the break-even rule protects). >= 0 normally.
    peak_low        : lowest price the short has seen (best profit). None => use
                      last_price (fresh short).
    atr             : optional ATR for distance sizing.
    Returns {action: 'HOLD'|'COVER', reason, u_now, u_peak, ...}.
    """
    c = {**DEFAULT_SHORT_CONFIG, **(cfg or {})}
    entry = float(entry); last = float(last_price)
    size = float(contract_size); q = abs(int(qty))
    low = float(peak_low) if peak_low is not None else last

    u_now = short_unrealized(entry, last, q, size)                 # $ now
    u_peak = short_unrealized(entry, low, q, size)                 # $ at best (lowest) price

    stop_dist = _dist(c["stop_x_atr"], atr, entry)
    arm_dist = _dist(c["arm_x_atr"], atr, entry)
    trail_dist = _dist(c["trail_x_atr"], atr, entry)

    # ---- 2/1 HARD STOP first (loss containment), realized-capped ------------
    # Price above entry by stop_dist -> the flip failed / bounced against us.
    stop_price = entry + stop_dist
    # Realized cap: never lose more (in $) than we already banked. Convert the
    # banked realized into a price distance above entry.
    if c["protect_realized"] and realized_banked and realized_banked > 0 and size * q > 0:
        realized_dist = float(realized_banked) / (size * q)
        stop_price = min(stop_price, entry + realized_dist)
    if last >= stop_price:
        return {"action": "COVER", "reason": (
                    f"hard stop: price {last:.4f} >= {stop_price:.4f} "
                    f"({'realized-capped ' if stop_price < entry + stop_dist else ''}short failed)"),
                "u_now": round(u_now, 2), "u_peak": round(u_peak, 2), "trigger": "hard_stop"}

    # ---- 1. PROTECT-REALIZED / BREAK-EVEN ----------------------------------
    # Once the short has been favorable by arm_dist, it must not turn into a loss:
    # lock at least lock_frac of the peak profit, floored at break-even (entry).
    armed = u_peak > 0 and (entry - low) >= arm_dist
    if armed:
        lock_profit = max(0.0, float(c["lock_frac"]) * u_peak)     # >= break-even
        if u_now <= lock_profit:
            be = " (break-even)" if lock_profit == 0.0 else ""
            return {"action": "COVER", "reason": (
                        f"protect-realized{be}: gave back to {u_now:.2f} of peak "
                        f"{u_peak:.2f}, lock {lock_profit:.2f}"),
                    "u_now": round(u_now, 2), "u_peak": round(u_peak, 2), "trigger": "protect_realized"}

    # ---- 3. TRAILING continuation ------------------------------------------
    if c.get("trail_enabled") and (entry - low) >= trail_dist:
        cover_at = low + trail_dist
        if last >= cover_at:
            return {"action": "COVER", "reason": (
                        f"trail: retraced up to {last:.4f} >= low {low:.4f} + {trail_dist:.4f}"),
                    "u_now": round(u_now, 2), "u_peak": round(u_peak, 2), "trigger": "trail"}

    return {"action": "HOLD", "reason": "short running — no cover trigger",
            "u_now": round(u_now, 2), "u_peak": round(u_peak, 2),
            "armed": armed, "trigger": None}
