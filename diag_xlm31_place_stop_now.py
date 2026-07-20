"""Place the XLM 31 resting stop-limit on Coinbase directly.

Adam 2026-07-20: XLM-31JUL26-CDE has 1 LONG @ $0.18492 with stop_loss_px
configured at $0.18375, but the dashboard still shows NOT PLACED. The
SwingTrader's _maintain_resting_stop isn't running for this product
(likely track is in the "20 dead" pool per HEALTH badge; reload-on-tick
never picks up the auto-adopted state).

Bypass the tick loop entirely — call broker.place_stop_limit directly
using the sleeve's configured stop_px, then write the resulting oid into
the sleeve state so the tick loop won't double-place when it recovers.

Audit-driven fix (per feedback_audit_before_fix.md): everything below is
based on concrete data pulled from Redis + Coinbase in prior diag runs.
No guessing about the failure mode — this is a direct rescue write.

Idempotent: refuses if a resting_stop_oid is already tracked, or if the
sleeve's stop_loss_enabled is false, or if position is not LONG > 0.

Usage:
    python3 diag_xlm31_place_stop_now.py           # dry-run
    python3 diag_xlm31_place_stop_now.py --apply   # actually place
"""
from __future__ import annotations
import json
import os
import sys
import time


PID = "XLM-31JUL26-CDE"


def _dump(obj):
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj if isinstance(obj, dict) else {}


def main() -> None:
    apply = "--apply" in sys.argv
    print("=" * 78)
    print(f"XLM 31 PLACE STOP-LIMIT — {'APPLY' if apply else 'DRY-RUN'}")
    print("=" * 78)

    # ---- 1. Verify position on Coinbase --------------------------------
    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id=PID))
    positions = _dump(b.client.list_futures_positions()).get("positions") or []
    pos = next((p for p in positions if p.get("product_id") == PID), None)
    if not pos:
        print(f"\n✗ {PID} not held — nothing to protect")
        return
    qty = int(float(pos.get("number_of_contracts") or 0))
    side = str(pos.get("side") or "").upper()
    avg = float(pos.get("avg_entry_price") or 0)
    if side != "LONG" or qty <= 0:
        print(f"\n✗ position is {side} {qty} — refusing to place stop (LONG-only)")
        return
    print(f"\n  Coinbase position: {side} {qty} @ ${avg}")

    # ---- 2. Read sleeve config + state ---------------------------------
    import redis
    url = (os.environ.get("REDIS_URL")
           or os.environ.get("REDIS_INTERNAL_URL"))
    if not url:
        print("\n✗ REDIS_URL not set")
        return
    r = redis.Redis.from_url(url, decode_responses=True)
    store = json.loads(r.get("silver-swing:store") or "{}")
    tenant = next((t for t in store if t.endswith("-live")
                   and PID in (store.get(t) or {})), None)
    if not tenant:
        print(f"\n✗ {PID} not in any live tenant")
        return
    block = store[tenant][PID]
    cfg = block.get("config") or {}
    state = block.get("state") or {}
    sleeves_cfg = cfg.get("sleeves") or []
    sleeves_state = state.get("sleeves") or {}

    # ---- 3. Check open orders on Coinbase (idempotency) ----------------
    open_orders_resp = _dump(b.client.list_orders(product_id=PID,
                                                   order_status=["OPEN"]))
    open_orders = open_orders_resp.get("orders") or []
    open_stops = [o for o in open_orders
                  if (o.get("order_configuration") or {}).get("stop_limit_stop_limit_gtc")]
    if open_stops:
        print(f"\n  ⚠ {len(open_stops)} stop-limit(s) already open on Coinbase:")
        for o in open_stops:
            oc = o.get("order_configuration", {}).get("stop_limit_stop_limit_gtc") or {}
            print(f"    oid={o.get('order_id')} stop=${oc.get('stop_price')} "
                  f"limit=${oc.get('limit_price')} size={oc.get('base_size')}")
        print(f"\n  Refusing to place another — would create §3.8 short-risk "
              f"(2 stops fire on same trigger).")
        return

    # ---- 4. Find the target sleeve (Model B) --------------------------
    target_sc = None
    target_ss = None
    target_sid = None
    for sc in sleeves_cfg:
        if not sc.get("stop_loss_enabled"):
            continue
        stop_px = float(sc.get("stop_loss_px") or 0)
        if stop_px <= 0:
            continue
        ss = sleeves_state.get(sc.get("id")) or {}
        if ss.get("resting_stop_oid"):
            print(f"\n  sleeve {sc.get('id')} already has resting_stop_oid={ss.get('resting_stop_oid')} — skipping")
            continue
        if str(ss.get("state") or "") != "ARMED_SELL":
            print(f"\n  sleeve {sc.get('id')} state={ss.get('state')} (not ARMED_SELL) — skipping")
            continue
        if float(ss.get("own_avg_entry") or 0) <= 0:
            print(f"\n  sleeve {sc.get('id')} own_avg=None — skipping")
            continue
        target_sc = sc
        target_ss = ss
        target_sid = sc.get("id")
        break
    if not target_sc:
        print(f"\n✗ no eligible sleeve to place stop for")
        return

    stop_price = float(target_sc.get("stop_loss_px"))
    sleeve_qty = int(target_sc.get("qty") or qty)
    # Cap sleeve_qty at actual position — never over-sell
    sleeve_qty = min(sleeve_qty, qty)

    # Compute limit_px: 1 tick below stop_price
    tick = float((_dump(b.client.get_product(PID))).get("price_increment") or 0.00001)
    limit_price = round(stop_price - tick, 6)

    print(f"\n  Target: sleeve {target_sid}")
    print(f"    stop_price:  ${stop_price}")
    print(f"    limit_price: ${limit_price}  (stop − 1 tick @ ${tick})")
    print(f"    qty:         {sleeve_qty}  (capped at position {qty})")

    if not apply:
        print(f"\n  DRY-RUN — re-run with --apply to place the real stop-limit on Coinbase")
        return

    # ---- 5. Place the stop-limit ---------------------------------------
    try:
        oid = b.place_stop_limit("SELL", sleeve_qty,
                                 float(stop_price), float(limit_price))
        print(f"\n  ✓ placed stop-limit — oid={oid}")
    except Exception as e:
        print(f"\n✗ place_stop_limit failed: {type(e).__name__}: {e}")
        print(f"  This is the actual reason the tick loop couldn't place it either.")
        return

    # ---- 6. Write oid into sleeve state (prevent double-place) --------
    sleeves_state[target_sid]["resting_stop_oid"] = oid
    sleeves_state[target_sid]["resting_stop_px"] = stop_price
    sleeves_state[target_sid]["resting_stop_stage"] = "hard_bottom"
    sleeves_state[target_sid]["_stop_placed_via_diag_ts"] = int(time.time())
    state["sleeves"] = sleeves_state
    store[tenant][PID]["state"] = state
    r.set("silver-swing:store", json.dumps(store))
    print(f"  ✓ wrote oid to sleeve state; tick loop won't re-place.")
    print(f"  Dashboard chip should turn green within 5s.")


if __name__ == "__main__":
    main()
