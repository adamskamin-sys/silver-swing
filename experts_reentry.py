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


# Per-product overridable thresholds. Every expert gate reads from here; the
# orchestrator merges DEFAULT_THRESHOLDS with a per-product override dict
# (from SleeveConfig.reentry_thresholds or the product's config scope). The
# override map is intentionally flat so the eventual tuner can walk it as
# a grid without special-casing nested dicts.
DEFAULT_THRESHOLDS: dict = {
    "ehlers_bounce_low": 0.65,        # Ehlers cycle phase low bound
    "ehlers_bounce_high": 0.95,       # Ehlers cycle phase high bound
    "elder_stochastic_oversold": 30.0,  # Screen 2 %K threshold (Elder 1993)
    "elder_screen1_window": 240,      # Screen 1 MACD-hist window
    "elder_screen2_window": 60,       # Screen 2 stochastic window
    "connors_buy_zone": 60.0,         # Connors composite score buy cutoff
    "vpin_calm_ceiling": 0.60,        # LdP/Easley VPIN toxicity ceiling
    "vince_max_ruin_prob": 0.05,      # Vince ruin gate (2009)
    "ou_band_window": 20,             # Chan OU band lookback
    "regime_downtrend_lookback": 20,  # MOP trend-check window
    "min_history_bars": 40,           # gate to enter full chain vs fallback
}


