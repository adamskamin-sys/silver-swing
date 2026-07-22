"""NEAR PERP CDE 500 buy-side cancel-replace loop trace.

Adam 2026-07-22 screenshot showed 16 CANCELLED BUY LIMITs at $2.0100
between 02:27-02:36 before a $1.8746 fill at 02:37:37. Same class as
the SELL-side loop we just fixed but on the ARM path.

Two known gates SHOULD prevent identical-price cancel-replace:
  - _reeval_cancel_replace drift gate at swing_leg.py:3707 (0.25%)
  - _maybe_auto_refresh_stale_sleeve cadence throttle (60s)

Both firing correctly means SOMETHING ELSE is cancelling. This diag
prints EVERY event touching NEAR in the last hour so we can trace
who did what.
"""
import os
import json
import time
from collections import Counter


def _fmt_ts(ts):
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "??:??:??"


def main():
    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = log.tail(5000)
    now = time.time()

    pid_candidates = ["NEAR-20DEC30-CDE", "NEAR-PERP-CDE"]
    near = []
    for e in events:
        sym = str(e.get("symbol") or "")
        if "NEAR" in sym.upper() and float(e.get("ts") or 0) >= now - 3600:
            near.append(e)

    if not near:
        print("no NEAR events in last hour")
        return

    print("=" * 96)
    print(f"NEAR event trace — {len(near)} events in last hour")
    print("=" * 96)

    # Event-type counts
    counts = Counter(e.get("event_type") for e in near)
    print(f"\n[event types]")
    for et, n in counts.most_common(30):
        print(f"  {n:>4}  {et}")

    # Every cancel-related and place-related event, chronologically
    interesting = set()
    for et in counts:
        et_l = str(et).lower()
        if any(k in et_l for k in ("cancel", "place", "reeval", "reanchor",
                                     "arm", "buy", "sell", "expire",
                                     "adopt", "expert_reentry", "credit",
                                     "fill", "sleeve_order")):
            interesting.add(et)

    trace = [e for e in near if e.get("event_type") in interesting]
    trace.sort(key=lambda e: float(e.get("ts") or 0))

    print(f"\n[interesting-event timeline — {len(trace)} events]")
    for e in trace:
        ts = _fmt_ts(e.get("ts"))
        et = e.get("event_type") or ""
        sid = str(e.get("sleeve_id") or "primary")[:20]
        px = (e.get("buy_px") or e.get("new_buy_px") or e.get("sell_px")
              or e.get("new_sell_px") or e.get("price") or e.get("target_px")
              or e.get("limit_px") or e.get("fill_price") or "")
        oid = str(e.get("oid") or e.get("order_id") or e.get("old_order_id")
                  or e.get("new_order_id") or e.get("cancelled_oid") or "")[:12]
        action = e.get("action") or ""
        reason = str(e.get("reason") or e.get("why") or e.get("error") or "")[:60]
        line = f"  {ts}  {et:<48}  {sid:<20}  px={str(px):<12}  oid={oid:<12}"
        if action:
            line += f"  action={action}"
        print(line)
        if reason:
            print(f"           reason: {reason}")

    # Cancel-per-place ratio
    places = sum(1 for e in near if "place" in str(e.get("event_type") or "").lower()
                 and "failed" not in str(e.get("event_type") or "").lower()
                 and "skipped" not in str(e.get("event_type") or "").lower())
    cancels = sum(1 for e in near if "cancel" in str(e.get("event_type") or "").lower()
                  and "failed" not in str(e.get("event_type") or "").lower())
    print(f"\n[cadence] places={places}  cancels={cancels}")


if __name__ == "__main__":
    main()
