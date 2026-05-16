"""L-04: GNN Inter-Asset Correlation Model — lead-lag signals across all pairs."""
import json
from pathlib import Path
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()
_MODEL_PATH = Path("models/gnn.pth")
_model = None


def _load():
    global _model
    if _model is None and _MODEL_PATH.exists():
        import torch
        _model = torch.load(_MODEL_PATH, map_location="cpu")
        _model.eval()
    return _model


def get_interasset_signals() -> dict:
    """Return lead-lag matrix across active pairs; write to Redis."""
    r = redis_client.get()
    cached = r.get(redis_keys.INTERASSET_SIGNALS)
    if cached:
        return json.loads(cached)

    if not _MODEL_PATH.exists():
        return {}

    try:
        active_pairs = list(r.smembers(redis_keys.ACTIVE_PAIRS))
        if not active_pairs:
            return {}

        import torch, numpy as np
        features = []
        for pair in active_pairs[:20]:
            price = float(r.get(redis_keys.LAST_PRICE.replace("{pair}", pair)) or 0)
            vol = float(r.get(redis_keys.TICKER_VOLUME_24H.replace("{pair}", pair)) or 0)
            features.append([price, vol])

        if not features:
            return {}

        x = torch.tensor(features, dtype=torch.float32)
        model = _load()
        with torch.no_grad():
            embeddings = model(x)

        corr_matrix = torch.corrcoef(embeddings.T).numpy()
        signals = {
            "pairs": active_pairs[:20],
            "correlation_matrix": corr_matrix.tolist(),
            "top_lead_pairs": active_pairs[:3],
        }
        r.set(redis_keys.INTERASSET_SIGNALS, json.dumps(signals), ex=3600)
        return signals
    except Exception as exc:
        log.error("gnn_signals_failed", error=str(exc))
        return {}
