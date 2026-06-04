"""cont. 60 — Per-pair conformal residual quantile producer.

For the EXIT conformal band consumer we need a rolling 5% quantile of
prediction residuals (in USDT price units) per pair. The residual = |actual
close - 1-step prediction|. The 1-step prediction is the simplest possible
naive AR(1): predict next close = current close (so residual = bar-to-bar
move). For pairs with Mamba forecasts, we use mamba prediction instead.

Schedule: every 60s via Celery beat.

Writes:
  `conformal:residual_q5:{pair}`  — 5% quantile of absolute residuals (USDT)

Source: arXiv:2507.05470 Temporal Conformal Prediction (Jul 2025).
"""
from __future__ import annotations
import json
import structlog

import redis_client
import redis_keys

log = structlog.get_logger()

_LOOKBACK = 100


def _quantile(vals: list[float], q: float) -> float:
    """Simple quantile using sort + index."""
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = max(0, min(len(s) - 1, int(q * len(s))))
    return s[idx]


def update_conformal_for_pair(pair: str) -> bool:
    r = redis_client.get()
    closes_raw = r.lrange(f"{pair}:close_history", 0, _LOOKBACK)
    if not closes_raw or len(closes_raw) < 30:
        # Fall back to candle list
        key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1m")
        raw = r.lrange(key, 0, _LOOKBACK + 5)
        if len(raw) < 30:
            return False
        closes = []
        for c in raw:
            try:
                cd = json.loads(c)
                closes.append(float(cd.get("c") or 0))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    else:
        try:
            closes = [float(x) for x in closes_raw]
        except (TypeError, ValueError):
            return False
    if len(closes) < 30:
        return False
    # closes is newest-first. Residual = |close[i] - close[i+1]|
    residuals = [abs(closes[i] - closes[i + 1]) for i in range(len(closes) - 1)]
    if not residuals:
        return False
    # 5% LOWER quantile of |residual| → for SL we want the wider 95% upper
    # quantile so the SL band covers 95% of typical moves.
    q95 = _quantile(residuals, 0.95)
    r.set(f"conformal:residual_q5:{pair}", round(q95, 8))  # Note key name kept for compat
    return True


def update_conformal_all_active() -> int:
    r = redis_client.get()
    try:
        pairs = sorted(r.smembers("scanner:active_pairs"))
    except Exception:
        return 0
    updated = 0
    for p in pairs:
        try:
            if update_conformal_for_pair(p):
                updated += 1
        except Exception as exc:
            log.debug("conformal_residual_producer_failed",
                      pair=p, error=str(exc)[:120])
    return updated
