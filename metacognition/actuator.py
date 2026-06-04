"""F9/F12 Decoder Writeback Actuator (Blueprint §10.7 "Filter Improvement Loop").

The decoders (metacognition/decoders.py) produce LLM postmortems on rejected
shadow-wins (F9) and high-pot-loser / low-pot-winner mismatches (F12). Pre-
cont.27 the postmortems were text-only — written to counterfactuals/mismatches
tables and never read again. This module closes the loop.

Design constraints:

1. **Bounded action vocabulary.** The LLM picks from a fixed enum of actions.
   No free-text JSON values that could be injected with arbitrary deltas.
2. **Per-call deltas are tiny** (small=±1, medium=±2 for strength; small=±0.02
   medium=±0.04 for thresholds). One bad decode can't dominate.
3. **Total accumulated drift is hard-capped.** Even if 1000 proposals all
   say "loosen", the override magnitude is clipped to ±_MAX_DRIFT. A reset
   via Redis del restores defaults.
4. **F30 governance gate.** If the writeback feature (F46) is deactivated,
   actuators no-op silently. Override keys remain readable (signals/engine
   still layers them) so a deactivation does NOT instantly revert drift —
   that's a separate explicit reset.
5. **Strict validation.** Unknown actions / out-of-bounds magnitudes are
   logged and dropped. No partial application.

The override keys are JSON dicts read by signals/engine.py at decision time
and added on top of the GA/config defaults with hard clip:
  effective_min_strength = clip(_ga_min + filter_overrides["min_signal_strength_delta"], 15, 35)

This module's only side effects are Redis writes + structlog audit. No DB
schema changes.
"""
from __future__ import annotations

import json
import structlog

import redis_client
import redis_keys

log = structlog.get_logger()


# --- F9 Filter writeback ----------------------------------------------------

# Action vocabulary. Each entry maps the LLM-chosen action to a (key, sign)
# pair. Magnitude is multiplied by sign so a single delta_value-per-magnitude
# table can drive both tighten/loosen.
_F9_ACTIONS = {
    "tighten_min_signal_strength":  ("min_signal_strength_delta",  +1.0),
    "loosen_min_signal_strength":   ("min_signal_strength_delta",  -1.0),
    "tighten_memrl_threshold":      ("memrl_threshold_delta",      +1.0),
    "loosen_memrl_threshold":       ("memrl_threshold_delta",      -1.0),
    "no_change":                    (None, 0.0),
}

# Per-magnitude step. Small/medium kept distinct so the LLM can express
# urgency without unbounded numeric input.
_F9_STEP = {
    "min_signal_strength_delta": {"small": 1.0, "medium": 2.0},
    "memrl_threshold_delta":     {"small": 0.02, "medium": 0.04},
}

# Hard cap on accumulated drift (absolute value). Even if every decode
# proposes the same direction forever, the override never exceeds this.
# Clamps mirror the absolute bounds enforced in signals/engine.py:
#   min_signal_strength: [15, 35]  → ±10 drift max (GA seed = 25 nominally)
#   memrl_threshold:     [0.10, 0.40] → ±0.15 drift max (default = 0.20)
_F9_MAX_DRIFT = {
    "min_signal_strength_delta": 10.0,
    "memrl_threshold_delta":     0.15,
}

# --- R1 + R3 (cont. 55): per-(regime × strength-band) buckets + density scaling -

# Band width for strength bucketing. 4-unit bands match the cluster shape in
# DB (340@42, 170@46, 46@49 — verified 2026-05-29). Key format: "bull|40-44".
_F9_BAND_WIDTH = 4

# Regime fallback hierarchy. Specific bucket -> "<regime>|*" -> _global_fallback.
_F9_REGIME_KEYS = ("bull", "bear", "range", "unknown")

# R3 — density / unanimity scaling caps.
_F9_DENSITY_CAP = 3.0    # at 60+ same-direction votes
_F9_DENSITY_DIV = 20.0   # density = n_total / DIV (clamped at DENSITY_CAP)


# --- F12 Scorer writeback ---------------------------------------------------

_F12_ACTIONS = {
    "increase_regime_weight":  ("regime_weight_delta",  +1.0),
    "decrease_regime_weight":  ("regime_weight_delta",  -1.0),
    "increase_ofi_weight":     ("ofi_weight_delta",     +1.0),
    "decrease_ofi_weight":     ("ofi_weight_delta",     -1.0),
    "increase_tft_weight":     ("tft_weight_delta",     +1.0),
    "decrease_tft_weight":     ("tft_weight_delta",     -1.0),
    "no_change":               (None, 0.0),
}

