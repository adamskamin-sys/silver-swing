"""Show recent crypto purchase attempts + why they failed.

Adam 2026-07-20: after two failed attempts to buy crypto (once as sleeve,
once as one-shot), we need to see what actually happened. Reads the trade
log for recent scanner_order + sleeve events on SPOT products.

Read-only. Usage:
    python3 diag_crypto_attempts.py
"""
from __future__ import annotations
import json
import os
import time


_SPOT_SUFFIXES = ("-USD", "-USDC", "-EUR", "-GBP")


def _is_spot(pid: str) -> bool:
    if not pid:
        return False
    # Coinbase futures end in -CDE (e.g. XLP-20DEC30-CDE); spot ends in
    # -USD / -USDC / etc. Cheap heuristic; good enough for filtering log lines.
    if pid.endswith("-CDE"):
        return False
    return any(pid.endswith(s) for s in _SPOT_SUFFIXES)


def main() -> None:
    print("=" * 78)
    print("CRYPTO PURCHASE ATTEMPTS — RECENT LOG SCAN")
    print("=" * 78)

    import redis
    url = (os.environ.get("REDIS_URL")
           or os.environ.get("REDIS_INTERNAL_URL"))
    if not url:
        print("\n✗ REDIS_URL env not set")
        return
    r = redis.Redis.from_url(url, decode_responses=True)

    # Pull the last 5000 trade log events. Redis stores newest at head; we
    # walk oldest → newest so timeline reads chronologically.
    raw_events = r.lrange("silver-swing:trade_log", 0, 5000) or []
    events = []
    for line in raw_events:
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    events.reverse()

    now = time.time()
    cutoff = now - (6 * 3600)  # last 6h

    spot_events = []
    for e in events:
        ts = float(e.get("ts") or 0)
        if ts < cutoff:
            continue
        pid = e.get("product_id") or e.get("symbol") or ""
        # Some events don't carry product_id — infer from sleeve_id / name
        if not pid:
            for k in ("sleeve_name", "sleeve_id"):
                v = str(e.get(k) or "")
                if v:
                    pid = v
                    break
        if _is_spot(pid) or (pid and any(s in pid for s in ("META", "PENGU", "MOG", "FART"))):
            spot_events.append(e)

    print(f"\nFound {len(spot_events)} spot-related events in last 6h")
    print("-" * 78)

    if not spot_events:
        print("\n(no spot events — either Adam's attempt failed before writing")
        print(" a log entry, or symbol doesn't match spot heuristic)")
        return

    for e in spot_events:
        ts = float(e.get("ts") or 0)
        rel = int(now - ts)
        rel_str = (f"{rel}s ago" if rel < 60
                   else f"{rel // 60}m ago" if rel < 3600
                   else f"{rel // 3600}h {(rel % 3600) // 60}m ago")
        etype = e.get("event_type") or "?"
        pid = e.get("product_id") or e.get("symbol") or "?"
        sev = e.get("severity") or ""
        sev_marker = "🚨 " if sev == "critical" else "⚠ " if sev == "warn" else ""
        print(f"\n{sev_marker}{etype}  [{rel_str}]  {pid}")
        # Print interesting fields inline
        for k in ("side", "qty", "order_type", "limit_price", "reason",
                  "error", "message", "order_id"):
            v = e.get(k)
            if v not in (None, ""):
                print(f"    {k}: {v}")

    # Also check scanner_order log file if backtest_worker writes one
    print("\n" + "=" * 78)
    print("BACKTEST_WORKER LAST 20 LINES (scanner_order handling)")
    print("=" * 78)
    log_path = os.path.expanduser("~/silver-swing/data/backtest_worker.log")
    if os.path.exists(log_path):
        with open(log_path, "r") as f:
            lines = f.readlines()
        for ln in lines[-20:]:
            ln = ln.rstrip()
            if "scanner_order" in ln:
                print(ln)
    else:
        print(f"(no {log_path} — worker logs to stdout only on Render)")


if __name__ == "__main__":
    main()
