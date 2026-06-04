"""cont. 60 — Cumulative Volume Delta (CVD) producer.

Reads recent 1m candles for each active pair, computes per-candle CVD from
(taker_buy_base_asset_volume - taker_sell_base_asset_volume), maintains a
rolling 100-bar window per pair.

Schedule: every 60s via Celery beat.

Writes:
  `{pair}:cvd_now`        — latest 1-bar CVD value
  `{pair}:cvd_history`    — Redis list, last 100 bars (newest first)
  `{pair}:close_history`  — Redis list, last 100 closes (newest first) — used
                            by exit_signals.evaluate_cvd_divergence
"""
from __future__ import annotations
import json
import structlog

import redis_client
import redis_keys

log = structlog.get_logger()

_WINDOW = 100


def update_cvd_for_pair(pair: str) -> bool:
    r = redis_client.get()
    key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1m")
    raw = r.lrange(key, 0, _WINDOW + 5)
    if len(raw) < 10:
        return False
    # candles list is newest-first
    candles = []
    for c in raw:
        try:
            candles.append(json.loads(c))
        except (json.JSONDecodeError, TypeError):
            continue
    if len(candles) < 10:
        return False
    # Compute per-bar delta. Binance candle fields:
    #   v = base asset volume, V = taker buy base asset volume
    deltas = []
    closes = []
    for c in candles:
        try:
            v  = float(c.get("v") or 0)
            V_ = float(c.get("V") or c.get("v_buy") or 0)
            close = float(c.get("c") or 0)
        except (TypeError, ValueError):
            continue
        # taker buy = V_, taker sell = v - V_. Net delta = V_ - (v - V_) = 2V_ - v
        delta = 2.0 * V_ - v
        deltas.append(delta)
        closes.append(close)
    if not deltas:
        return False
    # Newest-first storage
    pipe = r.pipeline()
    pipe.set(f"{pair}:cvd_now", round(deltas[0], 6))
    pipe.delete(f"{pair}:cvd_history")
    pipe.delete(f"{pair}:close_history")
    if deltas:
        pipe.rpush(f"{pair}:cvd_history", *[round(d, 6) for d in deltas])
        pipe.expire(f"{pair}:cvd_history", 600)
    if closes:
        pipe.rpush(f"{pair}:close_history", *[round(c, 8) for c in closes])
        pipe.expire(f"{pair}:close_history", 600)
    pipe.execute()
    return True


def update_cvd_all_active() -> int:
    r = redis_client.get()
    try:
        pairs = sorted(r.smembers("scanner:active_pairs"))
    except Exception:
        return 0
    updated = 0
    for p in pairs:
        try:
            if update_cvd_for_pair(p):
                updated += 1
        except Exception as exc:
            log.debug("cvd_producer_pair_failed", pair=p, error=str(exc)[:120])
    return updated
