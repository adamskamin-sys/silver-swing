"""Portfolio-level risk circuit breaker.

Adam's ask: 'if aggregate account drawdown > X% in Y minutes → halt all
new arms across the portfolio (existing positions ride, no new exposure).'

Why: per-sleeve stops protect each strategy individually, but nothing today
prevents ALL 8 sleeves from stopping out in the same 5% market crash. Van
Tharp: 'the best position sizing rule is to stop trading when things go
wrong.' This is the account-level implementation of that.

How it works:
    - Called from live_runner's main loop, once per SNAPSHOT_INTERVAL.
    - Aggregates realized_pnl + unrealized_pnl across every sleeve of every
      product in every live tenant. That's the account's total P&L for the
      SWING PORTFOLIO (excludes core hold — we don't want the core drifting
      to trigger a swing halt).
    - Maintains a rolling peak of that total. Drawdown = peak - current.
    - If drawdown exceeds `max_drawdown_pct` of the peak AND has moved
      >= `min_drawdown_dollars` (avoids halting on noise), set the
      `__portfolio_halted__` Redis scope.
    - SwingTrader.step() reads that scope at the top; if set, arms are
      blocked but existing legs and fills still process (never abandon
      an open order midflight).
    - Auto-resumes when drawdown < resume_threshold_pct × halt threshold
      (hysteresis). Prevents flapping when price bounces around the trigger.

Config lives in the __portfolio__ scope (existing snap block) as
portfolio_risk_config so the dashboard can edit thresholds without a
bot restart.
"""

from __future__ import annotations

import time
from typing import Optional


DEFAULT_CONFIG = {
    "enabled": True,
    "max_drawdown_pct": 5.0,          # 5% drawdown from peak → halt
    "min_drawdown_dollars": 50.0,     # ...but only if >= $50 absolute (noise floor)
    # [crew:#2] Absolute-dollar hard breaker. The percentage breaker above is
    # blind whenever peak P&L sits near $0 (a fresh or oscillating account):
    # abs(peak) <= 1.0 forces drawdown_pct to 0, so a large DOLLAR loss produces
    # a 0% drawdown and the halt NEVER fires — exactly when you'd want it. This
    # trips the halt on absolute dollars regardless of the percentage. It only
    # blocks NEW arms (open legs ride), and auto-resumes with hysteresis.
    # TUNE THIS to your risk tolerance; 0 disables it (and re-opens the dead zone).
    "max_drawdown_dollars": 500.0,
    "resume_threshold_pct": 0.5,      # resume when drawdown < 2.5% (half of 5%)
    "peak_lookback_secs": 24 * 3600,  # rolling 24h peak
}


def _load_config(store, tenant: str) -> dict:
    portfolio_block = (store.get_config(tenant, "__portfolio__") or {})
    user_cfg = portfolio_block.get("portfolio_risk_config") or {}
    return {**DEFAULT_CONFIG, **user_cfg}


def _load_risk_state(store, tenant: str) -> dict:
    raw = store.get_state(tenant, "__portfolio_risk__") or {}
    return raw


def _save_risk_state(store, tenant: str, state: dict) -> None:
    store.put_state(tenant, "__portfolio_risk__", state)


def _aggregate_swing_pnl(store, tenant: str) -> tuple[float, dict]:
    """Sum realized + unrealized P&L across every sleeve of every product
    in this tenant. Excludes the primary swing's core-hold P&L — we only
    want strategy-attached exposure, matching the dashboard's per-row
    'sleeve-only unrealized' semantic.
    Returns (total_pnl, breakdown_by_product)."""
    total = 0.0
    breakdown: dict[str, float] = {}
    for sym in store.list_symbols(tenant):
        if sym.startswith("__"):
            continue
        cfg = store.get_config(tenant, sym) or {}
        state = store.get_state(tenant, sym) or {}
        snap = (store.get_snapshot(tenant, sym) or {}) if hasattr(store, "get_snapshot") else {}
        sleeves = cfg.get("sleeves") or []
        contract_size = float(cfg.get("contract_size") or 0)
        mark = float(snap.get("last_mark") or 0)
        sleeve_states = (state.get("sleeves") or {})
        product_pnl = 0.0
        for s in sleeves:
            sid = s.get("id")
            ss = sleeve_states.get(sid) or {}
            realized = float(ss.get("realized_pnl") or 0)
            product_pnl += realized
            # Unrealized only counts if the sleeve holds contracts NOW.
            sleeve_state = str(ss.get("state") or "ARMED_SELL")
            sleeve_qty = int(s.get("qty") or 0)
            if sleeve_state == "ARMED_SELL" and sleeve_qty > 0 and contract_size > 0 and mark > 0:
                own_entry = ss.get("own_avg_entry")
                if own_entry is None or float(own_entry) <= 0:
                    own_entry = float(s.get("buy_px") or 0)
                if float(own_entry) > 0:
                    product_pnl += (mark - float(own_entry)) * contract_size * sleeve_qty
        if product_pnl != 0:
            breakdown[sym] = round(product_pnl, 2)
            total += product_pnl
    return total, breakdown


