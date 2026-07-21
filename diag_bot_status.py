"""Is the bot alive and trading right now?

Adam 2026-07-21: quick status check after kill-switch crash-loop.

Reports:
  - Kill switch state (Redis flag)
  - Most recent trade log event (age = liveness signal)
  - Recent __track_heartbeat__ ages per held product
  - Any orders placed in the last 5 min

Read-only. Run: python3 diag_bot_status.py
"""
from __future__ import annotations
import os
import json
import time
from typing import Any


def _dump(o: Any) -> dict:
    if hasattr(o, "to_dict"):
        try:
            return o.to_dict()
        except Exception:
            pass
    if isinstance(o, dict):
        return o
    return {}


def main() -> None:
    print("=" * 90)
    print("BOT STATUS")
    print("=" * 90)

    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if not url:
        print("\n✗ REDIS_URL not set — run on Render shell")
        return
    r = redis.Redis.from_url(url, decode_responses=True)

    # 1. Kill switch state
    print(f"\n[1] KILL SWITCH")
    ks_keys = [k for k in r.keys("*kill*") if isinstance(k, str)]
    for k in ks_keys:
        v = r.get(k)
        print(f"    {k}: {v}")
    if not ks_keys:
        print(f"    (no keys matching *kill* found)")

    # Also check store's kill switch field
    store = json.loads(r.get("silver-swing:store") or "{}")
    tenant = "adam-live"
    tbody = store.get(tenant) or {}
    for k in ("__kill_switch__", "__kill__", "kill_switch"):
        if k in tbody:
            print(f"    store.{tenant}.{k}: {tbody[k]}")

    # 2. Most recent trade log event
    print(f"\n[2] RECENT ACTIVITY")
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        events = log.tail(50)
        if not events:
            print("    (no events in trade log)")
        else:
            latest = max(events, key=lambda e: float(e.get("ts") or 0))
            age = int(time.time() - float(latest.get("ts") or 0))
            print(f"    most recent event: {latest.get('event_type')}  "
                  f"({age}s ago)")
            print(f"    symbol: {latest.get('symbol')}   "
                  f"severity: {latest.get('severity')}")
            reason = str(latest.get("reason") or "")[:100]
            if reason:
                print(f"    reason: {reason}")
            # count by severity in recent
            from collections import Counter
            sev_counts = Counter(e.get("severity") for e in events)
            print(f"    last 50 events by severity: {dict(sev_counts)}")
    except Exception as e:
        print(f"    ✗ trade log read failed: {e}")

    # 3. Per-product heartbeats
    print(f"\n[3] TRACK HEARTBEATS  (alive if < 300s, DEAD if > 600s)")
    hb_raw = tbody.get("__track_heartbeat__") or {}
    hb = hb_raw.get("config") or hb_raw
    tracks = hb.get("tracks") or {}
    now = time.time()
    alive = dead = zombie = 0
    for pid, t in sorted(tracks.items()):
        last_ok = float(t.get("last_step_ok_ts") or 0)
        if last_ok <= 0:
            print(f"    {pid:<24}  NEVER TICKED")
            dead += 1
            continue
        age = int(now - last_ok)
        if age < 300:
            status = "✓ alive"
            alive += 1
        elif age < 600:
            status = "⚠ zombie"
            zombie += 1
        else:
            status = "🚨 DEAD"
            dead += 1
        print(f"    {pid:<24}  {age:>5}s  ticks={t.get('tick_count', 0):<5}  {status}")
    print(f"\n    totals: {alive} alive, {zombie} zombie, {dead} dead")

    # 4. Recent orders on any product (last 5 min)
    print(f"\n[4] RECENT ORDERS (last 5 min across all held products)")
    from broker import BrokerConfig, CoinbaseBroker
    cutoff = now - 300
    products = sorted([p for p in tbody.keys()
                       if not p.startswith("__") and isinstance(tbody.get(p), dict)])
    total_recent = 0
    for pid in products:
        try:
            b = CoinbaseBroker(BrokerConfig(product_id=pid))
            resp = b.client.list_orders(product_id=pid, limit=20)
            orders = _dump(resp).get("orders") or []
            recent = []
            for o in orders:
                ct = str(o.get("created_time") or "")
                try:
                    from datetime import datetime
                    ct_ts = datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
                    if ct_ts >= cutoff:
                        recent.append((ct_ts, o))
                except Exception:
                    continue
            if recent:
                total_recent += len(recent)
                print(f"    {pid}: {len(recent)} orders")
                for _, o in sorted(recent):
                    side = str(o.get("side") or "")[:4]
                    st = str(o.get("status") or "")
                    print(f"      {side:<4} {st:<10} {o.get('order_id', '')[:20]}...")
        except Exception:
            pass
    if total_recent == 0:
        print(f"    (no orders placed in last 5 min on any product)")

    print()


if __name__ == "__main__":
    main()
