"""F49 §Component 3 — Auto HPO Engine.

Bayesian Optimisation via Optuna over the standard PyTorch hyperparameter
space (learning rate, dropout, batch size, weight decay). Used by each
model's `train()` to discover hyperparameters that work better than the
hardcoded defaults, with a strict trial budget so the search stays cheap.

Per-model best params cached in Redis:
  model:{name}:hpo_best_params  — JSON dict
  model:{name}:hpo_last_run_at  — unix timestamp
  model:{name}:hpo_trials       — int (total trials run)
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Callable

import structlog
import redis_client

log = structlog.get_logger()

TRIAL_BUDGET    = 10        # per retrain
DEFAULTS        = {
    "lr":            1e-3,
    "dropout":       0.3,
    "batch_size":    128,
    "weight_decay":  1e-5,
}
BATCH_SIZE_CHOICES = [64, 128, 256, 512]


def get_cached_params(model_name: str) -> dict:
    """Return cached best params for `model_name`, or defaults."""
    try:
        r = redis_client.get()
        raw = r.get(f"model:{model_name}:hpo_best_params")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return dict(DEFAULTS)


def search_best_params(model_name: str,
                       train_eval_fn: Callable[[dict], float],
                       n_trials: int = TRIAL_BUDGET,
                       direction: str = "maximize") -> dict:
    """Run Bayesian Optimisation over the param space and return best params.

    `train_eval_fn(params: dict) -> float` should train a short-budget model
    with the given params and return the metric to optimise (val_auc for
    direction-task models). Optuna's TPE sampler picks promising regions to
    explore — much smarter than random search at 10-trial budget.

    Falls back to DEFAULTS when optuna is unavailable or the search fails.
    """
    try:
        import optuna
        from optuna.samplers import TPESampler
    except Exception as exc:
        log.warning("hpo_optuna_unavailable", error=str(exc)[:200])
        return dict(DEFAULTS)

    # Seed Optuna with the previously discovered best params so the first
    # trial starts in a known-good region.
    seed_params = get_cached_params(model_name)

    def _objective(trial):
        params = {
            "lr":           trial.suggest_float("lr", 1e-4, 1e-2, log=True),
            "dropout":      trial.suggest_float("dropout", 0.1, 0.5),
            "batch_size":   trial.suggest_categorical("batch_size", BATCH_SIZE_CHOICES),
            "weight_decay": trial.suggest_float("weight_decay", 1e-6, 1e-3, log=True),
        }
        try:
            return float(train_eval_fn(params))
        except Exception as exc:
            log.warning("hpo_trial_failed",
                        model=model_name, error=str(exc)[:200])
            # Return worst-possible metric so Optuna avoids this region
            return 0.0 if direction == "maximize" else float("inf")

    try:
        sampler = TPESampler(seed=42, n_startup_trials=2)
        study = optuna.create_study(direction=direction, sampler=sampler)
        # Inject the cached best params as the first trial
        try:
            study.enqueue_trial({
                "lr":           seed_params.get("lr", DEFAULTS["lr"]),
                "dropout":      seed_params.get("dropout", DEFAULTS["dropout"]),
                "batch_size":   seed_params.get("batch_size", DEFAULTS["batch_size"]),
                "weight_decay": seed_params.get("weight_decay", DEFAULTS["weight_decay"]),
            })
        except Exception:
            pass

        study.optimize(_objective, n_trials=n_trials,
                       show_progress_bar=False)
        best = study.best_params

        # Cache the winning params
        try:
            r = redis_client.get()
            r.set(f"model:{model_name}:hpo_best_params", json.dumps(best))
            r.set(f"model:{model_name}:hpo_last_run_at",
                  int(datetime.now(timezone.utc).timestamp()))
            r.incrby(f"model:{model_name}:hpo_trials", n_trials)
            r.set(f"model:{model_name}:hpo_best_value",
                  round(float(study.best_value), 4))
        except Exception:
            pass

        log.info("hpo_search_done", model=model_name,
                 n_trials=n_trials,
                 best_value=round(float(study.best_value), 4),
                 best=best)
        return best
    except Exception as exc:
        log.warning("hpo_search_failed", model=model_name, error=str(exc)[:200])
        return dict(DEFAULTS)
