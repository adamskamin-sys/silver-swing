"""Force the scanner_worker to run a fresh scan NOW + verify result.

Adam 2026-07-20: after shipping b413327 + 8a19cc8 + 151a1e8 scanner
fixes, the Redis snapshot is stale (6 min old) because scanner_worker
runs on a 15-min auto interval. The dashboard "Refresh" button writes
a flag to trigger a scan; this diag does the same from CLI + polls
the resulting snapshot to confirm the new code produced crypto entries.

Read-only for stateful ops (only sets the request flag, doesn't
modify scoring). Usage:
    python3 diag_scanner_force_refresh.py
"""
from __future__ import annotations
import json
import os
import time


def main() -> None:
    print("=" * 78)
    print("FORCE SCANNER REFRESH + VERIFY")
    print("=" * 78)

    import redis
    url = (os.environ.get("REDIS_URL")
           or os.environ.get("REDIS_INTERNAL_URL"))
    if not url:
        print("\n✗ REDIS_URL env not set")
        return
    r = redis.Redis.from_url(url, decode_responses=True)

    # Read current snapshot age for baseline
    raw_before = r.get("silver-swing:scanner")
    if raw_before:
        try:
            data_before = json.loads(raw_before)
            gen_before = float(data_before.get("generated_at") or 0)
            age_before = int(time.time() - gen_before) if gen_before else -1
            crypto_before = len(data_before.get("top_crypto") or [])
            deriv_before = len(data_before.get("top_derivative") or [])
            print(f"\nBEFORE: snapshot age {age_before}s, "
                  f"crypto={crypto_before}, derivative={deriv_before}")
        except Exception:
            print(f"\nBEFORE: snapshot exists but couldn't parse")
    else:
        print(f"\nBEFORE: no snapshot in Redis")

    # Set refresh flag (same key/format the dashboard "Refresh" button uses)
    ttl_secs = 300
    r.set("silver-swing:scanner:refresh_requested",
          str(int(time.time())), ex=ttl_secs)
    print(f"\n✓ Refresh flag written (TTL {ttl_secs}s)")
    print(f"  scanner_worker will pick this up on its next iteration (~2s cadence)")

    # Poll for new snapshot
    print(f"\nPolling for fresh snapshot (up to 90s)...")
    start_wait = time.time()
    new_gen = None
    for i in range(90):
        time.sleep(1)
        raw = r.get("silver-swing:scanner")
        if not raw:
            continue
        try:
            data = json.loads(raw)
            g = float(data.get("generated_at") or 0)
            if raw_before:
                if g > gen_before + 1:  # newer than baseline
                    new_gen = g
                    print(f"  ✓ New snapshot at t+{int(time.time() - start_wait)}s "
                          f"(generated_at delta: {int(g - gen_before)}s)")
                    break
            else:
                if g > 0:
                    new_gen = g
                    break
        except Exception:
            continue
    else:
        print(f"  ✗ Timed out after 90s waiting for new snapshot")
        print(f"    scanner_worker may not be running OR scan is failing silently")
        return

    # Verify contents
    raw_after = r.get("silver-swing:scanner")
    data_after = json.loads(raw_after)
    top = data_after.get("top") or []
    top_crypto = data_after.get("top_crypto") or []
    top_deriv = data_after.get("top_derivative") or []

    print(f"\nAFTER:  top={len(top)}, crypto={len(top_crypto)}, derivative={len(top_deriv)}")

    if len(top_crypto) > 0:
        print(f"\n✓ CRYPTO section will populate. Top 5 spot pairs:")
        for e in top_crypto[:5]:
            score = e.get("expert_adjusted_score") or e.get("best_score") or 0
            tier = e.get("liquidity_tier") or "—"
            gate = e.get("arm_gate_allow")
            gate_str = "✓" if gate else ("✗" if gate is False else "—")
            print(f"    {e.get('product_id')}: ${score:.2f}/day  tier={tier}  gate={gate_str}")
    else:
        print(f"\n✗ CRYPTO still empty after fresh scan.")
        print(f"  Bug is downstream of scanner_worker — the scan ran but produced 0 crypto.")
        print(f"  Check: is fetch_and_rank code path shipped correctly?")

    if len(top_deriv) > 0:
        print(f"\n✓ DERIVATIVE section top 5:")
        for e in top_deriv[:5]:
            score = e.get("expert_adjusted_score") or e.get("best_score") or 0
            tier = e.get("liquidity_tier") or "—"
            gate = e.get("arm_gate_allow")
            gate_str = "✓" if gate else ("✗" if gate is False else "—")
            print(f"    {e.get('product_id')}: ${score:.2f}/day  tier={tier}  gate={gate_str}")


if __name__ == "__main__":
    main()
