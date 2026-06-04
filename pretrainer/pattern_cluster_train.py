"""Live-data-only HDBSCAN cluster trainer (Phase A.2, cont. 55).

User constraint 2026-05-29: NO closed-trade snapshots as input. The trainer
reads embeddings ONLY from the `pattern:embeddings` Redis stream populated by
pattern.live_capture from live exchange OHLCV at signal-fire time.

Run via:
  docker exec -it trading-bot-brain-1 python -m pretrainer.pattern_cluster_train

Recommended cadence (later wired as a celery beat task in Phase A.4):
  Every 6 hours after the stream accumulates ≥ MIN_TRAIN_SAMPLES live captures.
"""
from __future__ import annotations
import argparse
import sys
import time
import structlog

log = structlog.get_logger()

MIN_TRAIN_SAMPLES = 1_000   # require this many live embeddings before fitting
DEFAULT_MIN_CLUSTER_SIZE = 50


def train_and_persist(min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
                      max_age_seconds: int = 7 * 24 * 3600,
                      model_version: str | None = None) -> dict:
    """Read recent live captures, fit HDBSCAN, persist. Returns summary."""
    from pattern.live_capture import read_recent_embeddings
    from pattern.clusterer import fit_clusters, save
    import numpy as np

    if model_version is None:
        model_version = f"cluster-v{int(time.time())}"

    captures = read_recent_embeddings(max_count=200_000,
                                      max_age_s=max_age_seconds)
    if len(captures) < MIN_TRAIN_SAMPLES:
        log.info("pattern_cluster_train_insufficient_samples",
                 have=len(captures), need=MIN_TRAIN_SAMPLES)
        return {"status": "insufficient_samples",
                "have": len(captures),
                "need": MIN_TRAIN_SAMPLES}

    embeddings = np.array([c["embedding"] for c in captures],
                          dtype=np.float32)
    clusterer, centroids, labels = fit_clusters(embeddings,
                                                min_cluster_size=min_cluster_size)
    save(clusterer, centroids, model_version=model_version)

    log.info("pattern_cluster_train_done",
             n_samples=len(captures),
             n_clusters=len(centroids),
             n_noise=int((labels == -1).sum()),
             model_version=model_version)
    return {"status": "ok",
            "n_samples":     len(captures),
            "n_clusters":    len(centroids),
            "n_noise":       int((labels == -1).sum()),
            "model_version": model_version}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--min-cluster-size", type=int,
                   default=DEFAULT_MIN_CLUSTER_SIZE)
    p.add_argument("--max-age-seconds", type=int,
                   default=7 * 24 * 3600)
    p.add_argument("--model-version", default=None)
    args = p.parse_args()
    res = train_and_persist(
        min_cluster_size=args.min_cluster_size,
        max_age_seconds=args.max_age_seconds,
        model_version=args.model_version,
    )
    print(res)
    sys.exit(0 if res.get("status") == "ok" else 2)
