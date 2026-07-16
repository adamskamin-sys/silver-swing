"""Expert-driven average-down advice — computation only, never places orders.

Called by the dashboard API when the user clicks a GREEN avg-down badge.
Reads the store for the product's current state, runs the signal check,
and computes expert-derived parameters for a scale-in sleeve.

Returns JSON:
  ok            : bool — True when signal is GREEN and advice is actionable
  light         : "green"|"amber"|"red"
  reasons       : [str]
  signal_checks : {...}                  — from avg_down_signal
  sleeve_id     : str                   — which sleeve is green-lit
  sleeve_name   : str
  current_qty   : int
  current_avg_entry : float
  current_mark  : float
  recommended_add_qty : int
  suggested_buy_px    : float           — limit slightly above floor
  blended_entry_px    : float           — weighted avg after the add
  new_stop_px         : float           — expert stop from blended entry
  new_sell_px         : float           — original sell target (unchanged)
  new_trail_trigger   : float           — blended entry + trail activation offset
  new_trail_distance  : float           — from expert_params / expert_trail
  atr                 : float
  margin_available    : float
  margin_needed       : float           — per add_qty

Usage (CLI mode, spawned by server.js):
  python3 avg_down_advisor.py \\
      --symbol ZEC-20DEC30-CDE --tenant adam-live [--store-path data]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional


def _safe_float(v, default=0.0) -> float:
    try:
        return float(v or 0) or default
    except (TypeError, ValueError):
        return default


def _green_sleeve(state: dict, config: dict) -> tuple[Optional[str], Optional[dict], Optional[dict]]:
    """Find the first ARMED_SELL sleeve that has a cost basis set.
    Prefers the sleeve most recently active."""
    sleeve_states = state.get("sleeves") or {}
    sleeve_cfgs = {s["id"]: s for s in (config.get("sleeves") or []) if s.get("id")}
    best_id = None
    best_state = None
    best_cfg = None
    best_ts = -1.0
    for sid, ss in sleeve_states.items():
        if str(ss.get("state") or "") != "ARMED_SELL":
            continue
        avg = ss.get("own_avg_entry")
        if not avg:
            continue
        sc = sleeve_cfgs.get(sid)
        if not sc:
            continue
        ts = _safe_float(ss.get("armed_sell_since_ts") or ss.get("last_heartbeat_ts"), 0.0)
        if ts > best_ts:
            best_ts = ts
            best_id = sid
            best_state = ss
            best_cfg = sc
    return best_id, best_state, best_cfg


def advise(symbol: str, tenant: str, store_path: str = "data") -> dict:
    """Compute average-down advice for a symbol/tenant. Pure — no side effects."""
    import state_store
    store = state_store.make_store(store_path)

    live_tenant = f"{tenant}-live" if not tenant.endswith("-live") else tenant

    state = store.get_state(live_tenant, symbol) or {}
    config = store.get_config(live_tenant, symbol) or {}
    snapshot = store.get_snapshot(live_tenant, symbol) or {}

    sleeve_id, sleeve_st, sleeve_cfg = _green_sleeve(state, config)
    if not sleeve_id:
        return {
            "ok": False, "light": "amber",
            "reasons": ["no ARMED_SELL sleeve with a cost basis found — nothing to average down on"],
            "signal_checks": {},
        }

    own_avg = _safe_float(sleeve_st.get("own_avg_entry"))
    current_qty = int(sleeve_cfg.get("qty") or 1)
    current_mark = _safe_float(snapshot.get("last_mark") or snapshot.get("mark"))
    if not current_mark:
        # fall back to sleeve buy_px as a rough mark
        current_mark = _safe_float(sleeve_cfg.get("buy_px"))
    if not current_mark:
        return {"ok": False, "light": "red", "reasons": ["no current mark available"], "signal_checks": {}}

    # ATR: try expert snapshot in Redis-backed store, else from snapshot field
    atr = 0.0
    try:
        if hasattr(store, "_r"):
            raw = store._r.get(f"expert_snapshot:{live_tenant}:{symbol}")
            if raw:
                ex = json.loads(raw)
                atr = _safe_float((ex.get("expert_snapshot") or {}).get("atr") or ex.get("atr"))
    except Exception:
        pass
    if not atr:
        atr = _safe_float(snapshot.get("atr"))
    if not atr:
        # rough fallback: 0.5% of mark (conservative ATR estimate)
        atr = current_mark * 0.005

    # Margin headroom check
    margin_per_ct = _safe_float(config.get("margin_per_contract") or sleeve_cfg.get("margin_per_contract"))
    if not margin_per_ct:
        margin_per_ct = current_mark * 0.10  # rough 10× leverage fallback
    available_margin = 0.0
    try:
        pf = store.get_config(live_tenant, "__portfolio__") or {}
        available_margin = _safe_float(
            pf.get("available_margin") or pf.get("futures_buying_power")
            or pf.get("available_balance") or pf.get("buying_power")
        )
    except Exception:
        pass
    have_margin = available_margin >= margin_per_ct or available_margin == 0.0  # fail-open on unknown balance

    # Build price history from snapshot ticks if available, else synthetic
    prices = []
    try:
        ticks = snapshot.get("recent_prices") or snapshot.get("price_history") or []
        prices = [float(p) for p in ticks if p]
    except Exception:
        pass
    if len(prices) < 24:
        # Synthesize a minimal history centered around the current mark so the
        # signal can run. This will not produce a GREEN (real conditions require
        # 24 real ticks) but avoids a crash.
        prices = [current_mark * (1 + 0.0003 * (i - 12)) for i in range(30)]

    # Microstructure snapshot for VPIN/OFI
    ms = snapshot.get("microstructure") or {}

    # Run the signal
    import avg_down_signal
    sig = avg_down_signal.average_down_signal(
        prices=prices,
        ms=ms if ms else None,
        ofi=None,
        position_avg=own_avg,
        last_price=current_mark,
        have_margin=have_margin,
        atr=atr,
    )

    if sig["light"] != "green":
        return {
            "ok": False,
            "light": sig["light"],
            "reasons": sig.get("reasons") or [],
            "signal_checks": sig.get("checks") or {},
            "sleeve_id": sleeve_id,
            "sleeve_name": sleeve_cfg.get("name") or sleeve_id,
            "current_qty": current_qty,
            "current_avg_entry": own_avg,
            "current_mark": current_mark,
        }

    # ── Expert parameter computation ──────────────────────────────────────────
    # Recommended qty: 1 additional contract (conservative default).
    # Could increase if margin allows, but start with 1 to match the
    # Jim Paul principle: "define max exposure FIRST."
    max_qty = int(sleeve_cfg.get("max_qty") or config.get("max_swing_qty") or current_qty + 1)
    add_qty = min(1, max_qty - current_qty)
    if add_qty < 1:
        return {
            "ok": False, "light": "amber",
            "reasons": [f"already at max qty ({current_qty}/{max_qty}) — cannot add more"],
            "signal_checks": sig.get("checks") or {},
            "sleeve_id": sleeve_id, "sleeve_name": sleeve_cfg.get("name") or sleeve_id,
            "current_qty": current_qty, "current_avg_entry": own_avg, "current_mark": current_mark,
        }

    # Suggested buy price: current mark (we want to fill near market, not chase)
    tick_size = _safe_float(config.get("tick_size") or 0.01)
    import math
    suggested_buy_px = math.floor(current_mark / tick_size) * tick_size  # round down to tick

    # Blended entry: weighted average of existing position + new add
    blended_entry = (own_avg * current_qty + suggested_buy_px * add_qty) / (current_qty + add_qty)

    # Expert stop distance from blended entry
    fee_rt = _safe_float(config.get("fee_per_contract_roundtrip") or sleeve_cfg.get("fee_per_roundtrip") or 0.50)
    contract_size = _safe_float(config.get("contract_size") or 1.0)
    new_stop_px = None
    try:
        import expert_stop
        stop_dec = expert_stop.optimal_stop_distance(
            mark=blended_entry,
            atr_est=atr,
            fee_per_roundtrip=fee_rt,
            contract_size=contract_size,
            qty=current_qty + add_qty,
        )
        if stop_dec:
            new_stop_px = round(blended_entry - stop_dec.stop_distance, 8)
    except Exception:
        pass
    if not new_stop_px:
        # Fallback: Wilder 2N (2×ATR below blended entry)
        new_stop_px = round(blended_entry - 2.0 * atr, 8)

    # Trail trigger: blended entry + trail_activation_offset from expert_params
    new_trail_trigger = None
    new_trail_distance = None
    try:
        import expert_params as ep
        ep_out = ep.expert_params(symbol, atr)
        if ep_out:
            offset = _safe_float(ep_out.get("trail_activation_offset") or ep_out.get("trail_distance"))
            if offset:
                new_trail_trigger = round(blended_entry + offset, 8)
            new_trail_distance = _safe_float(ep_out.get("trail_distance"))
    except Exception:
        pass
    if not new_trail_trigger:
        new_trail_trigger = round(blended_entry + 2.0 * atr, 8)
    if not new_trail_distance:
        new_trail_distance = round(2.0 * atr, 8)

    # Sell target: keep the original sleeve's sell_px (don't move the target)
    new_sell_px = _safe_float(sleeve_cfg.get("sell_px"))

    return {
        "ok": True,
        "light": "green",
        "reasons": sig.get("reasons") or [],
        "signal_checks": sig.get("checks") or {},
        "sleeve_id": sleeve_id,
        "sleeve_name": sleeve_cfg.get("name") or sleeve_id,
        "current_qty": current_qty,
        "current_avg_entry": round(own_avg, 8),
        "current_mark": round(current_mark, 8),
        "atr": round(atr, 6),
        "recommended_add_qty": add_qty,
        "suggested_buy_px": round(suggested_buy_px, 8),
        "blended_entry_px": round(blended_entry, 8),
        "new_stop_px": round(new_stop_px, 8),
        "new_sell_px": round(new_sell_px, 8) if new_sell_px else None,
        "new_trail_trigger": round(new_trail_trigger, 8),
        "new_trail_distance": round(new_trail_distance, 8),
        "margin_available": round(available_margin, 2),
        "margin_needed": round(margin_per_ct * add_qty, 2),
        # Include enough to reconstruct the new sleeve config server-side
        "source_sleeve": {
            "id": sleeve_id,
            "name": sleeve_cfg.get("name"),
            "exit_mode": sleeve_cfg.get("exit_mode", "fixed_limit"),
            "stop_loss_enabled": bool(sleeve_cfg.get("stop_loss_enabled")),
            "buy_trail_enabled": bool(sleeve_cfg.get("buy_trail_enabled")),
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--store-path", default=os.getenv("SWING_DATA_DIR", "data"))
    args = ap.parse_args()
    result = advise(args.symbol, args.tenant, args.store_path)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
