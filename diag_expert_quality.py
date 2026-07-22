"""Expert-reentry quality audit — are experts choosing entry points correctly?

Answers the question "are the experts working right":

  1. COVERAGE: which ARMED_BUY sleeves are getting expert decisions?
     Any silent sleeves (never evaluated)? Stale (haven't run in >5m)?

  2. VOTE MIX: what fraction WAIT vs REBUY vs COOL_OFF? Is one expert
     always vetoing (e.g. Vince cooldown never releases)?

  3. PRICE QUALITY: for REBUY decisions, is the chosen buy_px:
     - Below current mark (correct — buying a dip)?
     - Above own_avg (would rebuy at loss basis)?
     - Distance from own_avg reasonable (0.5-3% below is expert intent)?

  4. OUTCOMES: did any expert-chosen rebuy actually fill? Where?
     Compare filled BUY price to what the expert chose.

  5. EXPERT INDIVIDUAL VOTES: per expert (Wilder, Kaufman, Chan,
     Connors, Faith, Vince, Menkveld, Timmermann), what fraction of
     the time did each vote to WAIT vs proceed? If one expert always
     WAITs, they're the sole veto — bot's effectively single-expert.
"""
import os
import json
import time
from collections import defaultdict, Counter


def main():
    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = log.tail(5000)
    now = time.time()

    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    r = redis.Redis.from_url(url, decode_responses=True) if url else None
    store = json.loads((r.get("silver-swing:store") if r else "{}") or "{}")
    tbody = store.get("adam-live") or {}

    print("=" * 96)
    print(f"EXPERT-REENTRY QUALITY AUDIT — {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 96)

    # ── 1. COVERAGE ────────────────────────────────────────────
    print("\n[1] COVERAGE — ARMED_BUY sleeves + last expert decision")
    armed_buy = []
    for pid, block in sorted(tbody.items()):
        if pid.startswith("__") or not isinstance(block, dict):
            continue
        state = block.get("state") or {}
        for sid, ss in (state.get("sleeves") or {}).items():
            if ss.get("state") == "ARMED_BUY":
                armed_buy.append((pid, sid))

    latest = defaultdict(lambda: 0.0)
    for e in events:
        if e.get("event_type") in ("expert_reentry_decision", "reentry_reeval_decision"):
            k = (e.get("symbol"), e.get("sleeve_id"))
            ts = float(e.get("ts") or 0)
            if ts > latest[k]:
                latest[k] = ts

    for pid, sid in armed_buy:
        last = latest[(pid, sid)]
        if last <= 0:
            status = "🚨 NEVER"
        else:
            age = now - last
            if age < 300:
                status = f"✓ {int(age)}s"
            elif age < 900:
                status = f"⚠ {int(age/60)}m"
            else:
                status = f"🚨 STALE {int(age/60)}m"
        print(f"  {pid:<24}  {sid:<18}  {status}")

    # ── 2. VOTE MIX (last hour) ────────────────────────────────
    print("\n[2] ACTION MIX — last hour")
    action_counts = Counter()
    per_sleeve_actions = defaultdict(Counter)
    for e in events:
        if e.get("event_type") != "expert_reentry_decision":
            continue
        if float(e.get("ts") or 0) < now - 3600:
            continue
        act = e.get("action") or "?"
        action_counts[act] += 1
        per_sleeve_actions[(e.get("symbol"), e.get("sleeve_id"))][act] += 1

    total = sum(action_counts.values())
    if total > 0:
        for act, n in action_counts.most_common():
            pct = 100.0 * n / total
            print(f"  {act:<10}  {n:>5}  ({pct:.1f}%)")
    else:
        print("  (no expert_reentry_decision events in last hour)")

    # ── 3. PRICE QUALITY (for REBUY decisions) ─────────────────
    print("\n[3] PRICE QUALITY — recent REBUY decisions")
    rebuys = [e for e in events
              if e.get("event_type") == "expert_reentry_decision"
              and e.get("action") == "rebuy"
              and float(e.get("ts") or 0) >= now - 3600]

    if not rebuys:
        print("  (no REBUY decisions in last hour)")
    else:
        print(f"  {len(rebuys)} rebuy decisions in last hour")
        good = bad_above_avg = bad_far = 0
        for e in rebuys[-10:]:
            pid = e.get("symbol")
            sid = e.get("sleeve_id")
            expert_buy = e.get("buy_px")
            block = tbody.get(pid) or {}
            state = block.get("state") or {}
            ss = (state.get("sleeves") or {}).get(sid) or {}
            cfg = block.get("config") or {}
            sc = next((s for s in (cfg.get("sleeves") or [])
                       if s.get("id") == sid), {})
            last_sell = ss.get("last_sell_fill_price")
            age = int(now - float(e.get("ts") or 0))
            if expert_buy and last_sell:
                try:
                    ratio = float(expert_buy) / float(last_sell)
                    delta_pct = (ratio - 1) * 100
                    flag = ""
                    if ratio > 1.0:
                        flag = "⚠ ABOVE last_sell (buying higher than we sold)"
                        bad_above_avg += 1
                    elif ratio < 0.95:
                        flag = f"⚠ FAR below (-{-delta_pct:.1f}%) — deep dip target"
                        bad_far += 1
                    else:
                        flag = f"✓ {delta_pct:+.2f}% vs last_sell"
                        good += 1
                    print(f"  {age:>5}s  {pid[:20]:<20}  buy={expert_buy}  "
                          f"last_sell={last_sell}  {flag}")
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
        if len(rebuys) > 10:
            print(f"  ... ({len(rebuys) - 10} more)")
        print(f"\n  Quality: ✓ good={good}   ⚠ above={bad_above_avg}   ⚠ far={bad_far}")

    # ── 4. OUTCOMES: did rebuys fill? ──────────────────────────
    print("\n[4] REBUY OUTCOMES — did the chosen buy_px actually fill?")
    # For each ARMED_BUY sleeve that has recent rebuy decisions, check
    # if any BUY fills happened after the decision.
    fills_by_sid = defaultdict(list)
    for e in events:
        et = e.get("event_type") or ""
        # Sleeve fill events
        if et in ("sleeve_on_fill", "sleeve_order_filled"):
            leg = e.get("leg") or ""
            if leg == "ARMED_BUY" or leg == "BUY":
                fills_by_sid[(e.get("symbol"), e.get("sleeve_id"))].append(e)

    if not any(rebuys):
        pass
    else:
        matched = unmatched = 0
        for e in rebuys[-20:]:
            k = (e.get("symbol"), e.get("sleeve_id"))
            ts = float(e.get("ts") or 0)
            expert_buy = e.get("buy_px")
            if expert_buy is None:
                continue
            # Find first fill AFTER this decision
            later = [f for f in fills_by_sid.get(k, [])
                     if float(f.get("ts") or 0) > ts]
            if later:
                first_fill = min(later, key=lambda f: float(f.get("ts") or 0))
                fill_px = first_fill.get("average_filled_price") or first_fill.get("fill_price")
                try:
                    slip = (float(fill_px) / float(expert_buy) - 1) * 100
                    print(f"  {e.get('symbol')[:20]:<20}  expert={expert_buy}  "
                          f"filled={fill_px}  slip={slip:+.2f}%")
                    matched += 1
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
            else:
                unmatched += 1
        if matched + unmatched > 0:
            print(f"\n  {matched} matched to actual fills, {unmatched} still pending")

    # ── 5. EXPERT INDIVIDUAL VOTES ─────────────────────────────
    print("\n[5] EXPERT INDIVIDUAL VOTES — who's blocking?")
    # Look at expert_votes field in decisions
    expert_wait_counts = Counter()
    expert_seen_counts = Counter()
    for e in events:
        if e.get("event_type") != "expert_reentry_decision":
            continue
        if float(e.get("ts") or 0) < now - 3600:
            continue
        votes = e.get("expert_votes") or {}
        for expert, vote in votes.items():
            expert_seen_counts[expert] += 1
            v_str = str(vote).lower() if not isinstance(vote, list) else str(vote[0] if vote else "").lower()
            if "wait" in v_str or "veto" in v_str or "no" in v_str or "hold" in v_str:
                expert_wait_counts[expert] += 1

    if not expert_seen_counts:
        print("  (no vote data captured — check that expert_votes field is populated in events)")
    else:
        print(f"  {'Expert':<18}  {'Total':>8}  {'Wait/veto':>10}  {'% wait':>8}")
        for expert, seen in expert_seen_counts.most_common():
            waits = expert_wait_counts.get(expert, 0)
            pct = 100.0 * waits / seen if seen else 0
            flag = ""
            if pct > 90 and seen > 20:
                flag = "  ⚠ dominant vetoer — bot's effectively single-expert"
            elif pct < 10 and seen > 20:
                flag = "  ⚠ always votes proceed — rubber stamp?"
            print(f"  {expert:<18}  {seen:>8}  {waits:>10}  {pct:>7.1f}%{flag}")

    # ── 6. SUMMARY ─────────────────────────────────────────────
    print("\n" + "=" * 96)
    print("SUMMARY")
    print("=" * 96)
    n_armed = len(armed_buy)
    n_never = sum(1 for pid, sid in armed_buy if latest[(pid, sid)] <= 0)
    n_stale = sum(1 for pid, sid in armed_buy
                   if latest[(pid, sid)] > 0 and now - latest[(pid, sid)] > 300)
    n_fresh = n_armed - n_never - n_stale
    print(f"  Coverage:  {n_fresh}/{n_armed} fresh, {n_stale} stale, {n_never} never")
    rebuy_frac = 0.0
    if total > 0:
        rebuy_frac = 100.0 * action_counts.get("rebuy", 0) / total
    print(f"  Rebuy rate: {rebuy_frac:.1f}% (rest is WAIT/COOL_OFF)")
    print(f"  {len(rebuys)} rebuy decisions in last hour; {len(fills_by_sid)} sleeves with any fills")


if __name__ == "__main__":
    main()
