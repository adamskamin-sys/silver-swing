"""Are we throttled by Coinbase, or is our own code the bottleneck?

Adam 2026-07-19: MC/NER/OND/XLM LAST TICK 49-52s (should be 5-15s);
XLP silent 5m, ZEC silent 1h. He asks: coinbase or us?

This diag answers by dumping three data sources in parallel:

  1. RECENT 429 / rate-limit events from the trade log (last 60 min)
     — proves whether Coinbase actively bounced us.
  2. The in-process RateLimitController.stats() — shows current
     utilization %, whether we're in backoff, per-endpoint counts of
     throttled/backoff/success responses.
  3. WebSocket-feed reconnect events (feed_stale_detected / feed_
     reconnect_attempted) per product — shows if data-plane bandwidth
     is the bottleneck rather than REST.

Verdict logic:
  - 429s > 0 last 60 min           → Coinbase is throttling us
  - controller util > 80%          → we're self-throttling (own limits)
  - feed_stale > 5 in last 10 min  → WS-plane is the bottleneck
  - none of the above + slow ticks → our code is the bottleneck (loop
                                     stall, blocking call, deadlock)

Read-only. Usage: python3 diag_throttle_check.py
"""
from __future__ import annotations
import os
import time
from collections import Counter


TENANT = "adam-live"
WINDOW_MIN = 60


def main() -> None:
    print("=" * 78)
    print(f"THROTTLE CHECK — is Coinbase throttling us, or is our code slow?")
    print("=" * 78)

    now = time.time()
    cutoff = now - (WINDOW_MIN * 60)

    # ---------- 1) Coinbase-side throttling (trade log events) ----------
    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = log.tail(5000) if hasattr(log, "tail") else []

    rate_limit_kinds = {
        "coinbase_rate_limit_hit", "rate_limit_backoff",
        "rate_limit_429", "coinbase_429", "rest_call_429",
        "coinbase_throttled", "rate_limit_soft_backoff",
    }
    rate_limit_events = []
    feed_stale_events = []
    reconnect_events = []
    step_failure_events = []
    for e in events:
        if not isinstance(e, dict): continue
        ts = float(e.get("ts") or 0)
        if ts < cutoff: continue
        kind = str(e.get("event_type") or "")
        if kind in rate_limit_kinds or "429" in kind or "rate_limit" in kind:
            rate_limit_events.append(e)
        elif "feed_stale" in kind:
            feed_stale_events.append(e)
        elif "reconnect" in kind:
            reconnect_events.append(e)
        elif "step_failure" in kind or "step_error" in kind:
            step_failure_events.append(e)

    print(f"\n[1/3] COINBASE-SIDE THROTTLING (last {WINDOW_MIN} min)")
    print(f"      rate-limit / 429 events:  {len(rate_limit_events)}")
    if rate_limit_events:
        by_sym = Counter(e.get("symbol") or "?" for e in rate_limit_events)
        for sym, cnt in by_sym.most_common(10):
            print(f"        {sym}: {cnt}")
        # Show most recent
        for e in rate_limit_events[-5:]:
            print(f"        [{int(now - float(e.get('ts', 0)))}s ago] "
                  f"{e.get('event_type')} sym={e.get('symbol')} "
                  f"reason={e.get('reason') or ''}")
    else:
        print(f"        ✓ NO rate-limit events — Coinbase is not throttling us")

    # ---------- 2) In-process rate limit controller ----------
    print(f"\n[2/3] OUR RATE-LIMIT CONTROLLER (current state)")
    try:
        from rate_limit_controller import get_controller
        stats = get_controller().stats()
        print(f"      public_util:  {stats['public_util_pct']}% "
              f"(rate {stats['public_rate_per_sec']}/s of {stats['public_limit']}/s cap)")
        print(f"      private_util: {stats['private_util_pct']}% "
              f"(rate {stats['private_rate_per_sec']}/s of {stats['private_limit']}/s cap)")
        print(f"      public_in_backoff:  {stats['public_in_backoff']}")
        print(f"      private_in_backoff: {stats['private_in_backoff']}")
        counts = stats.get("counts") or {}
        if counts:
            print(f"      counts: {dict(counts)}")
    except Exception as e:
        print(f"      ✗ controller unavailable: {e}")
        stats = {}

    # ---------- 3) WebSocket-feed health ----------
    print(f"\n[3/3] WS-FEED HEALTH (last {WINDOW_MIN} min)")
    print(f"      feed_stale events:  {len(feed_stale_events)}")
    print(f"      reconnect events:   {len(reconnect_events)}")
    print(f"      step_failure events:{len(step_failure_events)}")
    if feed_stale_events:
        by_sym = Counter(e.get("symbol") or "?" for e in feed_stale_events)
        print(f"      feed_stale by symbol:")
        for sym, cnt in by_sym.most_common(10):
            print(f"        {sym}: {cnt}")
    if step_failure_events:
        for e in step_failure_events[-5:]:
            print(f"        [{int(now - float(e.get('ts', 0)))}s ago] "
                  f"{e.get('event_type')} sym={e.get('symbol')} "
                  f"err={e.get('error') or e.get('reason') or ''}")

    # ---------- Verdict ----------
    print(f"\n{'=' * 78}")
    print("VERDICT")
    print(f"{'=' * 78}")
    cb_throttling = len(rate_limit_events) > 0
    self_throttling = (stats.get("public_util_pct", 0) > 80
                       or stats.get("private_util_pct", 0) > 80
                       or stats.get("public_in_backoff")
                       or stats.get("private_in_backoff"))
    feed_stress = len(feed_stale_events) > 5

    if cb_throttling:
        print(f"→ COINBASE IS THROTTLING: {len(rate_limit_events)} rate-limit "
              f"events in last hour. Slow down.")
    elif self_throttling:
        print(f"→ WE ARE SELF-THROTTLING: rate_limit_controller is limiting "
              f"our own rate below Coinbase's cap. Consider raising limits "
              f"in rate_limit_controller.py or reducing per-tick REST calls.")
    elif feed_stress:
        print(f"→ WS-FEED STRESS: {len(feed_stale_events)} feed_stale in the "
              f"last hour. Data plane (not REST) is the bottleneck. Check "
              f"WS connection stability + per-product feed init.")
    elif step_failure_events:
        print(f"→ STEP FAILURES: {len(step_failure_events)} step_failure "
              f"events. Investigate the individual error messages — the "
              f"tick loop is running but step() is raising.")
    else:
        print(f"→ NO THROTTLE + NO FEED STRESS + NO STEP FAILURES.")
        print(f"  If ticks are still slow, the bottleneck is in-loop work:")
        print(f"    - blocking calls (e.g., long broker.contract_spec fetches)")
        print(f"    - per-product loops taking too long each iteration")
        print(f"    - main.refresh_portfolio_snapshot() slow (check snap chip)")
        print(f"  Recommend running diag_coinbase_speed_test.py to measure")
        print(f"  what round-trip we're getting — if we can hit 30/s but only")
        print(f"  achieve 1/s, the throttle is downstream of REST.")


if __name__ == "__main__":
    main()
