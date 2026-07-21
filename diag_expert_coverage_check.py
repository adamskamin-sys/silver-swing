"""Prove that every ARMED_BUY sleeve is getting expert-consulted continuously.

Adam 2026-07-21: "every contract sleeve waiting for a rebuy, have the
experts reevaluated new entries? These should be continually tracking
data and adjusting."

For each ARMED_BUY sleeve on adam-live:
  - Show current buy_px / sell_px / stop_loss_px
  - Show timestamp of last expert_reentry_decision event (age in seconds)
  - Show timestamp of last sleeve_auto_refresh (buy_px update) event
  - Show timestamp of last sleeve_reanchored event (config update)
  - Flag sleeves that haven't been consulted in >5 min as STALE
  - Flag sleeves that have never been consulted as NEVER

Read-only. Run: python3 diag_expert_coverage_check.py
"""
from __future__ import annotations
import os
import json
import time
from collections import defaultdict


STALE_SECS = 300  # 5 min without consultation = stale


def _fmt_age(secs: float) -> str:
    if secs < 0:
        return "future?"
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs / 60)}m {int(secs % 60)}s"
    return f"{int(secs / 3600)}h {int((secs % 3600) / 60)}m"


def main() -> None:
    print("=" * 96)
    print("EXPERT COVERAGE CHECK — are ARMED_BUY sleeves getting consulted?")
    print("=" * 96)

    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if not url:
        print("\nREDIS_URL not set — run on Render shell")
        return
    r = redis.Redis.from_url(url, decode_responses=True)
    store = json.loads(r.get("silver-swing:store") or "{}")

    tenant = "adam-live"
    tbody = store.get(tenant) or {}
    now = time.time()

    # Pull recent trade log for consultation history
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        events = log.tail(20000)  # ~last ~2h of activity
    except Exception as e:
        print(f"\n✗ trade log read failed: {e}")
        events = []

    # Index events by (product, sleeve_id, event_type)
    latest = defaultdict(lambda: 0.0)
    for e in events:
        pid = e.get("symbol") or ""
        sid = e.get("sleeve_id") or ""
        et = e.get("event_type") or ""
        ts = float(e.get("ts") or 0)
        if ts > latest[(pid, sid, et)]:
            latest[(pid, sid, et)] = ts

    # Walk sleeves
    products = sorted([p for p in tbody.keys()
                       if not p.startswith("__") and isinstance(tbody.get(p), dict)])
    total = 0
    fresh = 0
    stale = 0
    never = 0

    for pid in products:
        block = tbody[pid] or {}
        cfg = block.get("config") or {}
        state = block.get("state") or {}
        sleeves_cfg = cfg.get("sleeves") or []
        sleeves_state = state.get("sleeves") or {}
        for sc in sleeves_cfg:
            sid = sc.get("id") or "?"
            ss = sleeves_state.get(sid) or {}
            sst = ss.get("state") or ""
            if sst != "ARMED_BUY":
                continue
            total += 1

            buy_px = sc.get("buy_px")
            sell_px = sc.get("sell_px")
            stop_loss_px = sc.get("stop_loss_px")
            live_order = ss.get("live_order_id")

            last_expert = latest[(pid, sid, "expert_reentry_decision")]
            last_refresh = latest[(pid, sid, "sleeve_auto_refresh")]
            last_reanchor = latest[(pid, sid, "sleeve_reanchored")]
            last_reeval = latest[(pid, sid, "reentry_reeval_decision")]

            print(f"\n{'─' * 96}")
            print(f"{pid}  ·  {sid}")
            print(f"  live_order_id: {live_order or '(none)'}")
            print(f"  buy_px: {buy_px}   sell_px: {sell_px}   stop_loss_px: {stop_loss_px}")

            def _line(label, ts):
                if ts <= 0:
                    return f"    {label:<32} NEVER"
                age = now - ts
                tag = "✓" if age < STALE_SECS else "🚨 STALE" if age < 3600 else "🚨🚨 VERY STALE"
                return f"    {label:<32} {_fmt_age(age):>10} ago  {tag}"

            print(_line("expert_reentry_decision:", last_expert))
            print(_line("reentry_reeval_decision:", last_reeval))
            print(_line("sleeve_auto_refresh:", last_refresh))
            print(_line("sleeve_reanchored:", last_reanchor))

            # Classify overall coverage
            most_recent = max(last_expert, last_reeval)
            if most_recent <= 0:
                print(f"  ⚠ NEVER consulted by experts")
                never += 1
            elif (now - most_recent) < STALE_SECS:
                print(f"  ✓ Fresh (last {_fmt_age(now - most_recent)} ago)")
                fresh += 1
            else:
                print(f"  🚨 STALE (last {_fmt_age(now - most_recent)} ago)")
                stale += 1

    print("\n" + "=" * 96)
    print("SUMMARY")
    print("=" * 96)
    print(f"  ARMED_BUY sleeves: {total}")
    print(f"    fresh (<{STALE_SECS}s): {fresh}")
    print(f"    stale:                 {stale}")
    print(f"    never consulted:       {never}")
    print()
    print("  Fresh = experts firing normally.")
    print("  Stale = gate blocking consultation (cadence/history/drift). Investigate.")
    print("  Never = expert path never fired. Coverage gap.")


if __name__ == "__main__":
    main()
