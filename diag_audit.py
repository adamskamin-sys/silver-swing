"""Comprehensive audit — how is the bot doing?

Reports:
  1. Liveness (tracks alive, ticks, uptime, kill switch)
  2. Current holdings (position, unrealized $, bracket status)
  3. Recent cycles (last 24h, win rate, realized $)
  4. Expert coverage (fresh/stale/never per ARMED_BUY sleeve)
  5. Critical events (last 100, severity distribution)
  6. Config drift (any None fields)
  7. Open orders on Coinbase (bracket completeness check)

Read-only. Run: python3 diag_audit.py
"""
from __future__ import annotations
import os
import json
import time
from collections import Counter, defaultdict


def _fmt_money(x: float, nd: int = 2) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.{nd}f}"


def _fmt_age(secs: float) -> str:
    if secs < 60:
        return f"{int(secs)}s"
    if secs < 3600:
        return f"{int(secs / 60)}m"
    if secs < 86400:
        return f"{int(secs / 3600)}h"
    return f"{int(secs / 86400)}d"


def _dump(o):
    if hasattr(o, "to_dict"):
        try:
            return o.to_dict()
        except Exception:
            pass
    if isinstance(o, dict):
        return o
    return {}


def main() -> None:
    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    if not url:
        print("REDIS_URL not set")
        return
    r = redis.Redis.from_url(url, decode_responses=True)
    store = json.loads(r.get("silver-swing:store") or "{}")

    tenant = "adam-live"
    tbody = store.get(tenant) or {}
    products = sorted([p for p in tbody.keys()
                       if not p.startswith("__") and isinstance(tbody.get(p), dict)])

    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = log.tail(10000)
    now = time.time()

    print("=" * 96)
    print(f"BOT AUDIT — {tenant} — {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 96)

    # ─────────────────────────────────────────────────────────
    # 1. LIVENESS
    # ─────────────────────────────────────────────────────────
    print("\n[1] LIVENESS")
    hb_raw = tbody.get("__track_heartbeat__") or {}
    hb = hb_raw.get("config") or hb_raw
    tracks = hb.get("tracks") or {}
    alive = zombie = dead = 0
    for pid, t in tracks.items():
        last_ok = float(t.get("last_step_ok_ts") or 0)
        if last_ok <= 0:
            dead += 1
            continue
        age = now - last_ok
        if age < 300:
            alive += 1
        elif age < 600:
            zombie += 1
        else:
            dead += 1
    print(f"  tracks: {alive} alive, {zombie} zombie, {dead} dead")

    # Kill switch
    try:
        from safety import KillSwitch
        from state_store import RedisJsonStore
        ss_store = RedisJsonStore(url)
        ks = KillSwitch(ss_store, tenant)
        print(f"  kill switch: {'🚨 ACTIVE' if ks.is_active() else '✓ off'}")
    except Exception as e:
        print(f"  kill switch: ? ({e})")

    # Phase A kill flag
    phase_a_off = r.get("silver-swing:phase_a_disabled")
    is_off = phase_a_off and str(phase_a_off).lower() not in ("", "0", "false", "none")
    print(f"  Phase A LIMIT placement: {'🚨 FROZEN' if is_off else '✓ active'}")

    # Bot boot event
    boot_events = [e for e in events if e.get("event_type") == "bot_started"]
    if boot_events:
        latest_boot = max(boot_events, key=lambda e: float(e.get("ts") or 0))
        uptime = now - float(latest_boot.get("ts") or 0)
        print(f"  uptime since last boot: {_fmt_age(uptime)}")

    # ─────────────────────────────────────────────────────────
    # 2. HOLDINGS + BRACKETS
    # ─────────────────────────────────────────────────────────
    print("\n[2] HOLDINGS + BRACKET STATUS")
    held_count = 0
    bracket_complete = bracket_partial = bracket_missing = 0
    for pid in products:
        block = tbody[pid] or {}
        state = block.get("state") or {}
        sleeves_state = state.get("sleeves") or {}
        for sid, ss in sleeves_state.items():
            if ss.get("state") != "ARMED_SELL":
                continue
            if not ss.get("own_avg_entry"):
                continue
            held_count += 1
            has_stop = bool(ss.get("resting_stop_oid"))
            has_profit = bool(ss.get("resting_profit_limit_oid"))
            if has_stop and has_profit:
                bracket_complete += 1
            elif has_stop or has_profit:
                bracket_partial += 1
            else:
                bracket_missing += 1
    print(f"  held sleeves: {held_count}")
    print(f"    ✓ full bracket (both stop + profit-lock): {bracket_complete}")
    print(f"    ⚠ partial bracket (one leg only):         {bracket_partial}")
    print(f"    🚨 NO bracket (unprotected):              {bracket_missing}")

    # ─────────────────────────────────────────────────────────
    # 3. RECENT CYCLES (24h) + REALIZED $
    # ─────────────────────────────────────────────────────────
    print("\n[3] RECENT CYCLES (last 24h)")
    cutoff_24h = now - 86400
    cycle_events = [e for e in events
                    if e.get("event_type") == "sleeve_cycle_completed"
                    and float(e.get("ts") or 0) >= cutoff_24h]
    if cycle_events:
        wins = sum(1 for e in cycle_events if float(e.get("cycle_pnl") or 0) > 0)
        losses = sum(1 for e in cycle_events if float(e.get("cycle_pnl") or 0) < 0)
        breaks = len(cycle_events) - wins - losses
        total_pnl = sum(float(e.get("cycle_pnl") or 0) for e in cycle_events)
        print(f"  {len(cycle_events)} cycles: {wins} wins, {losses} losses, {breaks} even")
        print(f"  net realized (bot-computed): {_fmt_money(total_pnl)}")
        by_pid = defaultdict(float)
        for e in cycle_events:
            by_pid[e.get("symbol") or "?"] += float(e.get("cycle_pnl") or 0)
        print(f"\n  per product:")
        for pid, pnl in sorted(by_pid.items(), key=lambda x: -x[1]):
            print(f"    {pid:<24}  {_fmt_money(pnl):>10}")
    else:
        print(f"  no cycles completed in last 24h")

    # ─────────────────────────────────────────────────────────
    # 4. EXPERT COVERAGE
    # ─────────────────────────────────────────────────────────
    print("\n[4] EXPERT COVERAGE (ARMED_BUY sleeves)")
    latest_expert = defaultdict(lambda: 0.0)
    for e in events:
        et = e.get("event_type") or ""
        if et in ("expert_reentry_decision", "reentry_reeval_decision"):
            k = (e.get("symbol"), e.get("sleeve_id"))
            ts = float(e.get("ts") or 0)
            if ts > latest_expert[k]:
                latest_expert[k] = ts

    action_counts_per_sleeve = defaultdict(Counter)
    for e in events:
        if e.get("event_type") != "expert_reentry_decision":
            continue
        if float(e.get("ts") or 0) < now - 3600:  # last hour
            continue
        k = (e.get("symbol"), e.get("sleeve_id"))
        action_counts_per_sleeve[k][e.get("action")] += 1

    armed_buy_sleeves = []
    for pid in products:
        block = tbody[pid] or {}
        state = block.get("state") or {}
        sleeves_state = state.get("sleeves") or {}
        for sid, ss in sleeves_state.items():
            if ss.get("state") == "ARMED_BUY":
                armed_buy_sleeves.append((pid, sid))

    print(f"  {len(armed_buy_sleeves)} ARMED_BUY sleeves waiting to rebuy")
    fresh = stale = never = 0
    for pid, sid in armed_buy_sleeves:
        last = latest_expert[(pid, sid)]
        if last <= 0:
            never += 1
            continue
        age = now - last
        if age < 300:
            fresh += 1
        else:
            stale += 1
    print(f"    ✓ fresh (<5m): {fresh}")
    print(f"    ⚠ stale:       {stale}")
    print(f"    🚨 never:       {never}")

    # Aggregate expert vote breakdown in last hour
    all_actions = Counter()
    for counts in action_counts_per_sleeve.values():
        for a, n in counts.items():
            all_actions[a] += n
    if all_actions:
        print(f"\n  expert decisions in last 1h (all sleeves): {dict(all_actions)}")

    # ─────────────────────────────────────────────────────────
    # 5. CRITICAL EVENTS (last 100)
    # ─────────────────────────────────────────────────────────
    print("\n[5] SEVERITY DISTRIBUTION (last 200 events)")
    sev_counts = Counter(e.get("severity") for e in events[-200:])
    print(f"  {dict(sev_counts)}")

    crit_recent = [e for e in events[-200:]
                   if (e.get("severity") or "") in ("critical", "warn")]
    if crit_recent:
        print(f"\n  most recent critical/warn (last 5):")
        for e in crit_recent[-5:]:
            age = int(now - float(e.get("ts") or 0))
            reason = str(e.get("reason") or "")[:70]
            print(f"    {age:>5}s  [{e.get('severity'):>8}]  {e.get('event_type'):<45}  {e.get('symbol'):<20}")
            if reason:
                print(f"           {reason}")

    # ─────────────────────────────────────────────────────────
    # 6. CONFIG DRIFT
    # ─────────────────────────────────────────────────────────
    print("\n[6] CONFIG DRIFT (None values in critical fields)")
    drift = []
    for pid in products:
        block = tbody[pid] or {}
        cfg = block.get("config") or {}
        problems = []
        for field in ("contract_size", "tick_size", "fee_per_contract_roundtrip"):
            if cfg.get(field) is None:
                problems.append(field)
        for sc in (cfg.get("sleeves") or []):
            for field in ("buy_px", "sell_px"):
                if sc.get(field) is None:
                    problems.append(f"{sc.get('id')}.{field}")
        if problems:
            drift.append((pid, problems))
    if drift:
        for pid, probs in drift:
            print(f"  {pid}: {', '.join(probs)}")
    else:
        print("  ✓ no drift")

    # ─────────────────────────────────────────────────────────
    # 7. OPEN ORDERS SUMMARY
    # ─────────────────────────────────────────────────────────
    print("\n[7] RECENT ORDER PLACEMENTS (last hour)")
    place_events = [e for e in events if float(e.get("ts") or 0) >= now - 3600
                    and e.get("event_type") in (
                        "profit_lock_limit_placed",
                        "resting_stop_placed",
                        "profit_lock_limit_adopted_from_broker",
                        "resting_stop_adopted_from_broker")]
    if place_events:
        by_type = Counter(e.get("event_type") for e in place_events)
        for et, n in by_type.most_common():
            print(f"  {n:>4}  {et}")
    else:
        print("  (no placements in last hour)")

    print("\n" + "=" * 96)
    print("AUDIT COMPLETE")
    print("=" * 96)


if __name__ == "__main__":
    main()
