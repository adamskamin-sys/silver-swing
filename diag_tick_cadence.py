"""Actual per-Track tick cadence — decoupled from sleeve-event count.

Adam 2026-07-19: fleet-health diag showed "LAST TICK 50s ago" for
several products, but LAST TICK there = last sleeve-scoped event,
which only fires when the sleeve state changes. Not the same as
raw tick rate. Question was: are we actually ticking at the designed
50ms cadence, or is the loop stalled?

This diag reads the `__track_heartbeat__` snapshot that live_runner
writes to Redis every 5s. Each Track's `tick_count` increments on
each successful (non-halted, non-raising) `trader.step()` — that IS
a real heartbeat, not an event count.

Reports for every non-primary Track:
  - Lifetime avg tick rate (total ticks / seconds since spawn)
  - Instant tick rate (delta since prev 5s snap)
  - Age of last successful step
  - Ratio vs designed LOOP_INTERVAL_SECS (1/0.05 = 20 tps designed)

Verdict:
  - instant_rate ≥ 80% of designed         → healthy
  - instant_rate 20-80%                    → degraded (something slow)
  - instant_rate < 20%                     → severely bottlenecked
  - instant_rate = 0 but has past ticks    → stalled RIGHT NOW

Read-only. Usage: python3 diag_tick_cadence.py
"""
from __future__ import annotations
import os
import time


TENANT = "adam-live"


def main() -> None:
    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))

    hb = store.get_config(TENANT, "__track_heartbeat__") or {}
    tracks = hb.get("tracks") or {}
    snap_ts = float(hb.get("snap_ts") or 0)
    designed_interval = float(hb.get("loop_interval_secs") or 0.05)
    designed_tps = 1.0 / designed_interval if designed_interval > 0 else 0.0

    now = time.time()
    print("=" * 118)
    print(f"TICK-CADENCE HEARTBEAT — tenant={TENANT}  "
          f"designed={designed_tps:.1f} ticks/sec (interval={designed_interval*1000:.0f}ms)")
    print("=" * 118)

    if not tracks:
        print(f"\n✗ No __track_heartbeat__ data in Redis.")
        print(f"  Either live_runner hasn't redeployed with the heartbeat writer,")
        print(f"  or no non-primary Tracks are alive right now. Try:")
        print(f"    python3 diag_track_health_fleet.py")
        return

    hb_age = int(now - snap_ts) if snap_ts > 0 else -1
    print(f"\nSnapshot age: {hb_age}s (heartbeat writes every 5s)")
    if hb_age > 60:
        print(f"⚠ Heartbeat is > 60s stale — live_runner may itself be stalled")

    print(f"\n{'PRODUCT':22s} {'LIFETIME':>10s} {'INSTANT':>10s} "
          f"{'% OF DESIGN':>10s} {'LAST STEP':>10s} {'ATTEMPT':>10s} "
          f"{'REASON':>20s} {'FAILS':>5s} {'STATUS':>16s}")
    print("-" * 130)

    healthy = 0
    degraded = 0
    stalled = 0
    for pid in sorted(tracks.keys()):
        t = tracks[pid] or {}
        tick_count = int(t.get("tick_count") or 0)
        spawn_ts = float(t.get("spawn_ts") or 0)
        last_ok = float(t.get("last_step_ok_ts") or 0)
        prev_count = int(t.get("prev_tick_count") or 0)
        prev_snap = float(t.get("prev_snap_ts") or 0)
        this_snap = float(t.get("snap_ts") or 0)
        fails = int(t.get("consecutive_step_failures") or 0)

        # Lifetime rate
        life_span = max(0.001, now - spawn_ts) if spawn_ts > 0 else 0
        life_rate = (tick_count / life_span) if life_span > 0 else 0.0

        # Instant rate = delta since prev heartbeat snap
        if prev_snap > 0 and this_snap > prev_snap:
            inst_span = this_snap - prev_snap
            inst_delta = tick_count - prev_count
            inst_rate = inst_delta / inst_span if inst_span > 0 else 0.0
        else:
            inst_rate = life_rate  # first snap — no prev to delta

        pct_of_design = (inst_rate / designed_tps * 100.0) if designed_tps > 0 else 0

        last_step_age = int(now - last_ok) if last_ok > 0 else -1
        last_step_str = f"{last_step_age}s ago" if last_step_age >= 0 else "never"
        last_attempt_ts = float(t.get("last_tick_attempt_ts") or 0)
        last_attempt_age = int(now - last_attempt_ts) if last_attempt_ts > 0 else -1
        last_attempt_str = f"{last_attempt_age}s ago" if last_attempt_age >= 0 else "never"
        last_reason = str(t.get("last_tick_reason") or "—")

        if inst_rate == 0 and tick_count > 0:
            status = "💀 STALLED NOW"
            stalled += 1
        elif pct_of_design >= 80:
            status = "✓ HEALTHY"
            healthy += 1
        elif pct_of_design >= 20:
            status = "⚠ DEGRADED"
            degraded += 1
        elif pct_of_design > 0:
            status = "🔻 BOTTLENECKED"
            stalled += 1
        else:
            status = "💀 NEVER TICKED"
            stalled += 1

        print(f"{pid:22s} {life_rate:>7.2f}/s {inst_rate:>7.2f}/s "
              f"{pct_of_design:>8.1f}% {last_step_str:>10s} {last_attempt_str:>10s} "
              f"{last_reason[:20]:>20s} {fails:>5d} {status:>16s}")

    total = len(tracks)
    print(f"\n[SUMMARY] {total} Tracks · designed {designed_tps:.1f} tps each")
    print(f"  ✓ HEALTHY:      {healthy:>3d}  ({100.0*healthy/total if total else 0:.0f}%)")
    print(f"  ⚠ DEGRADED:     {degraded:>3d}  ({100.0*degraded/total if total else 0:.0f}%)")
    print(f"  💀 STALLED:     {stalled:>3d}  ({100.0*stalled/total if total else 0:.0f}%)")

    if stalled + degraded > 0:
        print(f"\nDIAGNOSIS")
        if stalled > 0 and degraded == 0:
            print(f"  Cliff pattern: some Tracks fully stalled while others healthy.")
            print(f"  Investigate WS-feed per silent product (feed dead), or a")
            print(f"  specific step() failure that's not being caught.")
        elif degraded > 0 and stalled == 0:
            print(f"  Everyone-slower pattern: whole loop is throttled equally.")
            print(f"  Suspects: per-tick REST call in the loop, main.refresh_")
            print(f"  portfolio_snapshot() slow, or LOOP_INTERVAL_SECS raised.")
        else:
            print(f"  Mixed: some stalled + others degraded. Likely both issues.")
    else:
        print(f"\n✓ All Tracks ticking at ≥ 80% of designed cadence.")

    print("=" * 118)


if __name__ == "__main__":
    main()
