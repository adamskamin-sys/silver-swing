"""Show the latest expert recommendation per sleeve (2026-07-15).

Adam's question: "can we see if the experts have readjusted anything?"

The reentry_reeval module runs on every tick per pending ARMED_BUY sleeve.
In SHADOW mode (current) it logs `reentry_reeval_shadow_action` with the
would-be decision but does NOT touch the broker. In EXPERT mode it logs
`reentry_reeval_decision` AND acts.

This script walks the trade log and dumps the LATEST decision per sleeve —
one line per sleeve — plus a rollup of how many decisions the experts have
made per action type over the sample window.

Also surfaces `sleeve_auto_refresh` events (the 20-min-cadence buy_px
re-derivation via arm_level.pullback_buy_px) so you can see BOTH decision
pathways in one view.

Read-only. Usage:
    python3 diag_show_expert_recommendations.py           # last 5000 events
    python3 diag_show_expert_recommendations.py 20000     # deeper look
"""
from __future__ import annotations
import json
import os
import sys
import time
from collections import defaultdict


def _load_events(limit: int) -> list[dict]:
    """Load the last N trade log events via safety.make_trade_log (same path
    the bot writes to). RedisTradeLog if REDIS_URL set, JsonlTradeLog local."""
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        events = list(log.events())
        return events[-limit:] if len(events) > limit else events
    except Exception as e:
        print(f"  WARN: make_trade_log failed: {e}")
    return []


def _fmt_ts(ts: float | int | None) -> str:
    if not ts:
        return "?"
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "?"


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    print("=" * 78)
    print(f"EXPERT RECOMMENDATIONS — last {limit} trade-log events")
    print("=" * 78)

    events = _load_events(limit)
    if not events:
        print("\nNO EVENTS. Check SWING_DATA_DIR / REDIS_URL.")
        return

    # Latest decision per sleeve (from both event families)
    latest_reeval: dict[str, dict] = {}
    latest_refresh: dict[str, dict] = {}
    action_counts: dict[str, int] = defaultdict(int)
    reeval_mode_seen: set[str] = set()

    # Walk oldest -> newest so we end with the most recent decision per sleeve
    for e in events:
        if not isinstance(e, dict):
            continue
        et = e.get("event_type")
        if et in ("reentry_reeval_shadow_action", "reentry_reeval_decision"):
            sid = e.get("sleeve_id")
            if not sid:
                continue
            action = e.get("would_action") or e.get("action") or "?"
            action_counts[action] += 1
            mode = e.get("mode") or ("shadow" if et.endswith("shadow_action") else "?")
            reeval_mode_seen.add(mode)
            latest_reeval[sid] = {
                "action": action,
                "old_buy_px": e.get("old_buy_px"),
                "new_buy_px": e.get("would_new_buy_px") or e.get("new_buy_px"),
                "why": e.get("why"),
                "mode": mode,
                "ts": e.get("ts"),
                "sleeve_name": e.get("sleeve_name") or sid,
                "symbol": e.get("symbol"),
            }
        elif et == "sleeve_auto_refresh":
            sid = e.get("sleeve_id")
            if not sid:
                continue
            latest_refresh[sid] = {
                "old_buy_px": e.get("old_buy_px"),
                "new_buy_px": e.get("new_buy_px"),
                "drift_pct": e.get("drift_pct"),
                "armed_hours": e.get("armed_hours"),
                "current_market": e.get("current_market"),
                "ts": e.get("ts"),
                "sleeve_name": e.get("sleeve_name") or sid,
                "symbol": e.get("symbol"),
            }

    # ---- 1) Rollup ---------------------------------------------------------
    print(f"\n1) REEVAL DECISION COUNTS ({sum(action_counts.values())} events, modes seen: {sorted(reeval_mode_seen) or ['none']}):")
    if not action_counts:
        print("   NONE — reentry_reeval never fired. Likely reasons:")
        print("     - __reentry_mode__ scope not set for the live tenant (or set to 'legacy')")
        print("     - insufficient price history (< 30 bars per sleeve)")
        print("     - no sleeves currently in ARMED_BUY with a live order")
    else:
        for action, count in sorted(action_counts.items(), key=lambda x: -x[1]):
            print(f"     {action:20s} {count:>5d}")

    # ---- 2) Latest reeval decision per sleeve ------------------------------
    print(f"\n2) LATEST REEVAL DECISION PER SLEEVE ({len(latest_reeval)} sleeves):")
    if not latest_reeval:
        print("   NONE.")
    else:
        for sid, d in sorted(latest_reeval.items(),
                              key=lambda x: -(x[1]["ts"] or 0)):
            arrow = ""
            if d["new_buy_px"] and d["old_buy_px"]:
                try:
                    delta = float(d["new_buy_px"]) - float(d["old_buy_px"])
                    arrow = f" ({'+' if delta >= 0 else ''}{delta:.4f})"
                except Exception:
                    pass
            new_px = f"${d['new_buy_px']}" if d["new_buy_px"] else "—"
            print(f"   {_fmt_ts(d['ts'])} [{d['mode']:6s}] "
                  f"{(d['symbol'] or '?'):20s} "
                  f"{d['sleeve_name'][:20]:20s} "
                  f"→ {d['action']:10s} new_buy={new_px}{arrow}")
            if d.get("why"):
                print(f"       why: {d['why'][:100]}")

    # ---- 3) Latest auto-refresh per sleeve --------------------------------
    print(f"\n3) LATEST AUTO-REFRESH (20-min-cadence buy_px re-derive, {len(latest_refresh)} sleeves):")
    if not latest_refresh:
        print("   NONE — sleeve_auto_refresh never fired. Sleeves may not be stale enough.")
    else:
        for sid, d in sorted(latest_refresh.items(),
                              key=lambda x: -(x[1]["ts"] or 0)):
            old = d.get("old_buy_px")
            new = d.get("new_buy_px")
            drift = d.get("drift_pct")
            mkt = d.get("current_market")
            arm_h = d.get("armed_hours")
            print(f"   {_fmt_ts(d['ts'])} {(d['symbol'] or '?'):20s} "
                  f"{d['sleeve_name'][:20]:20s} "
                  f"buy ${old} → ${new}  drift={drift}%  mkt=${mkt}  armed={arm_h}h")

    # ---- 4) Interpretation -------------------------------------------------
    print("\n4) INTERPRETATION:")
    if "expert" in reeval_mode_seen:
        print("   ✓ Expert mode is ACTIVE — decisions are being executed.")
    elif "shadow" in reeval_mode_seen:
        print("   ⚠ Shadow mode — experts are OBSERVING but not acting.")
        print("     Flip to expert with: __reentry_mode__ scope → 'expert' on the live tenant")
    else:
        print("   ✗ Reeval not running. Check tenant __reentry_mode__ scope and history depth.")

    if latest_refresh:
        print(f"   ✓ Auto-refresh IS moving buy_px per experts on {len(latest_refresh)} sleeves.")
    else:
        print("   ⚠ Auto-refresh has NEVER moved buy_px — either every sleeve is armed <20 min,")
        print("     or the min-drift threshold is skipping all suggestions.")


if __name__ == "__main__":
    main()
