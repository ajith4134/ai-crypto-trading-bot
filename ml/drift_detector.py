"""F49 §Component 1 — Drift Detector.

Computes Population Stability Index (PSI) and Kolmogorov-Smirnov p-values
between training-time feature distributions and recent production samples.
When PSI > 0.25 on any feature OR KS p-value < 0.01 on a direction-correlated
feature, marks the model as drift-detected; the meta-orchestrator picks this
up on its next tick.

Per-model training distribution snapshot is captured at the end of each
`train()` call and persisted to Redis:
  model:{name}:feature_stats_train  — JSON of {"feature_i": {"hist": [...], "edges": [...]}}

Per-model live sample buffer (rolling window of recent inferences) is
maintained in Redis:
  model:{name}:feature_samples_live — JSON list of recent feature vectors
"""
from __future__ import annotations
import json
import math
from datetime import datetime, timezone

import structlog
import numpy as np

import redis_client

log = structlog.get_logger()

PSI_THRESHOLD     = 0.25
KS_P_THRESHOLD    = 0.01
N_BINS            = 10
LIVE_SAMPLE_LIMIT = 500


def snapshot_training_distribution(model_name: str,
                                   X: np.ndarray) -> None:
    """Save histogram for each feature (last-timestep value, when X is 3D).

    Called from train() right after the training set is finalised.
    X: ndarray of shape [N, T, F] or [N, F]. We use the last timestep
    of each sequence (the value the model conditions its prediction on)
    as the representative feature distribution.
    """
    if X.ndim == 3:
        # Take last-timestep snapshot per sample → [N, F]
        rep = X[:, -1, :]
    else:
        rep = X

    snapshots = {}
    for j in range(rep.shape[1]):
        col = rep[:, j]
        col = col[np.isfinite(col)]
        if col.size < 10:
            continue
        try:
            hist, edges = np.histogram(col, bins=N_BINS)
            # Smoothing — avoid zero bins which cause PSI to diverge
            hist = hist.astype(np.float64) + 1.0
            hist = hist / hist.sum()
            snapshots[f"f{j}"] = {
                "hist":   hist.tolist(),
                "edges":  edges.tolist(),
                "mean":   float(np.mean(col)),
                "std":    float(np.std(col)),
                "median": float(np.median(col)),
            }
        except Exception as exc:
            log.warning("drift_snapshot_feature_failed",
                        model=model_name, feature=j, error=str(exc)[:120])

    try:
        r = redis_client.get()
        r.set(f"model:{model_name}:feature_stats_train",
              json.dumps(snapshots))
        log.info("drift_training_snapshot_saved",
                 model=model_name, features=len(snapshots))
    except Exception as exc:
        log.warning("drift_snapshot_save_failed",
                    model=model_name, error=str(exc)[:200])


def record_live_sample(model_name: str, feature_vec: list[float]) -> None:
    """Append a live inference's feature vector to the rolling buffer.

    Called from each model's `run_inference()` after constructing the input.
    """
    try:
        r = redis_client.get()
        key = f"model:{model_name}:feature_samples_live"
        r.lpush(key, json.dumps(feature_vec))
        r.ltrim(key, 0, LIVE_SAMPLE_LIMIT - 1)
    except Exception:
        pass


def _psi(p: np.ndarray, q: np.ndarray) -> float:
    """Population Stability Index. p = training, q = live, both normalised."""
    eps = 1e-6
    p = np.clip(p, eps, None)
    q = np.clip(q, eps, None)
    return float(np.sum((q - p) * np.log(q / p)))


def _ks_pvalue(train_col: np.ndarray, live_col: np.ndarray) -> float:
    """Two-sample KS test p-value. Returns 1.0 (no difference) on failure."""
    try:
        from scipy.stats import ks_2samp
        _, p = ks_2samp(train_col, live_col)
        return float(p)
    except Exception:
        return 1.0


