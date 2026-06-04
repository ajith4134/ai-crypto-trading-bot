"""F49 §Component 5 — Model Version Manager.

Atomic versioned saves of every model with auto-rollback. Each `train()`
call writes to `<name>.v{utc_iso}.pth` then atomically symlinks the canonical
`<name>.pth` to it. Retention: 3 versions per model. Auto-rollback API
restores a previous version when the freshly-trained one fails validation
gates or post-deploy performance.

Redis keys per model name:
  model:{name}:active_version    — utc_iso string of currently linked version
  model:{name}:versions          — JSON list of available versions (newest first)
  model:{name}:rollback_count    — int (lifetime rollback count)
  model:{name}:last_rollback_at  — unix timestamp of most recent rollback
"""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import structlog

import redis_client

log = structlog.get_logger()

MODELS_DIR = Path("models")
RETENTION = 3   # keep this many versions per model


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _versioned_path(model_name: str, version: str) -> Path:
    """e.g. candlenet_1m → candlenet_1m.v20260525T160000Z.pth"""
    base, ext = os.path.splitext(model_name)
    if not ext:
        ext = ".pth"
    return MODELS_DIR / f"{base}.v{version}{ext}"


def _canonical_path(model_name: str) -> Path:
    """The symlink the bot reads at runtime."""
    base, ext = os.path.splitext(model_name)
    if not ext:
        ext = ".pth"
    return MODELS_DIR / f"{base}{ext}"


def _list_versions(model_name: str) -> list[str]:
    """Sorted list of available versions for `model_name`, newest first."""
    base, ext = os.path.splitext(model_name)
    if not ext:
        ext = ".pth"
    prefix = f"{base}.v"
    versions = []
    if not MODELS_DIR.exists():
        return versions
    for p in MODELS_DIR.iterdir():
        if p.name.startswith(prefix) and p.name.endswith(ext):
            # extract the version stamp between .v and the ext
            v = p.name[len(prefix): -len(ext)]
            versions.append(v)
    return sorted(versions, reverse=True)


def save_versioned(model_name: str, save_fn) -> dict:
    """Save a new model version atomically + relink canonical + prune old.

    `save_fn` is a callable that takes a Path and writes the model file there.
    This is the only public save API once F49 is enabled.

    Returns: {"version": <iso>, "path": <str>, "pruned": <list>}
    """
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    version = _now_iso()
    target  = _versioned_path(model_name, version)
    tmp     = target.with_suffix(target.suffix + ".tmp")

    save_fn(tmp)
    if not tmp.exists():
        raise RuntimeError(f"save_fn did not produce {tmp}")
    os.replace(tmp, target)

    # Atomically relink canonical to the new version
    canonical = _canonical_path(model_name)
    tmp_link  = canonical.with_suffix(canonical.suffix + ".linktmp")
    if tmp_link.exists() or tmp_link.is_symlink():
        try:
            tmp_link.unlink()
        except FileNotFoundError:
            pass
    # Use a relative symlink so the canonical path stays inside MODELS_DIR
    os.symlink(target.name, tmp_link)
    os.replace(tmp_link, canonical)

    # Prune older versions beyond RETENTION (skip whichever is now canonical)
    versions = _list_versions(model_name)
    pruned: list[str] = []
    for old_v in versions[RETENTION:]:
        old_path = _versioned_path(model_name, old_v)
        try:
            old_path.unlink()
            pruned.append(old_v)
        except Exception:
            pass

    # Publish to Redis
    try:
        r = redis_client.get()
        r.set(f"model:{model_name}:active_version", version)
        r.set(f"model:{model_name}:versions",
              json.dumps(_list_versions(model_name)))
        r.set(f"model:{model_name}:last_trained_at",
              int(datetime.now(timezone.utc).timestamp()))
        # cont. 69r — a new version invalidates the PRIOR version's shadow
        # track record. compute_rolling_auc + auto_rollback (ml/performance_
        # monitor.py) read model:{name}:prediction_log, which is NOT version-
        # tagged. Without this reset the freshly-deployed model is judged on
        # its predecessor's (pred,outcome) pairs: auto-rollback reverted a
        # val_auc-0.567 candlenet_15m to a 2d-old version ~4min after deploy
        # on a stale rolling_auc of 0.34, before the new model made a single
        # live prediction. Clear the shadow window + derived perf keys so
        # rolling_auc / peak_auc / rollback re-accumulate from THIS model's
        # own predictions (rollback can still fire once it has ROLLBACK_MIN_
        # SAMPLES of its own). Conformal (F56) reads the same log → it also
        # re-calibrates fresh, which is correct for a changed model.
        r.delete(
            f"model:{model_name}:prediction_log",
            f"model:{model_name}:rolling_auc",
            f"model:{model_name}:peak_auc",
            f"model:{model_name}:rolling_lift",
            f"model:{model_name}:rolling_n",
            f"model:{model_name}:retrain_needed",
            f"model:{model_name}:perf_degraded_at",
        )
        log.info("model_shadow_state_reset_on_deploy", model=model_name,
                 version=version)
    except Exception as exc:
        log.warning("model_version_redis_publish_failed",
                    model=model_name, error=str(exc)[:200])

    log.info("model_versioned_save",
             model=model_name, version=version, pruned=pruned)
    return {"version": version, "path": str(target), "pruned": pruned}


def rollback(model_name: str, n_steps: int = 1) -> dict:
    """Relink `model_name.pth` to the n-th most recent prior version.

    n_steps=1 means "back to immediate previous version" (the one before
    the current canonical). Returns dict with success / new version /
    reason. Caller should reload the model after this.

    Updates Redis:
      model:{name}:active_version
      model:{name}:rollback_count
      model:{name}:last_rollback_at
    """
    versions = _list_versions(model_name)
    if len(versions) <= n_steps:
        return {"status": "no_prior_version",
                "available": versions, "requested": n_steps}

    target_version = versions[n_steps]   # versions[0] is current; versions[n] is n-th back
    target_path    = _versioned_path(model_name, target_version)
    if not target_path.exists():
        return {"status": "target_missing", "version": target_version}

    canonical = _canonical_path(model_name)
    tmp_link  = canonical.with_suffix(canonical.suffix + ".linktmp")
    if tmp_link.exists() or tmp_link.is_symlink():
        try:
            tmp_link.unlink()
        except FileNotFoundError:
            pass
    os.symlink(target_path.name, tmp_link)
    os.replace(tmp_link, canonical)

    try:
        r = redis_client.get()
        r.set(f"model:{model_name}:active_version", target_version)
        r.incr(f"model:{model_name}:rollback_count")
        r.set(f"model:{model_name}:last_rollback_at",
              int(datetime.now(timezone.utc).timestamp()))
    except Exception:
        pass

    log.info("model_rolled_back", model=model_name, version=target_version,
             n_steps=n_steps)
    return {"status": "rolled_back", "version": target_version,
            "n_steps": n_steps}


def get_active_version(model_name: str) -> str | None:
    """Return the currently linked version, or None if no canonical file."""
    canonical = _canonical_path(model_name)
    if not canonical.exists():
        return None
    try:
        # If canonical is a symlink, the link target tells us the version
        if canonical.is_symlink():
            target = os.readlink(canonical)
            base, ext = os.path.splitext(model_name)
            if not ext:
                ext = ".pth"
            prefix = f"{base}.v"
            if target.startswith(prefix) and target.endswith(ext):
                return target[len(prefix): -len(ext)]
        # Non-symlink canonical (legacy file) → unknown version
        return None
    except Exception:
        return None
