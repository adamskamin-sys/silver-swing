"""Cancel duplicate limit-buy orders on META-USD.

Adam 2026-07-20 audit finding: Coinbase Orders page shows one META-USD
BUY 23 @ $4.394 FILLED at 03:52:12 (real fill, 23 META in account) plus
THREE orphan open limit BUYs 23 @ $4.155 (03:10:10, 02:57:25, 02:57:24)
— duplicates from repeat scanner arm-as-sleeve clicks. Each click
creates a new sleeve id + its own limit-buy. If mark rallies to $4.155,
all 3 fill → 69 unwanted extra META purchased at ~$286.98.

This diag lists every open limit BUY on META-USD, groups by price, and
cancels the DUPLICATES (keeps at most one at each unique price). Also
cancels ALL open buys if --all is passed.

Idempotent: no-op if nothing is duplicated.

Usage:
    python3 diag_meta_cancel_orphan_buys.py           # dry-run
    python3 diag_meta_cancel_orphan_buys.py --apply   # cancel dupes
    python3 diag_meta_cancel_orphan_buys.py --apply --all  # cancel ALL
"""
from __future__ import annotations
import json
import os
import sys


PID = "META-USD"


def _dump(obj):
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj if isinstance(obj, dict) else {}


def main() -> None:
    apply = "--apply" in sys.argv
    cancel_all = "--all" in sys.argv
    mode = "APPLY-ALL" if (apply and cancel_all) else "APPLY" if apply else "DRY-RUN"
    print("=" * 78)
    print(f"META-USD CANCEL ORPHAN LIMIT BUYS — {mode}")
    print("=" * 78)

    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id=PID))
    resp = _dump(b.client.list_orders(product_id=PID,
                                      order_status=["OPEN"]))
    orders = resp.get("orders") or []
    limit_buys = []
    for o in orders:
        side = str(o.get("side") or "").upper()
        otype = str(o.get("order_type") or "").upper()
        cfg = o.get("order_configuration") or {}
        lp_gtc = cfg.get("limit_limit_gtc") or {}
        if side != "BUY":
            continue
        if not lp_gtc:  # only plain limit-GTC buys (skip stops etc.)
            continue
        try:
            price = float(lp_gtc.get("limit_price") or 0)
            size = float(lp_gtc.get("base_size") or 0)
        except Exception:
            continue
        limit_buys.append({
            "oid": o.get("order_id"),
            "price": price,
            "size": size,
            "created": o.get("created_time"),
        })
    if not limit_buys:
        print("\n  ✓ no open limit BUYs on META-USD — nothing to cancel")
        return

    print(f"\n  {len(limit_buys)} open limit BUY(s):")
    for lb in sorted(limit_buys, key=lambda x: x["created"] or ""):
        print(f"    oid={lb['oid']} price=${lb['price']} size={lb['size']} created={lb['created']}")

    to_cancel = []
    if cancel_all:
        to_cancel = [lb["oid"] for lb in limit_buys]
    else:
        # Group by (price, size), keep the OLDEST one per group, cancel the rest.
        groups = {}
        for lb in limit_buys:
            key = (round(lb["price"], 6), round(lb["size"], 6))
            groups.setdefault(key, []).append(lb)
        for key, lbs in groups.items():
            if len(lbs) <= 1:
                continue
            lbs_sorted = sorted(lbs, key=lambda x: x["created"] or "")
            keep = lbs_sorted[0]
            for lb in lbs_sorted[1:]:
                to_cancel.append(lb["oid"])
        if not to_cancel:
            print("\n  ✓ no duplicates — every open buy is at a unique (price, size)")
            print(f"    Pass --all to cancel every open buy.")
            return

    print(f"\n  Would cancel {len(to_cancel)} order(s):")
    for oid in to_cancel:
        print(f"    {oid}")

    if not apply:
        print(f"\n  DRY-RUN — re-run with --apply to cancel")
        return

    ok, fail = 0, 0
    for oid in to_cancel:
        try:
            b.cancel(oid)
            ok += 1
            print(f"    ✓ cancelled {oid}")
        except Exception as e:
            fail += 1
            print(f"    ✗ {oid} FAILED: {type(e).__name__}: {e}")
    print(f"\n  Done: {ok} cancelled, {fail} failed.")


if __name__ == "__main__":
    main()
