"""List all severity=critical trade-log events in a recent window.

Adam 2026-07-20: after this session's 21 orphan/ghost fixes, all
clear-without-confirm sites now emit severity=critical with an
explicit `reason` when a Coinbase cancel truly fails (rate limit,
network, transient error). This diag surfaces them so we can watch
production and see which specific paths actually fire — turning
"what if cancel fails?" into hard data.

Also lists:
  - track_force_respawn_signal_honored (evict/spawn results from
    operator force-respawn diag)
  - primary_reconcile_order_status_unknown / sleeve_order_status_unknown
    (Coinbase returned UNKNOWN for an order — kept tracking)
  - resting_stop_placement_refused_multi_sleeve (multi-sleeve mutex
    caught a ghost sleeve trying to add a redundant stop)
  - reconciliation orphan_order / missing_resting_stop / position_mismatch
  - halt / evict / spawn_failed events

Read-only. Usage:
    python3 diag_critical_events.py                 # last 60 min
    python3 diag_critical_events.py 240             # last 4h
    python3 diag_critical_events.py 60 --by-kind    # group by event_type
"""
from __future__ import annotations
import os
import sys
import time
from collections import Counter, defaultdict


def main() -> None:
    minutes = 60.0
    by_kind = "--by-kind" in sys.argv
    for arg in sys.argv[1:]:
        if arg.startswith("--"):
            continue
        try:
            minutes = float(arg)
        except ValueError:
            pass

    print("=" * 118)
    print(f"CRITICAL / GHOST-RELATED EVENTS — last {minutes:.0f} min")
    print("=" * 118)

    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = log.tail(20000) if hasattr(log, "tail") else []

    now = time.time()
    cutoff = now - (minutes * 60)

    # Kinds of interest — orphan-guard fires + reconciliation reports
    NEW_ORPHAN_GUARD_KINDS = {
        # Class A (UNKNOWN status kept tracking)
        "primary_reconcile_order_status_unknown",
        "sleeve_order_status_unknown",
        "reconcile_order_status_unknown",
        # Class B (cancel-fail keeps tracking)
        "sleeve_halt_cancel_failed",
        "primary_halt_cancel_failed",
        "primary_resume_cancel_failed",
        "sleeve_resume_cancel_failed",
        "resting_stop_ratchet_cancel_failed",
        "resting_stop_ratchet_place_failed",
        "resting_stop_config_gone_cancel_failed",
        "resting_stop_cancel_tp_beat_stop_failed",
        "resting_stop_cancel_failed",  # no_position path
        "resting_stop_cancel_on_tp_fill_failed",
        "trail_breach_cancel_failed",
        "trail_breach_limit_sell_failed",
        "sleeve_removed_live_order_cancel_failed",
        "sleeve_removed_resting_stop_cancel_failed",
        "reentry_reeval_expire_cancel_failed",
        "sleeve_orphan_reconcile_cancel_failed",
        "cancel_failed",  # dashboard cancel-intent
        # Multi-sleeve mutex
        "resting_stop_placement_refused_multi_sleeve",
        # Reconciliation reports (from monitor)
        "orphan_order", "missing_order", "missing_resting_stop",
        "position_mismatch", "unprotected_position",
    }
    LIFECYCLE_KINDS = {
        "track_force_respawn_signal_honored",
        "track_zombie_detected",
        "track_silent_detected",
        "track_auto_respawn_attempted",
        "sleeve_halted", "primary_paused", "sleeve_paused",
        "sleeve_removed", "sleeve_ghost_armed",
    }

    critical_events = []
    guard_events = []
    lifecycle_events = []
    for e in events:
        if not isinstance(e, dict):
            continue
        ts = float(e.get("ts") or 0)
        if ts < cutoff:
            continue
        sev = str(e.get("severity") or "").lower()
        kind = str(e.get("event_type") or "")
        if sev == "critical":
            critical_events.append(e)
        if kind in NEW_ORPHAN_GUARD_KINDS:
            guard_events.append(e)
        if kind in LIFECYCLE_KINDS:
            lifecycle_events.append(e)

    print(f"\n[SEVERITY=CRITICAL] {len(critical_events)} events")
    print("-" * 118)
    if critical_events:
        for e in critical_events[-40:]:
            _print_event(e, now)
    else:
        print("  ✓ No critical events. Every cancel/place completed cleanly.")

    print(f"\n[ORPHAN-GUARD FIRES] {len(guard_events)} events")
    print("-" * 118)
    if by_kind:
        by = Counter(str(e.get("event_type") or "?") for e in guard_events)
        for k, c in by.most_common():
            print(f"  {c:>5d}  {k}")
        # Symbols with the most guards
        by_sym = Counter(str(e.get("symbol") or "?") for e in guard_events)
        if by_sym:
            print(f"\n  By product:")
            for s, c in by_sym.most_common(10):
                print(f"    {c:>5d}  {s}")
    else:
        if guard_events:
            for e in guard_events[-40:]:
                _print_event(e, now)
        else:
            print("  ✓ No orphan-guard fires. No cancel/place failed; no ghost placements refused.")

    print(f"\n[LIFECYCLE] {len(lifecycle_events)} events (halt/evict/respawn/ghost-arm)")
    print("-" * 118)
    if lifecycle_events:
        by = Counter(str(e.get("event_type") or "?") for e in lifecycle_events)
        for k, c in by.most_common():
            print(f"  {c:>5d}  {k}")
        # Show most recent 10
        print(f"\n  Recent 10:")
        for e in lifecycle_events[-10:]:
            _print_event(e, now, compact=True)
    else:
        print("  ✓ No halts, evicts, respawns, or ghost-arms.")

    print("=" * 118)


def _print_event(e: dict, now: float, compact: bool = False) -> None:
    ts_age = int(now - float(e.get("ts", 0)))
    et = str(e.get("event_type") or "?")
    sym = str(e.get("symbol") or "")
    sev = str(e.get("severity") or "info")[:4]
    sid = str(e.get("sleeve_id") or "")[:14]
    sev_marker = "💀" if sev == "crit" else ("⚠" if sev == "warn" else " ")
    line = f"  {sev_marker} [{ts_age:>5d}s] {sev:4s} {et:44s} {sym:22s} sleeve={sid:14s}"
    if not compact:
        # Extra key fields
        reason = str(e.get("reason") or "")[:80]
        err = str(e.get("error") or "")[:60]
        oid = str(e.get("order_id") or e.get("oid") or e.get("old_oid") or "")[:12]
        if oid:
            line += f" oid={oid}"
        if err:
            line += f" err={err}"
        if reason:
            line += f"\n         reason: {reason}"
    print(line)


if __name__ == "__main__":
    main()
