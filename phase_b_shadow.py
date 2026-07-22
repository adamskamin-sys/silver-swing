"""Phase B shadow mode — log what a ratcheting TP/SL bracket WOULD do.

Adam 2026-07-22 authorized shadow-only exploration of "dynamic bracket
where sell_px and stop_loss_px ratchet UP as HWM rises":

  - Baseline: buy fill sets HWM = own_avg
  - Each tick: HWM = max(HWM, last_price)
  - shadow_trail_floor = max(own_avg + fee_safety, HWM - trail_gap)
  - If last_price <= shadow_trail_floor AND we're still holding
    (Phase A hasn't exited yet), log a phase_b_would_exit event
  - No orders placed. All effect is telemetry.

Trail gap: Chandelier Exit (Le Beau/Lucas 1999) N×ATR trailing stop.
Wilder (1978) N=2 common. Falls back to 2% of HWM if ATR unavailable.

MODE flag guards emission — set to "shadow" for observation, "off" to
disable entirely. This module NEVER places or cancels orders. Compare
outcomes with diag_phase_b_shadow.py.

Rate-limited: only emits phase_b_would_exit ONCE per held cycle
(idempotent — subsequent triggers on the same held position get
skipped so log doesn't flood with duplicate hypotheticals). Also
emits phase_b_hwm_ratchet on meaningful HWM moves for post-cycle
comparison of "did HWM ever peak, and where."
"""
from __future__ import annotations

MODE = "shadow"  # "shadow" | "off"

# Chandelier trail multiplier. Wilder N=2 conservative; Le Beau
# suggested N=3 for less noise. Start at 2.5 as midpoint.
CHANDELIER_MULTIPLIER = 2.5

# Fallback trail_gap as fraction of HWM when ATR unavailable.
FALLBACK_TRAIL_GAP_FRACTION = 0.02  # 2%

# Minimum HWM-tick delta (fraction of HWM) that logs a ratchet event.
# Prevents phase_b_hwm_ratchet firing every tick on jittery marks.
HWM_RATCHET_TICK_FRACTION = 0.001  # 0.1% — one ratchet event per meaningful up-move


def _compute_atr(price_history: list[float], window: int = 14) -> float:
    """Wilder ATR proxy from per-tick prices — |Δ| average over window.
    Returns 0.0 on insufficient history."""
    if not price_history or len(price_history) < 3:
        return 0.0
    _samples = price_history[-max(3, window):]
    _deltas = [abs(_samples[i] - _samples[i - 1])
               for i in range(1, len(_samples))]
    if not _deltas:
        return 0.0
    return sum(_deltas) / len(_deltas)


