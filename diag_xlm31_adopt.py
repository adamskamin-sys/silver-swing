"""One-shot: auto-adopt the XLM 31 (XLM-31JUL26-CDE) Model B sleeve into
the existing 1 LONG position.

Adam 2026-07-20: XLM 31 currently holds 1 LONG @ ~$0.1849 but the attached
Model B sleeve is stuck in state=ARMED_BUY (thinks it doesn't hold) —
safety net refuses the buy because position is full, tick loop's
_maybe_reconcile_orphan_position isn't firing (track may be dead per
"5 dead" health badge). Result: unmanaged held position + orphan sleeve.

This script flips the sleeve to ARMED_SELL with own_avg_entry = broker
position avg, so the sleeve manages the exit properly.

Write-side but IDEMPOTENT: reads current state, only mutates if
state==ARMED_BUY + own_avg is empty + broker shows LONG. Prints intended
change and asks for confirmation before writing.

Usage:
    python3 diag_xlm31_adopt.py           # dry-run (default)
    python3 diag_xlm31_adopt.py --apply   # actually write
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
    print(f"XLM 31 ({PID}) ADOPT — {'APPLY' if apply else 'DRY-RUN'}")
    print("=" * 78)

    # ---- Broker position (source of truth) -------------------------------
    try:
        from broker import BrokerConfig, CoinbaseBroker
        b = CoinbaseBroker(BrokerConfig(product_id=PID))
        positions = _dump(b.client.list_futures_positions()).get("positions") or []
        pos = next((p for p in positions if p.get("product_id") == PID), None)
        if not pos:
            print(f"\n✗ {PID} not in list_futures_positions — position is FLAT.")
            print(f"  Nothing to adopt. If dashboard says 1 LONG, refresh __portfolio__.")
            return
        qty = int(float(pos.get("number_of_contracts") or 0))
        side = str(pos.get("side") or "").upper()
        avg = float(pos.get("avg_entry_price") or 0)
        print(f"\n  Coinbase reports: {side} {qty} @ ${avg}")
        if side != "LONG" or qty <= 0 or avg <= 0:
            print(f"  ✗ Not a valid LONG position — refusing to adopt.")
            return
    except Exception as e:
        print(f"\n✗ broker probe failed: {type(e).__name__}: {e}")
        return

    # ---- Redis store: find live tenant + sleeve --------------------------
    try:
        import redis
        url = (os.environ.get("REDIS_URL")
               or os.environ.get("REDIS_INTERNAL_URL"))
        if not url:
            print(f"\n✗ REDIS_URL not set")
            return
        r = redis.Redis.from_url(url, decode_responses=True)
        store_raw = r.get("silver-swing:store")
        store = json.loads(store_raw) if store_raw else {}
        live_tenants = [k for k in store.keys() if k.endswith("-live")]
        target_tenant = None
        for lt in live_tenants:
            if PID in (store.get(lt) or {}):
                target_tenant = lt
                break
        if not target_tenant:
            print(f"\n✗ {PID} not in any live tenant store")
            return
        block = store[target_tenant][PID]
        state_block = block.get("state") or {}
        sleeves = state_block.get("sleeves") or {}
        if not sleeves:
            print(f"\n✗ no sleeves on {PID}")
            return
        print(f"\n  tenant: {target_tenant}")
        print(f"  sleeves on this product ({len(sleeves)}):")
        for sid, ss in sleeves.items():
            print(f"    {sid}: state={ss.get('state')}, "
                  f"own_avg={ss.get('own_avg_entry')}, qty={ss.get('qty')}")
    except Exception as e:
        print(f"\n✗ Redis read failed: {type(e).__name__}: {e}")
        return

    # ---- Compute intended changes ---------------------------------------
    to_flip = []
    for sid, ss in sleeves.items():
        if str(ss.get("state") or "") != "ARMED_BUY":
            continue
        if ss.get("own_avg_entry") not in (None, 0, 0.0):
            continue
        to_flip.append(sid)
    if not to_flip:
        print(f"\n  ✓ nothing to adopt — no ARMED_BUY sleeves with empty own_avg")
        return

    print(f"\n  Would flip {len(to_flip)} sleeve(s) to ARMED_SELL + own_avg=${avg}:")
    for sid in to_flip:
        print(f"    {sid}")

    if not apply:
        print(f"\n  DRY-RUN — re-run with --apply to write.")
        return

    # ---- Apply -----------------------------------------------------------
    ts = int(time.time())
    for sid in to_flip:
        sleeves[sid]["state"] = "ARMED_SELL"
        sleeves[sid]["own_avg_entry"] = avg
        sleeves[sid]["_own_avg_source"] = "diag_xlm31_adopt_applied"
        sleeves[sid]["_adopted_ts"] = ts
    state_block["sleeves"] = sleeves
    store[target_tenant][PID]["state"] = state_block
    r.set("silver-swing:store", json.dumps(store))
    print(f"\n  ✓ wrote {len(to_flip)} sleeve(s) → ARMED_SELL")
    print(f"  Next tick loop will manage the exit; dashboard should refresh within 5s.")


if __name__ == "__main__":
    main()
