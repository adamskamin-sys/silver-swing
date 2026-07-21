"""Toggle place_limit caller-stack tracing.

    python3 diag_place_trace.py           # show state
    python3 diag_place_trace.py on
    python3 diag_place_trace.py off
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
    key = "silver-swing:trace_place_limit"

    if len(sys.argv) == 1:
        v = r.get(key)
        print(f"{key} = {v!r}")
        return

    action = sys.argv[1].lower()
    if action in ("on", "1", "true"):
        r.set(key, "1")
        print(f"✓ {key} = 1  →  every place_limit call will print caller stack")
    elif action in ("off", "0", "false"):
        r.delete(key)
        print(f"✓ deleted {key}  →  tracing disabled")
    else:
        print(f"unknown: {action}")


if __name__ == "__main__":
    main()
