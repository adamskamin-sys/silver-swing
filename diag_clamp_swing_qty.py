"""Clamp state.swing_qty down to match config.swing_qty.

Adam 2026-07-18: reconciliation autocorrector only fires when
exchange_qty == 0. For products where exchange still holds some
contracts (e.g., CHN today: exchange=1, bot-expected=3, drift=-2),
the drift persists. Bot keeps trying to sell more than it holds
until Redis is manually fixed.

This diag directly zeros state.swing_qty down to config.swing_qty
(the source of truth per feedback_scale_up_realized_only). Semantic:
"whatever the user configured is what should be held, minus any
core position."

Read-only by default. --apply required. Refuses if:
  - state has live_order_id (order in flight)
  - state.swing_qty already <= config.swing_qty (no drift)
  - --force can override (dangerous)

Usage:
    python3 diag_clamp_swing_qty.py CHN-19DEC30-CDE
    python3 diag_clamp_swing_qty.py CHN-19DEC30-CDE --apply
"""
from __future__ import annotations
import argparse
import os
import sys
import time


TENANT = "adam-live"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("product_id")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="override live-order guard")
    ap.add_argument("--data-dir", default=os.getenv("SWING_DATA_DIR", "data"))
    args = ap.parse_args()

    pid = args.product_id
    from state_store import make_store
    store = make_store(args.data_dir)
    st = store.get_state(TENANT, pid) or {}
    cfg = store.get_config(TENANT, pid) or {}

    print(f"BEFORE ({TENANT}/{pid}):")
    print(f"  state.state        = {st.get('state')!r}")
    print(f"  state.swing_qty    = {st.get('swing_qty')}")
    print(f"  state.live_order_id= {st.get('live_order_id')!r}")
    print(f"  config.swing_qty   = {cfg.get('swing_qty')}")
    print(f"  config.core_qty    = {cfg.get('core_qty')}")

    try:
        state_sq = int(st.get("swing_qty") or 0)
    except (TypeError, ValueError):
        state_sq = 0
    try:
        cfg_sq = int(cfg.get("swing_qty") or 0)
    except (TypeError, ValueError):
        cfg_sq = 0

    if state_sq <= cfg_sq:
        print(f"\n  ✓ No drift — state.swing_qty ({state_sq}) already "
              f"<= config.swing_qty ({cfg_sq}). Nothing to do.")
        return

    print(f"\n  ⚠ DRIFT: state.swing_qty={state_sq} > config.swing_qty={cfg_sq}")

    if not args.apply:
        print(f"\nPREVIEW — proposed change:")
        print(f"  state.swing_qty     = {state_sq} → {cfg_sq}")
        print(f"  state.live_order_id = {st.get('live_order_id')!r} → None "
              f"(clears stale order tracking)")
        print(f"\nRe-run with --apply to write.")
        return

    if st.get("live_order_id") and not args.force:
        print(f"\n✗ REFUSED — state has live_order_id={st.get('live_order_id')}. "
              f"Cancel the order on Coinbase first, or re-run with --force.")
        sys.exit(2)

    new_state = dict(st)
    new_state["swing_qty"] = cfg_sq
    new_state["live_order_id"] = None
    new_state["_clamp_ts"] = time.time()
    new_state["_clamp_prev_swing_qty"] = state_sq
    store.put_state(TENANT, pid, new_state)

    print(f"\n✓ CLAMPED: state.swing_qty {state_sq} → {cfg_sq}")
    print(f"  Bot will stop trying to sell {state_sq - cfg_sq} phantom contracts.")

    try:
        from safety import make_trade_log
        log = make_trade_log(args.data_dir)
        log.record("state_swing_qty_clamped_via_diag",
                   tenant=TENANT, symbol=pid,
                   old_swing_qty=state_sq, new_swing_qty=cfg_sq,
                   severity="warn",
                   reason="manual clamp via diag_clamp_swing_qty.py — "
                          "state drifted above config despite non-zero exchange qty")
    except Exception:
        pass


if __name__ == "__main__":
    main()
