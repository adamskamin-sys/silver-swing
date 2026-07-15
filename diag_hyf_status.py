"""What's happening with HYF-31JUL26-CDE (or any product) right now.

Adam 2026-07-15: armed HYF sleeve from scanner but no BUY order on
Coinbase. Diag walks the full pipeline:

  1. Does the tenant have a config block for HYF? (sleeve save reached
     the store?)
  2. What sleeves are configured? qty / buy_px / sell_px / exit_mode
  3. What's each sleeve's persisted state? (ARMED_BUY vs ARMED_SELL vs
     HALTED)  ← the Option B fix seeds this to ARMED_BUY
  4. What's the __portfolio__ snapshot say about position?
  5. What does Coinbase list_open_orders return for HYF?
  6. Print the last 5 min of trade-log events matching HYF — the
     `sleeve_arm_skipped_*` events are the smoking gun for a blocked
     arm (position full, portfolio halted, book imbalance, etc.)

Read-only; no code changes, no orders.

Usage:
    python3 diag_hyf_status.py                       # HYF-31JUL26-CDE, adam-live tenant
    python3 diag_hyf_status.py HYP-20DEC30-CDE       # different product
    python3 diag_hyf_status.py HYF-31JUL26-CDE adam  # different tenant
"""
from __future__ import annotations
import os
import sys
import time


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "?"


