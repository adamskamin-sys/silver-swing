"""
reentry_reeval.py — re-evaluate a PENDING (armed but unfilled) entry when it goes
stale or when a NEW higher trend has formed above the last sale (the CU case:
sold, armed a buy below, copper then trended up and left the order stranded).

Drives the existing (disabled) _maybe_reanchor_new_channel hook (swing_leg.py:1508)
with an expert-gated trigger + an anti-chase decision.

Pure decision logic — the caller computes features and executes the action as a
CANCEL-REPLACE (cancel the stale resting order, THEN place the new one) so this
never creates a duplicate working order.

Discipline (why this isn't FOMO):
  * Re-anchor to a PULLBACK in the new trend (Elder Triple Screen / Ehlers cycle
    trough), never to the current extended price.
  * Hard ceiling: won't move buy_px more than max_reanchor_x_atr ATRs above the
    OLD last sale — bounds the chase (Van Tharp: defined, bounded risk).
  * Breakout re-entry (Turtle/Donchian) only as a fallback, only in a strong-trend
    regime, only when a pullback never came — and still under the ceiling.
  * Dated futures near expiry: prefer EXPIRE over re-anchor (no runway for a
    pullback to play out).
"""
from dataclasses import dataclass


@dataclass
class ReevalParams:
    stale_after_bars: int = 20         # time trigger: re-evaluate after this many bars unfilled
    drift_trigger_x_atr: float = 2.0   # trend trigger: price this far above last sale => re-evaluate now
    reentry_x_atr: float = 1.0         # pullback depth in the NEW trend (same as re-entry)
    max_reanchor_x_atr: float = 4.0    # ANTI-CHASE ceiling above the old last sale
    trend_strength_min: float = 0.30   # Kaufman ER / normalized ADX to call it a real trend
    allow_breakout_when_stale: bool = True
    expire_near_expiry: bool = True
    # Anti-thrash for the DRIFT trigger (auditor 2026-07-14 Tier 1 gap).
    # The DRIFT trigger fires whenever price > last_sale + drift_x_atr*atr.
    # armed_at reset only guards the TIME trigger. Without a material-move
    # guard, price staying elevated would cancel/replace the resting order
    # every tick. If the proposed new_buy_px is within
    # reanchor_min_move_x_atr * atr of the current resting_buy_px, return
    # HOLD instead of thrashing.
    reanchor_min_move_x_atr: float = 0.5


@dataclass
class ReevalDecision:
    action: str          # "hold" | "reanchor" | "breakout" | "expire"
    new_buy_px: float    # meaningful for reanchor/breakout; else the old resting px
    why: str


def evaluate_pending(*, elapsed_bars, price, last_sale_px, resting_buy_px,
                     atr, htf_slope, trend_strength, dc_high, fast_ema,
                     near_expiry, params: ReevalParams) -> ReevalDecision:
    stale = elapsed_bars >= params.stale_after_bars
    moved_up = price > last_sale_px + params.drift_trigger_x_atr * atr

    # 1. Not stale and no new higher trend -> keep the resting order untouched.
    if not (stale or moved_up):
        return ReevalDecision("hold", resting_buy_px, "fresh; no staleness or trend drift")

    # 2. We're re-evaluating. Is there a confirmed NEW uptrend?
    new_trend_up = htf_slope > 0 and trend_strength >= params.trend_strength_min
    if not new_trend_up:
        if near_expiry and params.expire_near_expiry:
            return ReevalDecision("expire", resting_buy_px, "stale, no new uptrend, near expiry")
        return ReevalDecision("hold", resting_buy_px, "stale but no confirmed new uptrend; keep waiting")

    ceiling = last_sale_px + params.max_reanchor_x_atr * atr      # anti-chase cap
    min_move = params.reanchor_min_move_x_atr * atr                # material-move gate (Tier 1 anti-thrash)

    # 3. Re-anchor to a PULLBACK in the new trend (buy the dip, not the chase).
    pullback_px = fast_ema - params.reentry_x_atr * atr
    new_buy_px = min(pullback_px, ceiling)
    if new_buy_px < price:                                        # genuine pullback: we wait BELOW price
        # Anti-thrash (auditor 2026-07-14 Tier 1 fix): if the proposed new
        # buy_px is within reanchor_min_move_x_atr * ATR of the current
        # resting order, no material change — HOLD to prevent the
        # cancel-replace loop that would otherwise fire every tick while
        # price stays elevated above last_sale + drift_trigger_x_atr*ATR.
        if abs(new_buy_px - resting_buy_px) < min_move:
            return ReevalDecision("hold", resting_buy_px,
                f"drift/stale but proposed reanchor ({new_buy_px:.6f}) "
                f"within {params.reanchor_min_move_x_atr}xATR of resting "
                f"({resting_buy_px:.6f}); no material move — hold")
        return ReevalDecision("reanchor", round(new_buy_px, 6),
                              "re-anchor to pullback in new uptrend (capped)")

    # 4. Price extended above any pullback under the cap.
    if near_expiry and params.expire_near_expiry:
        return ReevalDecision("expire", resting_buy_px, "extended, no pullback room before expiry")
    if params.allow_breakout_when_stale and stale and trend_strength >= params.trend_strength_min:
        bo_px = min(dc_high, ceiling)                            # Turtle breakout, still capped
        if bo_px <= ceiling:
            # Same material-move guard for breakouts (Tier 1 anti-thrash).
            if abs(bo_px - resting_buy_px) < min_move:
                return ReevalDecision("hold", resting_buy_px,
                    f"breakout candidate ({bo_px:.6f}) within "
                    f"{params.reanchor_min_move_x_atr}xATR of resting "
                    f"({resting_buy_px:.6f}); no material move — hold")
            return ReevalDecision("breakout", round(bo_px, 6),
                                  "Turtle breakout continuation (strong trend, pullback never came)")
    return ReevalDecision("hold", resting_buy_px, "extended beyond chase cap; wait for pullback/expiry")