_F12_STEP = {
    "regime_weight_delta": {"small": 0.02, "medium": 0.04},
    "ofi_weight_delta":    {"small": 0.02, "medium": 0.04},
    "tft_weight_delta":    {"small": 0.02, "medium": 0.04},
}

# Component weights are in [0.05, 0.40] per signals/engine.py composite.
# Max drift ±0.15 from the nominal weight.
_F12_MAX_DRIFT = {
    "regime_weight_delta": 0.15,
    "ofi_weight_delta":    0.15,
    "tft_weight_delta":    0.15,
}


def _governance_active() -> bool:
    """F30 gate. F46 = decoder writeback. Skip when deactivated."""
    try:
        from feature_governance.registry import is_active
        return bool(is_active("F46"))
    except Exception:
        # If governance isn't reachable, default ACTIVE — don't silently
        # block the feature. Errors here will be logged by the caller's
        # try/except boundary.
        return True


def _apply(redis_key: str, action_dict: dict, vocab: dict, step: dict,
           cap: dict, kind: str) -> dict:
    """Shared apply logic for F9 + F12 writebacks.

    Returns a status dict {applied: bool, delta_key: ..., new_value: ...,
    reason: ...} for caller logging. No exceptions raised — all errors are
    captured and returned as applied=False.
    """
    if not isinstance(action_dict, dict):
        return {"applied": False, "reason": "action_not_dict"}

    action_name = action_dict.get("action")
    magnitude = action_dict.get("magnitude", "small")

    if action_name not in vocab:
        log.info("decoder_action_unknown", kind=kind, action=action_name,
                 vocab=list(vocab.keys()))
        return {"applied": False, "reason": f"unknown_action_{action_name}"}

    delta_key, sign = vocab[action_name]
    if delta_key is None:
        return {"applied": False, "reason": "no_change"}

    if magnitude not in ("small", "medium"):
        log.info("decoder_magnitude_invalid", kind=kind, magnitude=magnitude)
        return {"applied": False, "reason": f"invalid_magnitude_{magnitude}"}

    if not _governance_active():
        return {"applied": False, "reason": "governance_F46_inactive"}

    step_value = step.get(delta_key, {}).get(magnitude)
    if step_value is None:
        return {"applied": False, "reason": "no_step_defined"}

    delta = sign * step_value
    max_drift = cap.get(delta_key)
    if max_drift is None:
        return {"applied": False, "reason": "no_cap_defined"}

    r = redis_client.get()
    try:
        raw = r.get(redis_key)
        current = json.loads(raw) if raw else {}
        if not isinstance(current, dict):
            current = {}
    except Exception:
        current = {}

    prev = float(current.get(delta_key, 0.0))
    proposed = prev + delta
    # Clip to cap.
    clipped = max(-max_drift, min(max_drift, proposed))
    capped = clipped != proposed

    current[delta_key] = round(clipped, 4)
    try:
        r.set(redis_key, json.dumps(current))
        # Audit counters.
        r.incr(f"decoders:{kind}_writeback_applied_count")
        if capped:
            r.incr(f"decoders:{kind}_writeback_capped_count")
    except Exception as exc:
        return {"applied": False, "reason": f"redis_write_failed_{str(exc)[:60]}"}

    log.info("decoder_writeback_applied", kind=kind, action=action_name,
             magnitude=magnitude, delta_key=delta_key, prev=round(prev, 4),
             new=round(clipped, 4), capped=capped)
    return {
        "applied": True, "delta_key": delta_key,
        "prev": round(prev, 4), "new": round(clipped, 4),
        "delta": round(delta, 4), "capped": capped,
    }


