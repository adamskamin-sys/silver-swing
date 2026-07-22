"""XRP-PERP-CDE stop-limit cancel-loop tracer.

Adam 2026-07-22 10:26 screenshot: 15+ CANCELLED STOP-LIMIT SELLs on
XRP PERP CDE 500 at $1.0338/$1.0333 in ~9 min. Ratchet-noise fix +
Amihud-Mendelson threshold should've blocked identical-price replaces.
Something else is cancelling.

Prints every XRP event in the last 30 min chronologically so we can
name the code path.
"""
import os
import time
from collections import Counter


def _fmt(ts):
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return "??:??:??"


def main():
    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = log.tail(10000)
    now = time.time()

    # Match any XRP product id shape — including XPP alias per
    # project_xpp_xrp_alias memory (Coinbase display "XRP PERP CDE"
    # is internal product_id XPP-20DEC30-CDE).
    xrp = [e for e in events
           if (any(k in str(e.get("symbol") or "").upper()
                    for k in ("XRP", "XPP")))
           and float(e.get("ts") or 0) >= now - 1800]

    if not xrp:
        print("no XRP/XPP events in last 30min")
        return

    print("=" * 96)
    print(f"XRP events last 30min ({len(xrp)} total)")
    print("=" * 96)

    counts = Counter(e.get("event_type") for e in xrp)
    print("\n[event-type counts]")
    for et, n in counts.most_common(25):
        print(f"  {n:>4}  {et}")

    # Show stop-related events chronologically
    stop_keywords = ("stop", "ratchet", "cancel", "place", "broker_excess",
                     "reconcile", "trail_breach", "adopt")
    stops = [e for e in xrp
             if any(k in str(e.get("event_type") or "").lower()
                    for k in stop_keywords)]
    stops.sort(key=lambda e: float(e.get("ts") or 0))

    print(f"\n[stop-related timeline — {len(stops)} events]")
    for e in stops:
        ts = _fmt(e.get("ts"))
        et = e.get("event_type") or ""
        sid = str(e.get("sleeve_id") or "primary")[:16]
        px = (e.get("target_px") or e.get("to_px") or e.get("stop_px")
              or e.get("cancelled_stop_px") or e.get("new_stop_loss_px") or "")
        old_px = e.get("from_px") or e.get("old_stop_loss_px") or ""
        oid = str(e.get("new_oid") or e.get("oid") or e.get("old_oid")
                  or e.get("cancelled_oid") or "")[:12]
        line = f"  {ts}  {et:<45}  {sid:<16}"
        if old_px:
            line += f"  {old_px}→{px}"
        elif px:
            line += f"  px={px}"
        if oid:
            line += f"  oid={oid}"
        print(line)
        why = str(e.get("reason") or e.get("error") or e.get("why") or "")[:80]
        if why:
            print(f"           {why}")

    # Print the sleeve state
    import redis, json
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if url:
        r = redis.Redis.from_url(url, decode_responses=True)
        store = json.loads(r.get("silver-swing:store") or "{}")
        tbody = store.get("adam-live") or {}
        for pid, block in tbody.items():
            if "XRP" not in pid.upper() or not isinstance(block, dict):
                continue
            state = block.get("state") or {}
            cfg = block.get("config") or {}
            print(f"\n[SLEEVE STATE — {pid}]")
            print(f"  cfg contract_size={cfg.get('contract_size')} tick={cfg.get('tick_size')}")
            for sid, ss in (state.get("sleeves") or {}).items():
                sc = next((s for s in (cfg.get("sleeves") or [])
                           if s.get("id") == sid), {})
                print(f"  sleeve {sid}:")
                for k in ("state", "own_avg_entry", "resting_stop_oid",
                          "resting_stop_px", "resting_stop_stage",
                          "resting_profit_limit_oid", "resting_profit_limit_px",
                          "trail_high_water_price", "trail_armed"):
                    print(f"    {k}: {ss.get(k)}")
                print(f"    cfg.sell_px:      {sc.get('sell_px')}")
                print(f"    cfg.stop_loss_px: {sc.get('stop_loss_px')}")
                print(f"    cfg.qty:          {sc.get('qty')}")


if __name__ == "__main__":
    main()
