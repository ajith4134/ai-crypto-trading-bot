"""cont. 60 — Hawkes process self-excitation intensity producer.

Computes per-pair Hawkes intensity λ(t) from large-trade arrival times.
Large trade = bar with volume > 2σ of the 100-bar rolling mean.

Update rule:
  λ(t) = λ_0 + Σ_i α × exp(-β × (t - t_i)) for past large trades t_i
  with α=1.0, β=0.1 (per minute), λ_0=0.5 (baseline)

Also maintains a 60-bar baseline (1h rolling avg) for the exit consumer to
compute the excitation ratio.

Schedule: every 60s via Celery beat.

Writes:
  `{pair}:hawkes_intensity`           — current λ(t)
  `{pair}:hawkes_intensity_baseline`  — 60-bar rolling avg of λ(t)
  `{pair}:hawkes_baseline_history`    — internal: 60 prior λ values

Source: arXiv:2503.14814 Hawkes in HFT (Mar 2025).
"""
from __future__ import annotations
import json
import math
import structlog

import redis_client
import redis_keys

log = structlog.get_logger()

_ALPHA = 1.0
_BETA  = 0.1   # decay per minute
_LAMBDA_0 = 0.5
_LARGE_TRADE_SIGMA = 2.0
_LOOKBACK_BARS = 100
_BASELINE_WINDOW = 60


def _compute_lambda(bar_volumes: list[float]) -> float:
    """Compute Hawkes λ(t=0) for the newest bar, given volume history newest-first."""
    if len(bar_volumes) < 20:
        return _LAMBDA_0
    mean_v = sum(bar_volumes) / len(bar_volumes)
    if mean_v <= 0:
        return _LAMBDA_0
    var_v = sum((v - mean_v) ** 2 for v in bar_volumes) / len(bar_volumes)
    sigma_v = math.sqrt(var_v) if var_v > 0 else 1.0
    threshold = mean_v + _LARGE_TRADE_SIGMA * sigma_v
    # Sum excitation from all past large trades. t_i is in bars-ago units
    # (1 bar = 1 minute for 1m candles). bar_volumes[0] is "now", [1] is 1 min ago.
    lam = _LAMBDA_0
    for ago, v in enumerate(bar_volumes):
        if v >= threshold:
            lam += _ALPHA * math.exp(-_BETA * ago)
    return lam


def update_hawkes_for_pair(pair: str) -> bool:
    r = redis_client.get()
    key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1m")
    raw = r.lrange(key, 0, _LOOKBACK_BARS + 5)
    if len(raw) < 20:
        return False
    vols = []
    for c in raw:
        try:
            cd = json.loads(c)
            vols.append(float(cd.get("v") or 0))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    if len(vols) < 20:
        return False
    lam = _compute_lambda(vols)
    pipe = r.pipeline()
    pipe.set(f"{pair}:hawkes_intensity", round(lam, 4))
    # Maintain baseline history (LPUSH + LTRIM)
    pipe.lpush(f"{pair}:hawkes_baseline_history", round(lam, 4))
    pipe.ltrim(f"{pair}:hawkes_baseline_history", 0, _BASELINE_WINDOW - 1)
    pipe.execute()
    # Compute baseline avg
    hist = r.lrange(f"{pair}:hawkes_baseline_history", 0, -1)
    if len(hist) >= 10:
        try:
            baseline = sum(float(x) for x in hist) / len(hist)
            r.set(f"{pair}:hawkes_intensity_baseline", round(baseline, 4))
        except (TypeError, ValueError):
            pass
    return True


def update_hawkes_all_active() -> int:
    r = redis_client.get()
    try:
        pairs = sorted(r.smembers("scanner:active_pairs"))
    except Exception:
        return 0
    updated = 0
    for p in pairs:
        try:
            if update_hawkes_for_pair(p):
                updated += 1
        except Exception as exc:
            log.debug("hawkes_producer_pair_failed", pair=p, error=str(exc)[:120])
    return updated
