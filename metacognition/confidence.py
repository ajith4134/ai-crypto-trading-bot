"""R4 — Bot Self-Confidence Index (Bundle B / cont. 55).

Hourly-recomputed scalar in [0.20, 1.00] derived from the last 1h of:
  hits   = closed trades exited via trailing_sl with net_pnl > 0
  misses = F9 decodes inserted (rejected-but-would-have-won)
  losers = F12 decodes inserted (high-pot losers paired with low-pot winners)

confidence = clip( (hits + 1) / (hits + misses + losers + 1), 0.20, 1.00 )

Consumers (per project_reconciled_bundle_b memory):
  - signals/engine.py — capital_usdt *= confidence_t  (sizing site near line 1742)
  - signals/engine.py — hard-skip when confidence_t < 0.30
  - risk/manager.py:1209 — trail_dist_pct *= (2.0 - confidence_t)

F46 governance gate: get_confidence() returns 1.0 (no-op) when F46 inactive.
"""
from __future__ import annotations
import structlog
import time

import redis_client
import redis_keys

log = structlog.get_logger()

_CONFIDENCE_FLOOR    = 0.20
_CONFIDENCE_CEILING  = 1.00
_HARD_SKIP_THRESHOLD = 0.30


def _governance_active() -> bool:
    try:
        from feature_governance.registry import is_active
        return bool(is_active("F46"))
    except Exception:
        return True


def compute_and_store(window_seconds: int = 3600) -> dict:
    """Compute confidence over the last `window_seconds`, persist to Redis.
    Called by celery beat task `compute_bot_confidence` every 5 minutes."""
    from db import db_conn

    if not _governance_active():
        log.debug("bot_confidence_governance_inactive")
        return {"status": "skipped", "reason": "F46_inactive"}

    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                      (SELECT COUNT(*) FROM trades
                         WHERE exit_reason = 'trailing_sl'
                           AND net_pnl_usdt > 0
                           AND closed_at > NOW() - (%s || ' seconds')::interval) AS hits,
                      (SELECT COUNT(*) FROM counterfactuals
                         WHERE miss_decoded = TRUE
                           AND created_at > NOW() - (%s || ' seconds')::interval) AS misses,
                      (SELECT COUNT(*) FROM mismatches
                         WHERE decoded_at > NOW() - (%s || ' seconds')::interval) AS losers
                """, (window_seconds, window_seconds, window_seconds))
                hits, misses, losers = cur.fetchone()
    except Exception as exc:
        log.warning("bot_confidence_db_failed", error=str(exc)[:200])
        return {"status": "error", "reason": "db"}

    hits, misses, losers = int(hits or 0), int(misses or 0), int(losers or 0)
    raw = (hits + 1.0) / (hits + misses + losers + 1.0)
    conf = max(_CONFIDENCE_FLOOR, min(_CONFIDENCE_CEILING, raw))

    r = redis_client.get()
    try:
        r.set(redis_keys.BRAIN_BOT_CONFIDENCE, round(conf, 4))
        r.set(redis_keys.BRAIN_BOT_CONFIDENCE + ":updated_ts", int(time.time()))
        r.set(redis_keys.BRAIN_BOT_CONFIDENCE + ":components",
              f"hits={hits},miss={misses},lose={losers}")
    except Exception as exc:
        log.warning("bot_confidence_redis_failed", error=str(exc)[:200])

    log.info("bot_confidence_computed",
             confidence=round(conf, 4),
             hits=hits, misses=misses, losers=losers)
    return {"status": "ok", "confidence": round(conf, 4),
            "hits": hits, "misses": misses, "losers": losers}


def get_confidence() -> float:
    """Read the cached confidence value. Returns 1.0 (no-op) on any error
    or when F46 inactive. Consumers MUST default to no-op behavior on 1.0."""
    if not _governance_active():
        return 1.0
    r = redis_client.get()
    try:
        v = r.get(redis_keys.BRAIN_BOT_CONFIDENCE)
        if v is None:
            return 1.0
        c = float(v)
        return max(_CONFIDENCE_FLOOR, min(_CONFIDENCE_CEILING, c))
    except Exception:
        return 1.0


def should_hard_skip() -> bool:
    """R4 entry-gate hard-skip. True when confidence below 0.30."""
    if not _governance_active():
        return False
    return get_confidence() < _HARD_SKIP_THRESHOLD
