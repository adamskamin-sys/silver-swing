"""Aggressor-run shadow signal harness.

Piggybacks on twitter_scanner's shadow log format so the dashboard's
Signals tab renders BOTH twitter and tape signals side-by-side with
matching outcome evaluation at 1h/6h/24h.

SHADOW MODE — HARD GUARANTEE (mirrors twitter_scanner):
This module NEVER calls broker.place_limit or any order-placing API.
The AggressorRunDetector callback lives in the SwingTrader's tick path,
so it CAN see the broker, but this file's public API only writes to
Redis / the state store. Enforced by tests/test_tape_shadow_only.py.

Signal shape (compatible with twitter_scanner._evaluate_outcomes):
    {
      "id": uuid,
      "ts": epoch,
      "source": "tape@AggressorRun",
      "symbol": "SLR-27AUG26-CDE",
      "direction": "bullish" | "bearish",
      "run_length": 12,
      "aggressor_side": "buy" | "sell",
      "trigger_price": 30.51,
      "products_affected": ["SLR-27AUG26-CDE"],
      "baseline_marks": {"SLR-27AUG26-CDE": 30.51},
      "outcomes": {"1h": None, "6h": None, "24h": None},
      "shadow_mode": True,
      "trades_executed": False
    }
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Optional


# Guardrail — mirrors twitter_scanner.
EXECUTE_TRADES = False


# Reuse the twitter shadow log key so both signal types render in the same
# Signals tab with matching outcome evaluation. The `source` field
# distinguishes twitter@handle vs tape@AggressorRun in the UI.
LOG_KEY = "silver-swing:twitter-signals"
MAX_LOG_ENTRIES = 500


def _redis_client_from_store(store):
    """Extract redis client from RedisJsonStore, returns None otherwise."""
    return getattr(store, "_r", None)


def emit_aggressor_run_signal(
    store,
    tenant: str,
    symbol: str,
    crossing: dict,
    baseline_mark: Optional[float] = None,
) -> Optional[str]:
    """Called by the SwingTrader when AggressorRunDetector fires a crossing.

    crossing = {ts, side, run_length, price} from microstructure.AggressorRunDetector.

    Returns the entry id if written, None on error / no store.
    """
    if not crossing or not store:
        return None
    side = str(crossing.get("side") or "").lower()
    if side not in ("buy", "sell"):
        return None
    direction = "bullish" if side == "buy" else "bearish"
    trigger_price = float(crossing.get("price") or 0.0)
    # Baseline mark falls back to the trigger price if not passed explicitly.
    if baseline_mark is None or baseline_mark <= 0:
        baseline_mark = trigger_price if trigger_price > 0 else 0.0
    record = {
        "id": str(uuid.uuid4()),
        "ts": float(crossing.get("ts") or time.time()),
        "source": "tape@AggressorRun",
        "symbol": symbol,
        "tweet_url": "",  # keep the field so the dashboard renders "—" cleanly
        "tweet_text": (f"Aggressor run: {crossing.get('run_length')} consecutive "
                       f"{side.upper()}-side prints at ${trigger_price:.6g}"),
        "family": "tape",
        "direction": direction,
        "score": int(crossing.get("run_length") or 0),
        "keywords_matched": [f"run_length={crossing.get('run_length')}", side],
        "would_action": "ALERT_ARM" if direction == "bullish" else "EXIT_HINT",
        "aggressor_side": side,
        "trigger_price": trigger_price,
        "run_length": int(crossing.get("run_length") or 0),
        "products_affected": [symbol],
        "baseline_marks": {symbol: float(baseline_mark)},
        "outcomes": {"1h": None, "6h": None, "24h": None},
        "shadow_mode": True,
        "trades_executed": False,
    }
    r = _redis_client_from_store(store)
    if r:
        try:
            r.lpush(LOG_KEY, json.dumps(record))
            r.ltrim(LOG_KEY, 0, MAX_LOG_ENTRIES - 1)
            return record["id"]
        except Exception:
            return None
    # Fallback: state-store config scope, same shape as twitter_scanner uses
    try:
        blob = store.get_config("__twitter__", "__log__") or {"entries": []}
        entries = list(blob.get("entries", []))
        entries.insert(0, record)
        entries = entries[:MAX_LOG_ENTRIES]
        store.put_config("__twitter__", "__log__", {"entries": entries})
        return record["id"]
    except Exception:
        return None
