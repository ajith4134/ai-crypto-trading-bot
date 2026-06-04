"""
F56 — Conformal Prediction Uncertainty Wrapper (cont. 56, 2026-05-28).

Distribution-free, finite-sample confidence intervals over any black-box
directional predictor. Implements Split Conformal Prediction
(Romano et al. 2019, Angelopoulos & Bates 2023):

  1. Hold out a calibration set of (prediction, outcome) pairs.
  2. Compute nonconformity scores  s_i = |pred_i - outcome_i|.
  3. The 1-α quantile of {s_i} is the calibrated half-width h.
  4. For any new prediction p, the interval [p - h, p + h] has
     marginal coverage ≥ 1-α on exchangeable test points.

The bot's F49 performance monitor already maintains a per-model
`prediction_log` of (pred, outcome) pairs — F56 reuses it as the
calibration source.

Consumer hooks:
  - `kelly_multiplier(model_widths)` — scales position size by the
    inverse of the geometric mean of interval widths across the active
    forecasters. Wider intervals → smaller positions.
  - `should_abstain(pair, models)` — True when every wrapped forecaster's
    interval straddles 0.5 (i.e. no directional confidence at calibrated
    coverage). Used by `signals/engine.py:_record_routed_entry` as a
    soft-veto.

Cold-start safe — until a model accumulates ≥ MIN_CALIB_SAMPLES outcomes,
its interval is reported as None and consumers default to neutral (no
size scaling, no abstain).
"""
from __future__ import annotations

import json
import math
import time
from typing import Iterable

import structlog
import redis_client

from feature_governance.registry import register

log = structlog.get_logger()

_FG_ID = "F56"
try:
    register(_FG_ID, "Conformal Prediction Wrapper", activation_phase=0)
except Exception as _exc:
    log.debug("f56_self_register_deferred", err=str(_exc))


# ---- Tunables ---------------------------------------------------------------

# Default target miscoverage rate. α=0.10 → 90% intervals.
DEFAULT_ALPHA = 0.10

# Use the most-recent N samples for calibration. Mirrors F49's ROLLING_WINDOW
# of 100; we use 200 here because we don't apply the AUC degraded-flag
# resetting that F49 does — the conformal calibration is over the longest
# trustworthy history we have.
CALIB_WINDOW = 200

# Below this we report "no interval" and consumers default to neutral.
MIN_CALIB_SAMPLES = 30

# Models the wrapper knows how to read from F49's prediction_log. Adding a
# new model = add its name here + ensure log_outcome() is called for it.
KNOWN_MODELS = (
    "direction_model",   # F13
    "tft",               # F19
    "patchtst",          # F20
    "candlenet_1m",      # F48
    "candlenet_5m",
    "candlenet_15m",
    "candlenet_30m",
    "candlenet_1h",
    "mamba_1m",          # F50c
    "mamba_5m",
    "mamba_15m",
    "mamba_1h",
    "foundation",        # F50g
)

# Cap any single interval contribution to the size multiplier — defends
# against a model with a pathological calibration (very wide interval)
# crashing the position size to ~0.
_MIN_SIZE_MULT = 0.25
_MAX_INTERVAL_WIDTH = 1.0    # clamp before normalising


# ---- Redis I/O --------------------------------------------------------------

def _r():
    return redis_client.get()


def is_disabled() -> bool:
    return (_r().get("conformal:disabled") or b"0") in (b"1", "1")


def _load_calib(model: str) -> list[tuple[float, float]]:
    """Read (pred, outcome) pairs from F49's prediction_log, newest first.

    Returns at most CALIB_WINDOW samples. Filters out non-finite entries
    and trades that lack an outcome. Empty list → cold-start.
    """
    try:
        r = _r()
        raws = r.lrange(f"model:{model}:prediction_log", 0, CALIB_WINDOW - 1)
    except Exception:
        return []
    out: list[tuple[float, float]] = []
    for x in raws:
        try:
            d = json.loads(x)
            p = float(d["pred"]); o = float(d["outcome"])
            if not math.isfinite(p) or not math.isfinite(o):
                continue
            out.append((p, o))
        except Exception:
            continue
    return out


