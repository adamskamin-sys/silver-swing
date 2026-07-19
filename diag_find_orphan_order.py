"""Identify the mystery $60.17 open BUY order that surfaced during the
XLP stop-gap check on 2026-07-19.

Prior diag_check_stop_gap passed a bare-string product filter to
list_open_orders (which expects list[str]); the SDK sent garbage and
Coinbase returned all-account orders. That's how we saw an unrelated
BUY at $60.17 qty=1 in the XLP output.

This diag lists EVERY open order on the account (no filter), grouped
by product_id, and flags any that don't have a matching sleeve's
live_order_id or resting_stop_oid in bot state. Those are orphans —
either abandoned by an old bot session or placed manually.

Read-only. Usage: python3 diag_find_orphan_order.py
"""
from __future__ import annotations
import os


def _order_price(o: dict) -> float:
    """Coinbase orders bury price under order_configuration."""
    cfg = o.get("order_configuration") or {}
    for shape_key in ("limit_limit_gtc", "limit_limit_gtd",
                       "limit_limit_fok", "limit_limit_ioc",
                       "stop_limit_stop_limit_gtc", "stop_limit_stop_limit_gtd"):
        shape = cfg.get(shape_key)
        if isinstance(shape, dict):
            px = (shape.get("limit_price") or shape.get("stop_price")
                  or shape.get("price"))
            if px:
                try: return float(px)
                except (TypeError, ValueError): pass
    return 0.0


def _order_qty(o: dict) -> float:
    cfg = o.get("order_configuration") or {}
    for shape in cfg.values():
        if isinstance(shape, dict):
            for k in ("base_size", "size", "quote_size"):
                v = shape.get(k)
                if v:
                    try: return float(v)
                    except (TypeError, ValueError): pass
    return 0.0


def main() -> None:
    print("=" * 78)
    print("ALL OPEN ORDERS (all products, all sides)")
    print("=" * 78)

    from broker import BrokerConfig, CoinbaseBroker
    # product_id is required by BrokerConfig but any live product works
    # for listing account-wide orders.
    b = CoinbaseBroker(BrokerConfig(product_id="SLR-27AUG26-CDE"))

    try:
        raw_resp = b.client.list_orders(order_status=["OPEN"])
        raw = raw_resp.to_dict() if hasattr(raw_resp, "to_dict") else raw_resp
    except Exception as e:
        print(f"✗ list_orders failed: {e}")
        return

    orders = raw.get("orders") or []
    print(f"\nTotal open orders: {len(orders)}")

    # Bot's known oids from every sleeve on every tenant
    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    raw_state = store._load()
    known_oids: set[str] = set()
    for tenant, tdata in raw_state.items():
        if not isinstance(tdata, dict):
            continue
        for symbol, entry in tdata.items():
            if not isinstance(entry, dict):
                continue
            state = entry.get("state") or {}
            for sid, ss in (state.get("sleeves") or {}).items():
                if not isinstance(ss, dict): continue
                for k in ("live_order_id", "resting_stop_oid"):
                    v = ss.get(k)
                    if v: known_oids.add(str(v))
            # Top-level live_order_id (primary swing state)
            if state.get("live_order_id"):
                known_oids.add(str(state.get("live_order_id")))

    # Group orders by product_id, flag orphans
    by_pid: dict[str, list[dict]] = {}
    for o in orders:
        pid = o.get("product_id") or "UNKNOWN"
        by_pid.setdefault(pid, []).append(o)

    orphans: list[dict] = []
    for pid in sorted(by_pid.keys()):
        print(f"\n--- {pid} ---")
        for o in by_pid[pid]:
            oid = o.get("order_id") or o.get("id")
            side = o.get("side")
            typ = o.get("order_type") or "?"
            px = _order_price(o)
            qty = _order_qty(o)
            is_orphan = oid not in known_oids
            marker = " ⚠ ORPHAN" if is_orphan else ""
            print(f"  oid={oid}  side={side}  px={px}  qty={qty}{marker}")
            if is_orphan:
                orphans.append({"pid": pid, "oid": oid, "side": side,
                                "px": px, "qty": qty})

    print(f"\n{'=' * 78}")
    if orphans:
        print(f"⚠ {len(orphans)} ORPHAN ORDER(S) NOT KNOWN TO ANY SLEEVE:")
        for o in orphans:
            print(f"  - {o['pid']}: {o['side']} {o['qty']} @ ${o['px']} "
                  f"(oid={o['oid']})")
        print(f"\nInvestigate each. Options:")
        print(f"  - Real position you placed manually → attach a strategy or leave")
        print(f"  - Leftover from a killed bot session → cancel via Coinbase UI or")
        print(f"    `b.cancel(oid)` after verifying")
    else:
        print(f"✓ Every open order matches a known bot sleeve oid.")


if __name__ == "__main__":
    main()
