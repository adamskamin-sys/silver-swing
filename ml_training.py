"""Marcos López de Prado — Advances in Financial Machine Learning — training pipeline.

Replaces the hand-tuned linear weights in ml_predictor with a proper trained
model, using the shadow-log entries as our labeled dataset. Every shadow
signal we've emitted (twitter, tape, ML placeholder) has an `outcomes` dict
recording 1h/6h/24h price moves — those are the LABELS we train against.

López de Prado's key contributions we apply:

  1. Purged k-fold CV — training folds are separated from test folds by a
     gap ≥ label horizon, preventing "future leakage" where a train sample
     from t+30min is used to predict a test sample from t+60min.

  2. Meta-labeling — instead of predicting price direction (hard, noisy),
     predict whether a first-pass model's prediction is CORRECT. Layered
     structure: primary model produces raw scores; meta-model produces
     a confidence-weighted filter.

  3. Fractional differentiation — instead of first differences (stationarity
     at the cost of memory), fractional differencing preserves long-memory.
     López de Prado ch. 5. Not implemented in v1 — future upgrade.

This module ships:
  - training data assembly from the shadow log
  - purged k-fold CV
  - simple logistic regression training (numpy-only, no lightgbm dep)
  - model persistence to JSON (feature weights + bias)
  - ml_predictor auto-loads the trained model if it exists

Upgradeable to LightGBM/XGBoost later without breaking anything —
extract_features + predict interfaces stay stable.

Usage from a scheduled task or manual run:
    python -m ml_training --train --model_path data/ml_model.json
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Optional


MODEL_PATH_DEFAULT = "data/ml_model.json"
LOG_KEY = "silver-swing:twitter-signals"


def load_training_pairs(shadow_log_entries: list[dict],
                        horizon: str = "1h") -> list[tuple[list[float], float]]:
    """Extract (features, label) pairs from shadow log entries.

    label = +1 if the horizon outcome verdict is 'correct', -1 if 'wrong',
    else the entry is skipped (flat / unknown / pending).

    Only entries with `features` key (ML predictor's own emissions) are
    usable, since twitter/tape entries don't carry the feature vector.
    """
    pairs = []
    for e in shadow_log_entries or []:
        feats = e.get("features")
        if not feats or not isinstance(feats, dict):
            continue
        outs = (e.get("outcomes") or {}).get(horizon)
        if not outs:
            continue
        verdict = outs.get("verdict")
        if verdict == "correct":
            label = 1.0
        elif verdict == "wrong":
            label = -1.0
        else:
            continue
        try:
            from ml_predictor import FEATURE_NAMES
            fv = [float(feats.get(name) or 0.0) for name in FEATURE_NAMES]
        except Exception:
            continue
        pairs.append((fv, label))
    return pairs


def purged_kfold_indices(n: int, k: int = 5, gap: int = 5) -> list[tuple[list[int], list[int]]]:
    """Return k folds, each (train_indices, test_indices), where test is
    a contiguous chunk and train excludes the test chunk PLUS a gap of
    `gap` samples on each side (López de Prado purge).

    Prevents nearby-in-time samples from leaking future information into
    training. Standard k-fold's naive shuffle allows this leakage.
    """
    if n < k:
        # Not enough data for k-fold; return a single trivial split
        return [(list(range(n)), [])]
    fold_size = n // k
    folds = []
    for i in range(k):
        test_start = i * fold_size
        test_end = (i + 1) * fold_size if i < k - 1 else n
        test_idx = list(range(test_start, test_end))
        train_idx = [j for j in range(n)
                     if j < max(0, test_start - gap) or j >= min(n, test_end + gap)]
        folds.append((train_idx, test_idx))
    return folds


def train_logistic_regression(
    pairs: list[tuple[list[float], float]],
    lr: float = 0.05,
    epochs: int = 200,
    l2: float = 0.001,
) -> dict:
    """Simple SGD-fit logistic regression. Weights + bias returned as a
    dict that ml_predictor can load without any external dependencies.

    Not López de Prado's ideal (he'd want RF or gradient boosting), but
    the interface is model-agnostic — swap in LightGBM later, ml_predictor
    doesn't care as long as predict(features) returns a score in [-1, +1].
    """
    if not pairs:
        return {"weights": None, "bias": 0.0, "trained_on": 0}
    n_features = len(pairs[0][0])
    weights = [0.0] * n_features
    bias = 0.0
    for _ in range(epochs):
        for feats, label in pairs:
            # Logistic score: sigmoid(w·x + b) → map label {-1, +1} to {0, 1}
            score = bias + sum(weights[i] * feats[i] for i in range(n_features))
            score = max(-30.0, min(30.0, score))  # clamp for numerical stability
            prob = 1.0 / (1.0 + math.exp(-score))
            y = 1.0 if label > 0 else 0.0
            grad = prob - y
            # SGD step
            for i in range(n_features):
                weights[i] -= lr * (grad * feats[i] + l2 * weights[i])
            bias -= lr * grad
    return {
        "weights": weights,
        "bias": bias,
        "trained_on": len(pairs),
        "model_type": "logistic_regression_sgd",
    }


def score_holdout(
    model: dict,
    pairs: list[tuple[list[float], float]],
) -> dict:
    """Accuracy + hit rate of a model on held-out data."""
    if not model or not model.get("weights") or not pairs:
        return {"n": 0, "accuracy": None}
    w = model["weights"]
    b = model["bias"]
    correct = 0
    total = 0
    for feats, label in pairs:
        score = b + sum(w[i] * feats[i] for i in range(len(w)))
        pred = 1.0 if score > 0 else -1.0
        if pred == label:
            correct += 1
        total += 1
    return {"n": total, "accuracy": correct / total if total > 0 else None}


def save_model(model: dict, path: str = MODEL_PATH_DEFAULT) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(model, indent=2))


def load_model(path: str = MODEL_PATH_DEFAULT) -> Optional[dict]:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def train_from_store(store, k: int = 5, model_path: str = MODEL_PATH_DEFAULT) -> dict:
    """End-to-end: read shadow log entries from store, purged k-fold train,
    save the winning model. Returns training metrics.

    Meant to be run as a nightly cron once tick_recorder has been running
    for ~2 weeks and the shadow log has accumulated enough labeled entries.
    """
    r = getattr(store, "_r", None)
    entries = []
    if r:
        try:
            raws = r.lrange(LOG_KEY, 0, -1) or []
            for raw in raws:
                try:
                    entries.append(json.loads(raw))
                except Exception:
                    pass
        except Exception:
            entries = []
    if not entries:
        return {"trained": False, "reason": "no shadow log entries"}
    pairs = load_training_pairs(entries, horizon="1h")
    if len(pairs) < 20:
        return {"trained": False, "reason": f"insufficient labeled data ({len(pairs)} pairs, need 20+)"}
    folds = purged_kfold_indices(len(pairs), k=k, gap=3)
    fold_scores = []
    best_model = None
    best_acc = -1.0
    for train_idx, test_idx in folds:
        train = [pairs[i] for i in train_idx]
        test = [pairs[i] for i in test_idx]
        model = train_logistic_regression(train)
        score = score_holdout(model, test)
        fold_scores.append(score)
        if score.get("accuracy") is not None and score["accuracy"] > best_acc:
            best_acc = score["accuracy"]
            best_model = model
    if best_model:
        save_model(best_model, model_path)
    return {
        "trained": True,
        "pairs": len(pairs),
        "folds": len(folds),
        "fold_scores": fold_scores,
        "best_accuracy": best_acc if best_acc >= 0 else None,
        "saved_to": model_path if best_model else None,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ML training pipeline (López de Prado style)")
    parser.add_argument("--train", action="store_true")
    parser.add_argument("--model_path", default=MODEL_PATH_DEFAULT)
    args = parser.parse_args()
    if args.train:
        from state_store import make_store
        store = make_store(os.getenv("SWING_DATA_DIR", "data"))
        result = train_from_store(store, model_path=args.model_path)
        print(json.dumps(result, indent=2))
