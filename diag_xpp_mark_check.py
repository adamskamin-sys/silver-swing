"""Why is XPP-20DEC30-CDE showing no mark on the dashboard?

Checks:
  1. Is the product_id valid on Coinbase (contract_spec query)?
  2. Is there a snapshot in Redis with mark data?
  3. Is there a track heartbeat (feed running)?
  4. Recent trade log events for this product
"""
import os
import json
import time


PID_GUESSES = [
    "XPP-20DEC30-CDE",
    "XRP-20DEC30-CDE",
    "XRP-PERP-INTX",
    "XPP-PERP-CDE",
]


def main():
    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if not url:
        print("REDIS_URL not set")
        return
    r = redis.Redis.from_url(url, decode_responses=True)
    store = json.loads(r.get("silver-swing:store") or "{}")
    tbody = store.get("adam-live") or {}

    print("=" * 90)
    print("XPP MARK-MISSING DIAGNOSTIC")
    print("=" * 90)

    # 1. Find the actual PID in the store
    print("\n[1] Search adam-live tenant for XPP / XRP products:")
    matches = [k for k in tbody.keys()
               if not k.startswith("__") and (
                   "XPP" in k.upper() or "XRP" in k.upper())]
    if not matches:
        print("    (no XPP or XRP products in store)")
    for pid in matches:
        block = tbody.get(pid) or {}
        cfg = block.get("config") or {}
        state = block.get("state") or {}
        print(f"    ✓ {pid}")
        print(f"        state:        {state.get('state', '?')}")
        print(f"        contract_size: {cfg.get('contract_size')}")
        print(f"        tick_size:     {cfg.get('tick_size')}")
        sleeves = cfg.get('sleeves') or []
        print(f"        sleeves:       {len(sleeves)}")

    # 2. Snapshot check
    print("\n[2] Redis snapshot check:")
    for pid in matches or ["XPP-20DEC30-CDE"]:
        snap = json.loads(r.get(f"silver-swing:snapshot:adam-live:{pid}") or "{}")
        if snap:
            last_mark = snap.get("last_mark") or snap.get("mark")
            last_ts = snap.get("mark_ts") or snap.get("last_ts")
            age = int(time.time() - float(last_ts)) if last_ts else None
            print(f"    {pid}: mark={last_mark}, age={age}s" if age is not None
                  else f"    {pid}: mark={last_mark} (no ts)")
        else:
            print(f"    {pid}: NO SNAPSHOT in Redis")

    # 3. Track heartbeat
    print("\n[3] Track heartbeat:")
    hb_raw = tbody.get("__track_heartbeat__") or {}
    hb = hb_raw.get("config") or hb_raw
    tracks = hb.get("tracks") or {}
    for pid in matches or ["XPP-20DEC30-CDE"]:
        t = tracks.get(pid) or {}
        last_ok = float(t.get("last_step_ok_ts") or 0)
        if last_ok <= 0:
            print(f"    {pid}: NEVER TICKED")
        else:
            age = int(time.time() - last_ok)
            print(f"    {pid}: {age}s ago, ticks={t.get('tick_count', 0)}")

    # 4. Broker contract_spec query for each guess
    print("\n[4] Coinbase contract_spec query for each candidate name:")
    from broker import BrokerConfig, CoinbaseBroker
    for pid in PID_GUESSES + matches:
        pid_norm = pid
        if pid_norm in {p for p in PID_GUESSES}:
            already = False
        else:
            already = True
        try:
            b = CoinbaseBroker(BrokerConfig(product_id=pid_norm))
            spec = b.contract_spec() if hasattr(b, "contract_spec") else {}
            print(f"    {pid_norm}: contract_size={(spec or {}).get('contract_size')}   "
                  f"tick_size={(spec or {}).get('tick_size')}")
        except Exception as e:
            msg = str(e)[:80]
            print(f"    {pid_norm}: ✗ {msg}")


if __name__ == "__main__":
    main()
