"""
backtest_worker.py — background thread that services jobs the dashboard posts
to Redis. Handles two job types today:

  1. backtest — run a strategy over historical candles, return metrics.
  2. candles  — fetch historical candles for the scanner's chart modal.

Why one thread for both? The dashboard runs on a Node-only Render service (no
Python + no Coinbase creds). The Python paper worker has both, so we route
the work there via Redis. Same thread + same import graph = simpler ops.

Queue shapes:
  silver-swing:backtest:queue        LIST   FIFO of job ids
  silver-swing:backtest:req:<id>     STRING request JSON
  silver-swing:backtest:res:<id>     STRING result JSON

  silver-swing:candles:queue         LIST   FIFO of job ids
  silver-swing:candles:req:<id>      STRING request JSON
  silver-swing:candles:res:<id>      STRING result JSON

Candles are also cached by (product_id, granularity, days) at
  silver-swing:candles:cache:<product>:<gran>:<days>       TTL 60s
so a chart re-open within a minute doesn't re-hit Coinbase.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone


_BT_QUEUE = "silver-swing:backtest:queue"
_BT_REQ = "silver-swing:backtest:req:"
_BT_RES = "silver-swing:backtest:res:"

_CANDLES_QUEUE = "silver-swing:candles:queue"
_CANDLES_REQ = "silver-swing:candles:req:"
_CANDLES_RES = "silver-swing:candles:res:"
_CANDLES_CACHE = "silver-swing:candles:cache:"
_CANDLES_CACHE_TTL_SECS = 60

_RESULT_TTL_SECS = 300


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] jobs-worker: {msg}", flush=True)


def _run_loop(redis_url: str, stop_event: threading.Event) -> None:
    import redis

    backoff = 1.0
    r = None
    while not stop_event.is_set():
        try:
            if r is None:
                r = redis.Redis.from_url(redis_url, decode_responses=True)
                r.ping()
                _log("connected to Redis, watching backtest + candles queues")
                backoff = 1.0
            # BRPOP over both queues in priority order (backtest first — user is
            # actively waiting on those; candle fetches are usually cache hits).
            item = r.brpop([_BT_QUEUE, _CANDLES_QUEUE], timeout=2)
            if item is None:
                continue
            queue_key, job_id = item
            if queue_key == _BT_QUEUE:
                _handle_backtest_job(r, job_id)
            else:
                _handle_candles_job(r, job_id)
        except Exception as e:
            _log(f"loop error ({type(e).__name__}: {e}) — reconnecting in {backoff:.1f}s")
            r = None
            stop_event.wait(backoff)
            backoff = min(backoff * 2, 30.0)


def _handle_backtest_job(r, job_id: str) -> None:
    req_key = f"{_BT_REQ}{job_id}"
    res_key = f"{_BT_RES}{job_id}"
    raw = r.get(req_key)
    if not raw:
        _log(f"backtest {job_id}: request key missing — dropping")
        return
    try:
        req = json.loads(raw)
    except Exception as e:
        r.set(res_key, json.dumps({"ok": False, "error": f"bad request json: {e}"}), ex=_RESULT_TTL_SECS)
        r.delete(req_key)
        return

    started = time.time()
    _log(f"backtest {job_id}: {req.get('symbol')} {req.get('days')}d {req.get('granularity')} {req.get('mode')}")
    from scripts.run_backtest import execute
    result = execute(req)
    elapsed = time.time() - started
    result["_elapsed_secs"] = round(elapsed, 2)

    r.set(res_key, json.dumps(result), ex=_RESULT_TTL_SECS)
    r.delete(req_key)
    _log(f"backtest {job_id}: done in {elapsed:.1f}s ok={result.get('ok')}")


_coinbase_client_cache = {"client": None}


def _get_coinbase_client():
    """Reuse a single CoinbaseBroker across candle jobs — the SDK client is
    stateless enough that one instance handles concurrent product_ids fine,
    and creating one per call thrashes JWT signing."""
    if _coinbase_client_cache["client"] is None:
        from broker import BrokerConfig, CoinbaseBroker
        # product_id is required by BrokerConfig but the client itself is
        # product-agnostic; any valid symbol works as the seed.
        _coinbase_client_cache["client"] = CoinbaseBroker(BrokerConfig(product_id="SLR-27AUG26-CDE")).client
    return _coinbase_client_cache["client"]


def _handle_candles_job(r, job_id: str) -> None:
    req_key = f"{_CANDLES_REQ}{job_id}"
    res_key = f"{_CANDLES_RES}{job_id}"
    raw = r.get(req_key)
    if not raw:
        _log(f"candles {job_id}: request key missing — dropping")
        return
    try:
        req = json.loads(raw)
        product_id = req["product_id"]
        granularity = req.get("granularity", "FIVE_MINUTE")
        days = int(req.get("days", 7))
    except Exception as e:
        r.set(res_key, json.dumps({"ok": False, "error": f"bad request: {e}"}), ex=_RESULT_TTL_SECS)
        r.delete(req_key)
        return

    cache_key = f"{_CANDLES_CACHE}{product_id}:{granularity}:{days}"
    cached = r.get(cache_key)
    if cached:
        r.set(res_key, cached, ex=_RESULT_TTL_SECS)
        r.delete(req_key)
        _log(f"candles {job_id}: cache hit {product_id} {days}d {granularity}")
        return

    started = time.time()
    try:
        from backtest import fetch_candles
        client = _get_coinbase_client()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        candles = fetch_candles(client, product_id, start, end, granularity=granularity)
        # Compact form: [ts, open, high, low, close] tuples. Skip volume for size.
        packed = [[c.ts, c.open, c.high, c.low, c.close] for c in candles]
        payload = json.dumps({
            "ok": True,
            "product_id": product_id,
            "granularity": granularity,
            "days": days,
            "candles": packed,
            "generated_at": time.time(),
        })
        r.set(res_key, payload, ex=_RESULT_TTL_SECS)
        r.set(cache_key, payload, ex=_CANDLES_CACHE_TTL_SECS)
        r.delete(req_key)
        _log(f"candles {job_id}: {product_id} {days}d {granularity} → {len(packed)} bars ({time.time()-started:.1f}s)")
    except Exception as e:
        r.set(res_key, json.dumps({
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
        }), ex=_RESULT_TTL_SECS)
        r.delete(req_key)
        _log(f"candles {job_id}: failed {type(e).__name__}: {e}")


def start(redis_url: str) -> threading.Event:
    """Spawn the worker as a daemon thread. Returns the stop event the caller
    can set to shut down (daemon=True means it also dies with the process).
    """
    stop_event = threading.Event()
    t = threading.Thread(target=_run_loop, args=(redis_url, stop_event), daemon=True, name="jobs-worker")
    t.start()
    _log("thread started")
    return stop_event
