"""Pure-function autocorrect calculators used by live_runner reconciliation.

Extracted so the drift-clamp logic can be unit-tested without spinning up
the full live loop. The wiring stays in live_runner (see the reconciliation
autocorrect block around line 1449) — this module only computes the target
value; the caller decides whether to write.

Invariant enforced by `target_primary_swing_qty`:
    state.swing_qty ≤ max(config.swing_qty, exchange_qty - core - armed_sleeves)

State.swing_qty should never exceed either the user's intent (config) OR
the actual position on exchange attributable to the primary strategy. The
clamp target is the MAX so a stale-low config doesn't strand a real
primary position on exchange.

Closes the CHN class (2026-07-18): state=3, config=0, exchange=1. Prior
autocorrector only handled exchange==0 case; CHN was uncorrectable.
"""
from __future__ import annotations
from typing import Optional


def _sum_armed_sleeve_qty(config: dict, state: dict) -> int:
    """Sum config.qty for sleeves whose state is ARMED_SELL.

    Uses config qty (intent, not runtime) — matches how position_mismatch
    computes `expected_position` at live_runner.py:1366.
    """
    sleeve_states = state.get("sleeves") or {}
    total = 0
    for s_cfg in config.get("sleeves") or []:
        sid = s_cfg.get("id")
        if not sid:
            continue
        s_st = sleeve_states.get(sid) or {}
        if str(s_st.get("state") or "").upper() == "ARMED_SELL":
            try:
                total += int(s_cfg.get("qty") or 0)
            except (TypeError, ValueError):
                pass
    return total


def target_primary_swing_qty(
    state: dict,
    config: dict,
    exchange_qty: int,
) -> int:
    """Return the value state.swing_qty SHOULD be clamped to (or the
    current value if no clamp is needed).

    Returns state.swing_qty unchanged when:
      - state.swing_qty ≤ target (nothing to clamp)
      - anything malformed (fails safe — caller sees no change)

    Caller MUST also gate on:
      - snapshot freshness (don't act on stale portfolio data)
      - live_order_id is None (don't touch state during an in-flight order)
    """
    try:
        state_sq = int(state.get("swing_qty") or 0)
    except (TypeError, ValueError):
        return 0
    try:
        cfg_sq = int(config.get("swing_qty") or 0)
    except (TypeError, ValueError):
        cfg_sq = 0
    try:
        core = int(config.get("core_qty") or 0)
    except (TypeError, ValueError):
        core = 0

    armed_sleeve_qty = _sum_armed_sleeve_qty(config, state)
    expected_primary = max(0, int(exchange_qty) - core - armed_sleeve_qty)
    target = max(cfg_sq, expected_primary)
    if state_sq > target:
        return target
    return state_sq


def should_autocorrect(
    state: dict,
    config: dict,
    exchange_qty: int,
    snapshot_fresh: bool,
    symbol_present_in_snapshot: bool,
) -> tuple[bool, Optional[int], str]:
    """One-call decision: (should_write, new_swing_qty, reason).

    Returns (False, None, reason) when NOT safe to autocorrect. Reasons:
      - "live_order_in_flight"
      - "snapshot_stale_and_symbol_absent" (can't verify exchange truth)
      - "no_drift"
    """
    if state.get("live_order_id"):
        return False, None, "live_order_in_flight"
    if not snapshot_fresh and not symbol_present_in_snapshot:
        return False, None, "snapshot_stale_and_symbol_absent"

    try:
        state_sq = int(state.get("swing_qty") or 0)
    except (TypeError, ValueError):
        return False, None, "state_swing_qty_malformed"

    target = target_primary_swing_qty(state, config, exchange_qty)
    if target >= state_sq:
        return False, None, "no_drift"
    return True, target, "drift_clamped"
