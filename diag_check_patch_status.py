"""Why didn't the state_patch land?

Adam 2026-07-15: after queuing a state_patch, the dashboard should
reflect it within ~5-10s. If it doesn't, this script diagnoses:

  [1] Is the patch still sitting in the store? (bot hasn't consumed)
  [2] Did the bot log state_patch_applied? (consumed but maybe wrong sleeve)
  [3] Did state_patch_apply_failed fire? (consumer error)
  [4] Is the store even the same one the diag wrote to?
  [5] What's the CURRENT sleeve state (should be $14.55 if patch applied)

Read-only. Usage:
    python3 diag_check_patch_status.py PRODUCT_ID SLEEVE_ID

Example:
    python3 diag_check_patch_status.py HYP-20DEC30-CDE smrluux5w
"""
from __future__ import annotations
import os
import sys
import time


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "?"


def main() -> None:
    if len(sys.argv) < 3:
        print("USAGE: python3 diag_check_patch_status.py PRODUCT_ID SLEEVE_ID")
        return
    product_id = sys.argv[1]
    sleeve_id = sys.argv[2]
    tenant = "adam-live"

    print("=" * 78)
    print(f"STATE_PATCH DIAG — {tenant}/{product_id}/{sleeve_id}")
    print("=" * 78)

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
    store_kind = type(store).__name__
    print(f"\n[0] Store backend: {store_kind}")
    print(f"    (SWING_DATA_DIR={os.getenv('SWING_DATA_DIR', 'data')!r})")

    # [1] Is the patch still queued?
    print(f"\n[1] PENDING state_patch at {tenant}/{product_id}:")
    if not hasattr(store, "get_state_patch"):
        print(f"    ✗ store.get_state_patch does NOT exist on {store_kind}")
        print(f"    → Deploy hasn't landed yet, OR this store backend is out of date.")
        print(f"    → Try again in 60s. If still failing, check Render deploy status.")
        return
    patch = store.get_state_patch(tenant, product_id)
    if patch is None:
        print(f"    · no pending patch (already consumed or never written)")
    else:
        print(f"    ⚠ patch STILL QUEUED — bot has not consumed it yet.")
        print(f"      reason:   {patch.get('reason')}")
        print(f"      ts:       {_fmt_ts(patch.get('ts'))} ({int(time.time()) - int(patch.get('ts') or 0)}s ago)")
        print(f"      sleeves:  {list((patch.get('sleeves') or {}).keys())}")
        for sid, fields in (patch.get("sleeves") or {}).items():
            print(f"        · {sid}: {fields}")
        print(f"\n    → Either the bot isn't ticking this product,")
        print(f"      OR the tenant/symbol doesn't match what the bot uses,")
        print(f"      OR the bot code hasn't been redeployed with the consumer.")

    # [2] Recent state_patch_applied events?
    print(f"\n[2] Recent state_patch_applied events (last 30min):")
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    except Exception as e:
        print(f"    ✗ trade log load failed: {e}")
        return
    cutoff = time.time() - 1800
    applied = []
    failed = []
    for e in log.events():
        if not isinstance(e, dict):
            continue
        ts = float(e.get("ts", 0) or 0)
        if ts < cutoff:
            continue
        et = str(e.get("event_type", ""))
        if et == "state_patch_applied":
            applied.append(e)
        elif et == "state_patch_apply_failed":
            failed.append(e)
    if not applied:
        print(f"    · none found in last 30min")
    else:
        for e in applied[-10:]:
            print(f"    · {_fmt_ts(e.get('ts'))}  {e.get('symbol')}  reason={e.get('reason')}")
            print(f"      applied={e.get('applied')}")

    print(f"\n[3] Recent state_patch_apply_failed events (last 30min):")
    if not failed:
        print(f"    · none — that's good if applied events exist above")
    else:
        for e in failed[-5:]:
            print(f"    ✗ {_fmt_ts(e.get('ts'))}  {e.get('symbol')}  error={e.get('error')}")
            print(f"      reason={e.get('reason')}")

    # [4] Actual current sleeve state
    print(f"\n[4] CURRENT sleeve state (source of truth for dashboard):")
    state = store.get_state(tenant, product_id) or {}
    sleeves_state = state.get("sleeves") or {}
    ss = sleeves_state.get(sleeve_id)
    if ss is None:
        print(f"    ✗ sleeve {sleeve_id} not in state.sleeves for {product_id}")
        print(f"      existing: {list(sleeves_state.keys())}")
    else:
        print(f"    cycles:               {ss.get('cycles')}")
        print(f"    realized_pnl:         ${ss.get('realized_pnl')}")
        print(f"    own_avg_entry:        {ss.get('own_avg_entry')}")
        print(f"    state:                {ss.get('state')}")
        print(f"    credited_oids (last): {(ss.get('credited_oids') or [])[-5:]}")
        print(f"    recent_cycle_pnls:    {ss.get('recent_cycle_pnls')}")

    # [5] Recent ticks on this product — is the bot even ticking it?
    print(f"\n[5] Recent tick evidence for {product_id} (last 5min):")
    cutoff5 = time.time() - 300
    tick_evts = 0
    last_evt = None
    for e in log.events():
        if not isinstance(e, dict):
            continue
        if str(e.get("symbol") or "") != product_id:
            continue
        ts = float(e.get("ts", 0) or 0)
        if ts < cutoff5:
            continue
        tick_evts += 1
        last_evt = e
    print(f"    event count last 5min:  {tick_evts}")
    if last_evt:
        print(f"    most-recent event:      {_fmt_ts(last_evt.get('ts'))}  {last_evt.get('event_type')}")
    if tick_evts == 0:
        print(f"    ⚠ NO events for this symbol — bot may not be ticking it.")
        print(f"      Check live_runner logs for '{product_id}' spawn / eviction.")

    print("=" * 78)


if __name__ == "__main__":
    main()
