"""Enumerate all dead (evicted / silent) non-primary tracks with their
last-failure reasons.

Adam 2026-07-20: HEALTH went 5 dead → 20 dead within 30min after recent
commits. Rollback 97dede6 removed the aggressive spot iteration; this
diag surfaces what's STILL failing so we can either purge the tracks
or fix a specific root cause.

Reads:
  1. silver-swing:track_health (per-track cadence heartbeat)
  2. silver-swing:trade_log (recent track_spawn_failed / track_evicted /
     non_primary_config_auto_seed_failed events)
  3. store keys (any tenant symbol) — cross-reference which products have
     sleeves configured but no active track

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

    # ---- 1. Track health snapshot ---------------------------------------
    print("\n[1/4] Live track health snapshot")
    print("-" * 78)
    try:
        raw = r.get("silver-swing:track_health")
        health = json.loads(raw) if raw else {}
    except Exception as e:
        print(f"  ✗ read failed: {e}")
        health = {}

    tracks = health.get("tracks") or {}
    dead_ct = health.get("dead_count", 0)
    print(f"  Tracks reported: {len(tracks)}, dead_count={dead_ct}")
    now = time.time()
    dead_products = []
    for pid, t in sorted(tracks.items()):
        step_ok = float(t.get("last_step_ok_ts") or 0)
        tick_seen = float(t.get("last_tick_seen_ts") or 0)
        spawn_ts = float(t.get("spawn_ts") or 0)
        tick_count = int(t.get("tick_count") or 0)
        age_step = int(now - step_ok) if step_ok else -1
        age_tick = int(now - tick_seen) if tick_seen else -1
        is_dead = (age_step < 0 or age_step > 300) and (age_tick < 0 or age_tick > 300)
        marker = "💀" if is_dead else "✓ "
        print(f"  {marker} {pid:35s} tick_ct={tick_count:>4} "
              f"step_age={age_step:>5}s tick_age={age_tick:>5}s "
              f"spawn={int(now - spawn_ts) if spawn_ts else '?'}s ago")
        if is_dead:
            dead_products.append(pid)

    # ---- 2. Recent track-related failures -------------------------------
    print("\n[2/4] Recent track-related failure events (last 30 min)")
    print("-" * 78)
    interesting = {
        "track_spawn_failed", "track_evicted", "non_primary_track_evicted",
        "non_primary_config_auto_seed_failed",
        "non_primary_spawn_refused_no_config",
        "non_primary_step_failure", "track_health_discovery_failed_per_symbol",
        "track_auto_respawn_attempted", "track_silent_detected",
        "sleeve_orphan_reconcile_cancel_failed",
    }
    try:
        raw_events = r.lrange("silver-swing:trade_log", 0, 5000) or []
    except Exception as e:
        print(f"  ✗ trade log read failed: {e}")
        return
    events = []
    for line in raw_events:
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    events.reverse()
    cutoff = now - 1800  # last 30 min
    hits = [e for e in events
            if float(e.get("ts") or 0) > cutoff
            and e.get("event_type") in interesting]
    if not hits:
        print(f"  (no track failure events in last 30 min)")
    else:
        # Group by product + event type
        counts = {}
        for e in hits[-100:]:
            pid = e.get("symbol") or e.get("product_id") or "?"
            et = e.get("event_type")
            key = (pid, et)
            counts[key] = counts.get(key, 0) + 1
        print(f"  {len(hits)} failure events in last 30 min:")
        for (pid, et), n in sorted(counts.items(), key=lambda x: -x[1])[:30]:
            print(f"    {pid:35s} × {n:>3}  {et}")
        # Sample 3 recent errors with reason text
        print(f"\n  Sample of latest 5 with reason/error:")
        for e in hits[-5:]:
            ts = int(now - float(e.get("ts") or 0))
            print(f"    [{ts}s ago] {e.get('event_type')} {e.get('symbol') or e.get('product_id') or ''}")
            for k in ("reason", "error", "error_type", "error_message"):
                v = e.get(k)
                if v:
                    print(f"      {k}: {str(v)[:180]}")

    # ---- 3. Products with sleeves but dead tracks ------------------------
    print("\n[3/4] Products with sleeves configured but track is dead/silent")
    print("-" * 78)
    try:
        store_raw = r.get("silver-swing:store")
        store = json.loads(store_raw) if store_raw else {}
    except Exception as e:
        print(f"  ✗ store read failed: {e}")
        store = {}
    for tenant, tblock in store.items():
        if not tenant.endswith("-live"):
            continue
        for sym, block in (tblock or {}).items():
            if sym.startswith("__"):
                continue
            state = (block or {}).get("state") or {}
            sleeves = state.get("sleeves") or {}
            armed = [s for s in sleeves.values()
                     if str(s.get("state") or "") in ("ARMED_BUY", "ARMED_SELL")]
            if armed and sym in dead_products:
                print(f"  💀 {sym}  {len(armed)} armed sleeve(s) — WILL NOT TICK until track respawns")

    # ---- 4. Purge suggestion --------------------------------------------
    print("\n[4/4] Suggested purge (dead tracks with NO armed sleeves)")
    print("-" * 78)
    if not dead_products:
        print("  (no dead products)")
        return
    purgeable = []
    for pid in dead_products:
        has_armed = False
        for tenant, tblock in store.items():
            if not tenant.endswith("-live"):
                continue
            block = (tblock or {}).get(pid) or {}
            sleeves = (block.get("state") or {}).get("sleeves") or {}
            for s in sleeves.values():
                if str(s.get("state") or "") in ("ARMED_BUY", "ARMED_SELL"):
                    has_armed = True
                    break
            if has_armed:
                break
        if not has_armed:
            purgeable.append(pid)
    if not purgeable:
        print("  (no purgeable — every dead track has armed sleeves; investigate root cause)")
    else:
        print(f"  {len(purgeable)} dead tracks have NO armed sleeves — safe to purge from track dict:")
        for p in purgeable:
            print(f"    {p}")
        print(f"\n  (no auto-purge here — would need diag_purge_dead_tracks.py; report if needed)")


if __name__ == "__main__":
    main()
