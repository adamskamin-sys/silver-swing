"""ML direction predictor — feature stream + shadow-mode placeholder model.

This is the FOUNDATION for a future LightGBM/XGBoost model. What ships now:

  1. Feature extractor: given a store snapshot, compute a fixed feature
     vector (VPIN, OFI, ATR, aggressor run, session hour, recent returns).
     Fixed schema documented below so training data can be assembled from
     the tick_recorder logs.

  2. Placeholder linear predictor: hand-tuned coefficients that combine
     the features into a bullish/bearish score in [-1, +1]. This is NOT
     a trained model — it's a rules-based composite of the microstructure
     signals we already trust, packaged with the same interface a real
     model would use. When a trained model is dropped in later, only the
     `predict()` function body changes; everything else (feature extraction,
     shadow logging, evaluation harness) stays the same.

  3. Shadow-mode signal emission: when |score| crosses a threshold, log a
     shadow signal in the same Redis harness as twitter + tape. Outcomes
     at 1h/6h/24h feed accuracy stats — evaluate before promoting to a
     live gate.

SHADOW ONLY — same hard guarantees as twitter_scanner and tape_shadow.
No broker imports, no order-placing calls. Enforced by
tests/test_ml_predictor_shadow_only.py.

Feature schema (order MUST stay stable across training runs):
    [0] vpin           — VPIN toxicity in [0, 1], 0.5 = neutral
    [1] trade_ofi_60s  — signed trade OFI in [-1, +1]
    [2] trade_ofi_300s — same over 5-min window
    [3] atr_normalized — recent ATR / price (unitless volatility)
    [4] aggressor_side_int — +1 buyers dominant, -1 sellers, 0 no run
    [5] session_hour_us — 1 if UTC 13-21, else 0 (Livermore US-session)
    [6] recent_return_1m — log return over last 60s
    [7] recent_return_5m — log return over last 300s
"""

from __future__ import annotations

import math
import time
import uuid
from typing import Optional


# Guardrail — mirrors twitter_scanner / tape_shadow.
EXECUTE_TRADES = False


FEATURE_NAMES = [
    "vpin", "trade_ofi_60s", "trade_ofi_300s",
    "atr_normalized", "aggressor_side_int",
    "session_hour_us",
    "recent_return_1m", "recent_return_5m",
]


def extract_features(snapshot: dict) -> Optional[list[float]]:
    """Build the fixed-schema feature vector from a store snapshot.

    Returns None if the snapshot lacks the minimum data needed (mark +
    microstructure) — permissive default for the caller.
    """
    if not snapshot:
        return None
    mark = float(snapshot.get("last_mark") or 0)
    ms = snapshot.get("microstructure") or {}
    vpin = ms.get("vpin")
    ofi60 = ms.get("trade_ofi_60s")
    ofi300 = ms.get("trade_ofi_300s")
    agg = (ms.get("aggressor_run") or {})
    agg_side = agg.get("current_side")
    agg_run = int(agg.get("current_run") or 0)
    threshold = int(agg.get("threshold") or 8)
    if agg_run >= threshold and agg_side == "buy":
        agg_int = 1
    elif agg_run >= threshold and agg_side == "sell":
        agg_int = -1
    else:
        agg_int = 0
    # ATR normalized by price. If snapshot has expert_params.atr, use it.
    atr = 0.0
    ep = snapshot.get("expert_params") or {}
    if isinstance(ep, dict):
        try:
            atr = float(ep.get("atr") or 0)
        except (TypeError, ValueError):
            atr = 0.0
    atr_norm = (atr / mark) if (atr > 0 and mark > 0) else 0.0
    # Session hour (Livermore US-session = full weight)
    hour_utc = time.gmtime().tm_hour
    session_hour_us = 1.0 if 13 <= hour_utc <= 21 else 0.0
    # Recent returns from price_history
    ph = snapshot.get("price_history") or []
    ret_1m = _return_over(ph, 60.0)
    ret_5m = _return_over(ph, 300.0)
    if vpin is None and ofi60 is None and ofi300 is None:
        # No microstructure data at all — feature vector would be too weak
        # to predict meaningfully. Return None so caller skips this tick.
        return None
    return [
        float(vpin) if vpin is not None else 0.5,
        float(ofi60) if ofi60 is not None else 0.0,
        float(ofi300) if ofi300 is not None else 0.0,
        atr_norm,
        float(agg_int),
        session_hour_us,
        ret_1m,
        ret_5m,
    ]


def _return_over(price_history: list, window_secs: float) -> float:
    """Log return from oldest-in-window to most-recent. 0.0 if insufficient."""
    if not price_history or len(price_history) < 2:
        return 0.0
    cutoff = time.time() - window_secs
    oldest_in_window = None
    newest = None
    for entry in price_history:
        try:
            ts = float(entry[0])
            px = float(entry[1])
        except (TypeError, ValueError, IndexError):
            continue
        if px <= 0:
            continue
        if ts >= cutoff and oldest_in_window is None:
            oldest_in_window = px
        newest = px
    if oldest_in_window is None or newest is None or oldest_in_window <= 0:
        return 0.0
    return math.log(newest / oldest_in_window)


