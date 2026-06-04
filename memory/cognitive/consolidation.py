"""
Section V-05: F35 Fast→Slow memory consolidation.

Blueprint Feature 35 ("Dual Memory System — Complementary Learning Systems"):
  Fast = raw individual trades (Redis sorted set + trades table rows).
  Slow = consolidated patterns/clusters distilled from many trades
         (memory_clusters table, introduced by migration 015).

This module owns the promotion step Fast → Slow. Runs from
`memory.cognitive.memrl.run_sleep_consolidation` on the existing nightly
sleep schedule.

Cluster definition: a row in `memory_clusters` keyed by
  (market_regime, pair_class, direction, outcome_class)
where:
  - regime          ∈ {bull, bear, turbulent, ...} from trades.market_regime
  - pair_class      ∈ {majors, alts}              (BTC/ETH vs everything else)
  - direction       ∈ {long, short}
  - outcome_class   ∈ {win, loss, breakeven}      from net_pnl_usdt sign

Each cluster row carries:
  - centroid_embedding     : average of member trades' pgvector embeddings
  - n_trades               : running count
  - win_rate, avg_pnl_usdt, avg_hold_seconds  : running summary stats
  - last_updated, created_at

Incremental updates use a weighted running-average formula so old regime
knowledge isn't overwritten by new arrivals — the blueprint's "prevent
catastrophic forgetting between market regimes" mechanism.

Rule 4 honesty notes — see PROGRESS.md 2026-05-21 (cont. 6) for the full set.
"""
from __future__ import annotations

import time
import structlog

import redis_client
import redis_keys
from db import db_conn

log = structlog.get_logger()


_BREAKEVEN_USDT = 0.01     # |net_pnl| smaller than this → breakeven
_MIN_CLUSTER_SIZE = 3      # batches with fewer than N member trades → skipped as noise
_RECENT_TRADES_LOOKBACK = 500   # consolidate the last N closed trades on each run


def _classify_pair(pair: str | None) -> str:
    # cont. 62c — backed by scanner-built bucket assignment.
    if not pair:
        return "alts"
    from risk.pair_classes import class_2
    return "majors" if class_2(pair.upper()) == "major" else "alts"


def _classify_outcome(net_pnl: float | None) -> str:
    if net_pnl is None:
        return "breakeven"
    if net_pnl > _BREAKEVEN_USDT:
        return "win"
    if net_pnl < -_BREAKEVEN_USDT:
        return "loss"
    return "breakeven"


