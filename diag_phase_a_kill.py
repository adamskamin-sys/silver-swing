"""Freeze / unfreeze Phase A LIMIT placement.

Adam 2026-07-21: cancel-replace loop detected. This diag flips a Redis
flag that the bot reads each tick — if set, Phase A skips placement.

Existing orders on the book are UNAFFECTED. Stop-limit maintenance
(protective floor) continues normally. Only NEW profit-lock LIMIT
placement is disabled.

    python3 diag_phase_a_kill.py             # show current state
    python3 diag_phase_a_kill.py off         # DISABLE Phase A placement
    python3 diag_phase_a_kill.py on          # ENABLE Phase A placement
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
    key = "silver-swing:phase_a_disabled"

    if len(sys.argv) == 1:
        v = r.get(key)
        print(f"{key} = {v!r}")
        print(f"Phase A LIMIT placement is currently: "
              f"{'DISABLED' if v and str(v).lower() not in ('', '0', 'false', 'none') else 'ENABLED'}")
        return

    action = sys.argv[1].lower()
    if action in ("off", "disable", "1", "true"):
        r.set(key, "1")
        print(f"✓ set {key} = 1  →  Phase A LIMIT placement DISABLED")
        print("  (existing orders on Coinbase are unaffected; stop-limit path continues)")
    elif action in ("on", "enable", "0", "false"):
        r.delete(key)
        print(f"✓ deleted {key}  →  Phase A LIMIT placement ENABLED (next tick)")
    else:
        print(f"unknown arg: {action}")
        print("USAGE: python3 diag_phase_a_kill.py [on|off]")


if __name__ == "__main__":
    main()
