"""Audit XLP-20DEC30-CDE reblend + force reblend if the tick loop didn't.

Adam 2026-07-20: manual XLM PERP Buy at 05:12:00 (product = XLP-20DEC30-CDE
per project_xlp_xlm_alias memory) should have caused _maybe_reblend_on_
manual_add to refresh sleeve own_avg from Coinbase's blended avg. If XLP
Track is dead or reblend didn't fire, dashboard still shows old avg.

Read-only by default. --apply forces the reblend by writing new own_avg
into Redis. Use only if XLP's Track is still dead post-deploy AND the
dashboard shows old avg.

Usage:
    python3 diag_xlp_reblend_check.py           # audit
    python3 diag_xlp_reblend_check.py --apply   # force reblend
"""
from __future__ import annotations
import json
import os
import sys
import time


PID = "XLP-20DEC30-CDE"


def _dump(obj):
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj if isinstance(obj, dict) else {}


def main() -> None:
    apply = "--apply" in sys.argv
    print("=" * 78)
    print(f"XLP REBLEND AUDIT — {'APPLY' if apply else 'DRY-RUN'}")
    print("=" * 78)

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

    print(f"\n  tenant: {tenant}")
    print(f"  sleeves configured: {len(sleeves_cfg)}")

    # ---- Broker truth --------------------------------------------------
    from broker import BrokerConfig, CoinbaseBroker
    b = CoinbaseBroker(BrokerConfig(product_id=PID))
    positions = _dump(b.client.list_futures_positions()).get("positions") or []
    pos = next((p for p in positions if p.get("product_id") == PID), None)
    if not pos:
        print(f"\n✗ {PID} not held on Coinbase — nothing to reblend")
        return
    broker_qty = int(float(pos.get("number_of_contracts") or 0))
    broker_avg = float(pos.get("avg_entry_price") or 0)
    broker_side = str(pos.get("side") or "").upper()
    print(f"  Coinbase truth: {broker_side} {broker_qty} @ ${broker_avg}")

    # ---- Sleeve state --------------------------------------------------
    armed_sell_qty = 0
    for sc in sleeves_cfg:
        sid = sc.get("id")
        ss = sleeves_state.get(sid) or {}
        st = ss.get("state") or "?"
        own = ss.get("own_avg_entry")
        qty = sc.get("qty") or 0
        print(f"    • {sid} qty={qty} state={st} own_avg={own}")
        if st == "ARMED_SELL":
            armed_sell_qty += int(qty)
    core = int(cfg.get("core_qty") or 0)
    excess = broker_qty - armed_sell_qty - core
    print(f"\n  armed_sell_qty={armed_sell_qty}  core={core}  broker={broker_qty}")
    print(f"  excess = {excess}  {'(manual add detected)' if excess > 0 else '(no unaccounted qty)'}")

    if excess <= 0:
        print("\n  ✓ Nothing to reblend — all broker qty is claimed by sleeves.")
        return

    # ---- Heartbeat check ----------------------------------------------
    hb = ((store[tenant].get("__track_heartbeat__") or {}).get("config") or {})
    if not hb:
        hb = store[tenant].get("__track_heartbeat__") or {}
    tracks = hb.get("tracks") or {}
    xlp_alive = PID in tracks
    if xlp_alive:
        t = tracks[PID]
        step_age = int(time.time() - float(t.get("last_step_ok_ts") or 0))
        print(f"\n  ✓ XLP Track alive — step_age {step_age}s   ticks={t.get('tick_count')}")
        if step_age < 30:
            print("    Tick loop IS running for XLP — reblend should fire.")
            print("    If own_avg still shows old value, reblend condition failed")
            print("    (own_avg matches broker within tick_size, or in-flight buy).")
    else:
        print(f"\n  ✗ XLP Track NOT in heartbeat — tick loop not running for it.")
        print("    da59253 (critical bypass) should have spawned it. If still dead,")
        print("    something else is blocking spawn — needs deeper investigation.")

    # ---- Show what reblend WOULD do -----------------------------------
    print(f"\n  Reblend WOULD refresh own_avg on each ARMED_SELL sleeve:")
    for sc in sleeves_cfg:
        sid = sc.get("id")
        ss = sleeves_state.get(sid) or {}
        if ss.get("state") != "ARMED_SELL":
            continue
        prev = ss.get("own_avg_entry")
        print(f"    {sid}:  own_avg {prev} → {broker_avg}")

    if not apply:
        print(f"\n  DRY-RUN — re-run with --apply to force write reblend to Redis")
        print(f"  (only needed if XLP Track is dead or reblend logic isn't firing)")
        return

    # ---- Force reblend -------------------------------------------------
    print(f"\n  Writing new own_avg to Redis...")
    for sc in sleeves_cfg:
        sid = sc.get("id")
        ss = sleeves_state.get(sid) or {}
        if ss.get("state") != "ARMED_SELL":
            continue
        ss["own_avg_entry"] = broker_avg
        ss["sell_entry_avg"] = broker_avg
        ss["_own_avg_source"] = "diag_xlp_reblend_check_manual_add"
        sleeves_state[sid] = ss
        print(f"    ✓ {sid}: own_avg → ${broker_avg}")
    state["sleeves"] = sleeves_state
    store[tenant][PID]["state"] = state
    r.set("silver-swing:store", json.dumps(store))
    print(f"\n  ✓ Written to Redis. Dashboard will show new avg on next refresh.")


if __name__ == "__main__":
    main()
