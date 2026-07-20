"""Investigate SLR-27AUG26-CDE short position.

Adam 2026-07-20: portfolio dashboard shows SLR side=SHORT qty=50. This is
a §3.8 violation — no shorting on adam-live. Need to see:
  1. Actual Coinbase position (source of truth)
  2. Recent SLR fills — the sequence of BUY/SELL that netted us short
  3. Open orders — any resting SELL that could deepen the short
  4. Bot state for SLR (own_avg, sleeves, resting stops)

Read-only. Usage:
    python3 diag_slr_short_investigate.py
"""
from __future__ import annotations
import json
import os


SLR_PID = "SLR-27AUG26-CDE"


def _dump(obj):
    """Coinbase SDK response → dict (handles both dict and object shapes)."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj if isinstance(obj, dict) else {}


def main() -> None:
    print("=" * 78)
    print(f"SLR SHORT INVESTIGATION — {SLR_PID}")
    print("=" * 78)

    # ---- 1. Live position from Coinbase ---------------------------------
    print("\n[1/5] Live futures position from Coinbase")
    print("-" * 78)
    try:
        from broker import BrokerConfig, CoinbaseBroker
        b = CoinbaseBroker(BrokerConfig(product_id=SLR_PID))
        resp = _dump(b.client.list_futures_positions())
        positions = resp.get("positions") or []
        slr_pos = None
        for p in positions:
            if p.get("product_id") == SLR_PID:
                slr_pos = p
                break
        if slr_pos:
            print(f"  ✓ Coinbase reports SLR position:")
            for k in ("product_id", "number_of_contracts", "side",
                      "avg_entry_price", "current_price", "unrealized_pnl",
                      "liquidation_price", "expiration_time"):
                v = slr_pos.get(k)
                if v is not None:
                    print(f"    {k}: {v}")
            print(f"\n  RAW: {json.dumps(slr_pos, default=str, indent=2)[:800]}")
        else:
            print(f"  ✗ SLR NOT in list_futures_positions — position is FLAT on Coinbase")
            print(f"    (dashboard's SHORT label is stale — trigger a portfolio refresh)")
            print(f"    Other positions returned: "
                  f"{[p.get('product_id') for p in positions]}")
    except Exception as e:
        print(f"  ✗ FAILED: {type(e).__name__}: {e}")
        return

    # ---- 2. Recent fills on SLR ------------------------------------------
    print(f"\n[2/5] Last 30 SLR fills (chronological)")
    print("-" * 78)
    try:
        # list_fills is Coinbase's fills endpoint
        resp = _dump(b.client.list_fills(product_id=SLR_PID, limit=30))
        fills = resp.get("fills") or []
        if not fills:
            print(f"  (no recent fills)")
        else:
            print(f"  {len(fills)} fills returned:")
            print(f"  {'when':<26} {'side':<6} {'qty':<8} {'price':<12} {'order_id'}")
            running_net = 0
            # API returns newest first — walk in reverse for chronological
            for f in reversed(fills):
                side = str(f.get("side") or "").upper()
                size = float(f.get("size") or 0)
                price = float(f.get("price") or 0)
                when = str(f.get("trade_time") or "")[:23]
                oid = str(f.get("order_id") or "")[:18]
                signed = size if side == "BUY" else -size
                running_net += signed
                print(f"  {when:<26} {side:<6} {size:<8.0f} ${price:<10.4f} {oid}  net={running_net:+.0f}")
    except Exception as e:
        print(f"  ✗ list_fills failed: {type(e).__name__}: {e}")

    # ---- 3. Open orders on SLR -------------------------------------------
    print(f"\n[3/5] Open orders on SLR")
    print("-" * 78)
    try:
        resp = _dump(b.client.list_orders(product_id=SLR_PID,
                                          order_status=["OPEN", "PENDING"]))
        orders = resp.get("orders") or []
        if not orders:
            print(f"  (no open orders)")
        else:
            for o in orders:
                side = str(o.get("side") or "").upper()
                size = o.get("base_size") or "?"
                oid = o.get("order_id") or "?"
                otype = o.get("order_type") or "?"
                trigger = (o.get("order_configuration") or {})
                print(f"  {side:<6} {size:<8} type={otype:<15} oid={oid}")
                if trigger:
                    print(f"    config: {json.dumps(trigger, default=str)[:300]}")
    except Exception as e:
        print(f"  ✗ list_orders failed: {type(e).__name__}: {e}")

    # ---- 4. Bot state for SLR --------------------------------------------
    print(f"\n[4/5] Bot state for SLR (Redis)")
    print("-" * 78)
    try:
        import redis
        url = (os.environ.get("REDIS_URL")
               or os.environ.get("REDIS_INTERNAL_URL"))
        if not url:
            print(f"  REDIS_URL not set — skipping")
        else:
            r = redis.Redis.from_url(url, decode_responses=True)
            store_raw = r.get("silver-swing:store")
            store = json.loads(store_raw) if store_raw else {}
            # Find live tenant
            live_tenants = [k for k in store.keys() if k.endswith("-live")]
            for lt in live_tenants:
                slr_block = (store.get(lt) or {}).get(SLR_PID) or {}
                if not slr_block:
                    continue
                cfg = slr_block.get("config") or {}
                state = slr_block.get("state") or {}
                snap = slr_block.get("snapshot") or {}
                print(f"  tenant: {lt}")
                print(f"    config.swing_qty:    {cfg.get('swing_qty')}")
                print(f"    config.core_qty:     {cfg.get('core_qty')}")
                print(f"    snapshot.position_qty: {snap.get('position_qty')}")
                print(f"    state.state:         {state.get('state')}")
                print(f"    state.live_order_id: {state.get('live_order_id')}")
                sleeves = state.get("sleeves") or {}
                print(f"    sleeves count:       {len(sleeves)}")
                for sid, ss in sleeves.items():
                    print(f"      sleeve {sid}: state={ss.get('state')}, "
                          f"own_avg={ss.get('own_avg_entry')}, "
                          f"qty={ss.get('qty')}, "
                          f"resting_stop_oid={ss.get('resting_stop_oid')}")
    except Exception as e:
        print(f"  ✗ Redis check failed: {type(e).__name__}: {e}")

    # ---- 5. What to do next --------------------------------------------
    print(f"\n[5/5] Recommended action")
    print("-" * 78)
    print("  If Coinbase confirms SHORT 50:")
    print("    → 50 unwanted short exposure on silver. To flatten: BUY 50 SLR")
    print("      market. But confirm the order sequence above first — if there")
    print("      is a bot bug that will re-short after we flatten, cover just")
    print("      one shot at a time or pause the bot before covering.")
    print("  If Coinbase shows position=0:")
    print("    → dashboard is stale; run diag_refresh_portfolio.py.")
    print("  If Coinbase shows LONG (dashboard was wrong):")
    print("    → dashboard-display bug; check portfolio_snapshot side derivation.")


if __name__ == "__main__":
    main()
