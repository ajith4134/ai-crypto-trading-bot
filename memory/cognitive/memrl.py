"""
Section V: MemRL Quality-Weighted Memory — V-01 to V-04.
Phase 1: semantic similarity via pgvector.
Phase 2: learned Q-value re-ranking from `q_values` table — see
         memory.cognitive.q_learning. Activates at 100+ trades.

Phase 2 was previously a raw `sorted(candidates, key=net_pnl_usdt, reverse=True)`
— a single-trade observed return masquerading as a Q-value. Per blueprint
Feature 35 / arXiv:2601.03192, Phase 2 must re-rank by LEARNED Q-value: the
bucket-averaged expected return for the candidate's (state_bucket, action).
The q_learning module owns the learning side (constant-α MC from each closed
trade); this module owns the consumer side.
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


def _mark_fired(call_type: str, n_results: int) -> None:
    """Durable evidence for feature_health: every MemRL retrieval increments a
    counter and stamps a timestamp under memrl:* Redis keys."""
    try:
        import time as _t
        r = redis_client.get()
        r.incr(f"memrl:{call_type}_count")
        r.set(f"memrl:{call_type}_last_ts", str(int(_t.time())))
        r.set(f"memrl:{call_type}_last_n_results", str(n_results))
    except Exception:
        pass


def retrieve_relevant_memories(current_trade: dict, paper_closed_count: int, top_k: int = 10) -> list[dict]:
    """
    V-02/V-03: Two-phase retrieval.
    Phase 1 (always): pgvector cosine similarity — top 50 similar trades.
    Phase 2 (100+ trades): re-rank by net_pnl_usdt (Q-value) — return top 10.
    """
    embedding = _build_feature_vector(current_trade)
    top50 = _phase1_semantic_search(embedding, top_k=50)

    if paper_closed_count < 100:
        results = top50[:top_k]
        _mark_fired("retrieve_phase1", len(results))
        return results

    results = _phase2_quality_rerank(top50, top_k=top_k)
    _mark_fired("retrieve_phase2", len(results))
    return results


def get_cluster_context(current_trade: dict) -> dict | None:
    """V-06: Slow-memory consumer — return the nearest matching cluster's
    summary stats for the current trade context.

    Closes the cont. 6 producer/consumer gap: nightly consolidation builds
    rows in `memory_clusters` (regime × pair_class × direction × outcome
    centroids with running win_rate / avg_pnl / n_trades), but until cont. 14
    nothing read them. This function does the read.

    Match strategy: prefer EXACT (regime, pair_class, direction) match across
    win+loss outcomes (aggregating both into one cluster_context summary).
    Falls back to pgvector centroid cosine search when no exact bucket match
    exists. Returns None when the clusters table is empty.

    Output shape:
      {
        "n_trades": int,      # total trades across matched cluster(s)
        "win_rate": float,    # weighted across matched clusters by n_trades
        "avg_pnl_usdt": float,
        "matched_keys": [str],
        "match_kind": "exact_bucket" | "vector_nearest"
      }
    """
    pair = (current_trade.get("pair") or "").upper()
    direction = current_trade.get("direction")
    regime = current_trade.get("market_regime") or "unknown"
    # cont. 62c — backed by scanner-built bucket assignment.
    from risk.pair_classes import class_2 as _c2
    pair_class = "majors" if _c2(pair) == "major" else "alts"

    rows: list[tuple] = []
    matched_keys: list[str] = []
    match_kind = None

    # Cascade of progressively-relaxed symbolic matches before resorting to
    # vector search (vector match on a minimal probe dict is degenerate
    # because most embedding dims are zero). Order:
    #   1. EXACT bucket (regime, pair_class, direction)
    #   2. Cross-pair-class (regime, direction) — for new pairs in known regime
    #   3. Regime-only (regime) — for new direction/pair combos
    #   4. Vector-nearest — last resort
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                # 1. EXACT
                cur.execute(
                    "SELECT cluster_key, n_trades, win_rate, avg_pnl_usdt "
                    "FROM memory_clusters "
                    "WHERE market_regime=%s AND pair_class=%s AND direction=%s",
                    (regime, pair_class, direction),
                )
                rows = cur.fetchall()
                if rows:
                    match_kind = "exact_bucket"
                else:
                    # 2. cross-pair-class
                    cur.execute(
                        "SELECT cluster_key, n_trades, win_rate, avg_pnl_usdt "
                        "FROM memory_clusters "
                        "WHERE market_regime=%s AND direction=%s",
                        (regime, direction),
                    )
                    rows = cur.fetchall()
                    if rows:
                        match_kind = "regime_direction"
                    else:
                        # 3. regime-only
                        cur.execute(
                            "SELECT cluster_key, n_trades, win_rate, avg_pnl_usdt "
                            "FROM memory_clusters WHERE market_regime=%s",
                            (regime,),
                        )
                        rows = cur.fetchall()
                        if rows:
                            match_kind = "regime_only"
    except Exception as exc:
        log.warning("cluster_context_symbolic_failed", error=str(exc)[:120])
        return None

    if not rows:
        # 4. Last resort — vector-nearest on the centroid embedding.
        try:
            embedding = _build_feature_vector(current_trade)
            vec = json.dumps(embedding)
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT cluster_key, n_trades, win_rate, avg_pnl_usdt "
                        "FROM memory_clusters "
                        "WHERE centroid_embedding IS NOT NULL "
                        "ORDER BY centroid_embedding <=> %s::vector "
                        "LIMIT 3",
                        (vec,),
                    )
                    rows = cur.fetchall()
            if rows:
                match_kind = "vector_nearest"
        except Exception as exc:
            log.warning("cluster_context_vector_failed", error=str(exc)[:120])
            return None

    if not rows:
        return None

    total_n = 0
    weighted_wr_sum = 0.0
    weighted_pnl_sum = 0.0
    for key, n, wr, pnl in rows:
        n_int = int(n or 0)
        if n_int == 0:
            continue
        wr_f = float(wr or 0)
        pnl_f = float(pnl or 0)
        weighted_wr_sum  += wr_f  * n_int
        weighted_pnl_sum += pnl_f * n_int
        total_n          += n_int
        matched_keys.append(key)

    if total_n == 0:
        return None

    # Instrumentation for feature_health.
    try:
        import time as _t
        r = redis_client.get()
        r.incr("memrl:cluster_context_count")
        r.set("memrl:cluster_context_last_ts", str(int(_t.time())))
        r.set("memrl:cluster_context_last_match_kind", match_kind or "?")
        r.set("memrl:cluster_context_last_n_trades", str(total_n))
    except Exception:
        pass

    return {
        "n_trades":     total_n,
        "win_rate":     round(weighted_wr_sum / total_n, 2),
        "avg_pnl_usdt": round(weighted_pnl_sum / total_n, 4),
        "matched_keys": matched_keys,
        "match_kind":   match_kind,
    }


def get_base_rate_sample(current_trade: dict, top_k: int = 30,
                          recent_hours: int | None = 6) -> list[dict]:
    """Return unbiased sample of similar past trades for base-rate / win-rate analysis.

    Unlike retrieve_relevant_memories which Q-value-reranks at 100+ trades (biased toward
    historical wins by construction), this always returns top_k by semantic similarity
    only. Use this when you need to ESTIMATE the success rate of similar setups, not
    when you need INSPIRING examples to imitate.

    `recent_hours`: when set, only trades that closed within this window are sampled.
    Default 6h — deliberately short to keep the base rate tied to the *current* SL/risk
    config and let sparse neighborhoods (n<10) trigger the consumer's natural skip
    fallback in `signals/engine.py:364`. Widen this as post-fix trades accumulate.
    """
    embedding = _build_feature_vector(current_trade)
    results = _phase1_semantic_search(embedding, top_k=top_k, recent_hours=recent_hours)
    _mark_fired("base_rate_sample", len(results))
    return results


def _phase1_semantic_search(embedding: list[float], top_k: int = 50,
                             recent_hours: int | None = None) -> list[dict]:
    """V-02: pgvector cosine similarity search.

    Selects trade_potential_score + direction_confidence too so Phase 2 can
    bucket candidates the same way q_learning.update_q_from_trade did at
    close time. Backfill matches live writes by construction.

    `recent_hours`: optional time-window filter on exit_time. None = full history
    (default for Q-learning retrieval). Set by base-rate sampling to keep MemRL's
    win-rate verdict tied to recent data.
    """
    vec = json.dumps(embedding)
    where_extra = ""
    if recent_hours is not None and recent_hours > 0:
        # recent_hours is int we control — safe to inline as literal.
        where_extra = f" AND exit_time > NOW() - INTERVAL '{int(recent_hours)} hours'"
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, pair, direction, net_pnl_usdt, market_regime, "
                f"       trade_potential_score, direction_confidence, "
                f"       1 - (embedding <=> %s::vector) AS similarity "
                f"FROM trades WHERE embedding IS NOT NULL AND status = 'closed'{where_extra} "
                f"ORDER BY embedding <=> %s::vector LIMIT %s",
                (vec, vec, top_k),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def _phase2_quality_rerank(candidates: list[dict], top_k: int = 10) -> list[dict]:
    """V-03: Re-rank by learned Q-value from q_values table.

    For each candidate, derive (state_bucket, action) from its row, batch-fetch
    the learned Q. Rank by Q DESC. Candidates whose bucket has fewer than
    Q-learning's MIN_SAMPLES updates fall back to their raw net_pnl_usdt
    scaled into the same [-1, +1] reward space — so low-data buckets don't get
    crowd-promoted by a Q=0.0 default tie-break.

    Tie-breaker for equal Q (or equal fallback reward): prefer higher
    similarity (already implicit — list arrives ordered by similarity DESC
    from _phase1_semantic_search, sorted() is stable in Python).
    """
    from memory.cognitive.q_learning import (
        state_bucket, get_q_batch, is_q_trustworthy, reward_from_pnl,
    )

    if not candidates:
        return []

    # Build (bucket, action) tuples for every candidate; batch-fetch Q.
    keyed = []
    for t in candidates:
        b = state_bucket(
            regime=t.get("market_regime"),
            pair=t.get("pair"),
            strength=t.get("trade_potential_score") or t.get("direction_confidence"),
        )
        a = t.get("direction")
        keyed.append((t, b, a))

    pairs = list({(b, a) for _, b, a in keyed if a in ("long", "short")})
    q_lookup = get_q_batch(pairs) if pairs else {}

    # Cont. 17: cluster context blends into the score so Slow Memory affects
    # ranking, not just gating. Pre-fetch cluster aggregates once per unique
    # (regime, pair_class, direction) combo to avoid N queries.
    cluster_cache: dict[tuple, float | None] = {}
    cluster_blends = 0
    for t in candidates:
        # cont. 62c — backed by scanner-built bucket assignment.
        from risk.pair_classes import class_2 as _c2
        pair_class = "majors" if _c2((t.get("pair") or "").upper()) == "major" else "alts"
        key = (t.get("market_regime") or "unknown", pair_class, t.get("direction"))
        if key not in cluster_cache:
            ctx = get_cluster_context({
                "pair":          t.get("pair"),
                "direction":     t.get("direction"),
                "market_regime": t.get("market_regime"),
            })
            # Convert cluster win_rate to a [-1, +1] reward signal centered at 50%.
            # Require n_trades >= 30 to trust the cluster; below that, no blend.
            if ctx and ctx.get("n_trades", 0) >= 30:
                cluster_cache[key] = max(-1.0, min(1.0, (ctx["win_rate"] - 50.0) / 50.0))
            else:
                cluster_cache[key] = None

    def _score(item: tuple[dict, str, str]) -> float:
        t, b, a = item
        q_n = q_lookup.get((b, a))
        if q_n is not None and is_q_trustworthy(q_n[1]):
            base = q_n[0]
        else:
            # Fallback for low-data buckets — use the candidate's own normalized PnL.
            base = reward_from_pnl(t.get("net_pnl_usdt"))
        # Cluster blend: 70% base + 30% cluster signal when available.
        # cont. 62c — backed by scanner-built bucket assignment.
        from risk.pair_classes import class_2 as _c2
        pair_class = "majors" if _c2((t.get("pair") or "").upper()) == "major" else "alts"
        key = (t.get("market_regime") or "unknown", pair_class, t.get("direction"))
        cluster_signal = cluster_cache.get(key)
        if cluster_signal is not None:
            return 0.7 * base + 0.3 * cluster_signal
        return base

    keyed.sort(key=_score, reverse=True)

    # Tally blends for evidence (post-hoc — we already used the cache above).
    for key, sig in cluster_cache.items():
        if sig is not None:
            cluster_blends += 1

    # Evidence keys for feature_health — track Q consultation + cluster blending.
    try:
        import time as _t
        r = redis_client.get()
        r.incr("memrl:phase2_q_lookups_count")
        r.set("memrl:phase2_q_lookups_last_ts", str(int(_t.time())))
        r.set("memrl:phase2_q_lookups_last_size", str(len(pairs)))
        if cluster_blends > 0:
            r.incr("memrl:phase2_cluster_blend_count")
            r.set("memrl:phase2_cluster_blend_last_size", str(cluster_blends))
    except Exception:
        pass

    return [t for (t, _b, _a) in keyed[:top_k]]


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

    # V-05: Fast → Slow memory consolidation. Promotes recent trades into
    # the memory_clusters table (regime × pair_class × direction × outcome
    # buckets with centroid embedding + running summary stats). Migration
    # 015 introduced the destination table; module added 2026-05-21 (cont. 6).
    consolidation_result = None
    try:
        from memory.cognitive.consolidation import consolidate_fast_to_slow
        consolidation_result = consolidate_fast_to_slow()
        log.info("fast_to_slow_consolidation_done", **consolidation_result)
    except Exception as exc:
        log.warning("fast_to_slow_consolidation_failed", error=str(exc)[:200])

    # Blueprint F17 EWC — snapshot Fisher info against the world model reward head
    # so post-300-trade online updates penalise deviation from old-regime params.
    # F30 governance gate.
    ewc_status = None
    try:
        from feature_governance.registry import is_active as _fg_active_f17
        if _fg_active_f17("F17"):
            from ml.continual_learning import (
                consolidate_world_model_if_ready, load_state, replay_buffer_size,
            )
            # If the replay buffer is empty in this process (post-restart), nothing to do —
            # the buffer will refill from live closes. We still try to load prior Fisher
            # state so existing snapshots survive.
            load_state()
            from memory.query import get_paper_closed_count
            ewc_status = consolidate_world_model_if_ready(get_paper_closed_count())
            log.info("ewc_consolidation_attempted", status=ewc_status,
                     buffer_size=replay_buffer_size())
    except Exception as exc:
        log.warning("ewc_consolidation_skipped", error=str(exc))

    log.info("sleep_consolidation_complete", reviewed=len(trade_ids),
             updated=updated, ewc=ewc_status,
             fast_to_slow=consolidation_result)
    return {"reviewed": len(trade_ids), "updated": updated, "ewc": ewc_status,
            "fast_to_slow": consolidation_result}
