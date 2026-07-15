"""Is Track auto-recovery actually firing? Quick check.

Adam 2026-07-15: after commit d3d8893 deployed, we expect
track_silent_detected + track_auto_respawn_attempted events every
60s until dead Tracks come back to life. This diag confirms whether
those events are appearing OR whether the recovery loop isn't running.

Usage:
    python3 diag_track_recovery_check.py           # last 10min
    python3 diag_track_recovery_check.py 30        # last 30min
"""
from __future__ import annotations
import os
import sys
import time
from collections import Counter, defaultdict


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "?"


def main() -> None:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    print("=" * 100)
    print(f"TRACK RECOVERY CHECK — last {minutes:.0f}min")
    print("=" * 100)

    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    except Exception as e:
        print(f"\n✗ trade log load failed: {e}")
        return

    cutoff = time.time() - minutes * 60
    silent_detected = []
    respawn_attempted = []
    respawn_succeeded = []
    respawn_failed = []
    for e in log.events():
        if not isinstance(e, dict):
            continue
        ts = float(e.get("ts") or 0)
        if ts < cutoff:
            continue
        et = str(e.get("event_type") or "")
        if et == "track_silent_detected":
            silent_detected.append(e)
        elif et == "track_auto_respawn_attempted":
            respawn_attempted.append(e)
            if e.get("success"):
                respawn_succeeded.append(e)
            else:
                respawn_failed.append(e)

    print(f"\n[1] EVENT COUNTS:")
    print(f"    track_silent_detected:        {len(silent_detected)}")
    print(f"    track_auto_respawn_attempted: {len(respawn_attempted)}")
    print(f"      · succeeded:                {len(respawn_succeeded)}")
    print(f"      · failed:                   {len(respawn_failed)}")

    if not silent_detected and not respawn_attempted:
        print(f"\n⚠ AUTO-RECOVERY NOT FIRING")
        print(f"  No track_silent_detected events in {minutes:.0f}min.")
        print(f"  Possible reasons:")
        print(f"    (a) Deploy hasn't finished — wait 30-60s and retry")
        print(f"    (b) TICK_NON_PRIMARY=0 in env (auto-recovery is inside that guard)")
        print(f"    (c) The health-check interval hasn't elapsed since deploy")
        print(f"    (d) Bug in _maybe_recover_dead_tracks — investigate")
        return

    if silent_detected:
        print(f"\n[2] SILENT DETECTIONS by product:")
        by_sym = Counter(e.get("symbol") for e in silent_detected)
        for sym, n in by_sym.most_common():
            print(f"    {sym:22s} detected {n}× in window")

    if respawn_attempted:
        print(f"\n[3] RESPAWN ATTEMPTS (up to 15 most recent):")
        for e in respawn_attempted[-15:]:
            ok = "✓" if e.get("success") else "✗"
            print(f"    {_fmt_ts(e.get('ts'))}  {ok}  {e.get('symbol'):22s} "
                  f"success={e.get('success')}")

    if respawn_failed:
        print(f"\n[4] FAILURE PATTERNS (persistent unable-to-spawn):")
        fail_counts = Counter(e.get("symbol") for e in respawn_failed)
        for sym, n in fail_counts.most_common():
            if n >= 2:
                print(f"    {sym:22s} × {n} failures — root cause needs manual")
                print(f"    {' ':22s}   investigation (config issue, delisted, auth)")
                print(f"    {' ':22s}   Try: python3 diag_track_lifecycle.py {sym}")

    print(f"\n[BOTTOM LINE]")
    if len(respawn_succeeded) > 0:
        print(f"  ✓ Auto-recovery working — {len(respawn_succeeded)} Tracks respawned.")
        print(f"  Run diag_track_health_fleet.py to see current status.")
    elif len(silent_detected) > 0 and len(respawn_attempted) == 0:
        print(f"  ⚠ Silent detections logged but no respawn attempts — Tracks")
        print(f"  probably still in eviction cooldown. Wait ~15min OR restart Render.")
    print("=" * 100)


if __name__ == "__main__":
    main()