def detect_drift(model_name: str) -> dict:
    """Compute drift for one model. Returns {drift_score, drift_detected, ...}.

    Writes:
      model:{name}:drift_score        — max PSI across features
      model:{name}:drift_detected     — "1" if any feature trips threshold
      model:{name}:drift_detected_at  — unix timestamp (only if newly detected)
      model:{name}:drift_features     — JSON list of feature names that tripped
    """
    r = redis_client.get()
    snap_raw = r.get(f"model:{model_name}:feature_stats_train")
    if not snap_raw:
        return {"status": "no_training_snapshot"}

    live_raw = r.lrange(f"model:{model_name}:feature_samples_live", 0, -1)
    if len(live_raw) < 20:
        return {"status": "insufficient_live_samples",
                "live": len(live_raw)}

    try:
        snapshots = json.loads(snap_raw)
    except Exception:
        return {"status": "corrupt_snapshot"}

    live_vecs = []
    for x in live_raw:
        try:
            live_vecs.append(json.loads(x))
        except Exception:
            continue
    if not live_vecs:
        return {"status": "no_live_vectors"}
    live_arr = np.array(live_vecs, dtype=np.float64)
    if live_arr.ndim == 1:
        live_arr = live_arr.reshape(-1, 1)

    max_psi = 0.0
    triggered_features: list[str] = []

    for j_idx, (fname, snap) in enumerate(snapshots.items()):
        if j_idx >= live_arr.shape[1]:
            break
        train_hist = np.array(snap["hist"], dtype=np.float64)
        edges      = np.array(snap["edges"], dtype=np.float64)

        live_col = live_arr[:, j_idx]
        live_col = live_col[np.isfinite(live_col)]
        if live_col.size < 10:
            continue
        live_hist, _ = np.histogram(live_col, bins=edges)
        live_hist = live_hist.astype(np.float64) + 1.0
        live_hist = live_hist / live_hist.sum()

        psi = _psi(train_hist, live_hist)
        if psi > max_psi:
            max_psi = psi
        if psi > PSI_THRESHOLD:
            triggered_features.append(fname)

        # KS on the raw values (uses snapshot mean/std as approximation of
        # training distribution; full KS would require the original samples).
        # Simulate via sampling from snapshot's mean/std.
        try:
            t_mean = float(snap.get("mean", 0.0))
            t_std  = float(snap.get("std", 1.0)) or 1.0
            # Synthetic samples from snapshot statistics for KS
            synth = np.random.normal(t_mean, t_std, size=min(1000, live_col.size * 3))
            p = _ks_pvalue(synth, live_col)
            if p < KS_P_THRESHOLD and fname not in triggered_features:
                triggered_features.append(fname)
        except Exception:
            pass

    drift_detected = bool(triggered_features)

    # Publish to Redis
    try:
        r.set(f"model:{model_name}:drift_score", round(max_psi, 4))
        r.set(f"model:{model_name}:drift_detected", "1" if drift_detected else "0")
        r.set(f"model:{model_name}:drift_features",
              json.dumps(triggered_features))
        # Only set timestamp on transition 0→1 (avoid clobbering on each tick)
        if drift_detected:
            existing = r.get(f"model:{model_name}:drift_detected_at")
            if not existing:
                r.set(f"model:{model_name}:drift_detected_at",
                      int(datetime.now(timezone.utc).timestamp()))
        else:
            r.delete(f"model:{model_name}:drift_detected_at")
    except Exception as exc:
        log.warning("drift_publish_failed",
                    model=model_name, error=str(exc)[:200])

    return {
        "status":   "ok",
        "psi":      round(max_psi, 4),
        "drift":    drift_detected,
        "features": triggered_features,
        "live_n":   int(live_arr.shape[0]),
    }


# Model names whose drift status is tracked by the orchestrator.
TRACKED_MODELS = [
    "candlenet_1m", "candlenet_5m", "candlenet_15m",
    "candlenet_30m", "candlenet_1h",
    "tft", "patchtst", "direction_model",
]


def detect_drift_all() -> dict:
    """Run detect_drift across every tracked model. Called by Celery beat."""
    out = {}
    for name in TRACKED_MODELS:
        try:
            out[name] = detect_drift(name)
        except Exception as exc:
            out[name] = {"status": "error", "error": str(exc)[:200]}
    return out
