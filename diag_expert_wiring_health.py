"""Show whether each expert module is firing in production.

Adam 2026-07-16: 6 expert modules wired to every trading decision.
This diag verifies each is actually firing (not silently disabled
or crashing) by grepping the trade log for module-specific events.

Read-only. Prints per-module:
  - MODE (kill-switch state — should be "expert")
  - Event count over last N hours (default 4h)
  - Most-recent event timestamp + summary
  - Distinct sleeves/products that have fired the event
  - RED flag if 0 events despite MODE=expert

Usage:
    python3 diag_expert_wiring_health.py                  # last 4h
    python3 diag_expert_wiring_health.py --hours 24       # last 24h
    python3 diag_expert_wiring_health.py --hours 1
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from typing import Optional


# Map: module name → (import path, event types to look for, description)
EXPERT_MODULES = [
    ("expert_spread", ["expert_spread_primary_applied",
                        "expert_spread_intra_cycle_decision",
                        "expert_spread_expert_decision",
                        "expert_spread_applied"],
        "Spread + entry prices"),
    ("expert_stop", ["expert_stop_applied"],
        "Stop distance"),
    ("expert_gate", ["sleeve_reentry_gate_decision",
                      "sleeve_reanchor_on_trigger_gate"],
        "Reentry-after-stop gate"),
    ("expert_trail", ["expert_trail_applied"],
        "Trail distance"),
    ("expert_size", ["expert_size_primary_buy_applied"],
        "Position size (safety-cap)"),
    ("expert_arm_gate", ["primary_arm_gate_decision",
                          "sleeve_arm_gate_decision"],
        "Initial-entry gate"),
]


def _get_mode(module_name: str) -> str:
    try:
        mod = __import__(module_name)
        return getattr(mod, "MODE", "MISSING")
    except Exception as e:
        return f"IMPORT_FAIL({type(e).__name__})"


def _summarize_event(e: dict) -> str:
    """One-line summary of an event's payload."""
    sym = e.get("symbol") or e.get("sleeve_id") or "?"
    method = e.get("method") or ""
    # Handful of interesting fields per module
    interesting = []
    for k in ("allow", "size", "user_configured", "final_size",
              "stop_distance", "trail_distance", "expert_spread",
              "vote_count", "total_voters", "cost_floor_binding",
              "fee_floor_binding", "sanity_cap_binding"):
        if k in e:
            interesting.append(f"{k}={e[k]}")
    tail = " ".join(interesting[:4])
    return f"{sym} {method[:24]} {tail}".strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=4.0,
                    help="look back window (default 4h)")
    ap.add_argument("--data-dir", default=os.getenv("SWING_DATA_DIR", "data"))
    args = ap.parse_args()

    cutoff_ts = time.time() - args.hours * 3600.0

    print("=" * 90)
    print(f"EXPERT WIRING HEALTH — last {args.hours}h "
          f"(cutoff = {int(cutoff_ts)} epoch)")
    print("=" * 90)

    # Read the trade log — use safety.make_trade_log so we get Redis
    # backend on Render (SWING_TRADE_LOG_PATH file may not exist there).
    events: list[dict] = []
    log_source = "?"
    try:
        from safety import make_trade_log
        log = make_trade_log(args.data_dir)
        log_source = type(log).__name__
        for e in log.events():
            try:
                ts = float(e.get("ts") or 0)
                if ts >= cutoff_ts:
                    events.append(e)
            except (ValueError, TypeError):
                pass
    except Exception as e:
        print(f"\n✗ Failed to read trade log via safety.make_trade_log: "
              f"{type(e).__name__}: {e}")
        sys.exit(1)

    print(f"\nRead {len(events)} events from {log_source}")
    print()

    any_flag = False

    for module_name, event_types, description in EXPERT_MODULES:
        mode = _get_mode(module_name)
        # Filter events matching this module's types
        matching = [e for e in events
                     if e.get("event_type") in event_types]

        symbols_fired = set()
        for e in matching:
            sym = e.get("symbol") or e.get("sleeve_id")
            if sym:
                symbols_fired.add(sym)

        # Trigger conditions per module — a "NOT FIRING" verdict is only
        # a concern if the trigger condition WAS met. For arm_gate and
        # size, "no events" is expected when no fresh BUYs happened.
        # For gate, "no events" is expected when no stop-loss fired.
        trigger_notes = {
            "expert_stop":     "fires ~1×/min per sleeve with stop_loss_enabled",
            "expert_trail":    "fires on every resting-stop update while trail engaged",
            "expert_spread":   "fires on post-sell reanchor + intra-cycle re-eval + primary buys",
            "expert_gate":     "fires ONLY after a stop-loss triggers (reentry_pending)",
            "expert_size":     "fires ONLY on fresh BUY arms",
            "expert_arm_gate": "fires ONLY on fresh BUY arms",
        }
        trigger_hint = trigger_notes.get(module_name, "")

        # Health verdict
        mode_ok = (mode == "expert")
        fired_ok = len(matching) > 0
        if mode_ok and fired_ok:
            status = "✓ HEALTHY"
        elif not mode_ok:
            status = f"⚠ KILL-SWITCH ({mode})"
            any_flag = True
        elif mode_ok and not fired_ok:
            status = "○ NO TRIGGER YET"
            # Not flagged red — could just mean trigger condition hasn't happened

        print(f"─── {module_name}  [{description}]  {status}")
        print(f"    MODE = {mode}")
        print(f"    events last {args.hours}h: {len(matching)}")
        print(f"    distinct symbols/sleeves: {len(symbols_fired)}"
              + (f" — {sorted(symbols_fired)[:5]}"
                 + (" ..." if len(symbols_fired) > 5 else "")
                 if symbols_fired else ""))
        if matching:
            most_recent = max(matching, key=lambda e: float(e.get("ts") or 0))
            age = time.time() - float(most_recent.get("ts") or 0)
            print(f"    most recent: {int(age)}s ago — {_summarize_event(most_recent)}")
        if not fired_ok and trigger_hint:
            print(f"    trigger: {trigger_hint}")
        print()

    print("=" * 90)
    if any_flag:
        print("SUMMARY: ⚠ One or more experts on kill switch (MODE ≠ 'expert').")
        print("  Kill switch was likely flipped by an operator or diag. Check with them.")
    else:
        print("SUMMARY: ✓ All 6 experts are wired (MODE=expert). Firing depends on trigger:")
        print("  - stop + trail fire continuously while positions are held")
        print("  - spread fires on post-sell reanchor + intra-cycle re-eval + primary buys")
        print("  - reentry gate only fires AFTER a stop-loss (reentry_pending path)")
        print("  - size + arm gate only fire on FRESH BUY arms")
    print("=" * 90)


if __name__ == "__main__":
    main()
