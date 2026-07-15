"""Have the experts been adjusting entry/reentry prices?

Adam 2026-07-15: audit reentry_reeval activity across every sleeve.
Shows what the experts have actually DONE (walks) vs decided to hold
vs skipped for various reasons. If output is empty or all "held", the
experts haven't been given anything to adjust — either sleeves are
ARMED_SELL (holding, not waiting to buy) or no drift crossed the
0.25% min-drift gate.

Read-only. Usage:
    python3 diag_expert_activity.py                  # last 60 min, all sleeves
    python3 diag_expert_activity.py 240              # last 4 hours
    python3 diag_expert_activity.py 60 HYP-20DEC30-CDE  # one product
"""
from __future__ import annotations
import os
import sys
import time
from collections import defaultdict


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "?"


def main() -> None:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    product_filter = sys.argv[2] if len(sys.argv) > 2 else None

    print("=" * 78)
    print(f"EXPERT ACTIVITY — last {minutes:.0f}min"
          + (f"  product={product_filter}" if product_filter else ""))
    print("=" * 78)

    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    except Exception as e:
        print(f"trade log load failed: {e}")
        return

    cutoff = time.time() - minutes * 60
    events_by_type: dict[str, list] = defaultdict(list)
    per_sleeve = defaultdict(lambda: {"walks": 0, "held": 0, "skipped": 0,
                                       "expired": 0, "reanchored": 0,
                                       "symbol": "", "moves": []})

    for e in log.events():
        if not isinstance(e, dict):
            continue
        ts = float(e.get("ts", 0) or 0)
        if ts < cutoff:
            continue
        et = str(e.get("event_type", ""))
        if not ("reeval" in et or "reanchor" in et):
            continue
        sym = str(e.get("symbol") or "")
        if product_filter and sym != product_filter:
            continue
        events_by_type[et].append(e)
        sid = str(e.get("sleeve_id") or "")
        if not sid:
            continue
        per_sleeve[sid]["symbol"] = sym
        if et == "reentry_reeval_replaced":
            per_sleeve[sid]["walks"] += 1
            move = {
                "ts": ts,
                "old": e.get("old_buy_px"),
                "new": e.get("new_buy_px"),
                "why": e.get("why") or e.get("action"),
            }
            per_sleeve[sid]["moves"].append(move)
        elif et in ("reentry_reeval_decision", "reentry_reeval_shadow_action"):
            action = str(e.get("action") or e.get("would_action") or "").lower()
            if action == "hold":
                per_sleeve[sid]["held"] += 1
            elif action in ("reanchor", "walk", "replace"):
                per_sleeve[sid]["held"] += 0  # counted as walk in _replaced
            elif action == "expire":
                per_sleeve[sid]["expired"] += 1
        elif et == "reentry_reeval_replace_skipped_below_drift":
            per_sleeve[sid]["skipped"] += 1
        elif "reanchor" in et and "clamp" not in et:
            per_sleeve[sid]["reanchored"] += 1

    print(f"\n[1] TOTAL EVENTS BY TYPE (last {minutes:.0f}min):")
    if not events_by_type:
        print(f"    · NO reeval or reanchor events at all.")
        print(f"    → Either no sleeves are in ARMED_BUY (all holding),")
        print(f"      reeval mode is OFF, or the sleeves that ARE waiting")
        print(f"      don't have live orders for reeval to act on.")
    else:
        for et in sorted(events_by_type.keys()):
            print(f"    {et:50s}  {len(events_by_type[et])}")

    print(f"\n[2] PER-SLEEVE EXPERT ACTIVITY:")
    if not per_sleeve:
        print(f"    · no sleeve-scoped reeval events found")
    else:
        for sid in sorted(per_sleeve.keys(),
                          key=lambda s: -per_sleeve[s]["walks"]):
            s = per_sleeve[sid]
            print(f"\n    · {s['symbol']:20s}  sleeve={sid}")
            print(f"      walks={s['walks']:>3d}  held={s['held']:>3d}  "
                  f"skipped_drift={s['skipped']:>3d}  expired={s['expired']:>3d}  "
                  f"reanchored={s['reanchored']:>3d}")
            if s["moves"]:
                print(f"      recent walks (up to 8):")
                for m in s["moves"][-8:]:
                    old = m["old"]
                    new = m["new"]
                    delta = ""
                    try:
                        d = float(new) - float(old)
                        delta = f" ({d:+.4f})"
                    except (TypeError, ValueError):
                        pass
                    why = (m["why"] or "")[:80]
                    print(f"        {_fmt_ts(m['ts'])}  ${old} → ${new}{delta}  {why}")

    print(f"\n[3] SUMMARY:")
    total_walks = sum(s["walks"] for s in per_sleeve.values())
    total_held = sum(s["held"] for s in per_sleeve.values())
    total_skipped = sum(s["skipped"] for s in per_sleeve.values())
    total_reanchored = sum(s["reanchored"] for s in per_sleeve.values())
    print(f"    Experts made {total_walks} live-order walks (cancel + replace at new price)")
    print(f"    Experts said HOLD {total_held} times (no change needed)")
    print(f"    Experts wanted to walk but drift too small: {total_skipped} times")
    print(f"    Sleeve-level auto-reanchors (buy_px/sell_px rewrites): {total_reanchored}")
    print("=" * 78)


if __name__ == "__main__":
    main()
