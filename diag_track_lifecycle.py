"""Trace a product's Track lifecycle — spawn attempts, failures, evictions.

Adam 2026-07-15: HYF sleeve is armed but SwingTrader.step has never
fired (0 sleeve-scoped events in 24h). Track is dead. Reasons could be:

  1. Never spawned successfully — `track_spawn_failed` events in the
     log with phase='constructor' / 'start' / 'feed_timeout'
  2. Spawned then evicted — `track_evicted` (stdout) + eviction
     cooldown blocking re-spawn
  3. Persistent spawn failure — repeated attempts, repeated failures

Read-only. Usage:
    python3 diag_track_lifecycle.py PRODUCT_ID              # last 24h
    python3 diag_track_lifecycle.py PRODUCT_ID 720          # last 12h
"""
from __future__ import annotations
import os
import sys
import time
from collections import Counter


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "?"


def main() -> None:
    if len(sys.argv) < 2:
        print("USAGE: python3 diag_track_lifecycle.py PRODUCT_ID [minutes]")
        return
    product_id = sys.argv[1]
    minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 1440.0

    print("=" * 100)
    print(f"TRACK LIFECYCLE — {product_id}  last {minutes:.0f}min")
    print("=" * 100)

    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    except Exception as e:
        print(f"\n✗ trade log load failed: {e}")
        return

    cutoff = time.time() - minutes * 60
    spawn_events = []       # track_spawn_failed
    sleeve_events = []      # anything with sleeve_id on this product
    recon_events = []       # reconciliation_*
    all_events_for_product = []
    for e in log.events():
        if not isinstance(e, dict):
            continue
        ts = float(e.get("ts") or 0)
        if ts < cutoff:
            continue
        sym = str(e.get("symbol") or "")
        if sym != product_id:
            continue
        all_events_for_product.append(e)
        et = str(e.get("event_type") or "")
        if et == "track_spawn_failed":
            spawn_events.append(e)
        if e.get("sleeve_id"):
            sleeve_events.append(e)
        if et.startswith("reconciliation_"):
            recon_events.append(e)

    print(f"\n[1] EVENT COUNTS in window:")
    print(f"    total product events:      {len(all_events_for_product)}")
    print(f"    sleeve-scoped events:      {len(sleeve_events)}  ← nonzero = Track is running")
    print(f"    track_spawn_failed events: {len(spawn_events)}  ← nonzero = Track failed to spawn")
    print(f"    reconciliation_* events:   {len(recon_events)}  ← runs at live_runner level")

    if len(sleeve_events) == 0 and len(spawn_events) == 0:
        print(f"\n⚠ Zero sleeve-scoped AND zero spawn_failed events.")
        print(f"  Interpretation: either the Track was already dead before")
        print(f"  the window (never re-attempted spawn), OR live_runner is")
        print(f"  not running at all. Verify Render service status.")
    elif len(sleeve_events) == 0 and len(spawn_events) > 0:
        print(f"\n⚠ Zero sleeve events but {len(spawn_events)} spawn_failed events.")
        print(f"  Track has been trying to spawn but failing. See [2] for details.")

    if spawn_events:
        print(f"\n[2] SPAWN FAILURES (up to 10 most recent):")
        phase_counts = Counter(e.get("phase") for e in spawn_events)
        error_counts = Counter(e.get("error") for e in spawn_events)
        print(f"    By phase: {dict(phase_counts)}")
        print(f"    Top errors:")
        for err, n in error_counts.most_common(5):
            print(f"      × {n}  {str(err)[:80]}")
        print(f"\n    Recent failures:")
        for e in spawn_events[-10:]:
            print(f"    · {_fmt_ts(e.get('ts'))}  phase={e.get('phase')}  "
                  f"error={str(e.get('error') or '?')[:80]}")
        print(f"\n    → Fix: address the specific error above (bad config, "
              f"delisted product, auth issue, feed timeout). Once fixed, "
              f"restart Render or wait for the 15-min eviction cooldown.")

    if recon_events and not sleeve_events:
        print(f"\n[3] RECONCILIATION MONITOR is running — it emits events at the")
        print(f"    live_runner level regardless of Track status. Kinds seen:")
        kinds = Counter(str(e.get('event_type', '?')).replace('reconciliation_', '') for e in recon_events)
        for kind, n in kinds.most_common():
            print(f"      × {n}  {kind}")

    print(f"\n[4] RECOMMENDATION:")
    if len(sleeve_events) > 0:
        print(f"    Track IS ticking (nonzero sleeve events). Silent block is")
        print(f"    somewhere in the sleeve code path — dig into the specific")
        print(f"    event types shown in diag_sleeve_ready.")
    elif len(spawn_events) > 0:
        print(f"    Track has spawn failures. Fix the root cause (see [2] above),")
        print(f"    then restart Render to force fresh spawn attempts.")
    else:
        print(f"    Zero sleeve events + zero spawn attempts. Track is dead")
        print(f"    or was evicted before the window began. Simplest recovery:")
        print(f"    restart the Render service — clears in-memory eviction")
        print(f"    cooldown and forces fresh spawn attempts.")
    print("=" * 100)


if __name__ == "__main__":
    main()
