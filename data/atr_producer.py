"""Real ATR(14) producer — writes {pair}:atr to Redis from 1min candle data.

Blueprint §F46 side-effect: CandleNet inference already writes ATR as part
of its run_inference() cycle. This module provides a standalone lightweight
loop for pairs that haven't been inferred yet (e.g. during warm-up) and for
intervals where CandleNet is not running.

Reads: {pair}:1m:candles  (Redis list, newest-first, CONTEXT_LENGTH entries)
Writes: {pair}:atr        (float, no TTL — risk/manager.py reads this key)

Also computes NATR (normalised ATR = ATR / close × 100) for the dashboard:
Writes: {pair}:natr       (float %)

Run as a Celery periodic task every 60 seconds (see celery_app.py).
"""
import json
import structlog
import redis_client
import redis_keys
from ml.ha_features import compute_atr

log = structlog.get_logger()

ATR_PERIOD   = 14
MIN_CANDLES  = ATR_PERIOD + 5   # minimum candles needed


def update_atr_for_pair(pair: str) -> bool:
    """Compute and write ATR + NATR for one pair. Returns True on success."""
    r = redis_client.get()
    key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1m")
    raw = r.lrange(key, 0, MIN_CANDLES + 10)
    if len(raw) < MIN_CANDLES:
        return False

    candles = [json.loads(c) for c in reversed(raw)]
    highs  = [float(c["h"]) for c in candles]
    lows   = [float(c["l"]) for c in candles]
    closes = [float(c["c"]) for c in candles]

    atr = compute_atr(highs, lows, closes, period=ATR_PERIOD)
    if atr is None or atr <= 0:
        return False

    current_close = closes[-1]
    r.set(f"{pair}:atr", round(float(atr), 8))
    if current_close > 0:
        natr = atr / current_close * 100
        r.set(f"{pair}:natr", round(float(natr), 4))
    return True


def update_atr_all_active() -> int:
    """Update ATR for all active pairs. Returns count of successful updates."""
    r = redis_client.get()
    try:
        # cont. 58 fix: scanner:active_pairs is a Redis SET (redis_keys.ACTIVE_PAIRS).
        # Prior `r.get(...)` raised WRONGTYPE on every tick.
        pairs = sorted(r.smembers("scanner:active_pairs"))
        if not pairs:
            return 0
    except Exception:
        return 0

    updated = 0
    for pair in pairs:
        try:
            if update_atr_for_pair(pair):
                updated += 1
        except Exception as exc:
            log.warning("atr_producer_pair_failed",
                        pair=pair, error=str(exc)[:120])
    return updated
