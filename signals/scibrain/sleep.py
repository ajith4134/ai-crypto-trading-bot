"""SciBrain Phase-7e — isolated SLEEP / consolidation jobs (design §3.6 / §9 "isolated sleep jobs").

The brain's "sleep" cycle: a set of maintenance jobs run OFF the live trade path (isolated — a worker beat
task, single-flight, load-aware) so they never starve risk/execution. Each job wraps the machinery built
in steps 3-5 (episodic replay + EWC consolidation) plus calibration / adversarial-rehearsal / homeostasis
/ pruning, and reports its own health. Tier-0: pure read + ephemeral bookkeeping only — NO live trading
state is mutated.

  • replay        — exercise the prioritized-replay sampler (rare-failure coverage + IS-unbiasedness).
  • consolidation — check the EWC slow-consolidation health + flag if a re-consolidation is DUE.
  • calibration   — read the ex-ante audit calibration (Brier) + flag drift / freshness.
  • adversarial   — perturb recent episodes within a bounded ball; measure how STABLE the top-priority set
                    is (a fragile memory whose priorities flip under tiny noise is a problem).
  • homeostasis   — check internal balance (priority-distribution entropy / degeneracy) and report what
                    WOULD be rebalanced (read-only).
  • pruning       — surface prune candidates (PRUNE-verdict modules + redundant episodes); reports, never
                    deletes live state.
"""
from __future__ import annotations

import json
import math
import time

import numpy as np
import structlog

from . import keys as K

log = structlog.get_logger()

_LOCK = "scibrain:sleep:lock"
_LAST = "scibrain:sleep:last"
_CONSOLIDATION_DUE_H = 24      # re-consolidate if the last EWC report is older than this


def _safe(fn):
    try:
        return fn()
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:140]}


def _load_factor() -> float:
    try:
        import os
        return max(0.15, min(1.0, 1.0 - os.getloadavg()[0] / (os.cpu_count() or 8)))
    except Exception:
        return 0.6


# ── individual sleep jobs ────────────────────────────────────────────────────────────────────────
def _job_replay(r) -> dict:
    from . import episodic
    h = episodic.replay_health(r, limit=300, publish=True)
    if not h.get("available"):
        return {"status": "cold", "note": h.get("note")}
    est = h.get("estimates") or {}
    return {"status": "ran", "health": h.get("health"),
            "rare_oversampled": round(est.get("sampled_loss_rate", 0) - est.get("base_loss_rate", 0), 4),
            "is_unbiased": bool(est.get("bias_recovered")),
            "coverage": (h.get("coverage") or {}).get("ess_frac"),
            "warns": (h.get("n_issues") or {}).get("warn", 0)}


def _job_consolidation(r) -> dict:
    from . import consolidation_health
    h = consolidation_health.build_consolidation_health(r, publish=True)
    if h.get("health") == "no_run":
        return {"status": "due", "note": "no consolidation run yet — schedule ml.consolidation"}
    age_h = float(h.get("report_age_hours") or 0)
    due = age_h > _CONSOLIDATION_DUE_H
    return {"status": "due" if due else "fresh", "health": h.get("health"),
            "accepted": h.get("accepted"), "ewc_helps": h.get("ewc_reduces_forgetting"),
            "forget_ewc": (h.get("forgetting") or {}).get("with_ewc"), "age_h": round(age_h, 1)}


def _job_calibration(r) -> dict:
    cal = _jload(r.get(K.AUDIT_CALIBRATION), {})
    ic_map = r.hgetall(K.IC_MAP) or {}
    brier = cal.get("brier")
    # Brier vs base-rate baseline: a forecaster should beat predicting the base rate
    base = cal.get("base_rate")
    drift = (brier is not None and base is not None and brier > base * (1 - base) + 0.02)
    return {"status": "ran", "brier": brier, "n_graded": cal.get("n"),
            "n_modules_with_ic": len(ic_map), "drift": bool(drift)}


def _job_adversarial(r) -> dict:
    """Perturb recent episodes within a bounded ball; the top-priority replay set should be STABLE."""
    from . import episodic
    rows = episodic._load_rows(300)
    if len(rows) < 40:
        return {"status": "cold", "n": len(rows)}
    a0 = episodic._assemble(rows)
    p0 = a0["priority"]; top0 = set(np.argsort(-p0)[:30].tolist())
    # bounded perturbation of the module_vec inputs (the adversary)
    rng = np.random.default_rng(0)
    pert_rows = []
    for rw in rows:
        rw2 = dict(rw); me = dict(rw.get("module_embedding") or {})
        mv = list(me.get("module_vec") or [])
        me["module_vec"] = [float(v) + 0.05 * rng.standard_normal() for v in mv]
        rw2["module_embedding"] = me
        pert_rows.append(rw2)
    a1 = episodic._assemble(pert_rows)
    top1 = set(np.argsort(-a1["priority"])[:30].tolist())
    jacc = len(top0 & top1) / len(top0 | top1) if (top0 | top1) else 1.0
    return {"status": "ran", "epsilon": 0.05, "top_priority_jaccard": round(jacc, 3),
            "stable": bool(jacc >= 0.6)}


