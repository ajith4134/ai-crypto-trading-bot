"""Idea 3 — IPS/SNIPS threshold optimiser (cont. 64, 2026-05-30).

Treat the rejection policy as a logging policy. Each F9-decoded
counterfactual is a sample with:
    s_i = signal_strength at rejection
    r_i = clip(peak_profit_pct - 0.3*|peak_loss_pct|, -10, +30)
    π_i = sigmoid((s_i - current_τ) / temperature)  (propensity)

Self-Normalised IPS estimator:
        Σ_i  (1{s_i ≥ τ} / π_i) · r_i
V(τ) = ──────────────────────────────
        Σ_i  1{s_i ≥ τ} / π_i

For each (regime, band) bucket with ≥ MIN_EVIDENCE rows: pick
τ* = argmax_τ V(τ), then EWMA-blend the optimal delta (τ* − ga_base)
into the existing `min_signal_strength_delta` field of the
`brain:filter_overrides` JSON.

This module is called nightly by `celery_app.compute_ips_thresholds`.
F46 gated.

References:
  - arXiv:1809.03084 (Joachims & Swaminathan, 2018) — SNIPS theory
  - arXiv:2509.00333 (2025) — SNIPS applied to recommender logs
  - arXiv:1612.01205 — off-policy evaluation in contextual bandits
"""
from __future__ import annotations

import json
import math
import time
from typing import Iterable

import structlog

import redis_client
import redis_keys

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Tunables. All capped so a runaway estimator can't blow the strength band.
# ---------------------------------------------------------------------------

MIN_EVIDENCE        = 100        # rows per bucket before IPS produces a value
EWMA_ALPHA          = 0.10       # blend weight on the SNIPS-optimal each pass
DELTA_CAP           = 10.0       # hard clip on the blended delta (matches R1)
TEMPERATURE         = 3.0        # propensity sigmoid bandwidth (strength units)
PROPENSITY_FLOOR    = 0.05       # divide-by-zero guard on extreme tails
EFFECTIVE_SAMPLE_FLOOR = 5.0     # SNIPS denominator floor
REWARD_CLIP         = (-10.0, 30.0)
REWARD_LOSS_HAIRCUT = 0.3        # weight on |peak_loss_pct|

# Strength-band layout matches metacognition.actuator._bucket_key:
# bucket_lo = floor(strength / 4) * 4.
_BAND_WIDTH         = 4
_BAND_FLOOR         = 12         # bottom of strength scan
_BAND_CEIL          = 60         # top  of strength scan
_REGIMES            = ("bull", "bear", "turbulent", "unknown")


def _governance_active() -> bool:
    try:
        from feature_governance.registry import is_active
        return bool(is_active("F46"))
    except Exception:
        return True


def _ga_base_min_strength() -> float:
    """Return the active GA-evolved min_signal_strength, clipped to
    [15, 28] per the production cap in signals/engine.py:851."""
    try:
        from ml.genetic_algorithm import get_active_params
        ga = get_active_params() or {}
        v = float(ga.get("min_signal_strength", 20.0))
        return max(15.0, min(28.0, v))
    except Exception:
        return 20.0


def _bucket_key(regime: str, band_lo: int) -> str:
    band_hi = band_lo + _BAND_WIDTH
    return f"{regime}|{band_lo}-{band_hi}"