def tick(store, tenant: str, trade_log=None) -> Optional[dict]:
    """One risk check. Called from the main loop periodically. Returns a
    dict describing the state change (halted / resumed) or None if nothing
    changed."""
    cfg = _load_config(store, tenant)
    if not cfg.get("enabled", True):
        return None
    now = time.time()
    current_pnl, breakdown = _aggregate_swing_pnl(store, tenant)
    st = _load_risk_state(store, tenant)
    peak = float(st.get("peak_pnl", current_pnl))
    peak_ts = float(st.get("peak_ts", now))
    # Rolling window: if the peak is older than lookback_secs, reset to
    # current so a stale ATH from 2 weeks ago doesn't lock us out forever.
    lookback = float(cfg.get("peak_lookback_secs") or (24 * 3600))
    if (now - peak_ts) > lookback:
        peak = current_pnl
        peak_ts = now
    if current_pnl > peak:
        peak = current_pnl
        peak_ts = now
    drawdown = peak - current_pnl
    drawdown_pct = (drawdown / abs(peak) * 100.0) if abs(peak) > 1.0 else 0.0
    was_halted = bool(st.get("halted", False))
    change = None
    max_dd_dollars = float(cfg.get("max_drawdown_dollars") or 0.0)
    if not was_halted:
        # Halt trigger: BOTH % and absolute must exceed thresholds. Prevents
        # halting on a 5% swing of $10 (noise) or a $500 loss on a $50k
        # account (only 1%, not a real drawdown).
        pct_trip = (drawdown_pct >= float(cfg["max_drawdown_pct"])
                    and drawdown >= float(cfg["min_drawdown_dollars"]))
        # [crew:#2] Absolute-dollar hard breaker — fires even when the % path is
        # blind (peak P&L ~ $0 forces drawdown_pct to 0). This is the guard that
        # actually protects a fresh/oscillating account.
        abs_trip = max_dd_dollars > 0 and drawdown >= max_dd_dollars
        if pct_trip or abs_trip:
            st["halted"] = True
            st["halted_ts"] = now
            st["halt_reason"] = (
                f"portfolio drawdown ${drawdown:.2f} ({drawdown_pct:.1f}%) "
                + ("exceeds absolute $%.2f limit" % max_dd_dollars if (abs_trip and not pct_trip)
                   else f"exceeds {cfg['max_drawdown_pct']}%"))
            change = {
                "kind": "halted",
                "drawdown_pct": drawdown_pct,
                "drawdown_dollars": drawdown,
                "peak_pnl": peak,
                "current_pnl": current_pnl,
                "reason": st["halt_reason"],
                "breakdown": breakdown,
            }
            if trade_log is not None:
                try:
                    trade_log.record(
                        "portfolio_risk_halted",
                        tenant=tenant, **change,
                    )
                except Exception:
                    pass
    else:
        # Auto-resume when drawdown falls back to resume_threshold × halt.
        resume_pct = float(cfg["max_drawdown_pct"]) * float(cfg["resume_threshold_pct"])
        # [crew:#2] Also require the absolute drawdown to recover below its
        # hysteresis band, or an absolute-dollar halt would instantly re-resume
        # (its drawdown_pct is ~0, already under resume_pct).
        abs_ok = (max_dd_dollars <= 0) or (drawdown < max_dd_dollars * float(cfg["resume_threshold_pct"]))
        if drawdown_pct < resume_pct and abs_ok:
            st["halted"] = False
            st["resumed_ts"] = now
            st["last_halt_reason"] = st.get("halt_reason")
            st["halt_reason"] = None
            change = {
                "kind": "resumed",
                "drawdown_pct": drawdown_pct,
                "drawdown_dollars": drawdown,
                "peak_pnl": peak,
                "current_pnl": current_pnl,
            }
            if trade_log is not None:
                try:
                    trade_log.record(
                        "portfolio_risk_resumed",
                        tenant=tenant, **change,
                    )
                except Exception:
                    pass
    st["peak_pnl"] = peak
    st["peak_ts"] = peak_ts
    st["current_pnl"] = current_pnl
    st["drawdown_pct"] = drawdown_pct
    st["drawdown_dollars"] = drawdown
    st["last_tick_ts"] = now
    _save_risk_state(store, tenant, st)
    return change


def is_halted(store, tenant: str) -> bool:
    """Cheap read used by SwingTrader.step() every tick. No aggregation
    here — the tick() function above computes and writes; this only reads."""
    st = _load_risk_state(store, tenant)
    return bool(st.get("halted", False))


def halt_reason(store, tenant: str) -> Optional[str]:
    st = _load_risk_state(store, tenant)
    return st.get("halt_reason")