def _quantile(values: list[float], q: float) -> float:
    """Numpy-free q-quantile. Uses the linear (Type 7) definition."""
    if not values: return 0.0
    s = sorted(values)
    if len(s) == 1: return s[0]
    pos = q * (len(s) - 1)
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi: return s[lo]
    frac = pos - lo
    return s[lo] + (s[hi] - s[lo]) * frac


# ---- Per-model interval refresh ---------------------------------------------

def update_intervals(alpha: float = DEFAULT_ALPHA) -> dict:
    """Recompute the calibrated half-width for every known model.

    Called nightly (or on demand) via the celery beat. Writes:
      conformal:width:{model}      — float, half-width at the chosen alpha
      conformal:n_calib:{model}    — int, samples used
      conformal:alpha:{model}      — float, the alpha that was used
      conformal:last_update        — epoch
      conformal:health             — JSON summary

    Cold-start safe: models with fewer than MIN_CALIB_SAMPLES are written
    with width=None (literal Redis None → consumer-side default neutral).
    """
    if is_disabled():
        return {"ok": False, "reason": "disabled"}
    r = _r()
    summary = {"alpha": alpha, "models": {}, "warmed_up": [], "cold": []}
    pipe = r.pipeline(transaction=False)
    for m in KNOWN_MODELS:
        pairs = _load_calib(m)
        if len(pairs) < MIN_CALIB_SAMPLES:
            pipe.delete(f"conformal:width:{m}")
            pipe.set(f"conformal:n_calib:{m}", len(pairs))
            summary["cold"].append(m)
            summary["models"][m] = {"n": len(pairs), "width": None}
            continue
        # Nonconformity = |pred - outcome|. For a P(up) ∈ [0,1] predictor
        # with binary outcome ∈ {0,1}, this is in [0,1] — direct half-width.
        scores = sorted(abs(p - o) for p, o in pairs)
        # Conformal quantile uses (n+1)/n correction so coverage is finite-sample.
        n = len(scores)
        idx_q = min(n - 1, math.ceil((n + 1) * (1.0 - alpha)) - 1)
        width = float(scores[idx_q])
        width = min(width, _MAX_INTERVAL_WIDTH)
        pipe.set(f"conformal:width:{m}", width)
        pipe.set(f"conformal:n_calib:{m}", n)
        pipe.set(f"conformal:alpha:{m}", alpha)
        summary["warmed_up"].append(m)
        summary["models"][m] = {"n": n, "width": width}
    pipe.set("conformal:last_update", int(time.time()))
    pipe.set("conformal:health", json.dumps({
        "ts": int(time.time()),
        "warmed_up": len(summary["warmed_up"]),
        "cold": len(summary["cold"]),
    }))
    pipe.execute()
    log.info("f56_conformal_intervals_refreshed",
             warmed_up=len(summary["warmed_up"]),
             cold=len(summary["cold"]))
    return {"ok": True, **summary}


# ---- Consumers --------------------------------------------------------------

def width(model: str) -> float | None:
    """Return calibrated half-width for `model`, or None when cold."""
    if is_disabled(): return None
    try:
        raw = _r().get(f"conformal:width:{model}")
    except Exception:
        return None
    if raw is None: return None
    try:
        v = float(raw)
        if not math.isfinite(v) or v < 0: return None
        return v
    except Exception:
        return None


def all_widths() -> dict[str, float | None]:
    """Snapshot of every known model's current calibrated half-width."""
    return {m: width(m) for m in KNOWN_MODELS}


