"""Manually force a sleeve to ADOPT an orphan position.

Adam 2026-07-15: ZEC sleeve is ARMED_BUY with pos=1 but the auto-reconciler
_maybe_reconcile_orphan_position hasn't fired yet. This script:

  1. Verifies the target sleeve is in the state we expect (ARMED_BUY, no
     own_avg_entry, pos > 0, no other claimant)
  2. Reads Coinbase's actual avg entry
  3. If --apply: cancels any pending buy, sets own_avg_entry from broker,
     flips state to ARMED_SELL, persists
  4. Otherwise: dry-run showing what WOULD change

Read-only by default. Usage:
    python3 diag_adopt_orphan_position.py ZEC-20DEC30-CDE               # dry-run
    python3 diag_adopt_orphan_position.py ZEC-20DEC30-CDE --apply       # execute
"""
from __future__ import annotations
import os
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("USAGE: python3 diag_adopt_orphan_position.py <PRODUCT_ID> [--apply]")
        return
    product_id = sys.argv[1]
    apply = "--apply" in sys.argv

    print("=" * 78)
    print(f"ORPHAN ADOPTION{'  (APPLYING)' if apply else '  (dry-run)'} — {product_id}")
    print("=" * 78)

    # 1) Load state
    data_dir = os.getenv("SWING_DATA_DIR", "data")
    import state_store
    store = state_store.make_store(data_dir)
    raw = store._load()

    # 2) Find the sleeve
    target_tenant = None
    target_sleeve = None
    target_cfg = None
    other_owned_qty = 0
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
        for sid, sc in sleeves_cfg.items():
            ss = sleeves_state.get(sid, {}) or {}
            if ss.get("state") == "ARMED_BUY" and ss.get("own_avg_entry") is None:
                target_tenant = tenant
                target_sleeve = ss
                target_cfg = sc
                target_sid = sid
            elif ss.get("own_avg_entry"):
                other_owned_qty += int(sc.get("qty", 1) or 1)

    if not target_sleeve:
        print(f"\nNo ARMED_BUY sleeve without own_avg_entry found on {product_id}.")
        return

    print(f"\nFound: tenant={target_tenant} sleeve_id={target_sid}")
    print(f"       state={target_sleeve.get('state')}  own_avg_entry={target_sleeve.get('own_avg_entry')}")
    print(f"       cycles={target_sleeve.get('cycles', 0)}  realized_pnl=${target_sleeve.get('realized_pnl', 0):.2f}")
    print(f"       live_order_id={target_sleeve.get('live_order_id')}")

    # 3) Coinbase position + avg
    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id=product_id))
    pos_qty = int(b.position_qty() or 0)
    snap = b.snapshot()
    avg = float(snap.get("position_avg_entry") or 0)
    print(f"\nCoinbase: position_qty={pos_qty}  avg_entry=${avg}")
    print(f"          snapshot keys: {list(snap.keys())[:20]}")

    if pos_qty <= 0:
        print("\n✗ No Coinbase position — nothing to adopt.")
        return
    if avg <= 0:
        print("\n✗ Coinbase returned avg=0 — cannot adopt (this may be the bug).")
        print("  Trying alternate read via list_futures_positions...")
        try:
            from broker import _dump
            resp = _dump(b.client.list_futures_positions())
            for p in resp.get("positions") or []:
                if p.get("product_id") == product_id:
                    print(f"  raw position: {p}")
                    alt_avg = p.get("avg_entry_price") or p.get("entry_price")
                    if alt_avg:
                        avg = float(alt_avg)
                        print(f"  ✓ using alt_avg={avg}")
                    break
        except Exception as e:
            print(f"  alternate read failed: {e}")
    if avg <= 0:
        print("\n✗ Still no avg — abort.")
        return

    unclaimed = pos_qty - other_owned_qty
    print(f"\nOther sleeves claim: {other_owned_qty} contracts")
    print(f"Unclaimed:            {unclaimed} contracts")
    if unclaimed <= 0:
        print("✗ Position is already fully claimed by other sleeves — no adoption.")
        return

    print("\nWOULD PATCH:")
    print(f"  state:           ARMED_BUY → ARMED_SELL")
    print(f"  own_avg_entry:   None → ${avg}")
    print(f"  live_order_id:   {target_sleeve.get('live_order_id')} → None")

    if not apply:
        print("\n(dry-run — pass --apply to execute)")
        return

    # 4) Apply
    if target_sleeve.get("live_order_id"):
        try:
            b.cancel(target_sleeve["live_order_id"])
            print(f"\n✓ cancelled old buy order {target_sleeve['live_order_id']}")
        except Exception as e:
            print(f"\n⚠ cancel of {target_sleeve['live_order_id']} failed: {e}")
            print("  Proceeding with state patch anyway.")

    target_sleeve["state"] = "ARMED_SELL"
    target_sleeve["own_avg_entry"] = float(avg)
    target_sleeve["live_order_id"] = None

    # Persist via put_state — need to write back the whole sleeves state map
    entry = raw[target_tenant][product_id]
    state = entry.get("state") or {}
    sleeves_state = state.get("sleeves") or {}
    sleeves_state[target_sid] = target_sleeve
    state["sleeves"] = sleeves_state
    store.put_state(target_tenant, product_id, state)

    print(f"\n✓ APPLIED. Sleeve {target_sid} now ARMED_SELL with own_avg_entry=${avg}")
    print("  Next tick: sleeve arms a proper SELL, unrealized will show correctly.")


if __name__ == "__main__":
    main()
