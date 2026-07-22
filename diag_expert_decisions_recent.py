"""Show what expert_reentry actually DECIDED for each sleeve on recent ticks.

Answers: are experts saying WAIT, COOL_OFF, or REBUY? If REBUY, what price?
And how does that compare to the sleeve's current buy_px?
"""
import os, json, time
from collections import defaultdict


def main():
    from safety import make_trade_log
    log = make_trade_log(os.getenv("SWING_DATA_DIR", "data"))
    events = log.tail(3000)

    # Last N expert_reentry_decision per sleeve
    per_sleeve = defaultdict(list)
    for e in events:
        if e.get("event_type") != "expert_reentry_decision":
            continue
        per_sleeve[(e.get("symbol"), e.get("sleeve_id"))].append(e)

    if not per_sleeve:
        print("no expert_reentry_decision events in last 3000 events")
        return

    # Get current sleeve buy_px from Redis
    import redis
    url = os.environ.get("REDIS_URL") or os.environ.get("REDIS_INTERNAL_URL")
    r = redis.Redis.from_url(url, decode_responses=True) if url else None
    store = json.loads((r.get("silver-swing:store") if r else "{}") or "{}")
    tbody = store.get("adam-live") or {}

    now = time.time()
    print(f"Showing last 3 expert_reentry_decision events per sleeve:\n")

    for (pid, sid), es in sorted(per_sleeve.items()):
        es_sorted = sorted(es, key=lambda e: float(e.get("ts") or 0), reverse=True)
        # Get current sc.buy_px
        block = tbody.get(pid) or {}
        cfg = block.get("config") or {}
        sc = next((s for s in (cfg.get("sleeves") or []) if s.get("id") == sid), {})
        current_buy = sc.get("buy_px")
        current_sell = sc.get("sell_px")

        print("=" * 90)
        print(f"{pid}  ·  {sid}")
        print(f"  current sc.buy_px = {current_buy}   sc.sell_px = {current_sell}")
        print(f"  {len(es)} decisions in log window")

        # Action breakdown
        from collections import Counter
        actions = Counter(e.get("action") for e in es)
        print(f"  action counts: {dict(actions)}")

        # Show last 3
        for e in es_sorted[:3]:
            ts = float(e.get("ts") or 0)
            age = int(now - ts)
            action = e.get("action")
            buy_px = e.get("buy_px")
            wait_secs = e.get("wait_secs")
            votes = e.get("expert_votes") or {}
            print(f"\n  {age}s ago  ACTION={action}  buy_px={buy_px}  wait_secs={wait_secs}")
            for expert, vote in list(votes.items())[:5]:
                if isinstance(vote, list):
                    vote = vote[0] if vote else ""
                vote_str = str(vote)[:100]
                print(f"    {expert}: {vote_str}")


if __name__ == "__main__":
    main()
