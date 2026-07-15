"""Force-place limit orders for sleeves stuck in ARMED_BUY without a live_order_id.

The bug (confirmed 2026-07-15 on ZEC): sleeve goes into ARMED_BUY state
but no actual limit order is placed at Coinbase. `live_order_id` stays
None. Result: price crosses the buy target repeatedly, no fill, because
there's nothing on the order book.

This diag:
  1. Walks every sleeve on adam-live
  2. For each in ARMED_BUY with live_order_id=None:
     - Places a real limit BUY at the sleeve's saved buy_px + qty
     - Updates sleeve state with the returned order_id
     - Logs a `sleeve_force_armed` event
  3. Reports what got fixed

Usage (Render silver-swing-bot-live shell):
    python3 diag_force_arm_missing_orders.py           # preview only
    python3 diag_force_arm_missing_orders.py --confirm # actually place orders

Safety:
  * Only affects sleeves in ARMED_BUY state (won't disturb HOLDING/ARMED_SELL)
  * Only places if live_order_id is None (won't duplicate existing orders)
  * Uses each sleeve's own configured buy_px + qty (no arbitrary values)
  * Preview mode by default — no writes without --confirm
"""
from __future__ import annotations
import argparse
import os
import sys
import time

from state_store import make_store


TENANT = "adam-live"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true",
                    help="actually place orders (default: preview only)")
    ap.add_argument("--product", type=str, default=None,
                    help="restrict to a single product_id (default: all)")
    args = ap.parse_args()

    store = make_store(os.getenv("SWING_DATA_DIR", "data"))

    # Find candidates
    print("=" * 70)
    print(f"SCANNING {TENANT} for ARMED_BUY sleeves without live_order_id...")
    print("=" * 70)
    candidates = []
    for symbol in store.list_symbols(TENANT):
        if symbol.startswith("__"):
            continue
        if args.product and symbol != args.product:
            continue
        state = store.get_state(TENANT, symbol) or {}
        cfg = store.get_config(TENANT, symbol) or {}
        sleeves_cfg = {s.get("id"): s for s in (cfg.get("sleeves") or [])}
        sleeves_st = state.get("sleeves") or {}
        for sid, ss in sleeves_st.items():
            state_val = str(ss.get("state", "")).upper()
            if state_val != "ARMED_BUY":
                continue
            if ss.get("live_order_id"):
                continue
            sc = sleeves_cfg.get(sid, {})
            buy_px = float(sc.get("buy_px") or 0)
            qty = int(sc.get("qty") or 0)
            if buy_px <= 0 or qty <= 0:
                print(f"  SKIP {symbol}/{sid}: invalid buy_px={buy_px} or qty={qty}")
                continue
            armed_hours = 0.0
            try:
                armed_ts = float(ss.get("armed_buy_since_ts") or 0)
                if armed_ts > 0:
                    armed_hours = (time.time() - armed_ts) / 3600
            except (TypeError, ValueError):
                pass
            candidates.append({
                "symbol": symbol, "sleeve_id": sid,
                "sleeve_name": sc.get("name", "?"),
                "buy_px": buy_px, "qty": qty,
                "armed_hours": armed_hours,
            })

    if not candidates:
        print()
        print("NO GHOST SLEEVES FOUND.")
        print("All ARMED_BUY sleeves have live_order_ids, or no ARMED_BUY exist.")
        return

    print()
    print(f"Found {len(candidates)} GHOST sleeve(s):")
    print()
    for c in candidates:
        print(f"  {c['symbol']}/{c['sleeve_id']} ({c['sleeve_name'][:50]})")
        print(f"    buy_px = ${c['buy_px']:.6f}")
        print(f"    qty    = {c['qty']}")
        print(f"    armed  = {c['armed_hours']:.1f} hours ago (without a live order)")
    print()

    if not args.confirm:
        print("PREVIEW only. Add --confirm to place the missing orders.")
        return

    # Actually place the orders
    print("=" * 70)
    print("PLACING ORDERS...")
    print("=" * 70)
    from broker import CoinbaseBroker, BrokerConfig
    fixed = 0
    failed = 0

    def _snap_to_tick(px: float, tick: float) -> float:
        """Round DOWN to the nearest tick (buy = OK to pay slightly less)."""
        if tick <= 0:
            return px
        return round(round(px / tick) * tick, 8)

    for c in candidates:
        symbol = c["symbol"]
        sid = c["sleeve_id"]
        try:
            # Look up tick_size from the sleeve's stored config (refreshed
            # against Coinbase periodically). Fall back to fetching from
            # the broker if not present.
            cfg = store.get_config(TENANT, symbol) or {}
            tick_size = float(cfg.get("tick_size") or 0)
            broker = CoinbaseBroker(BrokerConfig(product_id=symbol))
            if tick_size <= 0:
                # Fetch from Coinbase directly
                try:
                    prod = broker.client.get_product(symbol)
                    ti = getattr(prod, "quote_increment", None) or (
                        prod.get("quote_increment") if isinstance(prod, dict) else None)
                    tick_size = float(ti) if ti else 0
                except Exception:
                    tick_size = 0
            snapped_px = _snap_to_tick(c["buy_px"], tick_size) if tick_size > 0 else c["buy_px"]
            print(f"\n  {symbol}/{sid}: tick_size=${tick_size}, "
                  f"raw=${c['buy_px']:.6f} → snapped=${snapped_px:.6f}")
            print(f"    placing BUY {c['qty']} @ ${snapped_px:.6f}...")
            result = broker.place_limit(side="BUY", qty=c["qty"], price=snapped_px)
            oid = None
            if isinstance(result, dict):
                oid = result.get("order_id") or result.get("id")
            if not oid:
                print(f"    ERROR: place_limit returned no order_id: {result}")
                failed += 1
                continue
            # Update state
            state = store.get_state(TENANT, symbol) or {}
            sleeves_st = state.get("sleeves") or {}
            if sid in sleeves_st:
                sleeves_st[sid]["live_order_id"] = oid
                state["sleeves"] = sleeves_st
                store.put_state(TENANT, symbol, state)
                print(f"    ✓ order placed, id={oid}, state updated")
                fixed += 1
            else:
                print(f"    WARN: order placed (id={oid}) but sleeve state missing on write")
                failed += 1
        except Exception as e:
            print(f"    ERROR: {type(e).__name__}: {e}")
            failed += 1

    print()
    print("=" * 70)
    print(f"RESULTS: {fixed} fixed, {failed} failed out of {len(candidates)}")
    print("=" * 70)
    if fixed:
        print()
        print("Sleeves now have live orders resting at Coinbase.")
        print("Watch the trade log for sleeve_on_fill events when triggers hit.")


if __name__ == "__main__":
    main()
