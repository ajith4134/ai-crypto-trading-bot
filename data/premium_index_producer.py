"""cont. 60 — Premium index producer (mark vs spot).

Reads `{pair}:mark_price` and `{pair}:last_price` (where the latter is the
spot/last-trade price from Binance) and computes:

  premium = (mark - spot) / spot

Schedule: every 60s via Celery beat.

Writes:
  `{pair}:premium_index`   — float, signed
"""
from __future__ import annotations
import structlog

import redis_client

log = structlog.get_logger()


def update_premium_for_pair(pair: str) -> bool:
    r = redis_client.get()
    mark_raw = r.get(f"{pair}:mark_price")
    spot_raw = r.get(f"{pair}:last_price")
    if mark_raw is None or spot_raw is None:
        return False
    try:
        mark = float(mark_raw)
        spot = float(spot_raw)
    except (TypeError, ValueError):
        return False
    if spot <= 0:
        return False
    premium = (mark - spot) / spot
    r.set(f"{pair}:premium_index", round(premium, 8))
    return True


def update_premium_all_active() -> int:
    r = redis_client.get()
    try:
        pairs = sorted(r.smembers("scanner:active_pairs"))
    except Exception:
        return 0
    updated = 0
    for p in pairs:
        try:
            if update_premium_for_pair(p):
                updated += 1
        except Exception as exc:
            log.debug("premium_producer_failed",
                      pair=p, error=str(exc)[:120])
    return updated
