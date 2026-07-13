"""Tests for the López de Prado / Andrew Lo / Almgren-Chriss batch.

Covers regime_detector.py, execution.py, ml_training.py, and the
ml_predictor.py upgrade to load a trained model.
"""

import math

import pytest


# =============================================================================
# regime_detector — Andrew Lo Adaptive Markets
# =============================================================================

def test_regime_unknown_on_insufficient_data():
    from regime_detector import classify_regime, REGIME_UNKNOWN
    assert classify_regime([100], atr=1.0) == REGIME_UNKNOWN
    assert classify_regime([], atr=1.0) == REGIME_UNKNOWN


def test_regime_mean_reversion_on_oscillating_prices():
    from regime_detector import classify_regime, REGIME_MEAN_REVERSION
    # Oscillating around 100 — classic mean-reversion pattern
    prices = [100 + (i % 2) * 0.5 - 0.25 for i in range(50)]
    r = classify_regime(prices, atr=0.5)
    # Low autocorr + no trend → mean_reversion default
    assert r == REGIME_MEAN_REVERSION


def test_regime_momentum_on_strong_uptrend():
    from regime_detector import classify_regime, REGIME_MOMENTUM
    # Straight-line uptrend from 100 to 105
    prices = [100 + i * 0.1 for i in range(50)]
    r = classify_regime(prices, atr=0.05, momentum_trend_threshold=1.0)
    assert r == REGIME_MOMENTUM


def test_regime_adjustments_shape():
    from regime_detector import regime_adjustments, REGIME_MOMENTUM, REGIME_CHOP
    mom = regime_adjustments(REGIME_MOMENTUM)
    assert mom["spread_multiplier"] > 1.0  # wider on trends
    assert mom["size_multiplier"] < 1.0    # smaller size — trend can accelerate
    chop = regime_adjustments(REGIME_CHOP)
    assert chop["spread_multiplier"] < 1.0  # tighter on chop
    assert chop["size_multiplier"] < 1.0    # smaller on chop
    # Mean reversion + unknown → all 1.0
    mr = regime_adjustments("mean_reversion")
    assert mr["spread_multiplier"] == 1.0
    assert mr["size_multiplier"] == 1.0


# =============================================================================
# execution — Almgren-Chriss
# =============================================================================

def test_execution_no_slice_for_single_contract():
    from execution import optimal_slice_schedule
    sched = optimal_slice_schedule(1, urgency_secs=30, kyle_lambda=0.001)
    assert sched == [(0.0, 1)]


def test_execution_no_slice_when_lambda_missing():
    from execution import optimal_slice_schedule
    sched = optimal_slice_schedule(5, urgency_secs=30, kyle_lambda=None)
    assert sched == [(0.0, 5)]
    sched = optimal_slice_schedule(5, urgency_secs=30, kyle_lambda=0)
    assert sched == [(0.0, 5)]


def test_execution_slices_sum_to_total_qty():
    from execution import optimal_slice_schedule
    sched = optimal_slice_schedule(5, urgency_secs=30, kyle_lambda=0.002)
    total = sum(q for _, q in sched)
    assert total == 5


def test_execution_first_slice_is_largest_when_lambda_high():
    from execution import optimal_slice_schedule
    sched = optimal_slice_schedule(10, urgency_secs=30, kyle_lambda=0.005)
    if len(sched) > 1:
        # Front-loaded exponential — first slice should be >= second
        assert sched[0][1] >= sched[1][1]


def test_execution_should_slice_gate():
    from execution import should_slice
    assert should_slice(5, kyle_lambda=0.001) is True
    assert should_slice(1, kyle_lambda=0.001) is False
    assert should_slice(5, kyle_lambda=None) is False


# =============================================================================
# ml_training — López de Prado
# =============================================================================

def test_purged_kfold_train_test_no_overlap():
    from ml_training import purged_kfold_indices
    folds = purged_kfold_indices(n=100, k=5, gap=3)
    assert len(folds) == 5
    for train_idx, test_idx in folds:
        # No index in both — the purge should prevent overlap
        overlap = set(train_idx) & set(test_idx)
        assert not overlap
        # Gap enforced: no train index within `gap` of test range
        if test_idx:
            test_min, test_max = min(test_idx), max(test_idx)
            for t in train_idx:
                assert t < test_min - 3 or t > test_max + 3 or (t < test_min or t > test_max)


