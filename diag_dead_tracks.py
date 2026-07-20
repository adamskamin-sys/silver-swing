"""Enumerate all dead (silent) non-primary tracks — the ones behind the
HEALTH badge "N dead" count.

Adam 2026-07-20: audit-first per feedback_audit_before_fix.md. The
dashboard's "N dead" count comes from `track_silent_detected` events
in the trade log in the last 5 min (app.js:474). The heartbeat lives
in `store[TENANT].__track_heartbeat__` (live_runner.py:1585), not
under a separate Redis key.

Reads:
  1. `__track_heartbeat__` per live tenant — per-track snapshot
     (tick_count, last_step_ok_ts, spawn_ts, consecutive_step_failures,
     last_tick_reason)
  2. Trade log — `track_silent_detected`, `track_auto_respawn_attempted`,
     `non_primary_config_auto_seed_failed`, `non_primary_step_failure`,
     `non_primary_track_evicted` events from the last 30 min with error
     text for each
  3. `_non_primary_last_evict_ts` state from live tenant (if published)

Read-only. Usage:
    python3 diag_dead_tracks.py
"""
from __future__ import annotations
import json
import os
import time


def main() -> None:
    print("=" * 78)
    print("DEAD TRACKS INVESTIGATION")
    print("=" * 78)

    import redis
    url = (os.environ.get("REDIS_URL")
           or os.environ.get("REDIS_INTERNAL_URL"))
    if not url:
        print("\n✗ REDIS_URL not set")
        return
    r = redis.Redis.from_url(url, decode_responses=True)

    store_raw = r.get("silver-swing:store")
    if not store_raw:
        print("\n✗ store not found in Redis")
        return
    store = json.loads(store_raw)
    live_tenants = [k for k in store.keys() if k.endswith("-live")]
    if not live_tenants:
        print("\n✗ no live tenants found")
        return

    now = time.time()
    dead_from_log = set()  # products flagged silent in last 5min (dashboard "N dead" source)

    # ---- 1. Trade log — the dashboard's source of truth ---------------
    print("\n[1/4] track_silent_detected events (last 5 min — dashboard source)")
    print("-" * 78)
    raw_events = r.lrange("silver-swing:trade_log", 0, 10000) or []
    events = []
    for line in raw_events:
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    events.reverse()
    cutoff_5min = now - 300
    silent_hits = [e for e in events
                   if float(e.get("ts") or 0) > cutoff_5min
                   and e.get("event_type") == "track_silent_detected"]
    for e in silent_hits:
        sym = e.get("symbol") or e.get("product_id") or "?"
        dead_from_log.add(sym)
    print(f"  {len(silent_hits)} silent-detected events, {len(dead_from_log)} unique products:")
    for p in sorted(dead_from_log):
        print(f"    💀 {p}")

    # ---- 2. Heartbeat per-tenant --------------------------------------
    print("\n[2/4] Per-tenant heartbeat (__track_heartbeat__)")
    print("-" * 78)
    for lt in live_tenants:
        hb = (store.get(lt) or {}).get("__track_heartbeat__") or {}
        cfg_block = hb.get("config") if isinstance(hb, dict) and "config" in hb else hb
        if not isinstance(cfg_block, dict):
            cfg_block = hb
        tracks = (cfg_block or {}).get("tracks") or {}
        snap_ts = float((cfg_block or {}).get("snap_ts") or 0)
        age = int(now - snap_ts) if snap_ts else -1
        print(f"\n  tenant: {lt}   heartbeat age: {age}s   tracks: {len(tracks)}")
        for pid, t in sorted(tracks.items()):
            step_ok = float(t.get("last_step_ok_ts") or 0)
            tick_ct = int(t.get("tick_count") or 0)
            spawn_ts = float(t.get("spawn_ts") or 0)
            fails = int(t.get("consecutive_step_failures") or 0)
            reason = t.get("last_tick_reason") or ""
            age_step = int(now - step_ok) if step_ok else -1
            age_spawn = int(now - spawn_ts) if spawn_ts else -1
            marker = "💀" if age_step > 300 or age_step < 0 else "✓"
            print(f"    {marker} {pid:35s} ticks={tick_ct:>5} step_age={age_step:>4}s "
                  f"spawn_age={age_spawn:>5}s fails={fails:>2}")
            if reason and marker == "💀":
                print(f"       last_reason: {reason[:120]}")

    # ---- 3. Track-related failure events (last 30 min) ----------------
    print("\n[3/4] Track failure events (last 30 min)")
    print("-" * 78)
    cutoff_30 = now - 1800
    failure_types = {
        "track_silent_detected", "track_auto_respawn_attempted",
        "non_primary_config_auto_seed_failed",
        "non_primary_step_failure", "non_primary_track_evicted",
        "non_primary_spawn_refused_no_config",
        "track_health_discovery_failed_per_symbol",
    }
    hits = [e for e in events
            if float(e.get("ts") or 0) > cutoff_30
            and e.get("event_type") in failure_types]
    if not hits:
        print("  (no track failure events)")
    else:
        # Group by (product, event_type)
        counts = {}
        for e in hits:
            pid = e.get("symbol") or e.get("product_id") or "?"
            key = (pid, e.get("event_type"))
            counts[key] = counts.get(key, 0) + 1
        print(f"  {len(hits)} events over last 30 min:")
        for (pid, et), n in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"    {pid:35s} × {n:>3}  {et}")
        # Latest 8 with detail
        print(f"\n  Latest 8 with reason/error:")
        for e in hits[-8:]:
            age = int(now - float(e.get("ts") or 0))
            sev = e.get("severity") or ""
            mark = "🚨" if sev == "critical" else "⚠" if sev == "warn" else " "
            pid = e.get("symbol") or e.get("product_id") or ""
            print(f"    {mark} [{age:>4}s ago] {e.get('event_type'):45s} {pid}")
            for k in ("reason", "error", "error_type", "error_message"):
                v = e.get(k)
                if v:
                    print(f"       {k}: {str(v)[:180]}")

    # ---- 4. Recommendation --------------------------------------------
    print("\n[4/4] What the data says")
    print("-" * 78)
    if not dead_from_log:
        print("  ✓ No silent tracks detected in last 5 min — the badge might be stale.")
        print("    (Live_runner emits track_silent_detected on each health check.")
        print("     If none in last 5 min, the tracks may have recovered.)")
    else:
        print(f"  {len(dead_from_log)} products currently silent:")
        for p in sorted(dead_from_log):
            print(f"    - {p}")
        print("\n  Correlate with [3] above — the same products should appear")
        print("  in failure events with an error/reason string. That's the")
        print("  root cause to fix.")


if __name__ == "__main__":
    main()