def tick(trader, sc, ss, last_price: float) -> None:
    """Called from _sleeve_step after profit_lock_limit maintenance.
    Read-only apart from ss.phase_b_shadow_* bookkeeping fields.
    NEVER raises — fail-open."""
    if MODE == "off":
        return
    try:
        own_avg = float(getattr(ss, "own_avg_entry", 0) or 0)
        if own_avg <= 0:
            return
        try:
            price = float(last_price or 0)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return
        # Shadow HWM (separate from ss.trail_high_water_price to keep
        # Phase A's HWM untouched — but seed from it if available).
        hwm = float(getattr(ss, "phase_b_shadow_hwm", 0) or 0)
        if hwm <= 0:
            hwm = max(own_avg, float(getattr(ss, "trail_high_water_price", 0) or 0), price)
        if price > hwm:
            hwm = price
        prior_hwm = float(getattr(ss, "phase_b_shadow_hwm", 0) or 0)
        # Persist HWM update (best-effort — no _save_state; the next
        # tick's Phase A save will pick it up along with everything else).
        try:
            ss.phase_b_shadow_hwm = hwm
        except Exception:
            pass
        # Log meaningful ratchet up-moves once (for post-cycle analysis).
        if prior_hwm > 0 and hwm > prior_hwm * (1 + HWM_RATCHET_TICK_FRACTION):
            trader._record(
                "phase_b_hwm_ratchet",
                sleeve_id=sc.id, sleeve_name=sc.name,
                prior_hwm=round(prior_hwm, 8),
                new_hwm=round(hwm, 8),
                own_avg=round(own_avg, 8),
                gain_since_buy=round((hwm - own_avg) / own_avg * 100, 3),
                severity="info",
            )
        # Compute trail_gap: Chandelier N×ATR, fallback to fraction of HWM.
        atr = 0.0
        try:
            hist = list(trader._sleeve_price_history.get(sc.id, []) or [])
            atr = _compute_atr(hist, window=14)
        except Exception:
            atr = 0.0
        if atr > 0:
            trail_gap = CHANDELIER_MULTIPLIER * atr
            gap_source = "chandelier_atr"
        else:
            trail_gap = FALLBACK_TRAIL_GAP_FRACTION * hwm
            gap_source = "fallback_pct_of_hwm"
        # Break-even floor: never exit below own_avg + fees + tick.
        fee_price = 0.0
        try:
            _cs = trader._get_contract_size()
            _rt = float(getattr(trader.cfg, "fee_per_contract_roundtrip", 0) or 0)
            if _cs > 0:
                fee_price = _rt / _cs / max(1, int(getattr(sc, "qty", 1) or 1))
        except Exception:
            fee_price = 0.0
        try:
            _tick = float(getattr(trader.cfg, "tick_size", 0) or 0.01)
        except Exception:
            _tick = 0.01
        be_floor = own_avg + fee_price + max(_tick, own_avg * 0.0005)
        shadow_floor = max(be_floor, hwm - trail_gap)
        # Hypothetical exit trigger: mark crossed floor from above.
        # Emit ONCE per held cycle (idempotent via ss.phase_b_shadow_would_exit_ts).
        already = float(getattr(ss, "phase_b_shadow_would_exit_ts", 0) or 0)
        if price <= shadow_floor and already <= 0:
            import time as _time
            hypothetical_pnl = 0.0
            try:
                # Round-trip fee proxy (same as _credit_stop_fill uses).
                _cs2 = trader._get_contract_size()
                _rt2 = float(getattr(trader.cfg, "fee_per_contract_roundtrip", 0) or 0)
                _qty = int(getattr(sc, "qty", 1) or 1)
                _gross = (shadow_floor - own_avg) * _qty * _cs2
                _half_fee = (_rt2 / 2.0) * _qty
                hypothetical_pnl = _gross - _half_fee
            except Exception:
                pass
            trader._record(
                "phase_b_would_exit",
                sleeve_id=sc.id, sleeve_name=sc.name,
                shadow_exit_px=round(shadow_floor, 8),
                hwm=round(hwm, 8),
                own_avg=round(own_avg, 8),
                last_price=round(price, 8),
                trail_gap=round(trail_gap, 8),
                trail_gap_source=gap_source,
                be_floor=round(be_floor, 8),
                actual_sell_px_config=float(getattr(sc, "sell_px", 0) or 0),
                actual_stop_loss_px_config=float(getattr(sc, "stop_loss_px", 0) or 0),
                hypothetical_pnl=round(hypothetical_pnl, 4),
                gain_over_own_avg_pct=round((shadow_floor - own_avg) / own_avg * 100, 3),
                severity="info",
                mode=MODE,
            )
            try:
                ss.phase_b_shadow_would_exit_ts = _time.time()
                ss.phase_b_shadow_would_exit_px = float(shadow_floor)
                ss.phase_b_shadow_would_exit_hwm = float(hwm)
            except Exception:
                pass
    except Exception:
        # Shadow must NEVER take down the tick loop.
        return


def reset_on_new_cycle(ss) -> None:
    """Called after a real cycle completes (buy fill starts a fresh
    hold). Clears shadow HWM and would_exit tracking so next cycle's
    shadow bracket starts fresh."""
    try:
        ss.phase_b_shadow_hwm = 0.0
        ss.phase_b_shadow_would_exit_ts = 0.0
        ss.phase_b_shadow_would_exit_px = 0.0
        ss.phase_b_shadow_would_exit_hwm = 0.0
    except Exception:
        pass
