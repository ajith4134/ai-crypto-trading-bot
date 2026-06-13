"""HDBSCAN clustering on live CandleNet forecasts (Phase A.2, cont. 55).

Embedding shape (28 dims): 4 timeframes × 7 CandleNet forecast values
  [dir1, dir3, dir5, mag1, mag3, mag5, trend] per (1m, 5m, 15m, 1h).

Consumers:
  - pattern.live_capture        — writes embeddings to Redis stream at signal time
  - pretrainer.pattern_cluster_train — reads stream, fits HDBSCAN, persists
  - prediction.refresh_loop (Phase B) — online predict_cluster() at decision time

Persistence:
  models/pattern_clusters.pkl  — fitted HDBSCAN + centroid table + version
"""
from __future__ import annotations
import pickle
import os
from pathlib import Path
import structlog

log = structlog.get_logger()

_MODEL_PATH = Path(os.environ.get(
    "PATTERN_CLUSTERS_PATH",
    "/app/models/pattern_clusters.pkl",
))

EMBED_DIM = 28   # 4 TFs × 7 CandleNet fields
TF_LIST   = ("1m", "5m", "15m", "1h")
TF_FIELDS = ("dir1", "dir3", "dir5", "mag1", "mag3", "mag5", "trend")


def embedding_from_forecasts(forecasts: dict[str, dict]) -> list[float] | None:
    """Concatenate CandleNet outputs into a fixed 28-dim feature vector.

    `forecasts` keyed by interval ("1m","5m","15m","1h"). Each value is a dict
    with the 7 fields above. Returns None if any timeframe is missing or empty.
    """
    vec: list[float] = []
    for tf in TF_LIST:
        f = forecasts.get(tf) or {}
        if not f:
            return None
        for field in TF_FIELDS:
            try:
                vec.append(float(f.get(field, 0.0)))
            except (TypeError, ValueError):
                return None
    if len(vec) != EMBED_DIM:
        return None
    return vec


def fit_clusters(embeddings, min_cluster_size: int = 50):
    """Batch-fit HDBSCAN on a (N, EMBED_DIM) embedding matrix.
    Returns the fitted clusterer + per-cluster centroid table + labels array."""
    import hdbscan
    import numpy as np

    arr = np.asarray(embeddings, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[1] != EMBED_DIM:
        raise ValueError(f"expected (N, {EMBED_DIM}) embeddings; got {arr.shape}")

    n_samples = arr.shape[0]
    adaptive_min = max(min_cluster_size, n_samples // 500)

    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=adaptive_min,
        min_samples=10,
        metric="euclidean",
        cluster_selection_method="eom",
        prediction_data=True,
    )
    labels = clusterer.fit_predict(arr)

    unique_labels = sorted(set(int(l) for l in labels) - {-1})
    centroids = {}
    for cid in unique_labels:
        mask = labels == cid
        centroids[int(cid)] = arr[mask].mean(axis=0).tolist()

    log.info("pattern_clusters_fit",
             n_samples=n_samples,
             n_clusters=len(unique_labels),
             n_noise=int((labels == -1).sum()),
             min_cluster_size=adaptive_min)

    return clusterer, centroids, labels


def save(clusterer, centroids, model_version: str) -> None:
    _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_MODEL_PATH, "wb") as f:
        pickle.dump({
            "clusterer":     clusterer,
            "centroids":     centroids,
            "model_version": model_version,
            "embed_dim":     EMBED_DIM,
            "tf_list":       list(TF_LIST),
            "tf_fields":     list(TF_FIELDS),
        }, f)
    log.info("pattern_clusters_saved", path=str(_MODEL_PATH),
             model_version=model_version)


_LOAD_CACHE: dict | None = None
_LOAD_MTIME: float = 0.0


def load() -> dict | None:
    """Load the fitted clusterer blob, cached in-process with mtime invalidation.

    The pickle is ~50 MB; predict_cluster runs per-pair-per-minute (≈300×/min),
    so re-reading it every call would be heavy disk I/O. Cache it and reload only
    when the file's mtime changes (i.e. after a retrain writes a new model)."""
    global _LOAD_CACHE, _LOAD_MTIME
    if not _MODEL_PATH.exists():
        _LOAD_CACHE, _LOAD_MTIME = None, 0.0
        return None
    try:
        mtime = _MODEL_PATH.stat().st_mtime
        if _LOAD_CACHE is not None and mtime == _LOAD_MTIME:
            return _LOAD_CACHE
        with open(_MODEL_PATH, "rb") as f:
            _LOAD_CACHE = pickle.load(f)
        _LOAD_MTIME = mtime
        return _LOAD_CACHE
    except Exception as exc:
        log.warning("pattern_clusters_load_failed", error=str(exc)[:200])
        return None


def predict_cluster(embedding) -> tuple[int, float]:
    """Online: return (cluster_id, membership_strength) for a 28-dim embedding.
    cluster_id = -1 means noise / no clusterer loaded yet.
    strength ∈ [0,1]; higher = more confident."""
    import hdbscan
    import numpy as np

    blob = load()
    if blob is None:
        return -1, 0.0
    clusterer = blob["clusterer"]
    try:
        emb_2d = np.asarray(embedding, dtype=np.float32).reshape(1, -1)
        if emb_2d.shape[1] != EMBED_DIM:
            return -1, 0.0
        labels, strengths = hdbscan.approximate_predict(clusterer, emb_2d)
        return int(labels[0]), float(strengths[0])
    except Exception as exc:
        log.debug("pattern_cluster_predict_failed", error=str(exc)[:120])
        return -1, 0.0
