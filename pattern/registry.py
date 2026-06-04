"""pattern_effectiveness_registry CRUD + on-close updater (Phase A.3, cont. 55).

Schema: migrations/027_predict_all_schema.sql (table pattern_effectiveness_registry).
Called by:
  - celery_app.update_pattern_registry beat task (added later in Phase A.4)
  - Phase B/C predict gates via lookup() for signal-side effectiveness reads
"""
from __future__ import annotations
import structlog

log = structlog.get_logger()


def lookup(pattern_cluster_id: int,
           market_regime: str,
           direction: str) -> dict | None:
    """Return registry row for (cluster, regime, direction) or None."""
    if pattern_cluster_id is None or pattern_cluster_id < 0:
        return None
    from db import db_conn
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT n_trades, n_wins, avg_rr, avg_hold_seconds,
                       net_pnl_usdt, calibration_ece, drift_flagged
                FROM pattern_effectiveness_registry
                WHERE pattern_cluster_id = %s
                  AND market_regime      = %s
                  AND direction          = %s
            """, (pattern_cluster_id, market_regime, direction))
            row = cur.fetchone()
    if row is None:
        return None
    n_trades, n_wins, avg_rr, avg_hold, net_pnl, ece, drift = row
    return {
        "pattern_cluster_id": pattern_cluster_id,
        "market_regime":      market_regime,
        "direction":          direction,
        "n_trades":           int(n_trades or 0),
        "n_wins":             int(n_wins or 0),
        "win_rate":           (n_wins / n_trades) if n_trades else None,
        "avg_rr":             float(avg_rr) if avg_rr is not None else None,
        "avg_hold_seconds":   int(avg_hold) if avg_hold is not None else None,
        "net_pnl_usdt":       float(net_pnl or 0),
        "calibration_ece":    float(ece) if ece is not None else None,
        "drift_flagged":      bool(drift),
    }


def update_on_close(trade_id: str) -> dict | None:
    """Recompute the registry row for the (cluster, regime, direction) tied to
    `trade_id`. Idempotent — recomputes from all closed trades in that bucket.

    Returns updated row dict or None if the trade has no pattern_cluster_id.
    """
    from db import db_conn
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT pattern_cluster_id, market_regime, direction
                FROM trades WHERE id = %s
            """, (trade_id,))
            row = cur.fetchone()
    if not row:
        log.debug("pattern_registry_update_trade_not_found", trade_id=trade_id)
        return None
    cluster_id, regime, direction = row
    if cluster_id is None or cluster_id < 0:
        return None

    with db_conn() as conn:
        with conn.cursor() as cur:
            # cont. 63 (2026-05-29) bugfix: column was `closed_at` (doesn't
            # exist in this schema) → use `status='closed'` + `exit_time`.
            cur.execute("""
                SELECT
                  COUNT(*) AS n_trades,
                  COUNT(*) FILTER (WHERE net_pnl_usdt > 0) AS n_wins,
                  AVG(actual_rr) AS avg_rr,
                  AVG(hold_time_seconds)::int AS avg_hold,
                  SUM(net_pnl_usdt) AS net_pnl
                FROM trades
                WHERE pattern_cluster_id = %s
                  AND market_regime      = %s
                  AND direction          = %s
                  AND status             = 'closed'
                  AND exit_time          IS NOT NULL
            """, (cluster_id, regime, direction))
            n_trades, n_wins, avg_rr, avg_hold, net_pnl = cur.fetchone()
            cur.execute("""
                INSERT INTO pattern_effectiveness_registry
                  (pattern_cluster_id, market_regime, direction,
                   n_trades, n_wins, avg_rr, avg_hold_seconds, net_pnl_usdt,
                   last_updated)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (pattern_cluster_id, market_regime, direction)
                DO UPDATE SET
                  n_trades         = EXCLUDED.n_trades,
                  n_wins           = EXCLUDED.n_wins,
                  avg_rr           = EXCLUDED.avg_rr,
                  avg_hold_seconds = EXCLUDED.avg_hold_seconds,
                  net_pnl_usdt     = EXCLUDED.net_pnl_usdt,
                  last_updated     = NOW()
            """, (cluster_id, regime, direction,
                  int(n_trades or 0), int(n_wins or 0),
                  float(avg_rr) if avg_rr is not None else None,
                  int(avg_hold) if avg_hold is not None else None,
                  float(net_pnl or 0)))
            conn.commit()
    log.info("pattern_registry_updated",
             cluster_id=cluster_id, regime=regime, direction=direction,
             n_trades=int(n_trades or 0), n_wins=int(n_wins or 0))
    return lookup(cluster_id, regime, direction)
