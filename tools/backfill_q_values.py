"""One-shot backfill: replay every historical closed trade through the
constant-α Monte Carlo Q-update so the q_values table starts populated.

Without this, F35 Phase 2 retrieval falls back to per-candidate raw PnL until
enough new closes accumulate per bucket — wasting the bot's accumulated trade
history. Backfill is deterministic: bucketing uses the same q_learning.state_bucket
function as live updates, ordered by exit_time ASC so the MC update order
mirrors how it would have looked had Q-learning existed since day 1.

Run inside the brain container:
    docker exec trading-bot-brain-1 python -m tools.backfill_q_values
"""
from __future__ import annotations
import sys
import structlog

from db import db_conn
from memory.cognitive.q_learning import update_q_from_trade

log = structlog.get_logger()


def backfill(limit: int | None = None) -> dict:
    """Replay closed paper trades in chronological order through update_q_from_trade.

    Returns a stats dict with counts.
    """
    sql = (
        "SELECT id, pair, direction, market_regime, "
        "       trade_potential_score, direction_confidence, net_pnl_usdt, "
        "       exit_time "
        "FROM trades "
        "WHERE status = 'closed' "
        "  AND is_paper = TRUE "
        "  AND direction IN ('long','short') "
        "  AND net_pnl_usdt IS NOT NULL "
        "ORDER BY exit_time ASC"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    n_total = 0
    n_long = 0
    n_short = 0
    n_skipped = 0
    bucket_counts: dict[str, int] = {}

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()

    for row in rows:
        n_total += 1
        trade = {
            "pair": row[1],
            "direction": row[2],
            "market_regime": row[3],
            "trade_potential_score": row[4],
            "direction_confidence": row[5],
            "net_pnl_usdt": float(row[6]) if row[6] is not None else 0.0,
        }
        try:
            res = update_q_from_trade(trade)
            if res.get("status") == "updated":
                bucket = res.get("bucket")
                if bucket:
                    bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
                if trade["direction"] == "long":
                    n_long += 1
                else:
                    n_short += 1
            else:
                n_skipped += 1
        except Exception as exc:
            n_skipped += 1
            log.warning("backfill_trade_failed",
                        trade_id=str(row[0])[:8], error=str(exc)[:120])

    stats = {
        "total_processed": n_total,
        "long_updates": n_long,
        "short_updates": n_short,
        "skipped": n_skipped,
        "distinct_buckets": len(bucket_counts),
        "per_bucket_counts": bucket_counts,
    }
    log.info("backfill_complete", **{k: v for k, v in stats.items()
                                      if k != "per_bucket_counts"})
    print("\n=== Q-values backfill complete ===")
    print(f"  total processed     : {stats['total_processed']}")
    print(f"  long updates        : {stats['long_updates']}")
    print(f"  short updates       : {stats['short_updates']}")
    print(f"  skipped             : {stats['skipped']}")
    print(f"  distinct buckets    : {stats['distinct_buckets']}")
    print("  per-bucket counts:")
    for b, n in sorted(bucket_counts.items()):
        print(f"    {b:<40s}  {n:>6d}")
    return stats


if __name__ == "__main__":
    backfill()
