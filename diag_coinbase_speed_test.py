"""Empirical Coinbase REST speed test.

Measures how fast we can hit various Coinbase Advanced Trade API
endpoints before we start getting rate-limit errors (429) or other
failures. Informs the choice of SWING_LOOP_INTERVAL and the
per-endpoint throttle in the Phase 3 adaptive rate-limit controller.

Coinbase documented limits (as of 2024):
  - Public endpoints:  10 req/sec per IP
  - Private endpoints: 30 req/sec per IP (higher for verified/institutional)
  - Order placement:   30 req/sec, ~500 orders/hr

But documented limits don't always match reality — real burst behavior
can be higher (short spikes tolerated) or lower (soft-throttled without
429s, just latency). This test finds the ACTUAL limit for Adam's account.

Usage (Render silver-swing-bot-live shell):
    python3 diag_coinbase_speed_test.py
    python3 diag_coinbase_speed_test.py --long   # runs the full 60s sweep

Read-only. Only calls GET endpoints (no orders placed, no state
modified). Runs for ~30 seconds by default.
"""
from __future__ import annotations
import argparse
import os
import sys
import time


PROBE_PRODUCT = "SLR-27AUG26-CDE"  # public endpoint probe target


def _time_request(fn, *args, **kwargs) -> tuple[float, bool, str]:
    """Run fn(*args, **kwargs), return (elapsed_ms, ok, error_str)."""
    t0 = time.perf_counter()
    try:
        fn(*args, **kwargs)
        return ((time.perf_counter() - t0) * 1000.0, True, "")
    except Exception as e:
        return ((time.perf_counter() - t0) * 1000.0, False, f"{type(e).__name__}: {e}")


