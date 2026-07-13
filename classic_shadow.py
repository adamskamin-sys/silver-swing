"""Shadow-log emitter for classic indicators (RSI, Bollinger, MACD).

Shares the twitter/tape/ml Redis log so all shadow-signal sources
render in the same Signals tab with unified 1h/6h/24h outcome scoring.

SHADOW ONLY. EXECUTE_TRADES=False. No broker imports. Enforced by
tests/test_classic_shadow_only.py (mirrors twitter/tape/ml).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Optional


EXECUTE_TRADES = False

LOG_KEY = "silver-swing:twitter-signals"
MAX_LOG_ENTRIES = 500


def _redis_client(store):
    return getattr(store, "_r", None)


def _emit(store, symbol: str, source: str, direction: str,
          detail: str, mark: float, keywords_matched: list[str]) -> Optional[str]:
    """Write a shadow-log entry compatible with the twitter/tape/ml
    outcome evaluator. `source` distinguishes rsi@ / bollinger@ / macd@
    in the dashboard."""
    if direction not in ("bullish", "bearish"):
        return None
    record = {
        "id": str(uuid.uuid4()),
        "ts": time.time(),
        "source": source,
        "symbol": symbol,
        "tweet_url": "",
        "tweet_text": detail,
        "family": "classic_indicator",
        "direction": direction,
        "score": 1.0 if direction == "bullish" else -1.0,
        "keywords_matched": keywords_matched,
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


def emit_rsi_signal(store, symbol: str, rsi: float, mark: float,
                    oversold: float = 30.0, overbought: float = 70.0) -> Optional[str]:
    import classic_indicators
    direction = classic_indicators.rsi_signal(rsi, oversold, overbought)
    if direction is None:
        return None
    return _emit(
        store, symbol, "rsi@Wilder",
        direction,
        f"RSI(14)={rsi:.1f} → {direction} (oversold≤{oversold}, overbought≥{overbought})",
        mark, [f"rsi={rsi:.1f}"]
    )


def emit_bollinger_signal(store, symbol: str, price: float,
                          bands: tuple, mark: float) -> Optional[str]:
    import classic_indicators
    direction = classic_indicators.bollinger_signal(price, bands)
    if direction is None:
        return None
    lower, mid, upper = bands
    return _emit(
        store, symbol, "bollinger@Bollinger",
        direction,
        f"Bollinger(20,2): price=${price:.4g}, band=[${lower:.4g}, ${mid:.4g}, ${upper:.4g}] → {direction}",
        mark, [f"price={price:.4g}", f"mid={mid:.4g}"]
    )


def emit_macd_signal(store, symbol: str, macd_tuple: tuple, mark: float) -> Optional[str]:
    import classic_indicators
    direction = classic_indicators.macd_signal(macd_tuple)
    if direction is None:
        return None
    macd, sig, hist = macd_tuple
    return _emit(
        store, symbol, "macd@Appel",
        direction,
        f"MACD(12,26,9)={macd:.4f}, signal={sig:.4f}, hist={hist:.4f} → {direction}",
        mark, [f"macd={macd:.4f}"]
    )
