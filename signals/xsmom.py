"""F45 Cross-Sectional Momentum (Liu-Tsyvinski 2022) — producer side.

Reference: Liu, Tsyvinski (2022) "Common Risk Factors in Cryptocurrency",
Journal of Finance. Cross-sectional momentum is the dominant idiosyncratic
alpha factor in their three-factor model (market, size, momentum).

This module is the PRODUCER. Reads 1h candles for every active pair, computes
the 7d return + max 1h return inside that window, ranks the universe, writes
per-pair rank/return/max to Redis. The signal-engine consumer in
`signals/engine.py` reads these keys to modulate per-candidate signal strength.

Sparse data handling: pairs with fewer than 168 1h candles are dropped from
ranking. If the universe shrinks below 5 surviving pairs, the function aborts
without overwriting prior values (so a transient candle gap doesn't poison the
modulator's input).

Blueprint Section 4.1 Feature 45 — gated by F45 in feature_governance.
"""
import json
import time
import structlog

import redis_client
import redis_keys

log = structlog.get_logger()

# Tuning constants (intentionally hand-picked; documented in blueprint as Rule 4 simplifications).
_LOOKBACK_HOURS    = 168     # 7 days × 24h
_MIN_CANDLES       = 168     # need full 7d history to qualify
_MIN_UNIVERSE_SIZE = 5       # below this, ranking is meaningless — abort
_TTL_SEC           = 900     # 15 min — stale producer means consumer should skip
_INTERVAL          = "1h"


def _candle_close(item: str | bytes) -> float | None:
    """Parse one candle list entry → close as float, or None on bad data."""
    try:
        d = json.loads(item)
        return float(d.get("c"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _per_pair_metrics(r, pair: str) -> tuple[float, float] | None:
    """Compute (return_7d, max_1h_7d) for one pair, or None if insufficient data."""
    key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", _INTERVAL)
    n = r.llen(key)
    if n < _MIN_CANDLES:
        return None

    # Pull the most recent _MIN_CANDLES entries. Order in Redis depends on the
    # producer (`data/feed.py` polls 1h klines); we treat the list as newest-at-
    # head OR oldest-at-head agnostically by reading the full window and using
    # the first vs last close. The actual order is checked at parse time.
    raw = r.lrange(key, 0, _MIN_CANDLES - 1)
    closes = [_candle_close(x) for x in raw]
    closes = [c for c in closes if c is not None and c > 0]
    if len(closes) < _MIN_CANDLES // 2:
        return None  # too many bad entries

    # Determine order by checking timestamps on first vs last entry.
    try:
        first = json.loads(raw[0])
        last  = json.loads(raw[-1])
        first_t = int(first.get("t", 0))
        last_t  = int(last.get("t", 0))
        newest_first = first_t > last_t
    except Exception:
        newest_first = True  # safe default

    if newest_first:
        latest_close = closes[0]
        oldest_close = closes[-1]
    else:
        latest_close = closes[-1]
        oldest_close = closes[0]

    if oldest_close <= 0:
        return None

    return_7d = (latest_close - oldest_close) / oldest_close

    # max 1h return inside the window: max |close[i] / close[i-1] - 1|
    ordered = closes if not newest_first else list(reversed(closes))
    max_abs_1h = 0.0
    for i in range(1, len(ordered)):
        prev = ordered[i - 1]
        if prev > 0:
            ret = abs(ordered[i] / prev - 1.0)
            if ret > max_abs_1h:
                max_abs_1h = ret

    return return_7d, max_abs_1h


def compute_and_publish() -> dict:
    """Compute cross-sectional momentum ranking and publish to Redis.

    Called by the Celery beat task `xsmom_compute_task` every 5 minutes.
    Returns a status dict for observability.
    """
    r = redis_client.get()

    try:
        from feature_governance.registry import is_active
        if not is_active("F45"):
            return {"status": "f45_inactive"}
    except Exception:
        pass

    pairs = list(r.smembers(redis_keys.ACTIVE_PAIRS))
    if not pairs:
        return {"status": "no_active_pairs"}

    # Compute per-pair metrics.
    metrics: dict[str, tuple[float, float]] = {}
    skipped_sparse = 0
    for pair in pairs:
        if isinstance(pair, bytes):
            pair = pair.decode()
        m = _per_pair_metrics(r, pair)
        if m is None:
            skipped_sparse += 1
            continue
        metrics[pair] = m

    if len(metrics) < _MIN_UNIVERSE_SIZE:
        log.warning("xsmom_universe_too_small",
                    surviving=len(metrics), skipped=skipped_sparse,
                    total=len(pairs))
        return {
            "status":     "universe_too_small",
            "surviving":  len(metrics),
            "skipped":    skipped_sparse,
        }

    # Rank by 7d return ascending; percentile = i / (n - 1) so bottom=0, top=1.
    sorted_pairs = sorted(metrics.items(), key=lambda kv: kv[1][0])
    n = len(sorted_pairs)

    pipe = r.pipeline()
    for i, (pair, (ret_7d, max_1h)) in enumerate(sorted_pairs):
        rank = i / (n - 1) if n > 1 else 0.5
        pipe.setex(redis_keys.XSMOM_RANK.replace("{pair}", pair),
                   _TTL_SEC, f"{rank:.6f}")
        pipe.setex(redis_keys.XSMOM_RETURN_7D.replace("{pair}", pair),
                   _TTL_SEC, f"{ret_7d:.8f}")
        pipe.setex(redis_keys.XSMOM_MAX_1H_7D.replace("{pair}", pair),
                   _TTL_SEC, f"{max_1h:.8f}")
    pipe.set(redis_keys.XSMOM_UPDATED_AT, str(int(time.time())))
    pipe.set(redis_keys.XSMOM_UNIVERSE_SIZE, str(n))
    pipe.execute()

    log.info("xsmom_published", surviving=n, skipped=skipped_sparse,
             top_pair=sorted_pairs[-1][0], top_return=round(sorted_pairs[-1][1][0], 4),
             bottom_pair=sorted_pairs[0][0], bottom_return=round(sorted_pairs[0][1][0], 4))

    return {
        "status":     "ok",
        "surviving":  n,
        "skipped":    skipped_sparse,
        "top_pair":   sorted_pairs[-1][0],
        "top_return": sorted_pairs[-1][1][0],
    }
