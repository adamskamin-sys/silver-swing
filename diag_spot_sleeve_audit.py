"""Audit every spot-crypto holding for sleeve health.

Adam 2026-07-20: "its not letting me create a sleeve for the crypto" +
"it keep strying to buy" on META. Per feedback_audit_before_fix.md,
audit first. This diag enumerates for EVERY spot holding:

  - wallet balance + mark + notional (from __portfolio__.crypto)
  - existing sleeve config + state
  - open Coinbase orders (BUY / SELL, price, qty)
  - recent scanner_order_placed / order_failed events (last 30 min)
  - WHY a new-sleeve arm might fail (capacity math walkthrough)
  - WHY the sleeve keeps re-trying (state mismatch check)

Read-only. Usage:
    python3 diag_spot_sleeve_audit.py
"""
from __future__ import annotations
import json
import os
import time


TENANT = "adam-live"


def _dump(obj):
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return obj if isinstance(obj, dict) else {}


def main() -> None:
    print("=" * 78)
    print("SPOT SLEEVE AUDIT — every crypto holding")
    print("=" * 78)

    import redis
    url = (os.environ.get("REDIS_URL")
           or os.environ.get("REDIS_INTERNAL_URL"))
    if not url:
        print("\n✗ REDIS_URL not set")
        return
    r = redis.Redis.from_url(url, decode_responses=True)

    store = json.loads(r.get("silver-swing:store") or "{}")
    tenant_block = store.get(TENANT) or {}
    pf = ((tenant_block.get("__portfolio__") or {}).get("config") or {})
    crypto = pf.get("crypto") or []
    derivs = pf.get("derivatives") or []
    pf_ts = float(pf.get("_refresh_ts") or 0)
    pf_age = int(time.time() - pf_ts) if pf_ts else -1
    pf_err = pf.get("_last_error")

    print(f"\n  __portfolio__ age: {pf_age}s   error: {pf_err or '(none)'}")
    print(f"  crypto holdings: {len(crypto)}   derivatives: {len(derivs)}")

    if not crypto:
        print("\n  ⚠ No spot crypto in wallet — nothing to audit.")
        return

    # Trade log for recent BUY attempts
    raw_events = r.lrange("silver-swing:trade_log", 0, 5000) or []
    events = []
    for line in raw_events:
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    events.reverse()
    now = time.time()
    cutoff_30 = now - 1800

    # Broker for open orders
    from broker import BrokerConfig, CoinbaseBroker

    for c in crypto:
        pid = c.get("product_id")
        bal = float(c.get("balance") or 0)
        mark = float(c.get("mark") or 0)
        notional = bal * mark
        print("\n" + "-" * 78)
        print(f"  {pid}   balance={bal}   mark=${mark}   notional=${notional:,.2f}")

        # ---- Sleeve config + state -----------------------------------------
        block = tenant_block.get(pid) or {}
        cfg = block.get("config") or {}
        state = block.get("state") or {}
        sleeves_cfg = cfg.get("sleeves") or []
        sleeves_state = state.get("sleeves") or {}
        print(f"    sleeves configured: {len(sleeves_cfg)}")
        for sc in sleeves_cfg:
            sid = sc.get("id")
            ss = sleeves_state.get(sid) or {}
            st = ss.get("state") or "(no state)"
            own = ss.get("own_avg_entry")
            live_oid = ss.get("live_order_id")
            qty = sc.get("qty")
            buy_px = sc.get("buy_px")
            sell_px = sc.get("sell_px")
            print(f"      • {sid}  qty={qty}  state={st}")
            print(f"          buy_px=${buy_px}  sell_px=${sell_px}  own_avg={own}  live_oid={live_oid}")

            # AUDIT #1: state mismatch — held position + ARMED_BUY = "will keep trying to buy"
            if bal > 0 and st == "ARMED_BUY":
                print(f"          🚨 STATE MISMATCH: wallet holds {bal} {pid.split('-')[0]} "
                      f"but sleeve is ARMED_BUY")
                print(f"          → Bot will place a BUY order on every tick.")
                print(f"          → Root cause: scanner-arm sent clientSeed.state='ARMED_BUY'")
                print(f"            which blocks server.js auto-adopt (line ~823).")

        # ---- Open orders on Coinbase --------------------------------------
        try:
            b = CoinbaseBroker(BrokerConfig(product_id=pid))
            resp = _dump(b.client.list_orders(product_id=pid,
                                              order_status=["OPEN"]))
            open_orders = resp.get("orders") or []
        except Exception as e:
            open_orders = []
            print(f"    ✗ list_orders failed: {type(e).__name__}: {e}")

        buys = [o for o in open_orders if str(o.get("side") or "").upper() == "BUY"]
        sells = [o for o in open_orders if str(o.get("side") or "").upper() == "SELL"]
        print(f"    open Coinbase orders: {len(buys)} BUY, {len(sells)} SELL")

        def _summarize(o):
            oc = o.get("order_configuration") or {}
            for k in ("limit_limit_gtc", "stop_limit_stop_limit_gtc",
                     "market_market_ioc", "limit_limit_ioc"):
                if k in oc:
                    conf = oc[k]
                    px = (conf.get("limit_price") or conf.get("stop_price")
                          or "market")
                    sz = conf.get("base_size") or conf.get("quote_size") or "?"
                    return f"{k.replace('_gtc','').replace('_ioc','')} px={px} sz={sz}"
            return "(unknown config)"

        for o in buys:
            print(f"      BUY  oid={o.get('order_id')}  {_summarize(o)}")
        for o in sells:
            print(f"      SELL oid={o.get('order_id')}  {_summarize(o)}")

        # AUDIT #2: multiple open buys = orphans building up
        if len(buys) > 1:
            print(f"    🚨 {len(buys)} open BUYs → each ticks-loop pass adds another. "
                  f"Cancel via diag_meta_cancel_orphan_buys.py (edit PID inside).")

        # ---- Recent BUY attempts in trade log -----------------------------
        recent = [e for e in events
                  if float(e.get("ts") or 0) > cutoff_30
                  and (e.get("symbol") == pid or e.get("product_id") == pid)
                  and e.get("event_type") in (
                      "scanner_order_placed",
                      "scanner_order_failed",
                      "sleeve_buy_placed",
                      "sleeve_buy_failed",
                      "order_placed",
                      "order_failed",
                      "sleeve_step_denied",
                  )]
        if recent:
            print(f"    recent events (last 30 min): {len(recent)}")
            for e in recent[-6:]:
                age = int(now - float(e.get("ts") or 0))
                et = e.get("event_type")
                extra = ""
                for k in ("side", "px", "qty", "reason", "error"):
                    v = e.get(k)
                    if v:
                        extra += f" {k}={str(v)[:60]}"
                print(f"      [{age:>4}s] {et}{extra}")

        # ---- Capacity walkthrough (what /api/sleeves would compute) -------
        primary = float(cfg.get("swing_qty") or 0)
        core = 0 if "core_qty" not in cfg else float(cfg.get("core_qty") or 0)
        active_qty = 0
        for sc in sleeves_cfg:
            ss = sleeves_state.get(sc.get("id")) or {}
            if str(ss.get("state") or "") == "ARMED_SELL":
                active_qty += float(sc.get("qty") or 0)
        pos = int(bal)
        budget = pos - core
        print(f"    capacity: pos={pos} - core={core} = budget {budget}")
        print(f"    already claimed (ARMED_SELL sleeves + primary): {active_qty + primary}")
        room = budget - active_qty - primary
        if room > 0:
            print(f"    ✓ room for new sleeve up to qty={int(room)}")
        elif bal == 0:
            print(f"    ⚠ no wallet balance — new sleeves would need to BUY first (ARMED_BUY seed is correct)")
        else:
            print(f"    ✗ no capacity — new sleeve would be rejected by /api/sleeves")

    # -----------------------------------------------------------------------
    # DEAD TRACKS — cross-ref should-be-tracked vs actually-tracked
    # -----------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("DEAD TRACK CROSS-REF (why HEALTH: N dead badge)")
    print("=" * 78)
    hb = ((tenant_block.get("__track_heartbeat__") or {}).get("config") or {})
    if not hb:
        hb = tenant_block.get("__track_heartbeat__") or {}
    tracks_alive_dict = hb.get("tracks") or {}
    tracks_alive = list(tracks_alive_dict.keys())
    hb_snap_ts = float(hb.get("snap_ts") or 0)
    hb_age = int(now - hb_snap_ts) if hb_snap_ts else -1
    print(f"\n  __track_heartbeat__ age: {hb_age}s   alive tracks: {len(tracks_alive)}")
    for pid, t in sorted(tracks_alive_dict.items()):
        step_ok = float(t.get("last_step_ok_ts") or 0)
        tick_ct = int(t.get("tick_count") or 0)
        age_step = int(now - step_ok) if step_ok else -1
        print(f"    ✓ {pid:35s} ticks={tick_ct:>5} step_age={age_step:>4}s")

    # Trade log HEALTH check: is anything writing to it at all?
    print(f"\n  trade log: read {len(events)} recent events")
    if events:
        latest_ts = max(float(e.get("ts") or 0) for e in events[-100:])
        oldest_ts = min(float(e.get("ts") or 0) for e in events[:100])
        latest_age = int(now - latest_ts) if latest_ts else -1
        window = int(latest_ts - oldest_ts) if latest_ts and oldest_ts else -1
        print(f"    latest event: {latest_age}s ago   window covered: {window}s "
              f"({window/60:.1f} min)")
        # Event-type histogram (last 5 min) — reveals if tick loop is actually running
        cutoff_5 = now - 300
        recent_events = [e for e in events if float(e.get("ts") or 0) > cutoff_5]
        et_counts = {}
        for e in recent_events:
            et = e.get("event_type") or "?"
            et_counts[et] = et_counts.get(et, 0) + 1
        print(f"    events in last 5 min: {len(recent_events)}")
        for et, n in sorted(et_counts.items(), key=lambda x: -x[1])[:15]:
            print(f"      {n:>5}  {et}")

    # Should-track set: (a) derivatives with qty != 0, (b) any symbol with ARMED_* sleeve
    should_track = set()
    for d in derivs:
        if float(d.get("qty") or 0) != 0:
            should_track.add(d.get("product_id"))
    for sym, block in (tenant_block or {}).items():
        if sym.startswith("__"):
            continue
        if not isinstance(block, dict):
            continue
        for ss in (((block.get("state") or {}).get("sleeves") or {}) or {}).values():
            if str(ss.get("state") or "") in ("ARMED_BUY", "ARMED_SELL"):
                should_track.add(sym)
                break

    dead = [pid for pid in should_track if pid not in tracks_alive]
    print(f"  should-be-tracked: {len(should_track)}   dead (missing from heartbeat): {len(dead)}")

    # Latest failure event per dead symbol
    cutoff_60 = now - 3600
    fail_types = {"non_primary_config_auto_seed_failed",
                  "track_creation_failed", "non_primary_step_failure",
                  "track_silent_detected", "track_auto_respawn_attempted"}
    fail_by_sym = {}
    for e in events:
        if float(e.get("ts") or 0) < cutoff_60:
            continue
        if e.get("event_type") not in fail_types:
            continue
        sym = e.get("symbol") or e.get("product_id")
        if sym:
            fail_by_sym.setdefault(sym, []).append(e)

    for pid in sorted(dead):
        print(f"\n  💀 {pid}")
        fails = fail_by_sym.get(pid, [])
        if not fails:
            print(f"       (no failure events in last hour — likely just spawn-budget queued)")
            continue
        for e in fails[-3:]:
            age = int(now - float(e.get("ts") or 0))
            et = e.get("event_type")
            reason = (e.get("reason") or e.get("error")
                      or e.get("cooldown_remaining_secs") or "")
            print(f"       [{age:>4}s] {et}  {str(reason)[:140]}")

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    print("  Look for 🚨 (spot arm blockers) and 💀 (dead tracks) above.")
    print("  Two common root causes:")
    print("    1) Wallet holds tokens + sleeve state=ARMED_BUY")
    print("         → scanner arm bypassed auto-adopt. FIX = server-side")
    print("           override so clientSeed.state='ARMED_BUY' loses to")
    print("           unclaimed-long check.")
    print("    2) Multiple open BUYs on Coinbase for one product")
    print("         → already-placed guard didn't fire. FIX = enforce")
    print("           idempotency in the tick loop's buy-place path for spot.")


if __name__ == "__main__":
    main()
