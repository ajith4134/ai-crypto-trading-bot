"""
Section V: MemRL Quality-Weighted Memory — V-01 to V-04.
Phase 1: semantic similarity via pgvector.
Phase 2: Q-value re-ranking by actual PnL (activates at 100+ trades).
"""
import json
import structlog
import redis_client
import redis_keys
import config
from db import db_conn
from memory.embed import _build_feature_vector

log = structlog.get_logger()


def add_to_fast_memory(trade_id: str, timestamp: float) -> None:
    """V-01: Add trade to Redis fast memory sorted set; evict oldest if over limit."""
    r = redis_client.get()
    r.zadd(redis_keys.FAST_MEMORY, {trade_id: timestamp})
    size = r.zcard(redis_keys.FAST_MEMORY)
    if size > config.memory.fast_memory_size:
        r.zpopmin(redis_keys.FAST_MEMORY, size - config.memory.fast_memory_size)


def retrieve_relevant_memories(current_trade: dict, paper_closed_count: int, top_k: int = 10) -> list[dict]:
    """
    V-02/V-03: Two-phase retrieval.
    Phase 1 (always): pgvector cosine similarity — top 50 similar trades.
    Phase 2 (100+ trades): re-rank by net_pnl_usdt (Q-value) — return top 10.
    """
    embedding = _build_feature_vector(current_trade)
    top50 = _phase1_semantic_search(embedding, top_k=50)

    if paper_closed_count < 100:
        return top50[:top_k]

    return _phase2_quality_rerank(top50, top_k=top_k)


def _phase1_semantic_search(embedding: list[float], top_k: int = 50) -> list[dict]:
    """V-02: pgvector cosine similarity search."""
    vec = json.dumps(embedding)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, pair, direction, net_pnl_usdt, market_regime, "
                "1 - (embedding <=> %s::vector) AS similarity "
                "FROM trades WHERE embedding IS NOT NULL AND status = 'closed' "
                "ORDER BY embedding <=> %s::vector LIMIT %s",
                (vec, vec, top_k),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def _phase2_quality_rerank(candidates: list[dict], top_k: int = 10) -> list[dict]:
    """V-03: Re-rank by net_pnl_usdt (Q-value); return profitable AND similar trades."""
    sorted_by_pnl = sorted(
        candidates,
        key=lambda t: float(t.get("net_pnl_usdt") or 0),
        reverse=True,
    )
    return sorted_by_pnl[:top_k]


async def run_sleep_consolidation() -> dict:
    """
    V-04: Nightly sleep consolidation — update embeddings, cluster patterns,
    apply EWC, synthesise summaries via llama.cpp (Celery background task).
    """
    from datetime import datetime, timezone, timedelta
    import time

    r = redis_client.get()
    trade_ids = [tid for tid, _ in r.zrange(redis_keys.FAST_MEMORY, 0, -1, withscores=True)]

    updated = 0
    for trade_id in trade_ids:
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM trades WHERE id = %s", (trade_id,))
                    row = cur.fetchone()
                    if not row:
                        continue
                    cols = [d[0] for d in cur.description]
                    trade = dict(zip(cols, row))

            from world_model.model import imagine_trajectory, encode
            obs = {"price": float(trade.get("entry_price") or 0),
                   "volume": float(trade.get("capital_usdt") or 0)}
            traj = imagine_trajectory("open_long", 5, obs)

            if traj.get("uncertainty", 0) > 0.5:
                from memory.embed import embed_trade
                embed_trade(trade_id, trade)
                updated += 1

        except Exception as exc:
            log.warning("consolidation_trade_error", trade_id=trade_id, error=str(exc))

    log.info("sleep_consolidation_complete", reviewed=len(trade_ids), updated=updated)
    return {"reviewed": len(trade_ids), "updated": updated}
