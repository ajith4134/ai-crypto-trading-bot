"""F49 §Component 6 — Meta-Orchestrator.

Decides WHICH model to train WHEN. Runs every 5 minutes via Celery beat.
Priority order:
  1. Drift detected since last train         (highest priority)
  2. Performance degraded (retrain_needed)   (medium priority)
  3. Weekly cron missed (last train > 8d)    (lowest priority)
  4. Otherwise: idle

Resource gate: only one model training at a time (CPU-bound on VPS).
When a Celery retrain task is in flight (`orchestrator:active_training`
key set), the orchestrator stays idle.

Audit log: every decision pushed to Redis list `orchestrator:decisions`
with rationale (LPUSH + LTRIM 100).
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

import structlog
import redis_client

log = structlog.get_logger()

# Time since last train at which the orchestrator will trigger a "missed
# weekly cron" retrain. 8 days because the weekly Celery beat fires every
# Sunday — if it's been more than 8 days, the worker missed it.
MISSED_CRON_SECS = 8 * 24 * 3600

# Models that the orchestrator can request retrain for, mapped to the
# Celery task name that retrains them.
RETRAIN_TASKS = {
    "candlenet_1m":    "celery_app.retrain_candlenet_1m",
    "candlenet_5m":    "celery_app.retrain_candlenet_5m",
    "candlenet_15m":   "celery_app.retrain_candlenet_15m",
    "tft":             None,                          # weekly only — no orchestrator override
    "patchtst":        None,                          # weekly only
    "direction_model": None,                          # event-driven (online learner)
}


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _audit(decision: dict) -> None:
    """Push the decision to the orchestrator log + set last_decision_at."""
    try:
        r = redis_client.get()
        decision["ts"] = _now_ts()
        r.lpush("orchestrator:decisions", json.dumps(decision))
        r.ltrim("orchestrator:decisions", 0, 99)
        r.set("orchestrator:last_decision_at", decision["ts"])
        r.set("orchestrator:last_decision", json.dumps(decision))
    except Exception:
        pass


def _is_training_in_flight() -> bool:
    """Returns True if Redis says a training task is currently running."""
    try:
        r = redis_client.get()
        active = r.get("orchestrator:active_training")
        if not active:
            return False
        # Stale flag protection: if the active flag is older than 6 hours,
        # assume the Celery worker crashed and the lock leaked.
        started_at_raw = r.get("orchestrator:active_training_started_at")
        if started_at_raw:
            try:
                started_at = int(started_at_raw)
                if _now_ts() - started_at > 6 * 3600:
                    r.delete("orchestrator:active_training",
                             "orchestrator:active_training_started_at")
                    return False
            except (TypeError, ValueError):
                pass
        return True
    except Exception:
        return False


def _model_state(name: str) -> dict:
    """Snapshot the per-model state we need to make a decision."""
    r = redis_client.get()
    def _i(k):
        v = r.get(k)
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0
    def _s(k):
        return r.get(k) or ""
    return {
        "name":               name,
        "last_trained_at":    _i(f"model:{name}:last_trained_at"),
        "drift_detected":     _s(f"model:{name}:drift_detected") == "1",
        "drift_detected_at":  _i(f"model:{name}:drift_detected_at"),
        "retrain_needed":     _s(f"model:{name}:retrain_needed") == "1",
        "perf_degraded_at":   _i(f"model:{name}:perf_degraded_at"),
    }


def _pick_candidate(states: list[dict]) -> tuple[dict | None, str]:
    """Priority-ranked picker. Returns (chosen_state, rationale)."""
    now = _now_ts()

    # 1. Drift since last train — highest priority
    drift_candidates = [
        s for s in states
        if s["drift_detected"] and s["drift_detected_at"] > s["last_trained_at"]
        and RETRAIN_TASKS.get(s["name"]) is not None
    ]
    if drift_candidates:
        # Pick the model whose drift is most recent
        chosen = max(drift_candidates, key=lambda s: s["drift_detected_at"])
        return chosen, "drift_detected_since_last_train"

    # 2. Perf-degraded — medium priority
    perf_candidates = [
        s for s in states
        if s["retrain_needed"] and RETRAIN_TASKS.get(s["name"]) is not None
    ]
    if perf_candidates:
        chosen = max(perf_candidates, key=lambda s: s["perf_degraded_at"])
        return chosen, "performance_degraded"

    # 3. Missed weekly cron — lowest priority
    missed_candidates = [
        s for s in states
        if (now - s["last_trained_at"]) > MISSED_CRON_SECS
        and s["last_trained_at"] > 0
        and RETRAIN_TASKS.get(s["name"]) is not None
    ]
    if missed_candidates:
        chosen = min(missed_candidates, key=lambda s: s["last_trained_at"])
        return chosen, "missed_weekly_cron"

    return None, "all_models_healthy"


def orchestrator_tick() -> dict:
    """One pass of the orchestrator. Called by Celery beat every 5 min."""
    if _is_training_in_flight():
        decision = {"action": "skip", "reason": "training_in_flight"}
        _audit(decision)
        return decision

    states = [_model_state(n) for n in RETRAIN_TASKS.keys()]
    chosen, rationale = _pick_candidate(states)
    if chosen is None:
        decision = {"action": "idle", "reason": rationale}
        _audit(decision)
        return decision

    task_name = RETRAIN_TASKS[chosen["name"]]
    decision = {
        "action":   "trigger_retrain",
        "model":    chosen["name"],
        "task":     task_name,
        "reason":   rationale,
    }

    # Submit the Celery task asynchronously
    try:
        from celery_app import app
        async_result = app.send_task(task_name, queue="default")
        decision["task_id"] = async_result.id

        # Set the in-flight lock + start timestamp
        r = redis_client.get()
        r.set("orchestrator:active_training", chosen["name"])
        r.set("orchestrator:active_training_started_at", _now_ts())
    except Exception as exc:
        decision["action"] = "trigger_failed"
        decision["error"] = str(exc)[:200]
        log.error("orchestrator_dispatch_failed",
                  model=chosen["name"], error=str(exc)[:200])

    _audit(decision)
    log.info("orchestrator_decision", **{k: v for k, v in decision.items()
                                          if k != "ts"})
    return decision


def mark_training_done(model_name: str, status: str = "ok") -> None:
    """Called by retrain Celery tasks at completion to release the lock."""
    try:
        r = redis_client.get()
        r.delete("orchestrator:active_training",
                 "orchestrator:active_training_started_at")
        # Successful train clears the retrain_needed flag (perf monitor will
        # re-flag if the new model is still degraded).
        if status == "ok":
            from ml.performance_monitor import clear_retrain_flag
            clear_retrain_flag(model_name)
            r.delete(f"model:{model_name}:drift_detected_at",
                     f"model:{model_name}:drift_detected")
        # Audit completion
        r.lpush("orchestrator:decisions", json.dumps({
            "action": "retrain_complete",
            "model":  model_name,
            "status": status,
            "ts":     _now_ts(),
        }))
        r.ltrim("orchestrator:decisions", 0, 99)
    except Exception:
        pass