def _job_homeostasis(r) -> dict:
    """Check the priority distribution isn't degenerate (entropy) — a runaway/collapsed memory is unhealthy."""
    from . import episodic
    rows = episodic._load_rows(300)
    if len(rows) < 40:
        return {"status": "cold", "n": len(rows)}
    p = episodic._assemble(rows)["priority"]
    p = p + 1e-6; q = p / p.sum()
    ent = float(-(q * np.log(q + 1e-12)).sum())
    max_ent = math.log(len(q))
    norm_ent = ent / max_ent if max_ent > 0 else 0.0           # 1 = uniform, →0 = collapsed onto few
    return {"status": "ran", "priority_entropy_norm": round(norm_ent, 3),
            "balanced": bool(norm_ent > 0.6)}


def _job_pruning(r) -> dict:
    """Surface prune candidates — PRUNE-verdict modules + redundant episodes. Reports only; deletes nothing live."""
    rep = _jload(r.get(K.ABLATION_REPORT), {})
    verdicts = rep.get("verdicts") if isinstance(rep.get("verdicts"), dict) else {}
    prune_mods = [m for m, v in verdicts.items() if isinstance(v, dict) and v.get("status") == "PRUNE"]
    from . import episodic
    rows = episodic._load_rows(300)
    redundant = 0
    if len(rows) >= 40:
        red = episodic._assemble(rows)["redundancy"]
        redundant = int((red > 0.6).sum())
    return {"status": "ran", "prune_candidate_modules": prune_mods[:10],
            "n_prune_modules": len(prune_mods), "n_redundant_episodes": redundant,
            "action": "report_only (no live state mutated)"}


def run_sleep_cycle(r, *, force: bool = False) -> dict:
    """Run one isolated sleep cycle (single-flight, load-aware). Each job is isolated — a failure in one
    does not abort the cycle. Returns the cycle report; writes _LAST. Never raises."""
    out = {"contract": "SleepCycle", "ts": round(time.time(), 3), "available": False}
    got_lock = False
    try:
        got_lock = bool(r.set(_LOCK, "1", nx=True, ex=600))
        if not got_lock and not force:
            return {**out, "skipped": "another sleep cycle is running"}
        load = _load_factor()
        t0 = time.time()
        jobs = {
            "replay": _safe(lambda: _job_replay(r)),
            "consolidation": _safe(lambda: _job_consolidation(r)),
            "calibration": _safe(lambda: _job_calibration(r)),
            "adversarial": _safe(lambda: _job_adversarial(r)),
            "homeostasis": _safe(lambda: _job_homeostasis(r)),
            "pruning": _safe(lambda: _job_pruning(r)),
        }
        # cycle-level issues
        issues = []
        if jobs["consolidation"].get("status") == "due":
            issues.append({"severity": "warn", "job": "consolidation",
                           "msg": "re-consolidation due (EWC report stale) — schedule ml.consolidation"})
        if jobs["calibration"].get("drift"):
            issues.append({"severity": "warn", "job": "calibration", "msg": "audit forecaster Brier worse than base-rate baseline"})
        if jobs["adversarial"].get("stable") is False:
            issues.append({"severity": "warn", "job": "adversarial",
                           "msg": f"replay priorities fragile under ε perturbation (jaccard {jobs['adversarial'].get('top_priority_jaccard')})"})
        if jobs["homeostasis"].get("balanced") is False:
            issues.append({"severity": "warn", "job": "homeostasis", "msg": "priority distribution collapsing (low entropy)"})
        errored = [k for k, v in jobs.items() if v.get("status") == "error"]
        for k in errored:
            issues.append({"severity": "critical", "job": k, "msg": jobs[k].get("error")})

        worst = 3 if errored else (2 if issues else 1)
        out.update({
            "available": True, "load_factor": round(load, 3), "duration_s": round(time.time() - t0, 2),
            "jobs": jobs, "issues": issues,
            "n_issues": {"critical": len(errored),
                         "warn": sum(1 for i in issues if i["severity"] == "warn"), "info": 0},
            "health": "errors" if errored else "watch" if issues else "rested",
            "note": ("Isolated sleep cycle (design §3.6/§9): replay rehearsal, EWC consolidation check, "
                     "calibration, adversarial robustness, homeostasis, and prune-candidate surfacing — run "
                     "OFF the hot path (single-flight, load-aware), pure read + ephemeral bookkeeping, no "
                     "live trading state mutated."),
        })
        try:
            r.set(_LAST, json.dumps(out))
        except Exception:
            pass
        log.info("scibrain_sleep_cycle", health=out["health"], duration_s=out["duration_s"],
                 issues=len(issues))
    except Exception as exc:
        out["error"] = str(exc)[:200]
        log.warning("scibrain_sleep_cycle_failed", error=str(exc)[:200])
    finally:
        if got_lock:
            try:
                r.delete(_LOCK)
            except Exception:
                pass
    return out


def sleep_status(r) -> dict:
    """Last sleep-cycle report (for the dashboard). Pure read."""
    last = _jload(r.get(_LAST), None)
    if not last:
        return {"available": False, "health": "never_slept", "note": "no sleep cycle has run yet"}
    last["age_s"] = round(time.time() - last.get("ts", 0), 1)
    return last


def _jload(v, default=None):
    try:
        return json.loads(v) if isinstance(v, (str, bytes)) else (v if v is not None else default)
    except (TypeError, ValueError):
        return default
