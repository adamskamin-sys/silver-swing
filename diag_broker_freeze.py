"""Emergency broker-level freeze — refuses ALL place_limit calls.

Adam 2026-07-21: Phase A kill switch wasn't enough — orders still
being placed and cancelled after freeze. This is a bigger hammer.

    python3 diag_broker_freeze.py             # show state
    python3 diag_broker_freeze.py freeze      # DISABLE all place_limit
    python3 diag_broker_freeze.py unfreeze    # re-enable

When frozen, every place_limit call raises RuntimeError. Existing
orders on Coinbase are unaffected — only NEW placements refused.
"""
import os
import sys


def main():
    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if not url:
        print("REDIS_URL not set")
        return
    r = redis.Redis.from_url(url, decode_responses=True)
    key = "silver-swing:broker_freeze"

    if len(sys.argv) == 1:
        v = r.get(key)
        state = "FROZEN" if v and str(v).lower() not in ("", "0", "false", "none") else "OPEN"
        print(f"{key} = {v!r}  →  broker is {state}")
        return

    action = sys.argv[1].lower()
    if action in ("freeze", "off", "1", "true"):
        r.set(key, "1")
        print(f"✓ {key} = 1  →  ALL place_limit calls will raise")
    elif action in ("unfreeze", "on", "0", "false"):
        r.delete(key)
        print(f"✓ deleted {key}  →  place_limit re-enabled")
    else:
        print(f"unknown arg: {action}")
        print("USAGE: python3 diag_broker_freeze.py [freeze|unfreeze]")


if __name__ == "__main__":
    main()
