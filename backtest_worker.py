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

_SCANNER_ORDER_QUEUE = "silver-swing:scanner_order:queue"
_SCANNER_ORDER_REQ = "silver-swing:scanner_order:req:"
_SCANNER_ORDER_RES = "silver-swing:scanner_order:res:"

_LIVE_PORTFOLIO_QUEUE = "silver-swing:live_portfolio:queue"
_LIVE_PORTFOLIO_REQ = "silver-swing:live_portfolio:req:"
_LIVE_PORTFOLIO_RES = "silver-swing:live_portfolio:res:"

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
            # BRPOP over all queues in priority: scanner orders (user is placing
            # real money orders, respond fast), backtests (actively waiting),
            # candles (usually cache hits).
            item = r.brpop([_LIVE_PORTFOLIO_QUEUE, _SCANNER_ORDER_QUEUE,
                            _BT_QUEUE, _CANDLES_QUEUE], timeout=2)
            if item is None:
                continue
            queue_key, job_id = item
            if queue_key == _BT_QUEUE:
                _handle_backtest_job(r, job_id)
            elif queue_key == _CANDLES_QUEUE:
                _handle_candles_job(r, job_id)
            elif queue_key == _SCANNER_ORDER_QUEUE:
                _handle_scanner_order_job(r, job_id)
            else:
                _handle_live_portfolio_job(r, job_id)
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
        # Sub-day windows pass `minutes`; day-scale windows pass `days`. Prefer
        # minutes when both somehow arrive.
        minutes = req.get("minutes")
        days = int(req.get("days", 7))
    except Exception as e:
        r.set(res_key, json.dumps({"ok": False, "error": f"bad request: {e}"}), ex=_RESULT_TTL_SECS)
        r.delete(req_key)
        return

    if minutes is not None:
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = None

    range_key = f"m{minutes}" if minutes is not None else f"d{days}"
    cache_key = f"{_CANDLES_CACHE}{product_id}:{granularity}:{range_key}"
    window_label = f"{minutes}m" if minutes is not None else f"{days}d"
    cached = r.get(cache_key)
    if cached:
        r.set(res_key, cached, ex=_RESULT_TTL_SECS)
        r.delete(req_key)
        _log(f"candles {job_id}: cache hit {product_id} {window_label} {granularity}")
        return

    started = time.time()
    try:
        from backtest import fetch_candles
        client = _get_coinbase_client()
        end = datetime.now(timezone.utc)
        start = end - (timedelta(minutes=minutes) if minutes is not None else timedelta(days=days))
        candles = fetch_candles(client, product_id, start, end, granularity=granularity)
        # 2026-07-15: include volume. Payload cost is minor (one float per bar),
        # and unlocks VWAP / Anchored VWAP / CVD / volume-bar rendering on the
        # frontend chart. Old [ts, o, h, l, c] format still readable by
        # existing code — new consumers can index [5] for volume.
        packed = [[c.ts, c.open, c.high, c.low, c.close, getattr(c, "volume", 0) or 0]
                  for c in candles]
        result_payload = {
            "ok": True,
            "product_id": product_id,
            "granularity": granularity,
            "candles": packed,
            "generated_at": time.time(),
        }
        if minutes is not None:
            result_payload["minutes"] = minutes
        else:
            result_payload["days"] = days
        payload = json.dumps(result_payload)
        r.set(res_key, payload, ex=_RESULT_TTL_SECS)
        r.set(cache_key, payload, ex=_CANDLES_CACHE_TTL_SECS)
        r.delete(req_key)
        _log(f"candles {job_id}: {product_id} {window_label} {granularity} → {len(packed)} bars ({time.time()-started:.1f}s)")
    except Exception as e:
        r.set(res_key, json.dumps({
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
        }), ex=_RESULT_TTL_SECS)
        r.delete(req_key)
        _log(f"candles {job_id}: failed {type(e).__name__}: {e}")