def _band_for(strength: float) -> str:
    """Band key like '40-44'. Strength bucket of width _F9_BAND_WIDTH."""
    lo = int(_F9_BAND_WIDTH * (int(strength) // _F9_BAND_WIDTH))
    return f"{lo}-{lo + _F9_BAND_WIDTH}"


def _bucket_key(regime: str | None, strength: float | None) -> str | None:
    """Return 'bull|40-44' or None if regime/strength missing."""
    if regime is None or strength is None:
        return None
    r = str(regime).lower()
    if r not in _F9_REGIME_KEYS:
        r = "unknown"
    return f"{r}|{_band_for(strength)}"


def _read_bucketed_overrides() -> dict:
    """Read v2-schema overrides JSON from Redis. Returns {} if absent."""
    r = redis_client.get()
    try:
        raw = r.get(redis_keys.BRAIN_FILTER_OVERRIDES)
        if not raw:
            return {}
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _votes_for_bucket(bucket_key: str, delta_key: str) -> tuple[int, int]:
    """How many recent decodes voted +/- on this bucket × delta_key?
    Returns (n_same, n_total) where n_same = max(plus, minus)."""
    if not bucket_key:
        return (0, 0)
    r = redis_client.get()
    try:
        plus  = int(r.get(f"decoders:f9_votes:{bucket_key}:{delta_key}:plus")  or 0)
        minus = int(r.get(f"decoders:f9_votes:{bucket_key}:{delta_key}:minus") or 0)
    except Exception:
        return (0, 0)
    total = plus + minus
    same  = max(plus, minus)
    return (same, total)


def _regret_mult(predicted_peak_profit_pct: float | None) -> float:
    """cont. 63 (2026-05-29) — proportional regret scaling per
    next_impl/predicted_profit_loop.md §3.1.

    Maps the F9-predicted peak profit % of a similar future signal to a
    multiplier on the base actuator step. Without this, a decode for a 5%
    miss and a decode for a 150% miss produce the SAME Redis delta — the
    actuator can't distinguish noise-level vs catastrophic-rule-failure
    regret. Even after the multiplier, the existing _F9_MAX_DRIFT cap
    enforces total accumulated drift bounds, so a runaway LLM cannot
    move the threshold past the hard band.

    Buckets per §3.1:
      < 5 %  → 1.0×  (noise)
      5-20 % → 1.5×  (systematic but bounded)
      ≥ 20 % → 1.5 × min(predicted / 20, 3.0)  (large rule failure; cap 4.5×)

    Negative / NULL / non-numeric → 1.0× (preserves current behaviour).
    """
    try:
        pp = float(predicted_peak_profit_pct)
    except (TypeError, ValueError):
        return 1.0
    if pp != pp:        # nan
        return 1.0
    pp_abs = abs(pp)
    if pp_abs < 5.0:
        return 1.0
    if pp_abs < 20.0:
        return 1.5
    return 1.5 * min(pp_abs / 20.0, 3.0)


def _apply_bucketed(action_dict: dict, regime: str | None,
                    strength: float | None,
                    predicted_peak_profit_pct: float | None = None,
                    kind: str = "f9") -> dict:
    """R1 + R3: bucketed override with density × unanimity² scaling.
    cont. 63 (2026-05-29): added predicted_peak_profit_pct for proportional
    regret-magnitude scaling (see _regret_mult).
    Failures non-fatal — all errors captured and returned as applied=False."""
    if not isinstance(action_dict, dict):
        return {"applied": False, "reason": "action_not_dict"}

    action_name = action_dict.get("action")
    magnitude   = action_dict.get("magnitude", "small")

    if action_name not in _F9_ACTIONS:
        log.info("decoder_action_unknown", kind=kind, action=action_name)
        return {"applied": False, "reason": f"unknown_action_{action_name}"}

    delta_key, sign = _F9_ACTIONS[action_name]
    if delta_key is None:
        return {"applied": False, "reason": "no_change"}

    if magnitude not in ("small", "medium"):
        return {"applied": False, "reason": f"invalid_magnitude_{magnitude}"}

    if not _governance_active():
        return {"applied": False, "reason": "governance_F46_inactive"}

    base_step = _F9_STEP.get(delta_key, {}).get(magnitude)
    if base_step is None:
        return {"applied": False, "reason": "no_step_defined"}

    cap = _F9_MAX_DRIFT.get(delta_key)
    if cap is None:
        return {"applied": False, "reason": "no_cap_defined"}

    bucket = _bucket_key(regime, strength)
    if not bucket:
        return {"applied": False, "reason": "no_bucket_key"}

    r = redis_client.get()
    try:
        vote_key = f"decoders:f9_votes:{bucket}:{delta_key}:{'plus' if sign > 0 else 'minus'}"
        r.incr(vote_key)
    except Exception:
        pass

    n_same, n_total = _votes_for_bucket(bucket, delta_key)
    if n_total > 0:
        unanimity = n_same / n_total
        density   = min(_F9_DENSITY_CAP, n_total / _F9_DENSITY_DIV)
        mult      = max(1.0, density * (unanimity ** 2))
    else:
        mult = 1.0

    # cont. 63 — proportional regret scaling chained with density × unanimity.
    regret_mult = _regret_mult(predicted_peak_profit_pct)
    mult = mult * regret_mult
    if regret_mult > 1.0:
        try:
            r.incr("decoders:f9_regret_scaled_count")
        except Exception:
            pass

    delta = sign * base_step * mult

    current = _read_bucketed_overrides()
    buckets = current.setdefault("by_regime_and_band", {})
    bucket_dict = buckets.setdefault(bucket, {})
    prev = float(bucket_dict.get(delta_key, 0.0))
    proposed = prev + delta
    clipped = max(-cap, min(cap, proposed))
    capped = clipped != proposed

    import time as _time
    bucket_dict[delta_key] = round(clipped, 4)
    bucket_dict["n_evidence"] = int(bucket_dict.get("n_evidence", 0)) + 1
    bucket_dict["last_update_ts"] = int(_time.time())
    current.setdefault("_meta", {})["schema_version"] = 2
    current["_meta"]["saved_ts"] = int(_time.time())

    try:
        r.set(redis_keys.BRAIN_FILTER_OVERRIDES, json.dumps(current))
        r.incr(f"decoders:{kind}_writeback_applied_count")
        r.incr(f"decoders:bucket_applied_count:{bucket}")
        if capped:
            r.incr(f"decoders:{kind}_writeback_capped_count")
    except Exception as exc:
        return {"applied": False, "reason": f"redis_write_failed_{str(exc)[:60]}"}

    log.info("decoder_writeback_bucketed_applied", kind=kind,
             action=action_name, bucket=bucket, delta_key=delta_key,
             prev=round(prev, 4), new=round(clipped, 4),
             mult=round(mult, 3), n_total=n_total, capped=capped)

    return {
        "applied": True, "bucket": bucket, "delta_key": delta_key,
        "prev": round(prev, 4), "new": round(clipped, 4),
        "mult": round(mult, 3), "n_total": n_total,
        "delta": round(delta, 4), "capped": capped,
    }


def apply_filter_change(action_dict: dict,
                        regime: str | None = None,
                        signal_strength: float | None = None,
                        predicted_peak_profit_pct: float | None = None) -> dict:
    """F9 — apply filter_change to bucketed BRAIN_FILTER_OVERRIDES (R1+R3).

    When regime/signal_strength omitted, falls back to legacy global _apply.
    Production callers (celery_app.decode_pending_misses) always pass both.
    cont. 63 (2026-05-29) — predicted_peak_profit_pct enables proportional
    regret-magnitude scaling (see _regret_mult). When None, behaves identical
    to pre-cont.63."""
    if regime is not None and signal_strength is not None:
        return _apply_bucketed(action_dict, regime=regime,
                               strength=signal_strength,
                               predicted_peak_profit_pct=predicted_peak_profit_pct,
                               kind="f9")
    return _apply(
        redis_keys.BRAIN_FILTER_OVERRIDES, action_dict,
        _F9_ACTIONS, _F9_STEP, _F9_MAX_DRIFT, kind="f9",
    )


def apply_scorer_change(action_dict: dict) -> dict:
    """F12 — apply a scorer_change action to BRAIN_SCORER_OVERRIDES."""
    return _apply(
        redis_keys.BRAIN_SCORER_OVERRIDES, action_dict,
        _F12_ACTIONS, _F12_STEP, _F12_MAX_DRIFT, kind="f12",
    )


def get_filter_overrides() -> dict:
    """Read v2 bucketed override dict. Empty when unset.
    Consumers should prefer get_bucket_delta() for resolving (regime, band) → delta."""
    return _read_bucketed_overrides()


def get_bucket_delta(regime: str | None,
                     signal_strength: float | None,
                     delta_key: str = "min_signal_strength_delta") -> float:
    """R1 read path. Resolve (regime, strength) → numeric delta.
    Fallback: bucket → '<regime>|*' → _global_fallback → 0.0.

    Also reads legacy v1 top-level key (delta_key directly under root) so
    existing pre-cont.55 overrides keep working through the transition."""
    overrides = _read_bucketed_overrides()
    bucket = _bucket_key(regime, signal_strength)
    if bucket:
        bd = overrides.get("by_regime_and_band", {}).get(bucket)
        if bd and delta_key in bd:
            return float(bd[delta_key])
    if regime:
        wide = f"{str(regime).lower()}|*"
        bd = overrides.get("by_regime_and_band", {}).get(wide)
        if bd and delta_key in bd:
            return float(bd[delta_key])
    gf = overrides.get("_global_fallback")
    if isinstance(gf, dict) and delta_key in gf:
        return float(gf[delta_key])
    if delta_key in overrides and not isinstance(overrides[delta_key], dict):
        return float(overrides[delta_key])
    return 0.0


def get_scorer_overrides() -> dict:
    """Read current F12 override dict. Empty when unset."""
    r = redis_client.get()
    try:
        raw = r.get(redis_keys.BRAIN_SCORER_OVERRIDES)
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def reset_overrides() -> dict:
    """Wipe both override keys. For F30 emergency revert or operator reset."""
    r = redis_client.get()
    r.delete(redis_keys.BRAIN_FILTER_OVERRIDES, redis_keys.BRAIN_SCORER_OVERRIDES)
    log.info("decoder_overrides_reset")
    return {"status": "reset"}