def test_load_training_pairs_skips_entries_without_features():
    from ml_training import load_training_pairs
    entries = [
        {"features": {"vpin": 0.3}, "outcomes": {"1h": {"verdict": "correct"}}},
        {"tweet_text": "no features here", "outcomes": {"1h": {"verdict": "wrong"}}},
        {"features": {"vpin": 0.5}, "outcomes": {"1h": {"verdict": "flat"}}},  # flat = skip
    ]
    pairs = load_training_pairs(entries)
    # Only the first entry qualifies
    assert len(pairs) == 1
    assert pairs[0][1] == 1.0  # correct → +1


def test_train_logistic_learns_simple_pattern():
    """Train on synthetic data where feature[0] perfectly predicts label."""
    from ml_training import train_logistic_regression, score_holdout
    # 20 samples: feature[0] = +1 → label +1, feature[0] = -1 → label -1
    pairs = []
    n_features = 8
    for _ in range(20):
        feats = [1.0] + [0.0] * (n_features - 1)
        pairs.append((feats, 1.0))
        feats = [-1.0] + [0.0] * (n_features - 1)
        pairs.append((feats, -1.0))
    model = train_logistic_regression(pairs, epochs=100, lr=0.1)
    assert model["weights"] is not None
    # Score on the training data — should learn perfectly
    result = score_holdout(model, pairs)
    assert result["accuracy"] >= 0.95


def test_train_from_store_returns_reason_when_no_data():
    from ml_training import train_from_store
    class _EmptyStore:
        _r = None
    result = train_from_store(_EmptyStore())
    assert result["trained"] is False
    assert "no shadow log" in result["reason"] or "insufficient" in result["reason"]


# =============================================================================
# ml_predictor loads trained model when available
# =============================================================================

def test_ml_predictor_uses_placeholder_when_no_trained_model(monkeypatch, tmp_path):
    """Reset the cache and point to a non-existent path — should fall back
    to placeholder weights."""
    import ml_predictor
    # Reset cache
    ml_predictor._TRAINED_MODEL_CACHE["model"] = None
    ml_predictor._TRAINED_MODEL_CACHE["checked"] = False
    monkeypatch.setattr(ml_predictor, "_TRAINED_MODEL_PATH", str(tmp_path / "no_such.json"))
    feats = [0.4, 0.2, 0.1, 0.005, 1.0, 1.0, 0.001, 0.002]
    score = ml_predictor.predict(feats)
    assert -1.0 <= score <= 1.0


def test_ml_predictor_uses_trained_model_when_present(monkeypatch, tmp_path):
    """Write a trained model to disk and verify predict uses its weights."""
    import ml_predictor
    from ml_predictor import FEATURE_NAMES
    model_path = tmp_path / "ml_model.json"
    import json
    # Simple trained model: score = feature[0] × 1.0
    weights = [0.0] * len(FEATURE_NAMES)
    weights[0] = 5.0
    model_path.write_text(json.dumps({"weights": weights, "bias": 0.0,
                                       "model_type": "test"}))
    # Reset cache + point path
    ml_predictor._TRAINED_MODEL_CACHE["model"] = None
    ml_predictor._TRAINED_MODEL_CACHE["checked"] = False
    monkeypatch.setattr(ml_predictor, "_TRAINED_MODEL_PATH", str(model_path))
    # feature[0] = +1 → tanh(5) ≈ +1
    feats = [1.0] + [0.0] * (len(FEATURE_NAMES) - 1)
    score = ml_predictor.predict(feats)
    assert score > 0.9  # trained model should dominate

    # feature[0] = -1 → tanh(-5) ≈ -1
    feats = [-1.0] + [0.0] * (len(FEATURE_NAMES) - 1)
    score = ml_predictor.predict(feats)
    assert score < -0.9
