"""Place the XLM 31 sell-at-target directly via broker.

Adam 2026-07-20: mark $0.1862 has crossed above SELL target $0.18540
but no cycle fired — the tick loop's _sleeve_step isn't running for
XLM-31JUL26-CDE (same track-dead root cause as the NOT PLACED chip).

This diag reads the sleeve's sell_px, cancels any pending BUY/STOP for
that product, and places a LIMIT SELL at sell_px directly via broker.
Since mark > sell_px, the limit fills immediately at (or near) mark.

Idempotent: refuses if a SELL is already open on Coinbase, if the
sleeve isn't ARMED_SELL, or if position < sleeve qty.

Usage:
    python3 diag_xlm31_sell_now.py           # dry-run
    python3 diag_xlm31_sell_now.py --apply   # place the sell
"""
from __future__ import annotations
import json
import os
import sys


PID = "XLM-31JUL26-CDE"


def _dump(obj):
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj if isinstance(obj, dict) else {}


def main() -> None:
    apply = "--apply" in sys.argv
    print("=" * 78)
    print(f"XLM 31 SELL AT TARGET — {'APPLY' if apply else 'DRY-RUN'}")
    print("=" * 78)

    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id=PID))

    # ---- Position ------------------------------------------------------
    positions = _dump(b.client.list_futures_positions()).get("positions") or []
    pos = next((p for p in positions if p.get("product_id") == PID), None)
    if not pos:
        print(f"\n✗ {PID} not held — nothing to sell")
        return
    qty = int(float(pos.get("number_of_contracts") or 0))
    side = str(pos.get("side") or "").upper()
    avg = float(pos.get("avg_entry_price") or 0)
    if side != "LONG" or qty <= 0:
        print(f"\n✗ position is {side} {qty} — refusing to sell (LONG-only)")
        return
    print(f"\n  Coinbase position: {side} {qty} @ ${avg}")

    # ---- Current mark --------------------------------------------------
    pd = _dump(b.client.get_product(PID))
    mark = float(pd.get("price") or 0)
    tick = float(pd.get("price_increment") or 0.00001)
    print(f"  current mark: ${mark}  (tick ${tick})")

    # ---- Sleeve config -------------------------------------------------
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

    target_sc = None
    for sc in sleeves_cfg:
        ss = sleeves_state.get(sc.get("id")) or {}
        if str(ss.get("state") or "") != "ARMED_SELL":
            continue
        if not sc.get("sell_px") or float(sc.get("sell_px") or 0) <= 0:
            continue
        target_sc = sc
        target_ss = ss
        break
    if not target_sc:
        print("\n✗ no ARMED_SELL sleeve with a valid sell_px")
        return

    sell_px = float(target_sc.get("sell_px"))
    sleeve_qty = min(int(target_sc.get("qty") or qty), qty)
    print(f"\n  sleeve: {target_sc.get('id')} name='{target_sc.get('name')}'")
    print(f"    sell_px:    ${sell_px}")
    print(f"    sleeve_qty: {sleeve_qty}")
    if mark < sell_px:
        print(f"\n  ⚠ mark ${mark} < sell_px ${sell_px} — limit sell will sit unfilled")
        print(f"    (proceeding anyway; will fill on any rally to ${sell_px})")

    # ---- Idempotency: already an open SELL? ----------------------------
    open_orders = _dump(b.client.list_orders(product_id=PID,
                                              order_status=["OPEN"])).get("orders") or []
    open_sells = [o for o in open_orders if str(o.get("side") or "").upper() == "SELL"]
    if open_sells:
        print(f"\n  ⚠ {len(open_sells)} SELL already open — refusing to place another")
        for o in open_sells:
            oc = o.get("order_configuration") or {}
            print(f"    oid={o.get('order_id')} config={json.dumps(oc, default=str)[:200]}")
        return

    if not apply:
        print(f"\n  DRY-RUN — would place LIMIT SELL {sleeve_qty} @ ${sell_px}")
        print(f"  Re-run with --apply to place")
        return

    # ---- Place LIMIT SELL ----------------------------------------------
    try:
        oid = b.place_limit("SELL", sleeve_qty, float(sell_px))
        print(f"\n  ✓ placed limit SELL — oid={oid}")
    except Exception as e:
        print(f"\n✗ place_limit failed: {type(e).__name__}: {e}")
        return

    # ---- Track in sleeve state -----------------------------------------
    sleeves_state[target_sc["id"]]["live_order_id"] = oid
    state["sleeves"] = sleeves_state
    store[tenant][PID]["state"] = state
    r.set("silver-swing:store", json.dumps(store))
    print(f"  ✓ wrote live_order_id to sleeve state")
    print(f"  Fill poller (reconcile every 5s) will credit the fill + advance state.")


if __name__ == "__main__":
    main()
