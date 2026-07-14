"""
boot_state_normalizer.py — bot-boot state coherence check.

Prevents the class of bug from the 2026-07-14 SLR incident, where the primary
state machine's runtime state.swing_qty drifted above config.swing_qty and got
stuck re-arming an unwanted position on every tick after cancellation.

Runs ONCE at startup, before the main tick loop begins. Only clamps when it's
provably SAFE (no live position tracked in state AND not mid-cycle), so we
NEVER shrink a legitimate active swing. Every clamp emits a trade-log event
and a CRIT notification so the operator sees the drift and can investigate.

Wire: called from live_runner right after SwingTrader is constructed. Read-
only unless drift is detected AND safety gates pass.
"""
from __future__ import annotations

import time


def normalize_primary_swing_qty(trader, *, log=None, notifier=None) -> dict:
    """Check + optionally clamp state.swing_qty against config.swing_qty.

    Returns a dict describing what happened:
      * drifted: bool — was state.swing_qty > config.swing_qty?
      * clamped: bool — did we actually rewrite state?
      * from, to: int — old and new state.swing_qty
      * reason: str — human-readable explanation

    Safety: only clamps when state.filled_qty == 0 AND state.state is
    ARMED_BUY or HALTED (never touches ARMED_SELL, which is mid-cycle).
    """
    from swing_leg import State

    cfg_target = int(trader.cfg.swing_qty)
    current = int(trader.s.swing_qty)

    if current <= cfg_target:
        return {"drifted": False, "clamped": False,
                "from": current, "to": current,
                "reason": f"state.swing_qty={current} <= config.swing_qty={cfg_target} (no drift)"}

    filled = int(getattr(trader.s, "filled_qty", 0) or 0)
    state_val = trader.s.state
    safe = (filled == 0) and (state_val in (State.ARMED_BUY, State.HALTED))
    if not safe:
        reason = (f"drift {current} > {cfg_target} but NOT safe to clamp "
                  f"(filled_qty={filled}, state={state_val.value}); manual review required")
        # Still notify — operator needs to know
        if notifier:
            try:
                from alerting import Priority
                notifier.send(
                    f"boot normalize refused: {trader.symbol} drift not safe",
                    f"tenant={trader.tenant_id}\nstate.swing_qty={current} > config.swing_qty={cfg_target}\n"
                    f"filled_qty={filled}, state={state_val.value}\n"
                    f"NOT clamping — mid-cycle position or non-zero fill. Manual review required.",
                    Priority.CRIT,
                )
            except Exception:
                pass
        if log:
            try:
                log.record("boot_state_normalize_refused",
                           tenant=trader.tenant_id, symbol=trader.symbol,
                           field="swing_qty", current=current, target=cfg_target,
                           filled_qty=filled, state=state_val.value)
            except Exception:
                pass
        return {"drifted": True, "clamped": False,
                "from": current, "to": current, "reason": reason}

    # Safe clamp: reset to config, clear any stale resting order, HALT
    # with an audit trail so the operator sees this on next boot.
    trader.s.swing_qty = cfg_target
    stale_oid = trader.s.live_order_id
    trader.s.live_order_id = None
    trader.s.state = State.HALTED
    prev_reason = trader.s.halt_reason or ""
    trader.s.halt_reason = (
        f"boot-normalize {int(time.time())}: state.swing_qty was {current}, "
        f"clamped to config.swing_qty={cfg_target}. Any stale resting order "
        f"({stale_oid}) cleared. Resume manually after Coinbase-side reconciliation."
        + (f" | prev: {prev_reason}" if prev_reason else "")
    )
    trader._save_state()

    if log:
        try:
            log.record("boot_state_normalize_clamped",
                       tenant=trader.tenant_id, symbol=trader.symbol,
                       field="swing_qty", from_=current, to=cfg_target,
                       filled_qty=filled, stale_live_order_id=stale_oid)
        except Exception:
            pass
    if notifier:
        try:
            from alerting import Priority
            notifier.send(
                f"boot normalize: {trader.symbol} swing_qty {current} → {cfg_target}",
                f"tenant={trader.tenant_id}\nstate.swing_qty drifted above config; "
                f"clamped to config on boot. Sleeve is HALTED — Resume manually "
                f"after verifying Coinbase position + no orphan orders.",
                Priority.CRIT,
            )
        except Exception:
            pass

    return {"drifted": True, "clamped": True,
            "from": current, "to": cfg_target,
            "reason": f"clamped to config on boot (was {current}, now {cfg_target})"}
