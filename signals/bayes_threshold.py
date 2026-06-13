"""Bayesian Beta-Distribution Adaptive Threshold — cont. 63 (2026-05-29).

Phase 2 of the signal-monitor remediation. Maintains a Beta(alpha, beta)
posterior over win-probability per signal-strength bucket and recomputes
T_high (the engine-side accept threshold) + T_low (the replay-pool
admission floor) every few minutes.

Updates come from two sources:
  * Rejected signals whose counterfactual evaluator decided would_have_won
    (called from celery_app.track_counterfactual). These speak to "how good
    are signals in bucket X that we *rejected*?"
  * Accepted trades that closed (called from execution.paper/live close).
    These speak to "how good are signals in bucket X that we *accepted*?"

T_high logic:
  * For each bucket compute posterior mean = alpha/(alpha+beta) and the
    5%-credible lower-bound via scipy.stats.beta.ppf(0.05, alpha, beta).
  * T_high = lowest bucket-floor whose lower-bound posterior >= max(0.55,
    accepted-bucket-mean - 0.05). Clipped to [25.0, 60.0].
  * T_low = T_high - 10, clipped to [15.0, T_high - 5].

When utility-calibration (Phase 4) has produced a recommendation, blend
50/50 with the Bayesian T_high so the two signals reinforce each other.

Cold-start guard: a bucket with fewer than _MIN_BUCKET_SAMPLES updates
contributes a posterior of (0.5, +inf width) — i.e. it is ignored when
choosing T_high.

Kill switch: bayes_threshold:enabled = "0".
"""
from __future__ import annotations

import time
from typing import Optional

import structlog

import redis_client
import redis_keys

log = structlog.get_logger()


# Strength buckets — closed-open intervals.
_BUCKETS: list[tuple[int, int]] = [
    (15, 25), (25, 35), (35, 45),
    (45, 55), (55, 65), (65, 75), (75, 101),
]

_MIN_BUCKET_SAMPLES = 30          # cold-start guard
_REFRESH_STALE_S = 7200           # T_high considered stale after 2h
_T_HIGH_MIN = 25.0
_T_HIGH_MAX = 60.0
_T_LOW_GAP = 10.0
_T_LOW_MIN_GAP = 5.0
_T_LOW_FLOOR = 15.0
_POSTERIOR_FLOOR = 0.55           # required lower-bound P(win)

# Lazy-imported once — scipy is already in the image.
_beta_ppf = None


def _enabled() -> bool:
    """Master switch. Default ON."""
    try:
        v = redis_client.get().get(redis_keys.BAYES_THRESHOLD_ENABLED)
        if v is None:
            return True
        return v in ("1", b"1")
    except Exception:
        return True


def _bucket_for(strength: float) -> Optional[tuple[int, int]]:
    """Return the (lo, hi) bucket containing `strength`, or None if below
    the lowest bucket / non-numeric."""
    try:
        s = float(strength)
    except (TypeError, ValueError):
        return None
    for lo, hi in _BUCKETS:
        if lo <= s < hi:
            return (lo, hi)
    return None


def _bucket_keys(bucket: tuple[int, int]) -> tuple[str, str]:
    lo, hi = bucket
    a = redis_keys.BAYES_BUCKET_ALPHA.replace(
        "{lo}", str(lo)).replace("{hi}", str(hi))
    b = redis_keys.BAYES_BUCKET_BETA.replace(
        "{lo}", str(lo)).replace("{hi}", str(hi))
    return a, b


def record_outcome(strength: float, won: bool) -> None:
    """Single update path. Bumps alpha (won) or beta (lost) by 1 in the
    appropriate bucket. Never raises — failure ≡ no-op."""
    bucket = _bucket_for(strength)
    if bucket is None:
        return
    a_key, b_key = _bucket_keys(bucket)
    r = redis_client.get()
    try:
        if won:
            r.incr(a_key)
        else:
            r.incr(b_key)
    except Exception as exc:
        log.warning("bayes_record_outcome_failed",
                    bucket=bucket, won=won, error=str(exc)[:120])


def _read_bucket(bucket: tuple[int, int]) -> tuple[float, float]:
    """Return (alpha, beta) with Jeffreys prior (0.5, 0.5)."""
    a_key, b_key = _bucket_keys(bucket)
    r = redis_client.get()
    try:
        a = float(r.get(a_key) or 0) + 0.5
        b = float(r.get(b_key) or 0) + 0.5
    except (TypeError, ValueError):
        a, b = 0.5, 0.5
    return a, b


