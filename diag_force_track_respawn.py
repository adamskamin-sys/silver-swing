"""Write a force-respawn signal so live_runner evicts + respawns Tracks
without waiting for the 15-min cooldown or a Render restart.

Adam 2026-07-19: XLP + ZEC went silent (LAST TICK > 60s + 1h respectively)
while the auto-recovery path was cooldown-blocked or otherwise stuck.
Rather than force a Render restart, this diag writes a signal to
CONFIG `__force_respawn__` which live_runner's `_maybe_recover_dead_tracks`
reads at the very top of each health check. On seeing product IDs in
the signal, live_runner:
  1. Force-evicts each Track from _non_primary_tracks
  2. Clears _non_primary_last_evict_ts (cooldown)
  3. Resets the zombie streak (bypasses the 3-strike slowdown)
  4. Records track_force_respawn_signal_honored (severity=warn)
  5. Clears the signal so it isn't re-processed
Then the next spawn path fires immediately.

Read-only by default. Usage:

    python3 diag_force_track_respawn.py                              # list current signal
    python3 diag_force_track_respawn.py XLP-20DEC30-CDE ZEC-20DEC30-CDE            # dry-run
    python3 diag_force_track_respawn.py XLP-20DEC30-CDE ZEC-20DEC30-CDE --apply    # write signal
"""
from __future__ import annotations
import os
import sys
import time


TENANT = "adam-live"


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv

    import state_store
    store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))

    print("=" * 78)
    print(f"FORCE TRACK RESPAWN {'(APPLY)' if apply else '(dry-run)'} — "
          f"tenant={TENANT}")
    print("=" * 78)

    # Show current signal state
    current = store.get_config(TENANT, "__force_respawn__") or {}
    current_pids = current.get("product_ids") or []
    cleared_ts = current.get("cleared_ts")
    print(f"\nCurrent __force_respawn__ signal:")
    if current_pids:
        print(f"  pending: {current_pids}")
    else:
        print(f"  pending: [] (empty)")
    if cleared_ts:
        print(f"  last cleared: {int(time.time() - float(cleared_ts))}s ago")

    if not args:
        print(f"\nNo product_ids provided. Nothing to do.")
        print(f"\nUSAGE: python3 diag_force_track_respawn.py <PID1> [PID2 ...] [--apply]")
        return

    # Validate: each pid should exist in tenant symbols (typo protection)
    known = set()
    try:
        known = set(store.list_symbols(TENANT))
    except Exception as e:
        print(f"\n(note: list_symbols failed: {e} — skipping typo check)")
    unknown = [p for p in args if known and p not in known]
    if unknown:
        print(f"\n✗ Unknown product_id(s) not in {TENANT}: {unknown}")
        print(f"  Known: {sorted(known)}")
        return

    print(f"\nRequested respawn for:")
    for p in args:
        print(f"  - {p}")

    print(f"\nAfter apply, live_runner will (on next tick, ~5-15s):")
    print(f"  1. Read __force_respawn__ from CONFIG")
    print(f"  2. For each pid: _evict_track + clear cooldown + clear zombie streak")
    print(f"  3. Record track_force_respawn_signal_honored (severity=warn)")
    print(f"  4. Clear the signal (so it doesn't re-process)")
    print(f"  5. Next spawn attempt fires immediately (no 15min wait)")

    if not apply:
        print(f"\n(dry-run — pass --apply to write the signal)")
        return

    signal = {
        "product_ids": args,
        "requested_ts": time.time(),
        "requested_by": "diag_force_track_respawn.py",
    }
    try:
        store.put_config(TENANT, "__force_respawn__", signal)
    except Exception as e:
        print(f"\n✗ put_config failed: {e}")
        return

    # Audit log
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        log.record(
            "force_track_respawn_signal_written",
            tenant=TENANT, product_ids=args,
            reason=("operator requested force-respawn bypassing 15-min "
                    "eviction cooldown + zombie-streak slowdown"),
            severity="warn",
        )
    except Exception as e:
        print(f"\n(note: trade log record failed: {e})")

    print(f"\n✓ Signal written. live_runner picks it up on the next tick.")
    print(f"  Confirm respawn: python3 diag_track_health_fleet.py")
    print(f"  Expect LAST TICK to reset to < 15s within one health-check cycle.")


if __name__ == "__main__":
    main()
