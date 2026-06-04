"""Idea 2 Postmortem RAG — consumer-side prior_belief lookup.

Called from signals/engine.py just before accept_or_reject. Given the
current signal context, it embeds a short description, vector-searches the
K nearest F9 postmortems by cosine distance, and returns the fraction of
those neighbours where the bot's past rejection LEFT MONEY ON THE TABLE
(peak_profit_pct > 5%).

  prior_belief ∈ [0, 1]   high  → "similar past rejections were wrong;
                                   the strength filter should be loosened
                                   for this signal"
                          low   → "similar past rejections were right;
                                   the filter is doing its job"
                          None  → not enough evidence (cold-start)

The consumer (signals/engine.py) acts on the value:

  if prior_belief > 0.7  →  +10 strength bonus (Idea 2 spec, F9/F12 file)
  else                   →  no-op

F46 governance: returns (None, {}) when feature inactive.
Embedder failure: returns (None, {...err}) — consumer treats as no signal.

Min-evidence floor: K=5 neighbours required. Below that, returns None.

F9 coverage only in v1 — counterfactuals.peak_profit_pct is the direct
"would have won" signal. F12 embeddings ARE stored (see celery_app
embed-on-decode) but not yet queried; cross-table RAG is a follow-up.
"""
from __future__ import annotations

import time
from typing import Any

import structlog

from metacognition import postmortem_embed

log = structlog.get_logger()

_MIN_EVIDENCE      = 5       # need ≥5 neighbours before producing a belief
_TOP_K             = 5       # search depth
_WIN_THRESHOLD_PCT = 5.0     # peak_profit_pct > 5% counts as "would-have-won"

# Per-call timeout for the vector search itself. Hard cap so the hot signal
# path can't be wedged by a slow DB query.
_DB_TIMEOUT_MS = 250


def _governance_active() -> bool:
    try:
        from feature_governance.registry import is_active
        return bool(is_active("F46"))
    except Exception:
        return True


def prior_belief(*, pair: str | None, direction: str | None,
                 timeframe: str | None, regime: str | None,
                 signal_strength: Any,
                 rejection_reason: str | None = None,
                 brain_stage: Any = None) -> tuple[float | None, dict]:
    """Return (fraction_would_have_won, evidence_dict) over the K nearest
    F9 postmortems. (None, {...}) when feature gated off, embedder down,
    or below the min-evidence floor.

    Latency budget: ≤ 30ms embed + ≤ 250ms DB.
    """
    if not _governance_active():
        return None, {"reason": "f46_inactive"}

    ctx = postmortem_embed.build_signal_context(
        pair=pair, direction=direction, timeframe=timeframe, regime=regime,
        signal_strength=signal_strength, rejection_reason=rejection_reason,
        brain_stage=brain_stage,
    )

    t0 = time.time()
    emb = postmortem_embed.embed_text(ctx)
    embed_ms = int((time.time() - t0) * 1000)
    if emb is None:
        return None, {"reason": "embed_failed", "embed_ms": embed_ms}

    vec = postmortem_embed.to_pgvector(emb)

    try:
        from db import db_conn
        with db_conn() as conn:
            with conn.cursor() as cur:
                # Hard timeout on this transaction so the hot signal path
                # is bounded even if the index is degraded.
                cur.execute("SET LOCAL statement_timeout = %s",
                            (_DB_TIMEOUT_MS,))
                cur.execute(
                    """
                    SELECT peak_profit_pct,
                           1 - (decode_reason_embedding <=> %s::vector) AS sim
                    FROM counterfactuals
                    WHERE decode_reason_embedding IS NOT NULL
                      AND peak_profit_pct IS NOT NULL
                    ORDER BY decode_reason_embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (vec, vec, _TOP_K),
                )
                rows = cur.fetchall()
    except Exception as exc:
        log.warning("postmortem_rag_db_failed", err=str(exc)[:160])
        return None, {"reason": "db_failed",
                      "err": str(exc)[:160], "embed_ms": embed_ms}

    if not rows or len(rows) < _MIN_EVIDENCE:
        return None, {"reason": "below_min_evidence",
                      "n": len(rows or []),
                      "min_required": _MIN_EVIDENCE,
                      "embed_ms": embed_ms}

    wins = [r for r in rows if float(r[0] or 0.0) > _WIN_THRESHOLD_PCT]
    belief = len(wins) / len(rows)
    avg_sim = sum(float(r[1] or 0.0) for r in rows) / len(rows)

    return float(belief), {
        "n": len(rows),
        "wins": len(wins),
        "avg_sim": round(avg_sim, 4),
        "embed_ms": embed_ms,
    }


# ---------------------------------------------------------------------------
# Tunables exposed for the consumer.
# ---------------------------------------------------------------------------

BELIEF_BOOST_THRESHOLD = 0.7    # prior_belief > this → loosen
STRENGTH_BONUS         = 10     # +10 to signal_strength when threshold crossed
AVG_SIM_FLOOR          = 0.55   # also require neighbours to be actually similar


def should_apply_bonus(belief: float | None, evidence: dict) -> bool:
    """Helper for signals/engine.py — encapsulates the activation gate so
    the threshold lives next to the RAG logic (not buried in engine.py)."""
    if belief is None:
        return False
    if belief <= BELIEF_BOOST_THRESHOLD:
        return False
    if float(evidence.get("avg_sim", 0.0)) < AVG_SIM_FLOOR:
        return False
    return True
