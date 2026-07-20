"""One-shot: enable stop_loss on the XLM 31 Model B sleeve.

Adam 2026-07-20: after diag_xlm31_adopt.py flipped Model B to ARMED_SELL,
the sleeve still shows "STOP LOSS: NOT PLACED" because its config has
stop_loss_enabled=false (from the Model B "Defensive plus" preset).
Position is a real 1 LONG with NO exchange stop — §3.6 blocker.

This script sets stop_loss_enabled=true + stop_loss_px = own_avg × 0.98
on any Model B / attached sleeve on XLM-31JUL26-CDE that lacks a stop.

Idempotent: no-op if stop_loss is already enabled with a valid px.

Usage:
    python3 diag_xlm31_enable_stop.py           # dry-run
    python3 diag_xlm31_enable_stop.py --apply   # write
"""
from __future__ import annotations
import json
import os
import sys


PID = "XLM-31JUL26-CDE"


def main() -> None:
    apply = "--apply" in sys.argv
    print("=" * 78)
    print(f"XLM 31 ({PID}) ENABLE STOP — {'APPLY' if apply else 'DRY-RUN'}")
    print("=" * 78)

    import redis
    url = (os.environ.get("REDIS_URL")
           or os.environ.get("REDIS_INTERNAL_URL"))
    if not url:
        print("\n✗ REDIS_URL not set")
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
        print(f"\n✗ {PID} not in any live tenant")
        return

    block = store[target_tenant][PID]
    cfg = block.get("config") or {}
    state = block.get("state") or {}
    sleeves_cfg = cfg.get("sleeves") or []
    sleeves_state = state.get("sleeves") or {}

    # Find own_avg from state or broker
    own_avg = None
    for sid, ss in sleeves_state.items():
        oa = ss.get("own_avg_entry")
        if oa and float(oa) > 0:
            own_avg = float(oa)
            break
    if not own_avg:
        # Fall back to broker
        try:
            from broker import BrokerConfig, CoinbaseBroker
            b = CoinbaseBroker(BrokerConfig(product_id=PID))
            for p in (b.client.list_futures_positions().to_dict()
                      .get("positions") or []):
                if p.get("product_id") == PID:
                    own_avg = float(p.get("avg_entry_price") or 0)
                    break
        except Exception as e:
            print(f"\n✗ broker fallback failed: {e}")
    if not own_avg or own_avg <= 0:
        print(f"\n✗ can't determine own_avg — refusing to set stop_px")
        return
    stop_px = round(own_avg * 0.98, 6)

    print(f"\n  own_avg = ${own_avg}")
    print(f"  new stop_loss_px = ${stop_px}  (2% below own_avg)")
    print(f"\n  sleeves on {PID}:")

    to_update = []
    for s in sleeves_cfg:
        sid = s.get("id")
        cur_en = s.get("stop_loss_enabled")
        cur_px = s.get("stop_loss_px")
        needs = cur_en is False or not cur_px or float(cur_px or 0) <= 0
        marker = "→ FIX" if needs else "ok"
        print(f"    {sid}: name='{s.get('name')}' stop_enabled={cur_en} "
              f"stop_px={cur_px}  {marker}")
        if needs:
            to_update.append(sid)

    if not to_update:
        print("\n  ✓ all sleeves already have stop_loss configured")
        return
    if not apply:
        print(f"\n  DRY-RUN — re-run with --apply to enable stop_loss on {len(to_update)} sleeve(s)")
        return

    for s in sleeves_cfg:
        if s.get("id") in to_update:
            s["stop_loss_enabled"] = True
            s["stop_loss_px"] = stop_px
            s["_stop_loss_auto_enabled_by_diag"] = True
    cfg["sleeves"] = sleeves_cfg
    store[target_tenant][PID]["config"] = cfg
    r.set("silver-swing:store", json.dumps(store))
    print(f"\n  ✓ enabled stop_loss on {len(to_update)} sleeve(s) @ ${stop_px}")
    print(f"  Next tick will place the stop-limit on Coinbase; chip should turn green ~15s.")


if __name__ == "__main__":
    main()
