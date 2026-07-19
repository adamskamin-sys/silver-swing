"""Read-only: how long has a sleeve been ARMED_SELL without a resting_stop_oid?

Adam 2026-07-19: XLP scan-mrqn4az1 holds own_avg_entry=0.18775 with no
resting stop protecting it. Rule feedback_ratchet_stop_never_gap says a
held position must ALWAYS have a stop; a gap is a blocker.

Reports for each sleeve:
  - sleeve state, own_avg, stop_oid, armed_ts
  - cfg stop_loss_enabled (flat key, defaults False in SleeveConfig)
  - Coinbase-side: actual position size + any open orders on the product
  - Verdict: OK / WATCHING / BLOCKER / STALE (own_avg but no position)

Usage:  python3 diag_check_stop_gap.py XLP-20DEC30-CDE
"""
from __future__ import annotations
import os
import sys
import time


def _fmt_age(secs: float) -> str:
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs/60:.1f}min"
    return f"{secs/3600:.2f}h"


def main() -> None:
    if len(sys.argv) < 2:
        print("USAGE: python3 diag_check_stop_gap.py <PRODUCT_ID>")
        return
    product_id = sys.argv[1]
    print("=" * 78)
    print(f"STOP-GAP CHECK — {product_id}")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    raw = store._load()

    now = time.time()
    findings: list[str] = []

    # Coinbase ground truth
    cb_position_size = None
    cb_position_avg = None
    cb_open_orders: list[dict] = []
    try:
        from broker import BrokerConfig, CoinbaseBroker
        b = CoinbaseBroker(BrokerConfig(product_id=product_id))
        try:
            cb_position_size = int(b.position_qty())  # signed int
        except Exception as e:
            print(f"(position_qty failed: {e})")
        try:
            p = b.position  # property → _Pos with .qty / .avg_entry
            cb_position_avg = float(getattr(p, "avg_entry", 0.0) or 0.0)
        except Exception as e:
            print(f"(position property failed: {e})")
        try:
            open_orders = b.list_open_orders([product_id]) or []
            # Defensive post-filter: some SDK versions ignore product_ids.
            cb_open_orders = [o for o in open_orders
                               if (o.get("product_id") or o.get("symbol")) in (product_id, None)]
            if len(cb_open_orders) != len(open_orders):
                print(f"(post-filtered {len(open_orders) - len(cb_open_orders)} unrelated orders)")
        except Exception as e:
            print(f"(list_open_orders failed: {e})")
    except Exception as e:
        print(f"(broker init failed: {e})")

    print(f"\nCoinbase ground truth for {product_id}:")
    print(f"  position size: {cb_position_size} (avg={cb_position_avg})")
    print(f"  open orders  : {len(cb_open_orders)}")
    for o in cb_open_orders:
        oid = o.get("order_id") or o.get("id")
        side = o.get("side")
        typ = o.get("order_type") or o.get("type")
        px = o.get("limit_price") or o.get("stop_price") or o.get("price")
        qty = o.get("size") or o.get("qty")
        print(f"    - oid={oid}  side={side}  type={typ}  px={px}  qty={qty}")

    hits = 0
    for tenant, tenant_data in raw.items():
        if not isinstance(tenant_data, dict):
            continue
        entry = tenant_data.get(product_id)
        if not isinstance(entry, dict):
            continue
        cfg = entry.get("config") or {}
        state = entry.get("state") or {}
        sleeves_cfg = {s.get("id"): s for s in (cfg.get("sleeves") or [])}
        sleeves_state = state.get("sleeves") or {}
        hb = state.get("last_heartbeat_ts")
        hb_age = (now - float(hb)) if hb else None

        print(f"\n--- tenant={tenant} ---")
        print(f"bot heartbeat age: {_fmt_age(hb_age) if hb_age is not None else 'unknown'}")
        print(f"configured sleeves ({len(sleeves_cfg)}): {list(sleeves_cfg.keys())}")
        print(f"state sleeves     ({len(sleeves_state)}): {list(sleeves_state.keys())}")
        missing = set(sleeves_cfg) - set(sleeves_state)
        if missing:
            for m in missing:
                findings.append(f"{tenant}/{m}: configured but NO STATE (dropped)")
                print(f"  ✗ CONFIGURED BUT NO STATE: {m}")

        for sid, ss in sleeves_state.items():
            hits += 1
            sc = sleeves_cfg.get(sid, {})
            st = ss.get("state")
            own_avg = ss.get("own_avg_entry")
            stop_oid = ss.get("resting_stop_oid")
            armed_ts = (ss.get("armed_sell_since_ts") or
                        ss.get("own_avg_entry_ts") or
                        ss.get("armed_since_ts"))
            armed_age = (now - float(armed_ts)) if armed_ts else None
            stop_enabled = sc.get("stop_loss_enabled", False)

            print(f"\n  sid={sid}")
            print(f"    state={st}  own_avg={own_avg}  stop_oid={stop_oid}")
            print(f"    armed_ts={_fmt_age(armed_age) if armed_age is not None else 'unknown'} ago")
            print(f"    cfg stop_loss_enabled={stop_enabled}")

            holds_position = (st == "ARMED_SELL") and own_avg is not None
            if not holds_position:
                print(f"    → OK (no position held per bot state)")
                continue

            # Bot thinks it holds — cross-check Coinbase.
            if cb_position_size is not None and cb_position_size == 0:
                findings.append(
                    f"{tenant}/{sid}: STALE own_avg — bot thinks HELD but Coinbase position=0"
                )
                print(f"    ✗ STALE: bot has own_avg={own_avg} but Coinbase position=0")
                print(f"      → own_avg is a ghost from a prior cycle; needs clearing")
                continue

            # Position is real.
            if stop_oid:
                # Check if the oid appears in open orders
                matched = any((o.get("order_id") or o.get("id")) == stop_oid
                              for o in cb_open_orders)
                if matched:
                    print(f"    → OK (stop_oid {stop_oid} matches open order)")
                else:
                    findings.append(
                        f"{tenant}/{sid}: stop_oid {stop_oid} NOT in Coinbase open orders — orphan"
                    )
                    print(f"    ✗ ORPHAN stop_oid: {stop_oid} not in Coinbase open orders")
                continue

            if not stop_enabled:
                print(f"    → BY DESIGN (stop_loss_enabled=False in cfg — no stop expected)")
                continue

            # Held real position, stop enabled, no oid.
            if armed_age is None:
                findings.append(
                    f"{tenant}/{sid}: HELD w/o stop, arm time unknown — likely stale"
                )
                print(f"    ⚠ GAP: real position, stop enabled, no oid, unknown arm time")
            elif armed_age < 30:
                print(f"    → PROBABLY OK (just armed — stop should place next tick)")
            elif armed_age < 120:
                print(f"    ⚠ WATCHING: armed {_fmt_age(armed_age)} ago, no stop yet")
            else:
                findings.append(
                    f"{tenant}/{sid}: HELD w/o stop for {_fmt_age(armed_age)} — blocker"
                )
                print(f"    ✗ BLOCKER: held {_fmt_age(armed_age)} without stop (ratchet_stop_never_gap)")

    if hits == 0:
        print(f"\nNo {product_id} sleeves found.")
        return

    print("\n" + "=" * 78)
    if findings:
        print(f"⚠ {len(findings)} FINDING(S):")
        for f in findings:
            print(f"  - {f}")
    else:
        print("✓ No findings.")


if __name__ == "__main__":
    main()