def _posterior_lower_bound(alpha: float, beta_: float,
                            quantile: float = 0.05) -> float:
    """5%-credible lower bound of P(win)."""
    global _beta_ppf
    if _beta_ppf is None:
        try:
            from scipy.stats import beta as _scipy_beta
            _beta_ppf = _scipy_beta.ppf
        except Exception:
            # scipy missing → return Wilson-style approximation.
            _beta_ppf = "fallback"
    if _beta_ppf == "fallback":
        n = alpha + beta_
        if n <= 0:
            return 0.0
        p = alpha / n
        z = 1.645  # 5%
        # Wilson lower bound
        denom = 1 + z ** 2 / n
        centre = p + z ** 2 / (2 * n)
        margin = z * ((p * (1 - p) / n + z ** 2 / (4 * n ** 2)) ** 0.5)
        return max(0.0, min(1.0, (centre - margin) / denom))
    try:
        v = float(_beta_ppf(quantile, alpha, beta_))
        if v != v:  # nan
            return 0.0
        return max(0.0, min(1.0, v))
    except Exception:
        return 0.0


def _bucket_summaries() -> list[dict]:
    """Per-bucket (lo, hi, alpha, beta, n, mean, lower_bound, mature)."""
    out: list[dict] = []
    for bucket in _BUCKETS:
        a, b = _read_bucket(bucket)
        n = (a - 0.5) + (b - 0.5)
        mean = a / (a + b)
        lb = _posterior_lower_bound(a, b)
        out.append({
            "lo": bucket[0], "hi": bucket[1],
            "alpha": round(a, 3), "beta": round(b, 3),
            "n": int(n),
            "mean": round(mean, 4),
            "lower_bound": round(lb, 4),
            "mature": n >= _MIN_BUCKET_SAMPLES,
        })
    return out


def _accepted_bucket_mean(summaries: list[dict],
                           current_t_high: float) -> float:
    """Average posterior P(win) of buckets currently above the threshold.
    Used to set the floor that lower buckets must beat to be admitted."""
    above = [s for s in summaries
             if s["mature"] and s["lo"] >= current_t_high]
    if not above:
        return _POSTERIOR_FLOOR
    return sum(s["mean"] for s in above) / len(above)


def refresh() -> dict:
    """Compute new T_high + T_low from posteriors. Called every 5 min by
    Celery beat. Returns a dict suitable for logging / dashboards."""
    if not _enabled():
        return {"enabled": False, "applied": False}
    summaries = _bucket_summaries()
    # Bayesian standalone T_high recommendation
    current_t_high = _T_HIGH_MIN
    try:
        v = redis_client.get().get(redis_keys.BAYES_THRESHOLD_T_HIGH)
        if v is not None:
            current_t_high = float(v)
    except Exception:
        pass
    floor = max(_POSTERIOR_FLOOR,
                _accepted_bucket_mean(summaries, current_t_high) - 0.05)
    chosen_lo: Optional[int] = None
    for s in summaries:
        if not s["mature"]:
            continue
        if s["lower_bound"] >= floor:
            chosen_lo = s["lo"]
            break
    if chosen_lo is None:
        # No bucket beats the floor → conservatively keep current or
        # raise to the lowest mature bucket above the current threshold.
        bayes_t_high = current_t_high if current_t_high else _T_HIGH_MIN
    else:
        bayes_t_high = float(chosen_lo)
    bayes_t_high = max(_T_HIGH_MIN, min(_T_HIGH_MAX, bayes_t_high))

    # Blend with Phase 4 utility-calibration recommendation if present.
    final_t_high = bayes_t_high
    util_t_high = None
    try:
        r = redis_client.get()
        ut = r.get(redis_keys.UTIL_CALIB_T_HIGH)
        ut_ts = r.get(redis_keys.UTIL_CALIB_RUN_TS)
        if ut is not None and ut_ts is not None:
            age = int(time.time()) - int(ut_ts)
            if age < 86400 * 3:           # accept if <3 days old
                util_t_high = float(ut)
                final_t_high = max(_T_HIGH_MIN,
                                   min(_T_HIGH_MAX,
                                       0.5 * bayes_t_high + 0.5 * util_t_high))
    except Exception:
        pass

    final_t_low = max(_T_LOW_FLOOR,
                      min(final_t_high - _T_LOW_MIN_GAP,
                          final_t_high - _T_LOW_GAP))

    now_ts = int(time.time())
    try:
        r = redis_client.get()
        # Phase-7c §11 no-bypass: route the live T_high/T_low write THROUGH the unified producer-bus
        # kernel. producers.apply_controller performs the bounded clamp + versioned audit record and does
        # the actual r.set — so this controller no longer self-applies a live parameter directly. The
        # written value is byte-identical to the old direct write (the clamp already held upstream); the
        # kernel re-asserts the hard bounds and keeps an audit trail. Never raises; falls back if absent.
        from signals.scibrain import producers
        producers.apply_controller(r, "bayes_threshold",
                                   redis_key=redis_keys.BAYES_THRESHOLD_T_HIGH,
                                   value=final_t_high, bounds=(_T_HIGH_MIN, _T_HIGH_MAX),
                                   reason="bayes posterior lower-bound + util-calib blend")
        producers.apply_controller(r, "bayes_threshold",
                                   redis_key=redis_keys.BAYES_THRESHOLD_T_LOW,
                                   value=final_t_low, bounds=(_T_LOW_FLOOR, final_t_high - _T_LOW_MIN_GAP),
                                   reason="T_high minus configured gap")
        r.set(redis_keys.BAYES_THRESHOLD_REFRESH_TS, now_ts)
    except Exception as exc:
        log.warning("bayes_threshold_write_failed", error=str(exc)[:120])
        # last-resort fallback so the controller never freezes if the bus is unavailable
        try:
            r = redis_client.get()
            r.set(redis_keys.BAYES_THRESHOLD_T_HIGH, round(final_t_high, 2))
            r.set(redis_keys.BAYES_THRESHOLD_T_LOW,  round(final_t_low, 2))
            r.set(redis_keys.BAYES_THRESHOLD_REFRESH_TS, now_ts)
        except Exception:
            pass

    report = {
        "enabled": True,
        "applied": True,
        "bayes_t_high": round(bayes_t_high, 2),
        "util_t_high": round(util_t_high, 2) if util_t_high is not None else None,
        "final_t_high": round(final_t_high, 2),
        "final_t_low": round(final_t_low, 2),
        "floor_used": round(floor, 4),
        "buckets": summaries,
        "refresh_ts": now_ts,
    }
    log.info("bayes_threshold_refreshed",
             t_high=report["final_t_high"],
             t_low=report["final_t_low"],
             bayes=report["bayes_t_high"],
             util=report["util_t_high"])
    return report


