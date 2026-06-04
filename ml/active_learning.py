"""F49 §Component 4 — Active Learning Sampler.

Identifies the bot's highest-uncertainty closed trades (prediction entropy)
and over-samples them in the next training set. This biases the model
toward learning from its own knowledge gaps faster.

Strategy: query-by-prediction-entropy. For a binary direction prediction
p ∈ [0, 1], entropy H = -p·log(p) - (1-p)·log(1-p), max at p = 0.5.

API:
  uncertainty_score(pred_prob) → entropy in [0, 1] (1 = max uncertainty)
  build_oversampled_indices(preds, n_total, factor) → list of indices to use
    in training, with high-entropy samples appearing `factor` times
"""
from __future__ import annotations
import math
from typing import Sequence

import numpy as np
import structlog

log = structlog.get_logger()

OVERSAMPLE_FACTOR    = 3
UNCERTAIN_QUANTILE   = 0.80   # top-20% highest-entropy


def uncertainty_score(p: float) -> float:
    """Binary entropy. Returns value in [0, 1] (1 = maximally uncertain)."""
    if p <= 0.0 or p >= 1.0:
        return 0.0
    h = -p * math.log(p) - (1 - p) * math.log(1 - p)
    return min(1.0, h / math.log(2))   # normalise to [0, 1]


def uncertainty_scores(preds: Sequence[float]) -> np.ndarray:
    """Vectorised binary entropy for a 1-D array of probabilities."""
    p = np.clip(np.asarray(preds, dtype=np.float64), 1e-9, 1 - 1e-9)
    h = -p * np.log(p) - (1 - p) * np.log(1 - p)
    return (h / math.log(2)).astype(np.float32)


def build_oversampled_indices(preds: Sequence[float],
                              factor: int = OVERSAMPLE_FACTOR) -> np.ndarray:
    """Return an index array that over-samples the top-20% most-uncertain
    predictions by `factor`. The bottom-80% remain singleton.

    Output: np.array of indices into preds. Use as `X[idx], y[idx]` to
    construct the oversampled training set.
    """
    preds = np.asarray(preds)
    n = len(preds)
    if n == 0:
        return np.array([], dtype=np.int64)

    scores = uncertainty_scores(preds)
    if n < 10:
        return np.arange(n, dtype=np.int64)

    threshold = float(np.quantile(scores, UNCERTAIN_QUANTILE))
    uncertain_mask = scores >= threshold

    indices = []
    for i in range(n):
        indices.append(i)
        if uncertain_mask[i]:
            for _ in range(factor - 1):
                indices.append(i)
    return np.array(indices, dtype=np.int64)


def filter_to_uncertain(preds: Sequence[float],
                        top_k_frac: float = 0.20) -> np.ndarray:
    """Return indices of the top-K% most-uncertain predictions (no
    oversampling — just a filter). Used for active labelling pipelines that
    pull a focused set of examples for re-labelling or retraining."""
    preds = np.asarray(preds)
    n = len(preds)
    if n == 0:
        return np.array([], dtype=np.int64)
    scores = uncertainty_scores(preds)
    k = max(1, int(n * top_k_frac))
    return np.argsort(-scores)[:k].astype(np.int64)
