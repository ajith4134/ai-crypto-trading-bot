"""cont. 60 — Markov-Modulated Hawkes spoof / cancel-burst score producer.

The full MM-Hawkes spoof detector (arXiv:2502.04027) operates on raw L2 order
book diff streams: it counts cancellation bursts that DON'T result in
significant price moves (the signature of spoofing — orders posted only to
be cancelled before execution).

Approximation here (because we don't have raw L2 diff stream): use volume
volatility-vs-price-stationarity ratio over the last 5 bars as a spoofing
proxy. High volume volatility with low price change = many large orders
appearing and disappearing without resulting trades.

  score = std(volume[5]) / (mean(volume[5]) × (1 + 100·|price_change_5|))

Score > 0.8 = active spoofing pattern detected.

Schedule: every 60s via Celery beat.

Writes:
  `{pair}:cancel_burst_score`   — 0-1 spoofing intensity proxy
"""
from __future__ import annotations
import json
import math
import structlog

import redis_client
import redis_keys

log = structlog.get_logger()


def _compute_score(volumes: list[float], closes: list[float]) -> float:
    if len(volumes) < 5 or len(closes) < 5:
        return 0.0
    recent_v = volumes[:5]
    mean_v = sum(recent_v) / 5
    if mean_v <= 0:
        return 0.0
    var_v = sum((v - mean_v) ** 2 for v in recent_v) / 5
    std_v = math.sqrt(var_v)
    cv = std_v / mean_v  # coefficient of variation
    if closes[0] <= 0 or closes[4] <= 0:
        return 0.0
    price_change = abs(closes[0] - closes[4]) / closes[4]
    # Spoofing: high vol-CV, low price change. Normalise to 0-1.
    raw_score = cv / (1.0 + 100.0 * price_change)
    return min(1.0, max(0.0, raw_score / 2.0))   # scale: cv=2 (very high) → 1.0


def update_mm_hawkes_for_pair(pair: str) -> bool:
    r = redis_client.get()
    key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1m")
    raw = r.lrange(key, 0, 10)
    if len(raw) < 5:
        return False
    vols, closes = [], []
    for c in raw:
        try:
            cd = json.loads(c)
            vols.append(float(cd.get("v") or 0))
            closes.append(float(cd.get("c") or 0))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    if len(vols) < 5:
        return False
    score = _compute_score(vols, closes)
    r.set(f"{pair}:cancel_burst_score", round(score, 4))
    return True


def update_mm_hawkes_all_active() -> int:
    r = redis_client.get()
    try:
        pairs = sorted(r.smembers("scanner:active_pairs"))
    except Exception:
        return 0
    updated = 0
    for p in pairs:
        try:
            if update_mm_hawkes_for_pair(p):
                updated += 1
        except Exception as exc:
            log.debug("mm_hawkes_producer_failed",
                      pair=p, error=str(exc)[:120])
    return updated