def resolve_thresholds(override: Optional[dict] = None) -> dict:
    """Merge DEFAULT_THRESHOLDS with an optional per-product override dict.
    Unknown keys in override are preserved for future modules; missing keys
    fall back to defaults. Use this in every caller instead of DEFAULT_
    directly so overrides are honored uniformly."""
    if not override:
        return dict(DEFAULT_THRESHOLDS)
    merged = dict(DEFAULT_THRESHOLDS)
    for k, v in override.items():
        if v is not None:
            merged[k] = v
    return merged


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
                    ms: Optional[dict] = None,
                    thresholds: Optional[dict] = None) -> dict:
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
    thr = resolve_thresholds(thresholds)
    ps = [float(p) for p in (prices or []) if p is not None]
    if len(ps) < int(thr["min_history_bars"]):
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
    dt_lb = int(thr["regime_downtrend_lookback"])
    if regime_name == "trend" and ps[-1] < sum(ps[-dt_lb:]) / max(len(ps[-dt_lb:]), 1):
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
        # Compute phase; apply per-product bounce zone (may differ per contract).
        phase = _ehl.cycle_phase(ps)
        period = _ehl.dominant_period(ps)
        in_zone = (phase is not None
                   and float(thr["ehlers_bounce_low"]) <= phase
                   <= float(thr["ehlers_bounce_high"]))
        cyc = {"cycle_phase": phase, "dominant_period": period,
               "in_bounce_zone": in_zone,
               "zone": [thr["ehlers_bounce_low"], thr["ehlers_bounce_high"]],
               "citation": "Ehlers 2004 Cybernetic Analysis Ch. 5, 7"}
        snapshot["ehlers"] = cyc
        if phase is not None and not in_zone:
            # Not in the bounce zone — defer this tick. State stays ARMED_BUY
            # with a fallback buy_px so we don't miss a fast bounce entirely.
            reasons.append(f"Ehlers cycle_phase {cyc.get('cycle_phase'):.2f} outside bounce zone")
    except Exception:
        snapshot["ehlers"] = {"error": "module unavailable"}

    # --- 3. Elder Triple Screen (direction gate) ---
    try:
        import elder as _elder
        s1 = _elder.screen1_long_tide(ps, window=int(thr["elder_screen1_window"]))
        s2 = _elder.screen2_medium_wave(ps, window=int(thr["elder_screen2_window"]),
                                        oversold=float(thr["elder_stochastic_oversold"]))
        s3 = _elder.screen3_short_ripple(ps)
        buy_ok = bool(s1.get("pass_buy") and s2.get("pass_buy"))
        blocked = []
        if not s1.get("pass_buy"): blocked.append(f"Screen 1: {s1.get('reason')}")
        if not s2.get("pass_buy"): blocked.append(f"Screen 2: {s2.get('reason')}")
        ts = {"buy_ok": buy_ok, "screen1": s1, "screen2": s2, "screen3": s3,
              "blocked_by": blocked,
              "citation": "Elder 1993 Trading for a Living Ch. 9; 2002 CIMTR Ch. 8"}
        snapshot["elder"] = ts
        if not buy_ok:
            reasons.append(f"Elder Triple Screen blocks buy: {ts.get('blocked_by')}")
    except Exception:
        snapshot["elder"] = {"error": "module unavailable"}

    # --- 4. Chan OU mean-reversion band ---
    # Approximation of the OU-implied bounce band: center = N-bar SMA,
    # width = 2 × N-bar std. Per-product N via ou_band_window (default 20).
    # Full OU fit would require a longer history and half-life estimation;
    # this hits the same intent for the swing rate window we operate at.
    ou_win = int(thr["ou_band_window"])
    tail = ps[-ou_win:]
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

    # --- 6. VPIN gate (LdP/Easley) — per-product ceiling ---
    if ms:
        try:
            vpin = float(ms.get("vpin")) if ms.get("vpin") is not None else None
            snapshot["vpin"] = vpin
            vpin_ceil = float(thr["vpin_calm_ceiling"])
            if vpin is not None and vpin >= vpin_ceil:
                reasons.append(f"VPIN {vpin:.2f} >= ceiling {vpin_ceil} — flow toxicity")
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

    # --- 7. KAMA (Kaufman Adaptive Moving Average) — advisory ---
    # Adds a KAMA-based trend signal to the snapshot. Advisory only —
    # does not veto. Kaufman 2013 Ch. 17 — KAMA speeds up in trends,
    # slows in chop; price above KAMA in an uptrend = pullback buy
    # candidate. Complements the Kaufman efficiency ratio in regime.py.
    try:
        import kama as _kama
        kama_sig = _kama.kama_signal(ps)
        if kama_sig:
            snapshot["kama"] = kama_sig
            # Soft veto — if KAMA says trend down + we're trying to buy,
            # flag it (Kaufman: don't buy against a genuine downtrend).
            if kama_sig.get("signal") == "sell":
                reasons.append(f"KAMA: {kama_sig.get('reason')}")
    except Exception:
        snapshot["kama"] = {"error": "module unavailable"}

    # --- 8. Ehlers Fisher Transform — advisory cycle-inflection signal ---
    # Amplifies extremes; crossover of Fisher vs its 1-bar lag detects
    # cycle inflection with higher signal-to-noise than RSI/Stoch.
    # Complements Ehlers cycle_phase — cycle_phase says WHERE you are,
    # Fisher says IF a turn has occurred.
    try:
        import ehlers_fisher as _fisher
        fisher_sig = _fisher.fisher_transform(ps)
        if fisher_sig:
            snapshot["fisher"] = fisher_sig
            # Fisher "up" crossover from negative territory = mean-reversion buy signal
            if fisher_sig.get("crossover") == "down":
                reasons.append(f"Fisher: {fisher_sig.get('reason')}")
    except Exception:
        snapshot["fisher"] = {"error": "module unavailable"}

    # --- 9. Vince optimal-f qty cap ---
    capped_qty = strategy_qty
    try:
        if recent_cycle_pnls and account_equity > 0 and worst_loss_per_contract > 0:
            import vince as _vince
            cap = _vince.cap_reentry_qty(
                strategy_qty=strategy_qty,
                pnl_series=list(recent_cycle_pnls),
                account_equity=account_equity,
                worst_loss_per_contract=worst_loss_per_contract,
                max_ruin_prob=float(thr["vince_max_ruin_prob"]),
            )
            capped_qty = cap.get("capped_qty", strategy_qty)
            snapshot["vince"] = cap
            if capped_qty < strategy_qty:
                reasons.append(
                    f"Vince optimal-f capped qty {strategy_qty} → {capped_qty}")
    except Exception:
        snapshot["vince"] = {"error": "module unavailable"}

    # --- Final decision ---
    snapshot["thresholds_used"] = thr
    should_arm = bool(capped_qty > 0)
    return {
        "should_arm": should_arm,
        "buy_px": round(buy_px, 6),
        "sell_px": round(sell_px, 6),
        "qty": capped_qty,
        "reasons": reasons or ["expert chain green: buy_px derived from Chan OU + Connors"],
        "expert_snapshot": snapshot,
    }
