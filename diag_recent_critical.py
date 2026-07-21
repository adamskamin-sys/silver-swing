"""Show the last N critical/warn events with full detail."""
import os, time


def main():
    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = log.tail(200)
    crit = [e for e in events if (e.get("severity") or "") in ("critical", "warn", "high")]
    print(f"{len(crit)} critical/warn/high events in last 200")
    print()
    for e in crit[-20:]:
        age = int(time.time() - float(e.get("ts") or 0))
        print(f"{age:>5}s  [{e.get('severity'):>8}]  {e.get('event_type')}")
        print(f"         symbol={e.get('symbol')}   sleeve={e.get('sleeve_id')}")
        for k in ("error", "reason", "cancelled_oid", "stop_px", "sell_px",
                  "correct_sell_px", "stale_px"):
            v = e.get(k)
            if v is not None:
                s = str(v)[:120]
                print(f"         {k}: {s}")
        print()


if __name__ == "__main__":
    main()