# Placeholder linear coefficients. These are NOT trained — they're hand-set
# from expert signal expectations (bullish OFI + bullish aggressor run +
# US session = bullish; high VPIN = uncertainty penalty). Structure stays
# stable so replacing with a trained model is a one-line swap.
_PLACEHOLDER_WEIGHTS: dict[str, float] = {
    "vpin":              -0.30,  # high toxicity penalizes confidence
    "trade_ofi_60s":      0.40,  # strong short-term direction predictor
    "trade_ofi_300s":     0.20,  # medium-term confirmation
    "atr_normalized":     0.00,  # neutral — high vol isn't inherently directional
    "aggressor_side_int": 0.25,  # tape confirmation of direction
    "session_hour_us":    0.05,  # small US-session boost (Livermore)
    "recent_return_1m":  10.0,   # momentum (log returns are small — scale up)
    "recent_return_5m":   5.0,
}


_TRAINED_MODEL_CACHE = {"model": None, "checked": False}
_TRAINED_MODEL_PATH = "data/ml_model.json"


def _load_trained_model_once():
    """Attempt to load a trained model from disk once per process. Cached
    result — bot restarts to pick up a new model. Returns None if the
    file doesn't exist (predict falls back to placeholder weights)."""
    if _TRAINED_MODEL_CACHE["checked"]:
        return _TRAINED_MODEL_CACHE["model"]
    try:
        from ml_training import load_model
        m = load_model(_TRAINED_MODEL_PATH)
        if m and m.get("weights"):
            _TRAINED_MODEL_CACHE["model"] = m
    except Exception:
        pass
    _TRAINED_MODEL_CACHE["checked"] = True
    return _TRAINED_MODEL_CACHE["model"]


def predict(features: list[float]) -> float:
    """Returns a score in ~[-1, +1] where positive is bullish.

    Prefers a trained model if data/ml_model.json exists (López de Prado
    training pipeline in ml_training.py produces this). Falls back to
    the placeholder linear weights when no trained model is available.
    Interface stays stable so a future LightGBM/XGBoost trained model
    is a drop-in swap — same features in, same score out."""
    if not features or len(features) != len(FEATURE_NAMES):
        return 0.0
    trained = _load_trained_model_once()
    if trained and trained.get("weights"):
        # Trained logistic regression: sigmoid(w·x + b), then map to [-1, +1]
        w = trained["weights"]
        b = float(trained.get("bias") or 0.0)
        if len(w) == len(features):
            score = b + sum(w[i] * features[i] for i in range(len(w)))
            # Map score → tanh for consistent range with placeholder path
            return math.tanh(score)
    # Placeholder linear path
    score = 0.0
    for name, val in zip(FEATURE_NAMES, features):
        w = _PLACEHOLDER_WEIGHTS.get(name, 0.0)
        if name == "vpin":
            val = val - 0.5
        score += w * val
    return math.tanh(score)


def emit_ml_shadow_signal(
    store,
    tenant: str,
    symbol: str,
    features: list[float],
    score: float,
    baseline_mark: float,
) -> Optional[str]:
    """Write a shadow-log entry compatible with the twitter_scanner /
    tape_shadow harness. Same LOG_KEY, so the Signals tab renders all
    three source types (twitter@ / tape@ / ml@) side-by-side with
    unified 1h/6h/24h outcome scoring."""
    import json
    LOG_KEY = "silver-swing:twitter-signals"
    MAX_LOG_ENTRIES = 500
    direction = "bullish" if score > 0 else ("bearish" if score < 0 else "neutral")
    if direction == "neutral":
        return None
    record = {
        "id": str(uuid.uuid4()),
        "ts": time.time(),
        "source": "ml@PlaceholderLinear",
        "symbol": symbol,
        "tweet_url": "",
        "tweet_text": (f"ML predictor: score={score:+.3f} "
                       f"(features={dict(zip(FEATURE_NAMES, [round(f, 4) for f in features]))})"),
        "family": "ml",
        "direction": direction,
        "score": round(float(score), 4),
        "keywords_matched": FEATURE_NAMES,
        "would_action": "ALERT_ARM" if direction == "bullish" else "EXIT_HINT",
        "products_affected": [symbol],
        "baseline_marks": {symbol: float(baseline_mark)},
        "outcomes": {"1h": None, "6h": None, "24h": None},
        "shadow_mode": True,
        "trades_executed": False,
        "model_version": "placeholder-linear-v1",
        "features": dict(zip(FEATURE_NAMES, features)),
    }
    r = getattr(store, "_r", None)
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
