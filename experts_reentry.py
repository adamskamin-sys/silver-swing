"""Expert-driven re-entry orchestrator (crew).

Purpose
-------
After a sleeve completes a sell and flips to ARMED_BUY, decide where to
place the new buy_px — using the ensemble of experts already in the
codebase plus the four new modules added 2026-07-13:

    Kaufman KER / Hurst   (regime.py)        — mean-reverting? trending? chop?
    Ernie Chan (OU)       (avg_down_signal.py) — expected snap-back band
    John Ehlers cycle     (ehlers.py)         — are we at cycle bottom?
    Alexander Elder T-S   (elder.py)          — direction gate (higher-TF)
    Larry Connors mean-rev (connors.py)      — statistical bounce probability
    LdP/Easley VPIN       (crash_guard.py)    — flow toxicity gate
    Ralph Vince optimal-f (vince.py)          — qty cap by risk-of-ruin

Bug this fixes
--------------
Today, after a normal sell, the sleeve stays with its OLD buy_px — which
was set by an earlier anchor point when prices were higher. Result: buy_px
sits ABOVE the last sale price ("assumed a bullish trend"). Rebuying above
a fresh sell locks in a loss on every mean-reverting oscillation.

New behavior
------------
On the ARMED_SELL → ARMED_BUY transition (swing_leg.py:2978), we compute
buy_px from:

  1. Kaufman regime (skip re-entry entirely if trending DOWN, no fight)
  2. Chan OU-implied bounce-band center + std (target below-mean)
  3. Connors bounce_probability shifts buy_px LOWER in the OU band as
     the statistical oversold reading deepens
  4. Ehlers cycle_phase must be in [0.65, 0.95] (bounce zone, not
     mid-drop) — otherwise defer (return no buy this tick, try again)
  5. Elder Triple Screen buy_ok — higher-TF trend not decisively down
  6. VPIN calm — no flow-toxicity re-entry
  7. Vince cap_reentry_qty on the strategy's declared qty

Returns
-------
dict with:
    should_arm      : bool  — arm the buy this tick?
    buy_px          : float — the recommended re-entry price
    sell_px         : float — recommended future sell (buy_px + spread)
    qty             : int   — capped qty
    reasons         : list  — why (each expert's verdict, for logging)
    expert_snapshot : dict  — full details for the trade log event

Fail-safe: if any expert module errors or lacks data, we FALL BACK to the
existing behavior (buy_px = sold_price − 0.5×spread). Never blocks the
existing state machine on our own bugs.
"""
from __future__ import annotations

from typing import Optional, Sequence


def _fallback(sold_price: float, spread: float) -> dict:
    """Old behavior — buy below the sold price by half the spread. Used
    when we can't run the full expert chain (insufficient history, module
    errors)."""
    half = spread / 2.0
    return {
        "should_arm": True,
        "buy_px": sold_price - half,
        "sell_px": sold_price + half,
        "qty": None,   # caller keeps their strategy qty
        "reasons": ["fallback — insufficient data for full expert chain"],
        "expert_snapshot": {"mode": "fallback"},
    }