def main() -> None:
    product_id = sys.argv[1] if len(sys.argv) > 1 else "HYF-31JUL26-CDE"
    tenant = sys.argv[2] if len(sys.argv) > 2 else "adam-live"

    print("=" * 78)
    print(f"HYF STATUS DIAG — {tenant}/{product_id}")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))

    # Step 1: config block?
    cfg = store.get_config(tenant, product_id) or {}
    print(f"\n[1] CONFIG block for {tenant}/{product_id}:")
    if not cfg:
        print("    ✗ NO CONFIG BLOCK — sleeve save didn't reach the store.")
        print("    → Either the arm-as-sleeve click failed silently, or it")
        print("      saved to a different tenant. Check dashboard/server.js logs.")
        return
    sleeves_cfg = cfg.get("sleeves") or []
    print(f"    ✓ config exists · {len(sleeves_cfg)} sleeve(s)")

    # Step 2: what sleeves?
    print(f"\n[2] SLEEVES configured on {product_id}:")
    if not sleeves_cfg:
        print("    ✗ config exists but NO sleeves. Nothing to arm.")
        return
    for s in sleeves_cfg:
        print(f"    · id={s.get('id')} name={s.get('name')!r}")
        print(f"      qty={s.get('qty')} exit_mode={s.get('exit_mode')}")
        print(f"      buy_px={s.get('buy_px')} sell_px={s.get('sell_px')}")
        print(f"      stop_loss_px={s.get('stop_loss_px')} anchor_type={s.get('anchor_type')}")
        print(f"      resting_stop_enabled={s.get('resting_stop_enabled')}")

    # Step 3: state?
    state = store.get_state(tenant, product_id) or {}
    sleeve_states = state.get("sleeves") or {}
    print(f"\n[3] SLEEVE STATE (from state_store):")
    if not sleeve_states:
        print("    ✗ NO STATE ROWS — the tick loop hasn't seen these sleeves yet")
        print("      OR the server didn't auto-seed on save (pre-520b4fc).")
        print("      A sleeve with no state defaults to ARMED_SELL on first")
        print("      tick, which for position=0 gets blocked by the floor guard")
        print("      and never buys.")
    else:
        for sid, ss in sleeve_states.items():
            print(f"    · id={sid} state={ss.get('state')}")
            print(f"      live_order_id={ss.get('live_order_id')}")
            print(f"      own_avg_entry={ss.get('own_avg_entry')}")
            print(f"      armed_buy_since_ts={_fmt_ts(ss.get('armed_buy_since_ts'))}")
            print(f"      halt_reason={ss.get('halt_reason')}")
            print(f"      resting_stop_oid={ss.get('resting_stop_oid')}")

    # Step 4: position from __portfolio__
    pf_cfg = (store.get_config(tenant, "__portfolio__") or {})
    derivs = pf_cfg.get("derivatives") or []
    print(f"\n[4] __portfolio__ snapshot position:")
    match = next((d for d in derivs if d.get("product_id") == product_id), None)
    if not match:
        print(f"    · {product_id} not in derivatives list (position = 0)")
    else:
        print(f"    · qty={match.get('qty')} avg={match.get('avg')} mark={match.get('mark')}")

    # Step 5: Coinbase open orders
    print(f"\n[5] COINBASE list_open_orders for {product_id}:")
    try:
        from broker import BrokerConfig, CoinbaseBroker
        b = CoinbaseBroker(BrokerConfig(product_id=product_id))
        opens = b.list_open_orders() or []
        matches = [o for o in opens if o.get("symbol") == product_id or o.get("product_id") == product_id]
        if not matches:
            print(f"    ✗ NO open orders for {product_id} on Coinbase")
        for o in matches:
            print(f"    · oid={str(o.get('order_id'))[:12]}... side={o.get('side')} "
                  f"price={o.get('price')} qty={o.get('qty')} status={o.get('status')}")
    except Exception as e:
        print(f"    ✗ list_open_orders failed: {e}")

    # Step 6: recent trade-log events matching HYF
    print(f"\n[6] TRADE LOG events matching {product_id} in last 15 min:")
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        cutoff = time.time() - 15 * 60
        events = [e for e in log.events()
                  if isinstance(e, dict) and float(e.get("ts", 0) or 0) >= cutoff]
        matches = [e for e in events
                   if str(e.get("symbol") or "") == product_id
                   or product_id in str(e.get("sleeve_id") or "")]
        matches.sort(key=lambda e: float(e.get("ts") or 0))
        if not matches:
            print(f"    · no events for {product_id} in last 15min")
            print(f"      (if you just armed, wait 30s and re-run — the discover")
            print(f"       loop is on a 10s cadence and the tick loop is 5s)")
        else:
            for e in matches[-25:]:
                ts = _fmt_ts(e.get("ts"))
                et = e.get("event_type", "?")
                keys = ("side", "qty", "price", "action", "reason", "why",
                        "sleeve_id", "state", "new_buy_px", "error", "current_position",
                        "intended_position", "position", "sleeve_qty", "core_qty")
                detail_bits = []
                for k in keys:
                    if k in e and e[k] not in (None, "", 0, 0.0):
                        v = e[k]
                        if isinstance(v, float):
                            v = f"{v:.4f}"
                        detail_bits.append(f"{k}={v}")
                print(f"      {ts}  {et:45s}  {' '.join(detail_bits)[:200]}")
    except Exception as e:
        print(f"    ✗ trade log read failed: {e}")

    print("\n" + "=" * 78)
    print("DIAGNOSIS")
    print("=" * 78)
    if not sleeve_states and sleeves_cfg:
        print("→ Sleeves configured but no state rows. Either the tick loop hasn't")
        print("  ticked them yet (wait 30s), or the server didn't auto-seed state")
        print("  (pre-520b4fc deploy — need to hard-refresh + re-arm).")
    elif sleeve_states:
        for sid, ss in sleeve_states.items():
            st = ss.get("state")
            if st == "ARMED_BUY" and not ss.get("live_order_id"):
                print(f"→ Sleeve {sid} is ARMED_BUY with no live order. Should be")
                print("  placing one on next tick. If it's been >30s, check step 6")
                print("  for a `sleeve_arm_skipped_*` event — some gate is blocking it.")
            elif st == "ARMED_SELL":
                print(f"→ Sleeve {sid} is ARMED_SELL (holding) but position=0. The")
                print("  server auto-seed didn't apply. Delete + re-arm the sleeve")
                print("  after refreshing the page.")


if __name__ == "__main__":
    main()
