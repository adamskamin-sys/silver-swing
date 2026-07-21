"""Did the trail stops cycle? For each product in adam-live, report:

  - current sleeve state (ARMED_BUY vs ARMED_SELL)
  - most recent BUY + SELL from Coinbase (chronological)
  - most recent trail-related events from trade log
  - verdict: cycled recently / still holding / stale / no trail configured

Adam 2026-07-20: "i had a few trail stops that i dont see anymore. can
you check if they cycled?"

If a sleeve WAS holding with a trail and now shows ARMED_BUY, and the
last SELL matches trail_breach_limit_sell / resting_stop_placed events
within the last few hours, the trail fired → cycle credited → re-armed.
That's healthy churn. If the sleeve is still ARMED_SELL but the trail
chip disappeared from the dashboard, that's a display bug — this diag
reports the underlying state so you can tell which.

Read-only.  python3 diag_trail_cycled_check.py
"""
from __future__ import annotations
import os
import json
import time
from typing import Any


LOOKBACK_HOURS = 6


def _dump(o: Any) -> dict:
    if hasattr(o, "to_dict"):
        try:
            return o.to_dict()
        except Exception:
            pass
    if isinstance(o, dict):
        return o
    return {}


def _iso_to_ts(s: str) -> float:
    if not s:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _fmt_hms(ts: float) -> str:
    if not ts:
        return "?"
    return time.strftime("%m-%d %H:%M:%S", time.gmtime(ts)) + " UTC"


