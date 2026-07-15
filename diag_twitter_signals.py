"""Watch Twitter/X shadow signals + score which handles have been the
best/worst predictors.

Adam 2026-07-15: twitter_scanner.py runs SHADOW-ONLY (EXECUTE_TRADES
= False, enforced by tests/test_twitter_shadow_only.py) and stores
signals in a separate Redis log — silver-swing:twitter-signals —
not the general trade log. This diag reads that log and reports.

Read-only. Usage:
    python3 diag_twitter_signals.py                # last 200 signals
    python3 diag_twitter_signals.py 500            # last 500
"""
from __future__ import annotations
import os
import sys
import time
from collections import defaultdict


def _fmt_ts(ts) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
    except Exception:
        return "?"


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    tenant = "adam-live"

    print("=" * 128)
    print(f"TWITTER SHADOW SIGNALS — last {limit} entries  tenant={tenant}")
    print("=" * 128)

    try:
        import twitter_scanner
        import state_store
        store = state_store.make_store(os.getenv("SWING_DATA_DIR", "data"))
        entries = twitter_scanner.read_log(store, limit=limit)
    except Exception as e:
        print(f"\n✗ Could not load Twitter signals: {e}")
        print(f"  If this is a fresh install, twitter_scanner may not have")
        print(f"  polled yet — check live_runner.py for scan cadence.")
        return

    if not entries:
        print(f"\n· No Twitter shadow signals recorded.")
        print(f"  * Scanner may not be running (check live_runner startup)")
        print(f"  * All Nitter instances may be down (check network)")
        print(f"  * No tweets matched the keyword taxonomy in the poll window")
        return

    print(f"\n[1] SUMMARY:  {len(entries)} signals in log")
    now = time.time()
    ages = [now - float(e.get("ts") or now) for e in entries]
    ages = [a for a in ages if a >= 0]
    if ages:
        print(f"    Oldest: {int(max(ages) / 3600)}h ago  · Newest: "
              f"{int(min(ages) / 60)}m ago")

    # [2] Per-handle accuracy
    print(f"\n[2] PER-HANDLE ACCURACY (across scored horizons):")
    per_handle = defaultdict(lambda: {
        "signals": 0,
        "hits_1h": 0, "misses_1h": 0, "unscored_1h": 0,
        "hits_6h": 0, "misses_6h": 0, "unscored_6h": 0,
        "hits_24h": 0, "misses_24h": 0, "unscored_24h": 0,
    })
    for e in entries:
        source = str(e.get("source") or "?")
        handle = source.replace("twitter@", "")
        s = per_handle[handle]
        s["signals"] += 1
        direction = str(e.get("direction") or "")
        outcomes = e.get("outcomes") or {}
        for horizon, key_hit, key_miss, key_uns in (
            ("1h", "hits_1h", "misses_1h", "unscored_1h"),
            ("6h", "hits_6h", "misses_6h", "unscored_6h"),
            ("24h", "hits_24h", "misses_24h", "unscored_24h"),
        ):
            out = outcomes.get(horizon)
            if out is None:
                s[key_uns] += 1
                continue
            # out is a dict of {product: pct_move}. Signal is a hit
            # if the AVERAGE pct move sign matches direction.
            if isinstance(out, dict):
                vals = [v for v in out.values() if isinstance(v, (int, float))]
                avg = sum(vals) / len(vals) if vals else 0
            else:
                try:
                    avg = float(out)
                except Exception:
                    avg = 0
            correct = (direction == "bullish" and avg > 0) or \
                      (direction == "bearish" and avg < 0)
            if correct:
                s[key_hit] += 1
            else:
                s[key_miss] += 1

    if not per_handle:
        print(f"    · no handles observed yet")
    else:
        print(f"\n    {'HANDLE':22s} {'N':>4s} {'HITS-1h':>10s} {'HITS-6h':>10s} "
              f"{'HITS-24h':>10s}")
        print(f"    {'-' * 68}")
        for handle in sorted(per_handle.keys(),
                             key=lambda h: -per_handle[h]["signals"]):
            s = per_handle[handle]
            def pct(hit, miss):
                total = hit + miss
                if total == 0:
                    return "  -"
                return f"{100.0 * hit / total:.0f}% ({hit}/{total})"
            print(f"    {handle:22s} {s['signals']:>4d} "
                  f"{pct(s['hits_1h'], s['misses_1h']):>10s} "
                  f"{pct(s['hits_6h'], s['misses_6h']):>10s} "
                  f"{pct(s['hits_24h'], s['misses_24h']):>10s}")

    # [3] Would-action distribution
    print(f"\n[3] WOULD-ACTION DISTRIBUTION:")
    by_action = defaultdict(int)
    for e in entries:
        by_action[str(e.get("would_action") or "?")] += 1
    for a, n in sorted(by_action.items(), key=lambda kv: -kv[1]):
        print(f"    {a:20s}  {n}")

    # [4] Most recent signals with tweet snippets
    print(f"\n[4] RECENT SIGNALS (up to 15):")
    for e in entries[:15]:
        text = str(e.get("tweet_text") or "")[:120]
        text = text.replace("\n", " ")
        print(f"    · {_fmt_ts(e.get('ts'))}  "
              f"{str(e.get('source') or '?'):22s} "
              f"{str(e.get('family') or '?'):10s} "
              f"{str(e.get('direction') or '?'):8s} "
              f"score={e.get('score')} "
              f"→ {str(e.get('would_action') or '?')}")
        if text:
            print(f"        \"{text}\"")

    print("=" * 128)


if __name__ == "__main__":
    main()