def _fetch_recent_trades(limit: int) -> list[dict]:
    """Pull the most-recent closed trades with embeddings populated."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, pair, direction, market_regime, net_pnl_usdt, "
                "       hold_time_seconds, embedding "
                "FROM trades "
                "WHERE status='closed' AND embedding IS NOT NULL "
                "ORDER BY exit_time DESC NULLS LAST LIMIT %s",
                (limit,),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def _parse_embedding(raw) -> list[float] | None:
    """pgvector comes back as either str ('[0.1, 0.2, ...]') or list depending
    on psycopg adapter. Handle both."""
    if raw is None:
        return None
    if isinstance(raw, list):
        return [float(x) for x in raw]
    if isinstance(raw, str):
        try:
            import json
            return [float(x) for x in json.loads(raw)]
        except Exception:
            return None
    return None


def consolidate_fast_to_slow(
    limit: int = _RECENT_TRADES_LOOKBACK,
    min_cluster_size: int = _MIN_CLUSTER_SIZE,
) -> dict:
    """V-05: Fast → Slow memory consolidation.

    Reads the last `limit` closed trades, groups by cluster key, computes the
    batch centroid + summary stats, then UPSERTs into memory_clusters with
    incremental running-average updates. Clusters with fewer than
    `min_cluster_size` member trades in THIS batch are skipped as noise.
    """
    trades = _fetch_recent_trades(limit)
    if not trades:
        log.info("consolidation_no_trades")
        return {
            "status": "ok", "trades_processed": 0,
            "clusters_updated": 0, "clusters_created": 0, "noise_skipped": 0,
        }

    # Bucket trades by cluster key.
    buckets: dict[tuple, list[dict]] = {}
    for t in trades:
        emb = _parse_embedding(t.get("embedding"))
        if emb is None:
            continue
        regime  = t.get("market_regime") or "unknown"
        pclass  = _classify_pair(t.get("pair"))
        dirn    = t.get("direction") or "unknown"
        outcome = _classify_outcome(float(t.get("net_pnl_usdt") or 0))
        key = (regime, pclass, dirn, outcome)
        t["_embedding_list"] = emb
        buckets.setdefault(key, []).append(t)

    updated = created = noise = 0
    for (regime, pclass, dirn, outcome), members in buckets.items():
        if len(members) < min_cluster_size:
            noise += 1
            continue
        cluster_key = f"{regime}|{pclass}|{dirn}|{outcome}"
        result = _upsert_cluster(
            cluster_key=cluster_key,
            regime=regime,
            pair_class=pclass,
            direction=dirn,
            outcome_class=outcome,
            members=members,
        )
        if result == "created":
            created += 1
        elif result == "updated":
            updated += 1

    try:
        r = redis_client.get()
        r.incr("memrl:consolidation_count")
        r.set("memrl:consolidation_last_ts", int(time.time()))
        r.set("memrl:consolidation_last_clusters_updated", updated)
        r.set("memrl:consolidation_last_clusters_created", created)
        r.set("memrl:consolidation_last_trades_processed", len(trades))
    except Exception:
        pass

    log.info("consolidation_complete",
             trades_processed=len(trades),
             clusters_updated=updated,
             clusters_created=created,
             noise_skipped=noise)
    return {
        "status": "ok",
        "trades_processed": len(trades),
        "clusters_updated": updated,
        "clusters_created": created,
        "noise_skipped": noise,
    }


def _upsert_cluster(
    cluster_key: str,
    regime: str,
    pair_class: str,
    direction: str,
    outcome_class: str,
    members: list[dict],
) -> str:
    """Insert new cluster or merge into existing one with incremental running
    averages. Returns 'created' or 'updated'."""
    batch_n = len(members)
    if batch_n == 0:
        return "noop"

    # Compute batch-side aggregates.
    embs = [m["_embedding_list"] for m in members]
    dim = len(embs[0])
    batch_centroid = [sum(e[i] for e in embs) / batch_n for i in range(dim)]

    wins        = sum(1 for m in members if (float(m.get("net_pnl_usdt") or 0)) > _BREAKEVEN_USDT)
    batch_winr  = (wins / batch_n) * 100.0
    batch_pnl   = sum(float(m.get("net_pnl_usdt") or 0) for m in members) / batch_n
    batch_hold  = sum(int(m.get("hold_time_seconds") or 0) for m in members) // batch_n

    import json
    centroid_str = "[" + ",".join(f"{x:.6f}" for x in batch_centroid) + "]"

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT n_trades, centroid_embedding, win_rate, avg_pnl_usdt, "
                "       avg_hold_seconds "
                "FROM memory_clusters WHERE cluster_key = %s",
                (cluster_key,),
            )
            existing = cur.fetchone()

            if existing is None:
                # Create
                cur.execute(
                    "INSERT INTO memory_clusters ("
                    "  cluster_key, market_regime, pair_class, direction, "
                    "  outcome_class, centroid_embedding, n_trades, win_rate, "
                    "  avg_pnl_usdt, avg_hold_seconds, last_updated, created_at"
                    ") VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s, %s, %s, NOW(), NOW())",
                    (cluster_key, regime, pair_class, direction, outcome_class,
                     centroid_str, batch_n, round(batch_winr, 2),
                     round(batch_pnl, 4), int(batch_hold)),
                )
                return "created"

            # Update — running weighted average. Old N + batch N denominator.
            old_n        = int(existing[0])
            old_centroid = _parse_embedding(existing[1])
            old_winr     = float(existing[2] or 0)
            old_pnl      = float(existing[3] or 0)
            old_hold     = int(existing[4] or 0)

            total_n  = old_n + batch_n
            if old_centroid and len(old_centroid) == dim:
                new_centroid = [
                    (old_centroid[i] * old_n + batch_centroid[i] * batch_n) / total_n
                    for i in range(dim)
                ]
            else:
                new_centroid = batch_centroid
            new_winr = (old_winr * old_n + batch_winr * batch_n) / total_n
            new_pnl  = (old_pnl  * old_n + batch_pnl  * batch_n) / total_n
            new_hold = (old_hold * old_n + batch_hold * batch_n) // total_n
            centroid_str_new = "[" + ",".join(f"{x:.6f}" for x in new_centroid) + "]"

            cur.execute(
                "UPDATE memory_clusters SET "
                "  centroid_embedding = %s::vector, n_trades = %s, "
                "  win_rate = %s, avg_pnl_usdt = %s, avg_hold_seconds = %s, "
                "  last_updated = NOW() "
                "WHERE cluster_key = %s",
                (centroid_str_new, total_n, round(new_winr, 2),
                 round(new_pnl, 4), int(new_hold), cluster_key),
            )
            return "updated"