def _fetch_bucket_rows(regime: str, band_lo: int) -> list[tuple[float, float, float]]:
    """SQL: (strength, peak_profit_pct, peak_loss_pct) for decoded F9 rows
    in (regime, band). Empty list on failure."""
    band_hi = band_lo + _BAND_WIDTH
    try:
        from db import db_conn
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.signal_strength::float,
                           COALESCE(c.peak_profit_pct, 0)::float,
                           COALESCE(c.peak_loss_pct,   0)::float
                      FROM counterfactuals c
                      JOIN signals s ON s.id = c.signal_id
                     WHERE c.miss_decoded = TRUE
                       AND LOWER(s.market_regime) = %s
                       AND s.signal_strength >= %s
                       AND s.signal_strength <  %s
                    """,
                    (regime, band_lo, band_hi),
                )
                return [(float(a), float(b), float(c))
                        for a, b, c in cur.fetchall()]
    except Exception as exc:
        log.warning("ips_fetch_failed",
                    regime=regime, band_lo=band_lo, err=str(exc)[:160])
        return []


def _snips_argmax(rows: list[tuple[float, float, float]],
                  current_tau: float) -> tuple[float | None, dict]:
    """Pure-Python SNIPS argmax over a τ grid.

    Returns (optimal_τ, diagnostics) or (None, diagnostics) when no
    candidate cleared the effective-sample-size floor.

    Avoids numpy to keep the module dependency-light — n ≤ a few thousand
    so a Python loop is fine.
    """
    if not rows:
        return None, {"reason": "no_rows"}

    strengths = [r[0] for r in rows]
    rewards   = [max(REWARD_CLIP[0],
                     min(REWARD_CLIP[1],
                         r[1] - REWARD_LOSS_HAIRCUT * abs(r[2])))
                 for r in rows]

    # Propensities under the CURRENT policy.
    propensities = []
    for s in strengths:
        try:
            sig = 1.0 / (1.0 + math.exp(-(s - current_tau) / TEMPERATURE))
        except OverflowError:
            sig = 0.0 if (s - current_tau) < 0 else 1.0
        propensities.append(max(PROPENSITY_FLOOR, sig))

    # Grid: ±10 strength units around current_τ in 0.5 steps.
    start = current_tau - 10.0
    stop  = current_tau + 10.0
    candidates = [start + 0.5 * k for k in range(int((stop - start) / 0.5) + 1)]

    best_tau, best_v = None, -float("inf")
    skipped = 0
    for tau in candidates:
        weights_sum = 0.0
        weighted_r  = 0.0
        for s, p, r in zip(strengths, propensities, rewards):
            if s >= tau:
                w = 1.0 / p
                weights_sum += w
                weighted_r  += w * r
        if weights_sum < EFFECTIVE_SAMPLE_FLOOR:
            skipped += 1
            continue
        v = weighted_r / weights_sum
        if v > best_v:
            best_v, best_tau = v, tau

    diag = {
        "n_rows": len(rows),
        "grid_size": len(candidates),
        "grid_skipped_no_ess": skipped,
        "best_v": round(best_v, 4) if best_tau is not None else None,
        "current_tau": current_tau,
    }
    return best_tau, diag


def run_full_pass() -> dict:
    """Iterate every (regime × band) bucket, compute SNIPS-optimal
    delta, EWMA-blend into `brain:filter_overrides`. Returns a summary
    dict for the celery task to log."""
    if not _governance_active():
        log.info("ips_governance_inactive")
        return {"status": "f46_inactive"}

    r = redis_client.get()
    ga_base = _ga_base_min_strength()

    # Load existing overrides JSON. Fail-open: if parse breaks, start fresh.
    try:
        raw = r.get(redis_keys.BRAIN_FILTER_OVERRIDES)
        current = json.loads(raw) if raw else {}
        if not isinstance(current, dict):
            current = {}
    except Exception:
        current = {}

    buckets_root = current.setdefault("by_regime_and_band", {})

    updated, below_min, no_ess = 0, 0, 0
    for regime in _REGIMES:
        for band_lo in range(_BAND_FLOOR, _BAND_CEIL + 1, _BAND_WIDTH):
            rows = _fetch_bucket_rows(regime, band_lo)
            if len(rows) < MIN_EVIDENCE:
                below_min += 1
                continue

            bucket_key = _bucket_key(regime, band_lo)
            bucket = buckets_root.setdefault(bucket_key, {})
            old_delta = float(bucket.get("min_signal_strength_delta", 0.0))
            current_tau = ga_base + old_delta

            tau_star, diag = _snips_argmax(rows, current_tau)
            if tau_star is None:
                no_ess += 1
                continue

            optimal_delta_raw = tau_star - ga_base
            blended = (1.0 - EWMA_ALPHA) * old_delta + EWMA_ALPHA * optimal_delta_raw
            clipped = max(-DELTA_CAP, min(DELTA_CAP, blended))

            bucket["min_signal_strength_delta"] = round(clipped, 4)
            bucket["ips_optimal_delta_raw"]     = round(optimal_delta_raw, 4)
            bucket["ips_n_evidence"]            = int(diag["n_rows"])
            bucket["ips_last_update_ts"]        = int(time.time())
            bucket["last_update_ts"]            = int(time.time())
            # Preserve `n_evidence` (the per-decode nudge count) untouched.

            if clipped != blended:
                try:
                    r.incr("decoders:ips_clipped_count")
                except Exception:
                    pass

            updated += 1
            log.info("ips_bucket_updated",
                     bucket=bucket_key, n=diag["n_rows"],
                     ga_base=ga_base, old_delta=round(old_delta, 4),
                     optimal=round(optimal_delta_raw, 4),
                     blended=round(blended, 4),
                     clipped=round(clipped, 4),
                     best_v=diag["best_v"])

    current.setdefault("_meta", {})["schema_version"] = 2
    current["_meta"]["saved_ts"] = int(time.time())
    current["_meta"]["last_ips_pass_ts"] = int(time.time())

    try:
        r.set(redis_keys.BRAIN_FILTER_OVERRIDES, json.dumps(current))
        r.incr("decoders:ips_buckets_updated_count", updated)
        if below_min:
            r.incrby("decoders:ips_below_min_evidence_count", below_min)
        if no_ess:
            r.incrby("decoders:ips_no_effective_sample_count", no_ess)
    except Exception as exc:
        log.warning("ips_redis_write_failed", err=str(exc)[:160])
        return {"status": "redis_write_failed",
                "updated": updated, "err": str(exc)[:160]}

    log.info("ips_full_pass_complete",
             ga_base=ga_base, updated=updated,
             below_min_evidence=below_min, no_effective_sample=no_ess)
    return {"status": "ok", "updated": updated,
            "below_min": below_min, "no_ess": no_ess,
            "ga_base": ga_base}