def get_adaptive_min_strength() -> Optional[float]:
    """Consumer entrypoint. Returns the current adaptive T_high or None
    if disabled / stale. signals/engine.py:accept_or_reject calls this
    after resolving the GA/override min_strength and uses it when fresh."""
    if not _enabled():
        return None
    try:
        r = redis_client.get()
        t = r.get(redis_keys.BAYES_THRESHOLD_T_HIGH)
        ts = r.get(redis_keys.BAYES_THRESHOLD_REFRESH_TS)
        if t is None or ts is None:
            return None
        if int(time.time()) - int(ts) > _REFRESH_STALE_S:
            return None
        # cont. 74 — DEADLOCK BREAKER. The Bayesian refresh can ratchet t_high
        # (+util-calib blend) ABOVE what the current regime can produce, e.g.
        # t_high=50 while turbulent-regime signals max ~42 post-MPP → zero trades
        # → no new win/loss data → threshold stays pinned → self-starving freeze
        # (observed 2026-06-07, ~4.5h drought). A Redis-tunable consumer-side CAP
        # (bayes:t_high_cap, fallback _T_HIGH_MAX) clamps the EFFECTIVE accept
        # threshold without fighting the 5-min refresh: the refresh may still
        # compute 50, but the engine never sees a gate above the cap, so the
        # strongest current signals trade and feed the buckets back to health.
        # Reversible + tunable live: redis-cli set bayes:t_high_cap 60 (or DEL).
        cap = _T_HIGH_MAX
        try:
            _cap_raw = r.get("bayes:t_high_cap")
            if _cap_raw is not None:
                cap = max(_T_HIGH_MIN, min(_T_HIGH_MAX, float(_cap_raw)))
        except (TypeError, ValueError):
            pass
        return max(_T_HIGH_MIN, min(cap, float(t)))
    except Exception:
        return None


def state() -> dict:
    """Dashboard helper."""
    r = redis_client.get()
    def _f(k: str) -> Optional[float]:
        try:
            v = r.get(k)
            return float(v) if v is not None else None
        except Exception:
            return None
    def _i(k: str) -> int:
        try:
            v = r.get(k)
            return int(v) if v is not None else 0
        except Exception:
            return 0
    return {
        "enabled": _enabled(),
        "t_high": _f(redis_keys.BAYES_THRESHOLD_T_HIGH),
        "t_low":  _f(redis_keys.BAYES_THRESHOLD_T_LOW),
        "refresh_ts": _i(redis_keys.BAYES_THRESHOLD_REFRESH_TS),
        "applied_count": _i(redis_keys.BAYES_APPLIED_COUNT),
        "buckets": _bucket_summaries(),
        "util_calib_t_high": _f(redis_keys.UTIL_CALIB_T_HIGH),
        "util_calib_run_ts": _i(redis_keys.UTIL_CALIB_RUN_TS),
    }