def kelly_multiplier(active_models: Iterable[str] | None = None) -> float:
    """Return a [_MIN_SIZE_MULT, 1.0] multiplier on Kelly fraction.

    Geometric mean of (1 - width) across the supplied active forecasters.
    Models with no calibration yet are skipped — they don't punish the
    multiplier downward, they just don't contribute. When ALL supplied
    models are cold the multiplier is 1.0 (no scaling, identical to
    pre-F56 behaviour).
    """
    if is_disabled():
        return 1.0
    models = list(active_models) if active_models is not None else list(KNOWN_MODELS)
    contribs: list[float] = []
    for m in models:
        w = width(m)
        if w is None: continue
        contribs.append(max(0.0, 1.0 - w))
    if not contribs:
        return 1.0
    # Geometric mean — penalises divergent uncertainty across models more
    # than arithmetic mean would.
    log_mean = sum(math.log(max(c, 1e-9)) for c in contribs) / len(contribs)
    mult = math.exp(log_mean)
    return max(_MIN_SIZE_MULT, min(1.0, mult))


def should_abstain(direction_conf: float | None = None,
                   models: Iterable[str] | None = None,
                   threshold: float = 0.50) -> bool:
    """Soft-veto: True when calibrated intervals straddle the neutral line.

    Logic:
      1. If kill switch on → False (don't abstain — disabled).
      2. If direction_conf provided (0..100): convert to P(up) ≈ conf/100.
         If P(up) is within ±max_width of 0.5, abstain.
      3. If direction_conf NOT provided: check if any model's calibrated
         interval straddles 0.5 entirely (i.e. predicting both
         directions is within the 90% interval). Abstain only when at
         least 60% of warmed-up models straddle.

    Cold-start safe — returns False when no model is warmed up.
    """
    if is_disabled(): return False
    models = list(models) if models is not None else list(KNOWN_MODELS)
    widths = [(m, width(m)) for m in models]
    warmed = [(m, w) for m, w in widths if w is not None]
    if not warmed:
        return False

    # cont. 69e: only abstain based on models that ACTUALLY have edge. A
    # calibrated half-width ≥ _EDGE_MAX (default 0.5) means the model's 90%
    # interval spans most of the [0,1] direction space → no edge → it carries
    # NO information, so it must NOT drive the abstain decision. The old code
    # used max(width) across ALL models, so one degenerate width (e.g. 0.83)
    # made `|p_up-0.5| < max_w` ALWAYS true → abstain on ~100% of signals →
    # all trades blocked (cont.69d incident). Excluding ≥_EDGE_MAX models means:
    # while every model is degenerate → `edged` is empty → don't abstain (trades
    # flow); as retraining produces edged models, the gate auto-activates on them.
    try:
        _edge_max = float(_r().get("conformal:edge_max_width") or 0.5)
    except Exception:
        _edge_max = 0.5
    edged = [(m, w) for m, w in warmed if w < _edge_max]
    if not edged:
        return False

    if direction_conf is not None:
        try:
            p_up = float(direction_conf) / 100.0
        except Exception:
            return False
        # Use the WIDEST of the edged (still-informative) models.
        max_w = max(w for _, w in edged)
        # Interval straddles threshold iff |p_up - threshold| < max_w.
        return abs(p_up - threshold) < max_w

    # No conf provided: abstain only if a majority of EDGED models still
    # straddle near-neutral (width in the upper part of the edged range).
    straddlers = sum(1 for _, w in edged if w >= (_edge_max * 0.9))
    return straddlers / len(edged) >= 0.60


def diagnostic_snapshot() -> dict:
    """One-call dashboard snapshot. Safe to call anywhere — pure read."""
    if is_disabled():
        return {"ok": False, "reason": "disabled"}
    out = {"ok": True, "models": {}, "kelly_mult": kelly_multiplier()}
    for m in KNOWN_MODELS:
        out["models"][m] = {"width": width(m),
                            "n_calib": int(_r().get(f"conformal:n_calib:{m}") or 0)}
    return out
