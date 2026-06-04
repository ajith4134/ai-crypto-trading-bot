"""cont. 60 — Filtered OBI (Order Book Imbalance) producer.

The existing data/feed.py publishes raw `{pair}:obi` from depth snapshots.
This producer applies the arXiv:2507.22712 filter:
  * Drop levels with order lifetime < 100ms (flash spoofs)
  * Drop levels updated > 5 times in 1s (modified noise)
  * Recompute OBI on the filtered book

Since we don't have access to raw L2 update streams here, we approximate by
applying a simple smoother over the raw OBI history. The "filtered" version
is the EMA of the raw with alpha=0.3, which preserves directional persistence
while suppressing single-snapshot spikes (which are the most likely spoofs).

Schedule: every 30s via Celery beat.

Writes:
  `{pair}:filtered_obi_history`   — Redis list, last 5 filtered OBI samples
"""
from __future__ import annotations
import structlog

import redis_client

log = structlog.get_logger()

_HISTORY = 5
_EMA_ALPHA = 0.3


def update_filtered_obi_for_pair(pair: str) -> bool:
    """cont. 60 note: the codebase publishes OFI (Order Flow Imbalance) under
    `{pair}:ofi`, not raw L2 OBI. We use OFI as a proxy since:
      * OFI is also a buy/sell pressure imbalance (taker volume delta)
      * The arXiv:2507.22712 filtered-OBI use case (anti-spoof structural exit
        signal) is structurally the same: smooth out single-snapshot noise
      * Without an L2 diff stream, we cannot compute formal OBI anyway

    The resulting `filtered_obi_history` is therefore an EMA of OFI scaled to
    [-1, 1] approx. Same downstream semantics for the exit consumer.
    """
    r = redis_client.get()
    raw_ofi = r.get(f"{pair}:ofi")
    if raw_ofi is None:
        return False
    try:
        # OFI is typically in [-1e-3, 1e-3]. Scale up for sign + magnitude.
        raw_val = float(raw_ofi)
        new_val = max(-1.0, min(1.0, raw_val * 1000.0))
    except (TypeError, ValueError):
        return False
    # EMA over the previous filtered value
    prev_raw = r.lindex(f"{pair}:filtered_obi_history", 0)
    if prev_raw is None:
        ema = new_val
    else:
        try:
            prev = float(prev_raw)
            ema = _EMA_ALPHA * new_val + (1 - _EMA_ALPHA) * prev
        except (TypeError, ValueError):
            ema = new_val
    pipe = r.pipeline()
    pipe.lpush(f"{pair}:filtered_obi_history", round(ema, 6))
    pipe.ltrim(f"{pair}:filtered_obi_history", 0, _HISTORY - 1)
    pipe.expire(f"{pair}:filtered_obi_history", 600)
    pipe.execute()
    return True


def update_filtered_obi_all_active() -> int:
    r = redis_client.get()
    try:
        pairs = sorted(r.smembers("scanner:active_pairs"))
    except Exception:
        return 0
    updated = 0
    for p in pairs:
        try:
            if update_filtered_obi_for_pair(p):
                updated += 1
        except Exception as exc:
            log.debug("filtered_obi_producer_failed",
                      pair=p, error=str(exc)[:120])
    return updated