def main() -> None:
    print("=" * 96)
    print(f"TRAIL-STOP CYCLE CHECK (last {LOOKBACK_HOURS}h)")
    print("=" * 96)

    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if not url:
        print("\n✗ REDIS_URL not set — run on Render shell")
        return
    r = redis.Redis.from_url(url, decode_responses=True)
    store = json.loads(r.get("silver-swing:store") or "{}")

    tenant = "adam-live"
    tbody = store.get(tenant) or {}
    products = sorted([p for p in tbody.keys()
                       if not p.startswith("__") and isinstance(tbody.get(p), dict)])

    # Preload recent events from the trade log
    events_by_product: dict[str, list[dict]] = {}
    try:
        from safety import make_trade_log
        log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
        all_events = log.tail(20000)
        cutoff = time.time() - LOOKBACK_HOURS * 3600
        for e in all_events:
            ts = float(e.get("ts") or 0)
            if ts < cutoff:
                continue
            pid = e.get("symbol") or ""
            events_by_product.setdefault(pid, []).append(e)
    except Exception as e:
        print(f"\n⚠ trade log read failed: {e} — continuing without events")

    from broker import BrokerConfig, CoinbaseBroker

    now = time.time()
    print(f"\nnow: {_fmt_hms(now)}  cutoff: {_fmt_hms(now - LOOKBACK_HOURS * 3600)}")

    for pid in products:
        block = tbody[pid] or {}
        cfg = block.get("config") or {}
        state = block.get("state") or {}
        sleeves_cfg = cfg.get("sleeves") or []
        sleeves_state = state.get("sleeves") or {}
        # Only interested in products where at least one sleeve has a
        # trail configured (exit_mode = trailing_stop or hybrid, or a
        # non-zero trail_distance).
        sleeves_with_trail = []
        for sc in sleeves_cfg:
            _mode = str(sc.get("exit_mode") or "").lower()
            _td = float(sc.get("trail_distance") or 0)
            if _mode in ("trailing_stop", "hybrid") or _td > 0:
                sleeves_with_trail.append(sc)
        if not sleeves_with_trail:
            continue

        print(f"\n{'─' * 96}")
        print(f"{pid}")
        print("─" * 96)

        # Product-level state
        print(f"  product state:  {state.get('state', '?')}"
              f"  halt: {state.get('halt_reason', 'none')}")

        for sc in sleeves_with_trail:
            sid = sc.get("id") or "?"
            ss = sleeves_state.get(sid) or {}
            sst = ss.get("state") or "?"
            own_avg = ss.get("own_avg_entry")
            rst_oid = ss.get("resting_stop_oid")
            rst_px = ss.get("resting_stop_px")
            rst_stage = ss.get("resting_stop_stage")
            hwm = ss.get("trail_high_water_price")
            trail_arm = ss.get("trail_armed")
            sell_px = sc.get("sell_px")
            sl_px = sc.get("stop_loss_px")
            td = sc.get("trail_distance")
            exit_mode = sc.get("exit_mode")

            print(f"  • {sid}  state={sst}")
            print(f"      exit_mode: {exit_mode}  trail_distance: {td}")
            print(f"      own_avg_entry:   {own_avg}")
            print(f"      sell_px:         {sell_px}   stop_loss_px: {sl_px}")
            print(f"      resting_stop_oid: {rst_oid or '(none)'}")
            print(f"      resting_stop_px:  {rst_px}  stage: {rst_stage}")
            print(f"      hwm:              {hwm}   trail_armed: {trail_arm}")

        # Coinbase truth: recent fills
        print(f"\n  COINBASE (position + recent orders):")
        try:
            b = CoinbaseBroker(BrokerConfig(product_id=pid))
        except Exception as e:
            print(f"    ✗ broker init: {e}")
            continue

        # Position
        try:
            positions = _dump(b.client.list_futures_positions()).get("positions") or []
            pos = next((p for p in positions if p.get("product_id") == pid), None)
            if pos:
                side = str(pos.get("side") or "").upper()
                pqty = int(float(pos.get("number_of_contracts") or 0))
                pavg = float(pos.get("avg_entry_price") or 0)
                print(f"    position: {side} {pqty}  avg=${pavg:.4f}")
            else:
                print(f"    position: FLAT")
        except Exception as e:
            print(f"    ✗ list_futures_positions: {e}")

        # Recent orders (BOTH open + filled)
        try:
            _resp = b.client.list_orders(product_id=pid, limit=20)
            orders = _dump(_resp).get("orders") or []
            recent_orders = []
            for o in orders:
                ct = _iso_to_ts(o.get("created_time") or "")
                if ct > 0 and ct >= (now - LOOKBACK_HOURS * 3600):
                    recent_orders.append((ct, o))
            recent_orders.sort(key=lambda x: x[0])
            if not recent_orders:
                print(f"    orders (last {LOOKBACK_HOURS}h): none")
            else:
                print(f"    orders (last {LOOKBACK_HOURS}h):  {len(recent_orders)}")
                for ct, o in recent_orders:
                    side = str(o.get("side") or "")[:4].upper()
                    st = str(o.get("status") or "")
                    cfg_o = o.get("order_configuration") or {}
                    type_key = ""
                    px = ""
                    q = ""
                    for k, v in (cfg_o.items() if isinstance(cfg_o, dict) else []):
                        type_key = k
                        if isinstance(v, dict):
                            px = str(v.get("limit_price")
                                     or v.get("stop_price") or "")
                            q = str(v.get("base_size") or v.get("size") or "")
                            break
                    print(f"      {_fmt_hms(ct):<20} {side:<4} {st:<12} "
                          f"type={type_key:<24} px={px:<10} qty={q}")
        except Exception as e:
            print(f"    ✗ list_orders: {e}")

        # Trade log events (trail-relevant)
        pevents = events_by_product.get(pid, [])
        trail_events = [e for e in pevents if any(
            k in (e.get("event_type") or "") for k in
            ("trail", "resting_stop", "cycle", "sleeve_on_fill",
             "reanchor", "expert_stop"))]
        if trail_events:
            print(f"\n  TRADE LOG (last {LOOKBACK_HOURS}h, trail-relevant): "
                  f"{len(trail_events)} events (showing last 15)")
            for e in trail_events[-15:]:
                et = e.get("event_type") or ""
                ts_evt = float(e.get("ts") or 0)
                reason = (e.get("reason") or e.get("stage") or "")[:60]
                sev = e.get("severity") or ""
                print(f"    {_fmt_hms(ts_evt):<20} [{sev:>8}] {et:<45} {reason}")

        # Verdict
        print(f"\n  VERDICT:")
        cycled = False
        holding = False
        for sc in sleeves_with_trail:
            sid = sc.get("id") or ""
            ss = sleeves_state.get(sid) or {}
            sst = ss.get("state") or ""
            if sst == "ARMED_SELL" and float(ss.get("own_avg_entry") or 0) > 0:
                holding = True
                print(f"    • {sid}: STILL HOLDING — "
                      f"own_avg=${float(ss.get('own_avg_entry') or 0):.4f}, "
                      f"hwm=${float(ss.get('trail_high_water_price') or 0):.4f}")
            elif sst == "ARMED_BUY":
                # Look for cycle_completed or sleeve_on_fill within window
                cycle_evt = [e for e in pevents if
                             (e.get("event_type") or "") in
                             ("sleeve_on_fill", "cycle_completed",
                              "resting_stop_filled",
                              "trail_breach_limit_sell") and
                             (e.get("side") or "").upper() != "BUY"]
                if cycle_evt:
                    cycled = True
                    last_evt = cycle_evt[-1]
                    print(f"    • {sid}: CYCLED — most recent exit at "
                          f"{_fmt_hms(float(last_evt.get('ts') or 0))}  "
                          f"({last_evt.get('event_type')})")
                else:
                    print(f"    • {sid}: ARMED_BUY (no recent exit in log)")
            else:
                print(f"    • {sid}: state={sst}")
        if not (cycled or holding):
            print(f"    → no active trail on this product")


if __name__ == "__main__":
    main()
