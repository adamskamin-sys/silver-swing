"""Pinpoint WHICH code path is cancelling/replacing a sleeve's order.

Adam 2026-07-15: min-drift gate deployed but BIT still shows 8 places /
8 cancels per hour. Need to know which code path is firing.

For a target sleeve (default: BIT's Model B — Defensive plus), dump every
event that touches an order in the last N minutes, sorted by timestamp.
This shows the EXACT sequence: what triggered each cancel, what triggered
each place, whether the min-drift gate is firing but skipping, etc.

Reveals which of these is churning:
  - reeval cancel-replace (reentry_reeval_replaced)
  - reeval skip below drift (reentry_reeval_replace_skipped_below_drift)
  - auto-refresh (sleeve_auto_refresh)
  - sleeve reanchor (sleeve_reanchored)
  - ghost force-arm (sleeve_ghost_force_arm)
  - normal sleeve place/cancel (sleeve_order_placed / sleeve_order_cancelled)
  - anything else that touches live_order_id

Read-only. Usage:
    python3 diag_churn_source.py                              # last 60 min, BIT
    python3 diag_churn_source.py 30                           # last 30 min, BIT
    python3 diag_churn_source.py 60 smrh594lh                 # explicit sleeve
    python3 diag_churn_source.py 60 BIT-31JUL26-CDE           # by symbol
"""
from __future__ import annotations
import os
import sys
import time
from collections import Counter


DEFAULT_SLEEVE = "smrh594lh"  # BIT's Model B — Defensive plus (from prior diag)


def _load_events(minutes: float) -> list[dict]:
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        cutoff = time.time() - (minutes * 60)
        return [e for e in log.events() if isinstance(e, dict)
                and float(e.get("ts", 0) or 0) >= cutoff]
    except Exception as e:
        print(f"  WARN: make_trade_log failed: {e}")
        return []


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "?"


# Event types worth surfacing for churn-source triage
CHURN_EVENTS = {
    # Reeval family
    "reentry_reeval_decision",
    "reentry_reeval_shadow_action",
    "reentry_reeval_replaced",
    "reentry_reeval_replace_skipped_below_drift",  # NEW min-drift skip
    "reentry_reeval_expired",
    "reentry_reeval_cancel_failed",
    "reentry_reeval_place_failed",
    "reentry_reeval_lock_blocked",
    # Auto-refresh family
    "sleeve_auto_refresh",
    "sleeve_auto_refresh_error",
    # Reanchor
    "sleeve_reanchored",
    "sleeve_reanchor_place_failed",
    # Ghost resurrection
    "sleeve_ghost_force_arm",
    "sleeve_ghost_force_arm_failed",
    "sleeve_ghost_force_arm_skipped",
    # Order lifecycle
    "sleeve_order_placed",
    "sleeve_arm_placed",
    "sleeve_order_cancelled",
    "sleeve_order_filled",
    "sleeve_order_expired",
    "sleeve_order_cleared",
    "sleeve_arm_skipped_position_full",
    "sleeve_arm_skipped",
    # Resting stop (new)
    "resting_stop_placed",
    "resting_stop_ratcheted",
    "resting_stop_cleared",
    "resting_stop_place_failed",
}


def main() -> None:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    target = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_SLEEVE
    # Detect symbol vs sleeve_id — symbols contain a '-'
    match_symbol = "-" in target
    print("=" * 78)
    kind = "symbol" if match_symbol else "sleeve_id"
    print(f"CHURN SOURCE — last {minutes:.0f}min, {kind}={target}")
    print("=" * 78)

    events = _load_events(minutes)
    if not events:
        print("\nNO EVENTS.")
        return

    # Filter to the target sleeve/symbol AND to churn-relevant event types
    relevant: list[dict] = []
    for e in events:
        et = e.get("event_type")
        if et not in CHURN_EVENTS:
            continue
        matches = (
            (match_symbol and e.get("symbol") == target) or
            (not match_symbol and e.get("sleeve_id") == target)
        )
        if not matches:
            continue
        relevant.append(e)
    if not relevant:
        print(f"\nNo churn-relevant events for {kind}={target} in the window.")
        print("(Try widening the window or checking a different sleeve/symbol.)")
        return

    relevant.sort(key=lambda e: float(e.get("ts") or 0))

    # ---- 1) Event type rollup ---------------------------------------------
    counts = Counter(e["event_type"] for e in relevant)
    print(f"\n1) EVENT COUNTS ({len(relevant)} total):")
    for et, c in counts.most_common():
        print(f"     {et:50s} {c:>4d}")

    # ---- 2) Chronological sequence ----------------------------------------
    print(f"\n2) EVENT SEQUENCE ({len(relevant)} events, most recent last):")
    for e in relevant:
        et = e["event_type"]
        ts = _fmt_ts(e.get("ts"))
        details = []
        for k in ("action", "would_action", "new_buy_px", "would_new_buy_px",
                  "old_buy_px", "current_buy_px", "drift_pct", "threshold_pct",
                  "price", "limit_price", "side", "reason", "why", "error",
                  "sleeve_qty", "target_px", "from_px", "to_px", "stage",
                  "old_order_id", "new_order_id", "oid", "order_id"):
            if k in e and e[k] not in (None, "", 0):
                v = e[k]
                if isinstance(v, float):
                    v = f"{v:.4f}"
                details.append(f"{k}={v}")
        detail_s = " ".join(details) if details else ""
        print(f"   {ts}  {et:45s}  {detail_s[:120]}")

    # ---- 3) Interpretation -------------------------------------------------
    print("\n3) INTERPRETATION:")
    reeval_replaces = counts.get("reentry_reeval_replaced", 0)
    reeval_skipped = counts.get("reentry_reeval_replace_skipped_below_drift", 0)
    auto_refresh = counts.get("sleeve_auto_refresh", 0)
    reanchors = counts.get("sleeve_reanchored", 0)
    ghost_arms = counts.get("sleeve_ghost_force_arm", 0)
    places = (counts.get("sleeve_order_placed", 0)
              + counts.get("sleeve_arm_placed", 0)
              + reeval_replaces)
    cancels = counts.get("sleeve_order_cancelled", 0)

    print(f"   reeval replaced (executed cancel-replace): {reeval_replaces}")
    print(f"   reeval SKIPPED (min-drift gate blocked):   {reeval_skipped}")
    print(f"   auto-refresh moved buy_px:                 {auto_refresh}")
    print(f"   sleeve_reanchored total:                   {reanchors}")
    print(f"   ghost force-arms:                          {ghost_arms}")
    print(f"   total order places:                        {places}")
    print(f"   total order cancels:                       {cancels}")

    if reeval_replaces > 0 and reeval_skipped == 0:
        print("\n   → Min-drift gate is NOT firing. Reeval is genuinely re-anchoring")
        print("     to prices ≥ 0.25% different. That's a real regime signal, not")
        print("     churn — the market IS moving fast enough to justify replaces.")
    elif reeval_skipped > 0 and reeval_replaces > 0:
        print(f"\n   → Min-drift gate is WORKING but insufficient. {reeval_skipped} "
              f"replaces skipped, {reeval_replaces} still passed through.")
    elif reeval_replaces == 0 and cancels > 0:
        print("\n   → Cancels are NOT coming from reeval. Look at the sequence")
        print("     above for the non-reeval cancel source (probably auto-refresh")
        print("     via sleeve_reanchored, or ghost-arm re-placement).")


if __name__ == "__main__":
    main()
