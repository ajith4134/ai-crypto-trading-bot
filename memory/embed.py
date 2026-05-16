"""M-06: Generate 512-dim trade embeddings and write to trades.embedding."""
import json
import numpy as np
import structlog
from db import db_conn

log = structlog.get_logger()

_EMBED_DIM = 512


def _build_feature_vector(trade: dict) -> list[float]:
    """Build a 512-dim float vector from trade fields for pgvector similarity."""
    fv = trade.get("feature_vector") or {}
    if isinstance(fv, str):
        fv = json.loads(fv)

    raw = [
        float(trade.get("entry_price") or 0),
        float(trade.get("capital_usdt") or 0),
        float(trade.get("leverage") or 0),
        1.0 if trade.get("direction") == "long" else -1.0,
        float(trade.get("trade_potential_score") or 0),
        float(trade.get("direction_confidence") or 0),
    ]

    for k in sorted(fv.keys())[:(_EMBED_DIM - len(raw))]:
        try:
            raw.append(float(fv[k]))
        except (TypeError, ValueError):
            raw.append(0.0)

    vec = raw[:_EMBED_DIM]
    while len(vec) < _EMBED_DIM:
        vec.append(0.0)

    arr = np.array(vec, dtype=np.float32)
    norm = np.linalg.norm(arr)
    if norm > 0:
        arr = arr / norm
    return arr.tolist()


def embed_trade(trade_id: str, trade: dict) -> None:
    """Generate embedding and write to trades.embedding column."""
    vec = _build_feature_vector(trade)
    vec_str = json.dumps(vec)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE trades SET embedding = %s::vector WHERE id = %s",
                (vec_str, trade_id),
            )
    log.debug("trade_embedded", trade_id=trade_id)
