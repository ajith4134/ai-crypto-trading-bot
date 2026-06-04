"""F49 §Component 2 — Performance Monitor.

For each model with a directional prediction, tracks the rolling 100-trade
correctness of its forecasts vs actual outcomes. When rolling AUC drops
below 90% of the all-time peak, sets retrain_needed=1.

Per-trade prediction snapshots are captured at signal-firing time and
matched at trade-close time. Stored in a Redis list per model:
  model:{name}:prediction_log     — JSON entries {pred: float, outcome: float, ts: int}

Status keys:
  model:{name}:rolling_auc        — float, latest rolling-100 AUC
  model:{name}:peak_auc           — float, all-time best rolling-100 AUC
  model:{name}:rolling_lift       — float, latest top-decile lift
  model:{name}:perf_degraded_at   — unix timestamp (only when retrain triggered)
  model:{name}:retrain_needed     — "0" / "1"
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

import structlog
import numpy as np

import redis_client

log = structlog.get_logger()

ROLLING_WINDOW = 100
DEGRADE_RATIO  = 0.90   # rolling_auc < peak_auc * 0.90 → retrain
MIN_SAMPLES    = 30     # don't fire below this much data

# Auto-rollback config — F49 §Component 5 wiring.
# Stricter than DEGRADE_RATIO because a rollback is irreversible-ish — we
# want strong evidence the new model is worse before reverting. The
# detection window means we only rollback freshly-deployed models (a model
# that's been in production for weeks degrading is a retrain candidate,
# not a rollback candidate — the prior version is also stale by then).
ROLLBACK_DEGRADE_RATIO   = 0.85    # rolling_auc < peak * 0.85 → rollback
ROLLBACK_DETECTION_HOURS = 48      # only rollback if active version < this old
ROLLBACK_COOLDOWN_HOURS  = 24      # don't rollback the same model twice in 24h
ROLLBACK_MIN_SAMPLES     = 50      # need more evidence than for retrain flag


def log_prediction(model_name: str, pred: float, ts: int | None = None) -> None:
    """Record a per-trade prediction. Pair with `log_outcome()` at trade close."""
    if ts is None:
        ts = int(datetime.now(timezone.utc).timestamp())
    try:
        r = redis_client.get()
        key = f"model:{model_name}:pending_predictions"
        r.lpush(key, json.dumps({"pred": float(pred), "ts": ts}))
        r.ltrim(key, 0, 9999)
    except Exception:
        pass


def log_outcome(model_name: str, pred: float, outcome: float) -> None:
    """Record a prediction-outcome pair after a trade closes.

    pred:    P(up) at the time the signal fired (0.0–1.0)
    outcome: 1.0 if the actual move was up, 0.0 if down
    """
    try:
        r = redis_client.get()
        key = f"model:{model_name}:prediction_log"
        r.lpush(key, json.dumps({
            "pred":    float(pred),
            "outcome": float(outcome),
            "ts":      int(datetime.now(timezone.utc).timestamp()),
        }))
        r.ltrim(key, 0, ROLLING_WINDOW * 10 - 1)
    except Exception:
        pass


def compute_rolling_auc(model_name: str) -> dict:
    """Compute rolling AUC + top-decile lift on the most recent ROLLING_WINDOW
    prediction-outcome pairs. Updates Redis status keys + retrain_needed flag.
    """
    try:
        from sklearn.metrics import roc_auc_score
    except Exception:
        return {"status": "sklearn_unavailable"}

    r = redis_client.get()
    raws = r.lrange(f"model:{model_name}:prediction_log", 0, ROLLING_WINDOW - 1)
    if len(raws) < MIN_SAMPLES:
        return {"status": "insufficient_data", "n": len(raws)}

    preds, outcomes = [], []
    for x in raws:
        try:
            d = json.loads(x)
            preds.append(float(d["pred"]))
            outcomes.append(float(d["outcome"]))
        except Exception:
            continue
    if len(preds) < MIN_SAMPLES:
        return {"status": "insufficient_clean", "n": len(preds)}

    preds_arr = np.array(preds)
    out_arr   = np.array(outcomes)

    # AUC — needs both classes present
    if len(set(outcomes)) < 2:
        auc = 0.5
    else:
        try:
            auc = float(roc_auc_score(out_arr, preds_arr))
        except Exception:
            auc = 0.5

    # Top-decile lift
    decile_n = max(1, len(preds_arr) // 10)
    order = np.argsort(-preds_arr)
    pos_rate = float(out_arr.mean())
    top_winrate = float(out_arr[order[:decile_n]].mean())
    lift = top_winrate / pos_rate if pos_rate > 0 else 0.0

    # Compare to peak
    try:
        peak_raw = r.get(f"model:{model_name}:peak_auc")
        peak = float(peak_raw) if peak_raw else auc
    except Exception:
        peak = auc
    if auc > peak:
        peak = auc

    retrain_needed = (auc < peak * DEGRADE_RATIO and peak > 0.55)

    try:
        r.set(f"model:{model_name}:rolling_auc", round(auc, 4))
        r.set(f"model:{model_name}:peak_auc", round(peak, 4))
        r.set(f"model:{model_name}:rolling_lift", round(lift, 4))
        r.set(f"model:{model_name}:rolling_n", len(preds))
        if retrain_needed:
            r.set(f"model:{model_name}:retrain_needed", "1")
            existing = r.get(f"model:{model_name}:perf_degraded_at")
            if not existing:
                r.set(f"model:{model_name}:perf_degraded_at",
                      int(datetime.now(timezone.utc).timestamp()))
        # Note: do NOT clear retrain_needed here — only the orchestrator clears
        # it after a successful retrain.
    except Exception as exc:
        log.warning("perf_publish_failed", model=model_name, error=str(exc)[:200])

    # F49 §Component 5 — auto-rollback if a freshly-deployed version is
    # underperforming. Runs INSIDE compute_rolling_auc so it fires the same
    # cadence as performance checks (every 5 min via Celery beat).
    rollback_result = None
    if auc < peak * ROLLBACK_DEGRADE_RATIO and len(preds) >= ROLLBACK_MIN_SAMPLES:
        rollback_result = auto_rollback_if_perf_degraded(
            model_name, current_auc=auc, peak_auc=peak)

    return {
        "status":          "ok",
        "auc":             round(auc, 4),
        "peak_auc":        round(peak, 4),
        "lift":            round(lift, 4),
        "n":               len(preds),
        "retrain_needed":  retrain_needed,
        "rollback":        rollback_result,
    }


def auto_rollback_if_perf_degraded(model_name: str, current_auc: float,
                                    peak_auc: float) -> dict:
    """F49 §Component 5 trigger — rollback to the prior version when a newly
    deployed model is performing materially worse than the prior peak.

    Decision logic:
      - Active version must have been deployed within ROLLBACK_DETECTION_HOURS.
      - At least 2 versions must exist on disk.
      - Cooldown gate: no rollback in the last ROLLBACK_COOLDOWN_HOURS.
      - rolling_auc < peak * ROLLBACK_DEGRADE_RATIO (already checked by caller).

    Returns a dict describing the action (status: skipped | rolled_back |
    error). Sets Redis circuit-breaker key `model:{name}:rollback_cooldown_until`
    so a flapping model can't ping-pong between versions.
    """
    try:
        from ml.model_versions import (
            rollback, get_active_version, _list_versions,
        )
    except Exception as exc:
        return {"status": "module_unavailable", "error": str(exc)[:120]}

    try:
        r = redis_client.get()
        now_ts = int(datetime.now(timezone.utc).timestamp())

        # Cooldown gate first — cheap and avoids version-listing IO.
        cooldown_raw = r.get(f"model:{model_name}:rollback_cooldown_until")
        if cooldown_raw:
            try:
                if int(cooldown_raw) > now_ts:
                    return {"status": "in_cooldown",
                            "until": int(cooldown_raw)}
            except (TypeError, ValueError):
                pass

        # Need an active version to know when it was deployed.
        active = get_active_version(model_name)
        if active is None:
            return {"status": "no_active_version"}

        last_trained_raw = r.get(f"model:{model_name}:last_trained_at")
        if not last_trained_raw:
            return {"status": "no_last_trained_at"}
        try:
            last_trained = int(last_trained_raw)
        except (TypeError, ValueError):
            return {"status": "bad_last_trained_at"}

        age_secs = now_ts - last_trained
        if age_secs > ROLLBACK_DETECTION_HOURS * 3600:
            # Active version isn't "fresh" — the prior version is now stale
            # too; rollback wouldn't help. Let the orchestrator schedule a
            # fresh retrain instead.
            return {"status": "active_version_too_old",
                    "age_secs": age_secs}

        # Need a prior version to roll back to.
        versions = _list_versions(model_name)
        if len(versions) < 2:
            return {"status": "no_prior_version",
                    "versions_on_disk": versions}

        # All gates passed — rollback.
        rollback_result = rollback(model_name, n_steps=1)
        if rollback_result.get("status") != "rolled_back":
            return {"status": "rollback_failed", **rollback_result}

        # Set cooldown so we don't rollback again immediately if the prior
        # version is also underperforming on the same prediction stream.
        cooldown_until = now_ts + ROLLBACK_COOLDOWN_HOURS * 3600
        r.set(f"model:{model_name}:rollback_cooldown_until", cooldown_until)

        # Clear retrain_needed — rollback IS the response. If the prior
        # version also degrades, the next perf check will re-flag it and
        # the orchestrator will schedule a real retrain.
        clear_retrain_flag(model_name)

        # Reset peak_auc to a sane prior since we're now serving a different
        # model. Use the current rolling_auc + 0.05 as a fresh anchor —
        # avoids the new (prior) version immediately being flagged as
        # "below peak" by the leftover peak from the rolled-back version.
        r.set(f"model:{model_name}:peak_auc", round(current_auc + 0.05, 4))

        # Audit
        r.incr(f"model:{model_name}:auto_rollback_count")
        r.set(f"model:{model_name}:last_auto_rollback_at", now_ts)
        r.lpush("orchestrator:decisions", json.dumps({
            "action":      "auto_rollback",
            "model":       model_name,
            "from_version": rollback_result.get("version"),
            "current_auc": round(current_auc, 4),
            "peak_auc":    round(peak_auc, 4),
            "ratio":       round(current_auc / peak_auc, 4)
                           if peak_auc > 0 else 0.0,
            "ts":          now_ts,
        }))
        r.ltrim("orchestrator:decisions", 0, 99)

        log.warning("auto_rollback_triggered",
                    model=model_name,
                    current_auc=round(current_auc, 4),
                    peak_auc=round(peak_auc, 4),
                    rolled_back_to=rollback_result.get("version"))
        return {"status": "rolled_back",
                "to_version": rollback_result.get("version"),
                "cooldown_until": cooldown_until}
    except Exception as exc:
        log.warning("auto_rollback_failed",
                    model=model_name, error=str(exc)[:200])
        return {"status": "error", "error": str(exc)[:200]}


# Model names with directional predictions (tracked by performance monitor)
TRACKED_MODELS = [
    "candlenet_1m", "candlenet_5m", "candlenet_15m",
    "candlenet_30m", "candlenet_1h",
    "tft", "patchtst", "direction_model",
]


def performance_check_all() -> dict:
    """Compute rolling AUC for every tracked model. Called by Celery beat."""
    out = {}
    for name in TRACKED_MODELS:
        try:
            out[name] = compute_rolling_auc(name)
        except Exception as exc:
            out[name] = {"status": "error", "error": str(exc)[:200]}
    return out


def clear_retrain_flag(model_name: str) -> None:
    """Called by the orchestrator after a successful retrain to clear the flag."""
    try:
        r = redis_client.get()
        r.set(f"model:{model_name}:retrain_needed", "0")
        r.delete(f"model:{model_name}:perf_degraded_at")
    except Exception:
        pass