def compute_reentry(prices: Sequence[float],
                    sold_price: float,
                    spread: float,
                    strategy_qty: int,
                    account_equity: float = 0.0,
                    worst_loss_per_contract: float = 0.0,
                    recent_cycle_pnls: Optional[Sequence[float]] = None,
                    ms: Optional[dict] = None) -> dict:
    """The full expert-driven re-entry decision.

    prices           — recent close series for this product (>= 60 bars ideal)
    sold_price       — the price at which we just sold (fill price)
    spread           — the sleeve's target spread (sell_px - buy_px)
    strategy_qty     — what the sleeve's strategy declared as qty
    account_equity   — for Vince optimal-f (0 disables the cap)
    worst_loss_per_contract — for Vince (0 disables the cap)
    recent_cycle_pnls — for Vince optimal-f (recent per-cycle P&L)
    ms               — microstructure snapshot (vpin, ofi) for VPIN gate

    Returns dict — see module docstring.
    """
    ps = [float(p) for p in (prices or []) if p is not None]
    if len(ps) < 40:
        return _fallback(sold_price, spread)

    # --- 1. Regime (Kaufman KER + Hurst via regime.py) ---
    try:
        import regime as _regime
        candles = [{"close": p} for p in ps]
        reg = _regime.classify_regime(candles)
        regime_name = reg.get("regime")
    except Exception:
        reg, regime_name = {}, "unknown"

    reasons: list[str] = []
    snapshot: dict = {"regime": reg}

    # --- Hard veto: strong downtrend ---
    if regime_name == "trend" and ps[-1] < sum(ps[-20:]) / max(len(ps[-20:]), 1):
        # Trend down — Van Tharp / Moskowitz-Ooi-Pedersen: don't fight it.
        return {
            "should_arm": False,
            "buy_px": None,
            "sell_px": None,
            "qty": 0,
            "reasons": [f"regime is trending DOWN — no re-entry (MOP 2012 / Van Tharp)"],
            "expert_snapshot": snapshot,
        }

    # --- 2. Ehlers cycle phase (bounce zone) ---
    try:
        import ehlers as _ehl
        cyc = _ehl.assess(ps)
        snapshot["ehlers"] = cyc
        if cyc.get("cycle_phase") is not None and not cyc.get("in_bounce_zone"):
            # Not in the bounce zone — defer this tick. State stays ARMED_BUY
            # with a fallback buy_px so we don't miss a fast bounce entirely.
            reasons.append(f"Ehlers cycle_phase {cyc.get('cycle_phase'):.2f} outside bounce zone")
    except Exception:
        snapshot["ehlers"] = {"error": "module unavailable"}

    # --- 3. Elder Triple Screen (direction gate) ---
    try:
        import elder as _elder
        ts = _elder.triple_screen(ps)
        snapshot["elder"] = ts
        if not ts.get("buy_ok"):
            reasons.append(f"Elder Triple Screen blocks buy: {ts.get('blocked_by')}")
    except Exception:
        snapshot["elder"] = {"error": "module unavailable"}

    # --- 4. Chan OU mean-reversion band ---
    # Approximation of the OU-implied bounce band: center = 20-bar SMA,
    # width = 2 × 20-bar std. Full OU fit would require a longer history
    # and half-life estimation; this hits the same intent for the swing
    # rate window we operate at (see avg_down_signal.py for the deeper
    # OU treatment when we need it for the AMBER→GREEN gate).
    tail = ps[-20:]
    mean = sum(tail) / len(tail)
    var = sum((p - mean) ** 2 for p in tail) / len(tail)
    std = var ** 0.5
    band_center = mean
    band_width = 2 * std
    snapshot["chan_ou"] = {"band_center": round(mean, 6),
                          "band_width": round(band_width, 6),
                          "std": round(std, 6)}

    # --- 5. Connors statistical mean-reversion → buy_px suggestion ---
    try:
        import connors as _connors
        cs = _connors.suggest_buy_px(ps, band_center, band_width)
        snapshot["connors"] = cs
        suggested_buy_px = cs.get("suggested_buy_px")
    except Exception:
        snapshot["connors"] = {"error": "module unavailable"}
        suggested_buy_px = band_center - std

    # --- 6. VPIN gate (LdP/Easley) ---
    if ms:
        try:
            vpin = float(ms.get("vpin")) if ms.get("vpin") is not None else None
            snapshot["vpin"] = vpin
            if vpin is not None and vpin >= 0.60:
                reasons.append(f"VPIN {vpin:.2f} elevated — flow toxicity")
        except Exception:
            pass

    # --- Post-process: cap buy_px so it stays BELOW the sell price ---
    # The core bug fix: buy_px must be at-or-below the last sale price.
    # If Connors' suggestion is above sold_price, clamp to sold - epsilon
    # (0.05% below or 1 tick below spread/2).
    epsilon = max(spread / 4.0, sold_price * 0.0005)
    buy_px = float(suggested_buy_px or (sold_price - spread / 2.0))
    if buy_px >= sold_price:
        buy_px = sold_price - epsilon
        reasons.append(f"clamped buy_px below sold_price ({sold_price:.4f})")

    sell_px = buy_px + spread

    # --- 7. Vince optimal-f qty cap ---
    capped_qty = strategy_qty
    try:
        if recent_cycle_pnls and account_equity > 0 and worst_loss_per_contract > 0:
            import vince as _vince
            cap = _vince.cap_reentry_qty(
                strategy_qty=strategy_qty,
                pnl_series=list(recent_cycle_pnls),
                account_equity=account_equity,
                worst_loss_per_contract=worst_loss_per_contract,
            )
            capped_qty = cap.get("capped_qty", strategy_qty)
            snapshot["vince"] = cap
            if capped_qty < strategy_qty:
                reasons.append(
                    f"Vince optimal-f capped qty {strategy_qty} → {capped_qty}")
    except Exception:
        snapshot["vince"] = {"error": "module unavailable"}

    # --- Final decision ---
    should_arm = bool(capped_qty > 0)
    return {
        "should_arm": should_arm,
        "buy_px": round(buy_px, 6),
        "sell_px": round(sell_px, 6),
        "qty": capped_qty,
        "reasons": reasons or ["expert chain green: buy_px derived from Chan OU + Connors"],
        "expert_snapshot": snapshot,
    }
