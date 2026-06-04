"""Idea 2 Postmortem RAG — text embedder + canonical signal-context builder.

Shared producer/consumer module. Two concerns:

  1. `embed_text(text)` — synchronous Ollama call to nomic-embed-text (768-dim).
     Used by:
       - celery decode_pending_misses / decode_pending_mismatches (producer)
       - celery embed_pending_postmortems / backfill_postmortem_embeddings
       - signals/engine.py prior_belief lookup (consumer side, via postmortem_rag)

  2. `build_signal_context(...)` — canonical short text describing a
     signal. MUST match the format used inside the F9/F12 decode_reason so
     query embeddings land near the postmortem embeddings in vector space.

Embedder: Ollama nomic-embed-text. 768-dim. Reached via config.llm.ollama_url
(no async; this runs both in celery workers and the hot signal path —
synchronous httpx keeps the call sites simple, latency ~30ms on CPU).

Failure mode: any Ollama hiccup returns None. The consumer/producer are
responsible for treating None as "no RAG signal" and proceeding without it.
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx
import structlog

import config

log = structlog.get_logger()

_EMBED_MODEL = "nomic-embed-text"
_EMBED_DIM   = 768
_EMBED_TIMEOUT_S = 8.0


def build_signal_context(*, pair: str | None, direction: str | None,
                         timeframe: str | None, regime: str | None,
                         signal_strength: Any, rejection_reason: str | None = None,
                         brain_stage: Any = None) -> str:
    """Canonical signal-context string.

    Match the same key=value layout used in the F9 _miss_prompt header so the
    consumer query embeddings land near producer postmortems in cosine space.
    Keep it short (< 200 chars) — nomic-embed-text handles short inputs well
    and longer strings dilute the discriminative tokens.
    """
    def _s(v: Any) -> str:
        return "n/a" if v is None or v == "" else str(v)

    fields = [
        f"pair={_s(pair)}",
        f"direction={_s(direction)}",
        f"timeframe={_s(timeframe)}",
        f"regime={_s(regime)}",
        f"strength={_s(signal_strength)}",
    ]
    if rejection_reason:
        fields.append(f"reject_reason={rejection_reason[:60]}")
    if brain_stage is not None:
        fields.append(f"stage={_s(brain_stage)}")
    return " ".join(fields)


def embed_text(text: str) -> list[float] | None:
    """Return a 768-dim embedding from Ollama nomic-embed-text.

    Returns None on any error (network, model missing, malformed response).
    Callers treat None as "no embedding available, skip RAG for this row /
    this decision". Never raises.
    """
    if not text or not isinstance(text, str):
        return None
    try:
        url = f"{config.llm.ollama_url}/api/embeddings"
        with httpx.Client(timeout=_EMBED_TIMEOUT_S) as client:
            resp = client.post(url, json={"model": _EMBED_MODEL, "prompt": text})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.warning("postmortem_embed_failed",
                    err=str(exc)[:160], text_head=text[:60])
        try:
            import redis_client as _rc
            _rc.get().incr("postmortem_rag:embed_failure_count")
        except Exception:
            pass
        return None

    emb = data.get("embedding")
    if not isinstance(emb, list) or len(emb) != _EMBED_DIM:
        log.warning("postmortem_embed_bad_shape",
                    dim=len(emb) if isinstance(emb, list) else None)
        try:
            import redis_client as _rc
            _rc.get().incr("postmortem_rag:embed_bad_shape_count")
        except Exception:
            pass
        return None

    return [float(x) for x in emb]


def to_pgvector(emb: list[float]) -> str:
    """Format a Python list as the pgvector textual literal `[v1,v2,...]`."""
    return json.dumps(emb)


def now_ts() -> int:
    return int(time.time())
