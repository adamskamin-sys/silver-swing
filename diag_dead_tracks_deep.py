"""Why is each dead track dead? Deep audit for the 13-dead-tracks incident.

Adam 2026-07-20: Health chip shows '13 dead'. My earlier critical-bypass
fix (da59253) SHOULD force held-position products to always spawn. If 13
are still dead, one of:
  (a) Not flagged critical — pf.crypto / pf.derivatives read fails
  (b) Spawn keeps succeeding but they zombie again in <threshold window
  (c) Zombie-streak >3 → stuck in 15-min cooldown
  (d) Products have no held position AND no armed sleeve — dead is correct

For every product the loop thinks it should track, print:
  - alive? (in _non_primary_tracks & last_step_ok_age < 600s)
  - critical? (held position >= $10 or held futures)
  - last_step_ok_age (secs since track produced work)
  - zombie_streak count
  - last_evict_ts age (secs since last force-evict)
  - cooldown_remaining
  - recent 5 track_* events for this pid from trade log

Read-only. Run:  python3 diag_dead_tracks_deep.py
"""
from __future__ import annotations
import os
import time
import json
from collections import Counter


def main() -> None:
    print("=" * 88)
    print("DEEP DEAD-TRACK AUDIT")
    print("=" * 88)

    # Redis for heartbeat + config
    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if not url:
        print("\n✗ REDIS_URL not set")
        return
    r = redis.Redis.from_url(url, decode_responses=True)
    store = json.loads(r.get("silver-swing:store") or "{}")

    tenant = next((t for t in store if t.endswith("-live")), None)
    if not tenant:
        print(f"\n✗ no live tenant in store")
        return
    print(f"\n  tenant: {tenant}")

    # Heartbeat: __track_heartbeat__ blob written by main tick loop
    hb_raw = store[tenant].get("__track_heartbeat__") or {}
    hb = hb_raw.get("config") or hb_raw
    tracks = hb.get("tracks") or {}
    print(f"  __track_heartbeat__ has {len(tracks)} track entries")

    # Portfolio snapshot (used to classify critical)
    pf_raw = store[tenant].get("__portfolio__") or {}
    pf = pf_raw.get("config") or pf_raw
    derivatives = pf.get("derivatives") or []
    crypto = pf.get("crypto") or []
    print(f"  __portfolio__: {len(derivatives)} derivatives, {len(crypto)} crypto")

    # Enumerate every product with either a sleeve or a held position
    products_with_sleeves = set()
    for pid, blk in store[tenant].items():
        if pid.startswith("__"):
            continue
        cfg = (blk or {}).get("config") or {}
        if cfg.get("sleeves"):
            products_with_sleeves.add(pid)

    products_held = set()
    for d in derivatives:
        pid = d.get("product_id") or d.get("symbol")
        if pid:
            products_held.add(pid)
    for c in crypto:
        pid = c.get("product_id") or c.get("symbol") or c.get("currency")
        if pid:
            products_held.add(pid)

    expected = products_with_sleeves | products_held
    print(f"\n  Expected tracked products: {len(expected)}")
    print(f"    with sleeves:  {len(products_with_sleeves)}")
    print(f"    with positions: {len(products_held)}")

    # Load recent track_* events per pid
    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    all_events = log.tail(15000)
    now = time.time()

    def _recent_track_events(pid: str, limit: int = 5) -> list:
        out = []
        for e in reversed(all_events):
            if (e.get("symbol") or "") != pid:
                continue
            et = e.get("event_type") or ""
            if "track_" in et or "zombie" in et or "spawn" in et or "evict" in et:
                out.append(e)
                if len(out) >= limit:
                    break
        return out

    alive_count = 0
    dead_count = 0
    critical_dead = []

    print("\n" + "-" * 88)
    print(f"  {'PID':<24} {'alive?':<8} {'critical?':<10} {'step_age':<10} {'evict_age':<10}")
    print("-" * 88)

    for pid in sorted(expected):
        t = tracks.get(pid) or {}
        last_step_ok_ts = float(t.get("last_step_ok_ts") or 0)
        alive = last_step_ok_ts > 0 and (now - last_step_ok_ts) < 600
        step_age = int(now - last_step_ok_ts) if last_step_ok_ts > 0 else -1

        # Critical determination — mirrors live_runner logic
        critical = pid in products_held
        # Also mark critical if crypto notional >= $10 (dust threshold)
        for c in crypto:
            if (c.get("product_id") or c.get("symbol") or c.get("currency")) == pid:
                notional = float(c.get("notional") or c.get("balance_usd") or 0)
                if notional >= 10:
                    critical = True
                break

        evict_ts = float(t.get("last_evict_ts") or 0)
        evict_age = int(now - evict_ts) if evict_ts > 0 else -1

        if alive:
            alive_count += 1
        else:
            dead_count += 1
            if critical:
                critical_dead.append(pid)

        flags = ""
        if not alive and critical:
            flags = " 🚨 CRITICAL DEAD"
        elif not alive:
            flags = " dead"
        elif alive:
            flags = " ✓"

        print(f"  {pid:<24} {'yes' if alive else 'NO':<8} "
              f"{'YES' if critical else '-':<10} "
              f"{step_age if step_age>=0 else '?':<10} "
              f"{evict_age if evict_age>=0 else '?':<10}{flags}")

    print("-" * 88)
    print(f"\n  SUMMARY: {alive_count} alive, {dead_count} dead")
    if critical_dead:
        print(f"  🚨 {len(critical_dead)} DEAD + CRITICAL (held positions unprotected):")
        for pid in critical_dead:
            print(f"       {pid}")

    # For each critical-dead, dump recent track events + likely cause
    if critical_dead:
        print("\n" + "=" * 88)
        print("  CRITICAL-DEAD DEEP DIVE")
        print("=" * 88)
        for pid in critical_dead:
            print(f"\n  --- {pid} ---")
            events = _recent_track_events(pid, 8)
            if not events:
                print(f"    (no recent track_/spawn/evict events)")
            for e in events:
                ts = int(now - float(e.get('ts') or now))
                et = e.get('event_type') or ''
                reason = (e.get('reason') or '')[:100]
                print(f"    {ts:5d}s ago  {et:44s}  {reason}")

    # Aggregate — most common causes across all dead
    print("\n" + "=" * 88)
    print("  AGGREGATE CAUSES (top event types across dead tracks)")
    print("=" * 88)
    dead_pids = [pid for pid in expected
                 if not (tracks.get(pid, {}).get("last_step_ok_ts")
                         and (now - float(tracks[pid]["last_step_ok_ts"])) < 600)]
    dead_events = [e for e in all_events
                   if (e.get("symbol") or "") in dead_pids
                   and any(kw in (e.get("event_type") or "")
                           for kw in ("track_", "spawn", "zombie", "evict"))]
    print(f"  {len(dead_events)} track-related events across {len(dead_pids)} dead pids")
    counts = Counter(e.get("event_type") for e in dead_events)
    for et, n in counts.most_common(15):
        print(f"    {n:5d}  {et}")


if __name__ == "__main__":
    main()