def _handle_scanner_order_job(r, job_id: str) -> None:
    """One-shot market order for an arbitrary Coinbase futures product,
    executed straight from the scanner. Doesn't require a tracked strategy —
    the whole point is to let the user act on scanner picks without a
    pre-configured sleeve. LIVE places a real Coinbase order. PAPER simulates
    a fill and records it to a scanner-order log for review.
    """
    req_key = f"{_SCANNER_ORDER_REQ}{job_id}"
    res_key = f"{_SCANNER_ORDER_RES}{job_id}"
    raw = r.get(req_key)
    if not raw:
        _log(f"scanner_order {job_id}: request key missing — dropping")
        return
    try:
        req = json.loads(raw)
        product_id = str(req["product_id"])
        side = str(req["side"]).upper()
        qty = int(req["qty"])
        mode = str(req.get("mode") or "paper").lower()
        order_type = str(req.get("order_type") or "market").lower()
        limit_price = req.get("limit_price")
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        if qty < 1:
            raise ValueError(f"qty must be >= 1, got {qty}")
        if mode not in ("paper", "live", "lab"):
            raise ValueError(f"mode must be paper, lab, or live, got {mode!r}")
        if order_type not in ("market", "limit"):
            raise ValueError(f"order_type must be market or limit, got {order_type!r}")
        if order_type == "limit":
            lp = float(limit_price) if limit_price is not None else 0.0
            if lp <= 0:
                raise ValueError("limit_price must be > 0 for limit orders")
            limit_price = lp
    except Exception as e:
        r.set(res_key, json.dumps({"ok": False, "error": f"bad request: {e}"}), ex=_RESULT_TTL_SECS)
        r.delete(req_key)
        return

    started = time.time()
    _log(f"scanner_order {job_id}: {mode} {order_type} {side} {qty} {product_id}")
    try:
        if mode == "live":
            from broker import BrokerConfig, CoinbaseBroker
            broker = CoinbaseBroker(BrokerConfig(product_id=product_id))
            if order_type == "limit":
                order_id = broker.place_limit(side, qty, float(limit_price))
                msg = f"placed real LIMIT {side} {qty} {product_id} @ {limit_price} — order {order_id}"
            else:
                order_id = broker.place_market(side, qty)
                msg = f"placed real MARKET {side} {qty} {product_id} — order {order_id}"
            result = {
                "ok": True,
                "mode": "live",
                "product_id": product_id,
                "side": side,
                "qty": qty,
                "order_type": order_type,
                "limit_price": limit_price,
                "order_id": order_id,
                "message": msg,
            }
        else:
            # Paper "scanner order" is not managed by any strategy — it just
            # logs what would have happened so the user can see fills without
            # setting up a tracked symbol first. This is the honest tradeoff:
            # a real paper simulation would need to track the position over
            # time via a fresh WS feed, which is out of scope for MVP.
            detail = (f"at LIMIT ${limit_price:.4f}" if order_type == "limit"
                      else "at market")
            result = {
                "ok": True,
                "mode": "paper",
                "product_id": product_id,
                "side": side,
                "qty": qty,
                "order_type": order_type,
                "limit_price": limit_price,
                "message": (
                    f"[PAPER SIMULATED] would {side} {qty} {product_id} {detail}. "
                    "For persistent paper tracking, add this as a tracked symbol first."
                ),
            }
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    elapsed = time.time() - started
    result["_elapsed_secs"] = round(elapsed, 2)
    r.set(res_key, json.dumps(result), ex=_RESULT_TTL_SECS)
    r.delete(req_key)
    _log(f"scanner_order {job_id}: done in {elapsed:.1f}s ok={result.get('ok')}")


def _handle_live_portfolio_job(r, job_id: str) -> None:
    """On-demand live portfolio pull. Dashboard triggers this when the user
    opens the Live tab or clicks a derivative row so they see fresh data
    directly from Coinbase — not a 15-second-old cached snapshot.

    Returns the full portfolio_snapshot() dict so the frontend can render
    without waiting for the next background sync.
    """
    req_key = f"{_LIVE_PORTFOLIO_REQ}{job_id}"
    res_key = f"{_LIVE_PORTFOLIO_RES}{job_id}"
    raw = r.get(req_key)
    if not raw:
        _log(f"live_portfolio {job_id}: request key missing — dropping")
        return
    started = time.time()
    try:
        req = json.loads(raw) if raw else {}
        product_id = req.get("product_id") or _get_default_product()
        from broker import BrokerConfig, CoinbaseBroker
        broker = CoinbaseBroker(BrokerConfig(product_id=product_id))
        snap = broker.portfolio_snapshot()
        # Also expose per-product contract specs so the modal can render
        # correct swing width / per-contract math without another round trip.
        specs = {}
        for d in snap.get("derivatives") or []:
            pid = d.get("product_id")
            if not pid:
                continue
            try:
                specs[pid] = CoinbaseBroker(BrokerConfig(product_id=pid)).contract_spec()
            except Exception:
                pass
        result = {"ok": True, "portfolio": snap, "specs": specs}
    except Exception as e:
        result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    elapsed = time.time() - started
    result["_elapsed_secs"] = round(elapsed, 2)
    r.set(res_key, json.dumps(result), ex=_RESULT_TTL_SECS)
    r.delete(req_key)
    _log(f"live_portfolio {job_id}: done in {elapsed:.1f}s ok={result.get('ok')}")


def _get_default_product() -> str:
    import os
    return os.getenv("SWING_SYMBOL", "SLR-27AUG26-CDE")


def start(redis_url: str) -> threading.Event:
    """Spawn the worker as a daemon thread. Returns the stop event the caller
    can set to shut down (daemon=True means it also dies with the process).
    """
    stop_event = threading.Event()
    t = threading.Thread(target=_run_loop, args=(redis_url, stop_event), daemon=True, name="jobs-worker")
    t.start()
    _log("thread started")
    return stop_event
