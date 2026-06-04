"""Position-Based Multi-Play Thompson Sampling Slot Selector — cont. 63.

Phase 3 of the signal-monitor remediation. When K=max_open-n_open trade
slots are available and a candidate pool of live + replay-pool signals
exists, select the top-K via Thompson sampling on a per-(pair, regime)
Beta posterior over historical win probability.

Falls back cleanly to first-come-first-served (legacy behaviour) when
the bandit is disabled or when a (pair, regime) has too few samples to
sample meaningfully.

Reference: Komiyama, Honda, Nakagawa 2017 — "Position-Based Multiple-Play
Bandits with Thompson Sampling" (arXiv 2009.13181).

Kill switch: bandit:enabled = "0" (default OFF until ≥10 trades exist per
(pair, regime) bucket — engine.py callers honour this default).
"""
from __future__ import annotations

import random
from typing import Optional

import structlog

import redis_client
import redis_keys

log = structlog.get_logger()


_COLD_START_MIN_N = 10            # need ≥ N closed trades per (pair, regime)
_DEFAULT_ENABLED = False          # opt-in until bucket data warms up


def _enabled() -> bool:
    """Master switch. Default OFF (opt-in)."""
    try:
        v = redis_client.get().get(redis_keys.BANDIT_ENABLED)
        if v is None:
            return _DEFAULT_ENABLED
        return v in ("1", b"1")
    except Exception:
        return _DEFAULT_ENABLED


def _bucket_keys(pair: str, regime: str) -> tuple[str, str]:
    a = redis_keys.BANDIT_PAIR_REGIME_ALPHA.replace(
        "{pair}", pair).replace("{regime}", regime)
    b = redis_keys.BANDIT_PAIR_REGIME_BETA.replace(
        "{pair}", pair).replace("{regime}", regime)
    return a, b


def record_outcome(pair: str, regime: str, won: bool) -> None:
    """Called by execution/{paper,live}.close_trade. Bumps alpha (win)
    or beta (loss). Never raises — failure ≡ no-op."""
    if not pair or not regime:
        return
    a_key, b_key = _bucket_keys(pair, regime)
    r = redis_client.get()
    try:
        if won:
            r.incr(a_key)
        else:
            r.incr(b_key)
    except Exception as exc:
        log.warning("bandit_record_outcome_failed",
                    pair=pair, regime=regime, error=str(exc)[:120])


def _read_bucket(pair: str, regime: str) -> tuple[float, float, int]:
    """Return (alpha, beta, n) with Jeffreys prior (0.5, 0.5)."""
    a_key, b_key = _bucket_keys(pair, regime)
    r = redis_client.get()
    try:
        a_raw = float(r.get(a_key) or 0)
        b_raw = float(r.get(b_key) or 0)
    except (TypeError, ValueError):
        a_raw, b_raw = 0.0, 0.0
    return a_raw + 0.5, b_raw + 0.5, int(a_raw + b_raw)


def _sample_theta(pair: str, regime: str, strength: float) -> float:
    """Thompson-sample posterior win probability for (pair, regime). Cold
    buckets fall back to strength/100 (the bot's own prior on this signal)."""
    alpha, beta_, n = _read_bucket(pair, regime)
    if n < _COLD_START_MIN_N:
        try:
            return max(0.0, min(1.0, float(strength) / 100.0))
        except (TypeError, ValueError):
            return 0.5
    try:
        return random.betavariate(alpha, beta_)
    except Exception:
        return alpha / (alpha + beta_)


def rank(candidates: list[dict]) -> list[dict]:
    """Sort a list of candidate signal dicts by sampled theta DESC. Each
    dict must have keys: pair, regime, signal_strength (or strength).
    Original order is preserved as a tie-break. Returns a NEW list (does
    not mutate inputs).

    When the bandit is disabled, returns candidates unchanged."""
    if not _enabled() or not candidates:
        return list(candidates)
    annotated = []
    for idx, cand in enumerate(candidates):
        pair = cand.get("pair", "")
        regime = cand.get("regime") or cand.get("market_regime") or "unknown"
        strength = cand.get("signal_strength")
        if strength is None:
            strength = cand.get("strength")
        try:
            strength = float(strength or 0)
        except (TypeError, ValueError):
            strength = 0.0
        theta = _sample_theta(pair, regime, strength)
        annotated.append((theta, -idx, cand, strength))
    # Sort: theta DESC, then original-index ASC, then strength DESC
    annotated.sort(key=lambda t: (-t[0], -t[1], -t[3]))
    try:
        redis_client.get().incr(redis_keys.BANDIT_SELECTION_COUNT)
    except Exception:
        pass
    return [t[2] for t in annotated]


def state(top_pairs: Optional[list[str]] = None) -> dict:
    """Dashboard helper. Shows enabled flag, total selections, and the
    posterior for an optional list of top-N pairs × regimes."""
    r = redis_client.get()
    try:
        n_sel = int(r.get(redis_keys.BANDIT_SELECTION_COUNT) or 0)
    except Exception:
        n_sel = 0
    breakdown = []
    if top_pairs:
        for pair in top_pairs:
            for regime in ("bull", "bear", "turbulent"):
                a, b, n = _read_bucket(pair, regime)
                breakdown.append({
                    "pair": pair, "regime": regime,
                    "alpha": round(a, 3), "beta": round(b, 3),
                    "n": n,
                    "posterior_mean": round(a / (a + b), 4),
                    "mature": n >= _COLD_START_MIN_N,
                })
    return {
        "enabled": _enabled(),
        "selections": n_sel,
        "cold_start_min_n": _COLD_START_MIN_N,
        "breakdown": breakdown,
    }