def _burst_test(fn, label: str, n_requests: int, sleep_between_s: float) -> dict:
    """Fire n_requests calls to fn spaced by sleep_between_s. Report:
      - success rate
      - avg / min / max latency
      - any 429s / errors
    """
    latencies = []
    successes = 0
    errors_by_type = {}
    t_start = time.perf_counter()
    for i in range(n_requests):
        elapsed_ms, ok, err = _time_request(fn)
        if ok:
            successes += 1
            latencies.append(elapsed_ms)
        else:
            key = err[:80]
            errors_by_type[key] = errors_by_type.get(key, 0) + 1
        if sleep_between_s > 0 and i < n_requests - 1:
            time.sleep(sleep_between_s)
    duration = time.perf_counter() - t_start
    actual_rate = n_requests / duration if duration > 0 else 0

    result = {
        "label": label,
        "n_requests": n_requests,
        "target_rate_per_sec": (1.0 / sleep_between_s) if sleep_between_s > 0 else float("inf"),
        "actual_rate_per_sec": round(actual_rate, 2),
        "successes": successes,
        "failures": n_requests - successes,
        "success_pct": round(100.0 * successes / n_requests, 1) if n_requests else 0,
        "duration_secs": round(duration, 2),
    }
    if latencies:
        latencies.sort()
        result["latency_ms"] = {
            "min": round(latencies[0], 1),
            "median": round(latencies[len(latencies) // 2], 1),
            "avg": round(sum(latencies) / len(latencies), 1),
            "p95": round(latencies[int(len(latencies) * 0.95)], 1) if len(latencies) > 20 else round(latencies[-1], 1),
            "max": round(latencies[-1], 1),
        }
    if errors_by_type:
        result["errors"] = errors_by_type
    return result


def _print_result(r: dict) -> None:
    print(f"\n--- {r['label']} ---")
    print(f"  requests:    {r['n_requests']}  ({r['successes']} ok, {r['failures']} fail — {r['success_pct']}% success)")
    print(f"  target rate: {r['target_rate_per_sec']:.2f} req/s")
    print(f"  actual rate: {r['actual_rate_per_sec']:.2f} req/s (over {r['duration_secs']}s)")
    if "latency_ms" in r:
        l = r["latency_ms"]
        print(f"  latency:     min={l['min']}  median={l['median']}  avg={l['avg']}  p95={l['p95']}  max={l['max']}  ms")
    if "errors" in r:
        print(f"  errors ({sum(r['errors'].values())} total):")
        for msg, count in sorted(r['errors'].items(), key=lambda x: -x[1]):
            print(f"    [{count}x] {msg}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--long", action="store_true",
                    help="Run the full 60s sweep (default: 30s quick test)")
    args = ap.parse_args()

    print("=" * 70)
    print("COINBASE ADVANCED TRADE — EMPIRICAL SPEED TEST")
    print("=" * 70)
    print(f"Probe product: {PROBE_PRODUCT}")
    print(f"Duration:      {'~60s (long)' if args.long else '~30s (quick)'}")
    print()

    try:
        from broker import CoinbaseBroker, BrokerConfig
        broker = CoinbaseBroker(BrokerConfig(product_id=PROBE_PRODUCT))
    except Exception as e:
        print(f"BROKER INIT FAILED: {type(e).__name__}: {e}")
        sys.exit(1)

    # Endpoints to probe. Each is a zero-arg callable via lambda.
    probes = [
        ("get_product (public)",
         lambda: broker.client.get_product(PROBE_PRODUCT)),
        ("get_candles (public, 60 bars)",
         lambda: broker.client.get_candles(
             product_id=PROBE_PRODUCT,
             start=str(int(time.time()) - 3600),
             end=str(int(time.time())),
             granularity="ONE_MINUTE",
         )),
        ("get_accounts (private)",
         lambda: broker.client.get_accounts(limit=1)),
    ]

    # Sweep of rates. Rate = 1 / sleep_between. E.g., 0.1s → 10 req/s.
    sweeps = [
        (10,  0.5,   "5 req/s (conservative)"),
        (20,  0.2,   "5 req/s → 10 req/s (2× coinbase public doc limit)"),
        (30,  0.1,   "10 req/s (at coinbase public doc limit)"),
        (30,  0.05,  "20 req/s (2× doc limit — burst test)"),
    ]
    if args.long:
        sweeps.append((60, 0.033, "30 req/s (3× public doc / at private doc)"))
        sweeps.append((60, 0.02,  "50 req/s (extreme burst — expect throttling)"))

    print("Warmup call to each endpoint...")
    for label, fn in probes:
        elapsed_ms, ok, err = _time_request(fn)
        status = "OK" if ok else f"FAIL ({err[:60]})"
        print(f"  {label}: {status} ({elapsed_ms:.0f}ms)")

    print("\n" + "=" * 70)
    print("BURST TESTS PER ENDPOINT")
    print("=" * 70)

    findings = []
    for probe_label, fn in probes:
        print(f"\n{'=' * 70}")
        print(f"ENDPOINT: {probe_label}")
        print("=" * 70)
        for n, sleep_s, sweep_label in sweeps:
            print(f"\nSweep: {sweep_label}  ({n} requests, {sleep_s}s between)")
            result = _burst_test(fn, sweep_label, n, sleep_s)
            _print_result(result)
            findings.append({"endpoint": probe_label, **result})
            # Back off between sweeps to reset any tokens/counters
            time.sleep(2.0)
            # Abort remaining sweeps for this endpoint if success rate is bad
            if result["success_pct"] < 80:
                print(f"\n  WARNING: success rate dropped below 80% at {sweep_label}. Stopping sweeps for this endpoint.")
                break

    # ---- Summary
    print("\n" + "=" * 70)
    print("SUMMARY — MAX SUSTAINED RATE PER ENDPOINT (100% success required)")
    print("=" * 70)
    by_endpoint = {}
    for f in findings:
        by_endpoint.setdefault(f["endpoint"], []).append(f)
    for endpoint, results in by_endpoint.items():
        # Highest rate with 100% success
        clean = [r for r in results if r["success_pct"] == 100.0]
        if clean:
            best = max(clean, key=lambda r: r["actual_rate_per_sec"])
            print(f"  {endpoint}: {best['actual_rate_per_sec']:.2f} req/s  ({best['label']})")
        else:
            print(f"  {endpoint}: no clean burst — throttled at all tested rates")

    print("\n" + "=" * 70)
    print("RECOMMENDATION")
    print("=" * 70)
    print("Use the LOWEST of the per-endpoint max rates as the safe global")
    print("REST call budget. The 0.25s SWING_LOOP_INTERVAL corresponds to 4")
    print("loop iterations/sec; each iteration currently makes at most ~1")
    print("private REST call (portfolio_snapshot every SNAPSHOT_INTERVAL).")
    print("At 0.25s loop + 5s snapshot interval, effective REST rate is well")
    print("under 1 req/s. Room to push loop cadence 5-10× tighter if needed,")
    print("provided the adaptive controller (Phase 3, not yet built) can back")
    print("off gracefully on 429s.")


if __name__ == "__main__":
    main()
