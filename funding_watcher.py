"""Funding sign-flip watcher — periodic scan for crypto perp funding
crossovers, emit shadow-log signals into the same Signals tab as
twitter / tape / ml / classic indicators.

Uses the last-seen funding rate cached per product in Redis. When the
current funding rate has flipped sign (positive→negative or vice
versa) since the last check, emit a signal:
  - Positive → Negative: BULLISH (we now get paid to hold long)
  - Negative → Positive: BEARISH (paying to hold long becomes expensive)

SHADOW ONLY. EXECUTE_TRADES=False. No broker imports.
Aksoy-Cheng-Hasbrouck (2018) rationale: funding sign flips + extremes
are strong short-term direction predictors.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Optional


EXECUTE_TRADES = False

LOG_KEY = "silver-swing:twitter-signals"
STATE_KEY_PREFIX = "silver-swing:funding-last:"
MAX_LOG_ENTRIES = 500


def _redis_client(store):
    return getattr(store, "_r", None)


def _last_seen_rate(store, symbol: str) -> Optional[float]:
    r = _redis_client(store)
    if not r:
        return None
    try:
        v = r.get(STATE_KEY_PREFIX + symbol)
        return float(v) if v is not None else None
    except Exception:
        return None


def _record_last_seen(store, symbol: str, rate: float) -> None:
    r = _redis_client(store)
    if not r:
        return
    try:
        r.set(STATE_KEY_PREFIX + symbol, str(rate))
        r.expire(STATE_KEY_PREFIX + symbol, 3 * 24 * 3600)  # 3d TTL
    except Exception:
        pass


def _emit_flip_signal(
    store,
    symbol: str,
    prev_rate: float,
    curr_rate: float,
    mark: float,
) -> Optional[str]:
    """Emit shadow signal for a funding sign flip."""
    direction = "bullish" if curr_rate < 0 else "bearish"
    detail = (f"Funding flipped {prev_rate:+.5f} → {curr_rate:+.5f} "
              f"({'shorts pay longs now — free carry' if direction == 'bullish' else 'longs pay shorts — expensive carry begins'})")
    record = {
        "id": str(uuid.uuid4()),
        "ts": time.time(),
        "source": "funding@AksoyChengHasbrouck",
        "symbol": symbol,
        "tweet_url": "",
        "tweet_text": detail,
        "family": "funding",
        "direction": direction,
        "score": abs(curr_rate) / 0.001,  # magnitude in "0.1%" units
        "keywords_matched": [f"funding={curr_rate:+.5f}", f"prev={prev_rate:+.5f}"],
        "would_action": "ALERT_ARM" if direction == "bullish" else "EXIT_HINT",
        "products_affected": [symbol],
        "baseline_marks": {symbol: float(mark)},
        "outcomes": {"1h": None, "6h": None, "24h": None},
        "shadow_mode": True,
        "trades_executed": False,
    }
    r = _redis_client(store)
    if r:
        try:
            r.lpush(LOG_KEY, json.dumps(record))
            r.ltrim(LOG_KEY, 0, MAX_LOG_ENTRIES - 1)
            return record["id"]
        except Exception:
            return None
    try:
        blob = store.get_config("__twitter__", "__log__") or {"entries": []}
        entries = list(blob.get("entries", []))
        entries.insert(0, record)
        entries = entries[:MAX_LOG_ENTRIES]
        store.put_config("__twitter__", "__log__", {"entries": entries})
        return record["id"]
    except Exception:
        return None


def tick(store, tenant: str = "__default__") -> dict:
    """Scan every perp product in every tenant, detect funding sign flips,
    emit shadow signals. Meant to be called every ~5 min from live_runner.

    Returns telemetry: {perps_scanned, flips_detected, last_rates}.
    """
    import funding_signals as _fs
    perps_scanned = 0
    flips = 0
    for t in store.list_tenants():
        for sym in store.list_symbols(t):
            if sym.startswith("__") or not _fs.is_perp(sym):
                continue
            snap = store.get_snapshot(t, sym) or {}
            curr_rate = _fs.funding_rate_of(snap)
            if curr_rate is None:
                continue
            perps_scanned += 1
            prev_rate = _last_seen_rate(store, sym)
            _record_last_seen(store, sym, curr_rate)
            # Only emit on sign flip (not first observation).
            if prev_rate is None:
                continue
            if (prev_rate > 0) != (curr_rate > 0):
                mark = float(snap.get("last_mark") or 0)
                _emit_flip_signal(store, sym, prev_rate, curr_rate, mark)
                flips += 1
    return {"perps_scanned": perps_scanned, "flips_detected": flips}
