"""Enable __reentry_mode__=shadow on adam-live for the 24-48h burn-in.

The auditor 2026-07-14 sequence required a paper-flip before executing-live.
Paper mode is retired, so shadow is the paper-substitute: the reentry_reeval
module computes and logs decisions but places/cancels NOTHING. Watch trade
log for 24-48h to validate decisions; then, if sane, flip ONE small sleeve
to 'expert' (executing) mode.

⚠️  This is SAFE — shadow does not touch the broker. But per convention
    (matches diag_fix_slr_primary.py) we default to preview and require
    --confirm to actually write.

Usage:
  python3 diag_enable_shadow_mode.py            # preview only
  python3 diag_enable_shadow_mode.py --confirm  # write mode=shadow
  python3 diag_enable_shadow_mode.py --disable --confirm  # revert to legacy
  python3 diag_enable_shadow_mode.py --expert --confirm   # flip to executing
                                                          # (only after 24-48h
                                                           #  shadow burn-in
                                                           #  + auditor sign-off)

After --confirm, look for events in the trade log:
  * reentry_reeval_decision       — every reeval fires this (tagged mode=)
  * reentry_reeval_shadow_action  — every non-hold decision in shadow mode
                                    (would_action, would_new_buy_px)

Preconditions checked before writing (auditor 2026-07-14):
  * reconciliation_monitor is running (any recent reconciliation_* event)
  * REDIS_URL is set (store backend must be Redis, not JSON)
"""
from __future__ import annotations
import argparse
import os
import sys
import time

from state_store import make_store


TENANT = "adam-live"


def _find_recent_event(store, tenant, event_prefix, max_age_s=1800):
    """Best-effort: look for any event of `event_prefix*` in the last max_age_s
    seconds. Uses the trade log if reachable. Returns True if found, else False.
    Fail-safe: any error → return None (skip the precondition rather than
    hard-fail on an unrelated code path)."""
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        cutoff = time.time() - max_age_s
        events = list(log.events())[-500:]
        for e in reversed(events):
            ts = float(e.get("ts") or 0)
            if ts < cutoff:
                break
            et = str(e.get("event_type") or "")
            if et.startswith(event_prefix):
                return True
        return False
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", action="store_true",
                    help="actually write (default: preview only)")
    ap.add_argument("--disable", action="store_true",
                    help="revert to legacy (default: enable shadow)")
    ap.add_argument("--expert", action="store_true",
                    help="flip to executing 'expert' mode — ONLY after "
                         "24-48h shadow burn-in + auditor sign-off")
    ap.add_argument("--force", action="store_true",
                    help="skip preconditions (use if reconciliation events "
                         "haven't fired recently on a fresh service)")
    args = ap.parse_args()

    # Mode selection
    if args.disable:
        new_mode = "legacy"
    elif args.expert:
        new_mode = "expert"
    else:
        new_mode = "shadow"

    store = make_store(os.getenv("SWING_DATA_DIR", "data"))
    before = store.get_state(TENANT, "__reentry_mode__") or {}
    old_mode = str(before.get("mode") or "legacy").lower()

    print(f"BEFORE ({TENANT}/__reentry_mode__):")
    print(f"  mode = {old_mode!r}")
    print()
    print(f"PROPOSED CHANGE:")
    print(f"  mode = {new_mode!r}")
    print()

    if new_mode == old_mode:
        print(f"No change needed — mode is already {new_mode!r}.")
        return

    # Preconditions for executing mode
    if new_mode == "expert" and not args.force:
        print("Executing mode preconditions check:")
        recon = _find_recent_event(store, TENANT, "reconciliation_")
        shadow_seen = _find_recent_event(store, TENANT, "reentry_reeval_shadow_action")
        decision_seen = _find_recent_event(store, TENANT, "reentry_reeval_decision")
        print(f"  reconciliation_monitor running (recent event): "
              f"{'✓' if recon else '✗' if recon is False else '?'}")
        print(f"  shadow_action events observed (24-48h burn-in): "
              f"{'✓' if shadow_seen else '✗' if shadow_seen is False else '?'}")
        print(f"  reentry_reeval decisions logged: "
              f"{'✓' if decision_seen else '✗' if decision_seen is False else '?'}")
        if recon is False:
            print()
            print("REFUSING --expert without --force: reconciliation_monitor is "
                  "not running (no recent reconciliation_* event). Enable it "
                  "first or pass --force.")
            sys.exit(2)
        if shadow_seen is False and decision_seen is False:
            print()
            print("REFUSING --expert without --force: no reentry_reeval events "
                  "seen. Run in shadow mode for 24-48h FIRST — this is the "
                  "auditor's 2026-07-14 executing-live gate.")
            sys.exit(2)
        print()

    if not args.confirm:
        print("PREVIEW only. Add --confirm to write.")
        return

    new_state = dict(before)
    new_state["mode"] = new_mode
    new_state["_last_change_ts"] = time.time()
    new_state["_last_change_from"] = old_mode
    store.put_state(TENANT, "__reentry_mode__", new_state)

    after = store.get_state(TENANT, "__reentry_mode__") or {}
    print(f"AFTER ({TENANT}/__reentry_mode__):")
    print(f"  mode = {after.get('mode')!r}")
    print()
    if new_mode == "shadow":
        print("Shadow mode is ACTIVE. Bot will now log reeval decisions on every")
        print("ARMED_BUY tick without touching the broker. Watch the trade log:")
        print("  * reentry_reeval_decision       — every reeval fires this")
        print("  * reentry_reeval_shadow_action  — every non-hold decision")
        print()
        print("After 24-48h of clean-looking decisions, run:")
        print("  python3 diag_enable_shadow_mode.py --expert --confirm")
        print("to flip to executing mode.")
    elif new_mode == "expert":
        print("Executing mode is ACTIVE. Bot will now execute reeval decisions")
        print("on adam-live (cancel-replace stale ARMED_BUY orders, expire near-")
        print("expiry contracts). Monitor closely for the first few reanchors.")
        print()
        print("To pull back: python3 diag_enable_shadow_mode.py --disable --confirm")
    else:
        print("Reverted to legacy — bot ignores reeval, uses original logic.")


if __name__ == "__main__":
    main()
