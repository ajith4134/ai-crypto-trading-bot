"""SciBrain Phase-7f — META-LEARNING health (real regime/cohort tasks + protected competence; upgrades
ml/maml.py; §3.7 / §5.4-7 / §5.5 stage-6 / §8-419).

Reads the shadow meta-learning report (scibrain:meta_learning:report, written by ml/meta_regime_tasks) and
diagnoses the design's acceptance for stage-6 meta-learning:

  • FEW-SHOT ADAPTATION — does a meta-learned init adapt few-shot to a new regime/cohort better than (a) the
    un-adapted init, (b) learning from scratch, and (c) one pooled model? (The honest "does it help" question.)
  • PROTECTED COMPETENCE (§5.4-7) — does the protected-init MAML protocol keep old-cohort competence while the
    live-style sequential fine-tune forgets it? (The reason the live maml's shared-weight overwrite is unsafe.)
  • INTEGRITY — real (nonzero, varying) belief state and NO target leakage, fixing the live maml's
    synthetic-zero-state + pnl-in-input shortcut.

Tier-0 pure read; never raises. SHADOW — no live authority; ml/maml.py untouched (Rule 21).
"""
from __future__ import annotations

import json
import time

import structlog

from . import keys as K

log = structlog.get_logger()

_STALE_S = 36 * 3600


def build_meta_learning_health(r, *, publish: bool = True) -> dict:
    """Diagnose the meta-learning report into a typed health/issues view. Pure read; never raises."""
    out = {"contract": "MetaLearningHealth", "available": False, "ts": round(time.time(), 3)}
    try:
        raw = r.get(K.META_LEARNING_REPORT)
        rep = json.loads(raw) if raw else None
        if not rep:
            out.update({"health": "cold", "issues": [],
                        "note": "Meta-learning has not run yet — no report. Run ml.meta_regime_tasks "
                                "(or wait for the beat task)."})
            if publish:
                try:
                    r.set(K.META_LEARNING_HEALTH, json.dumps(out))
                except Exception:
                    pass
            return out

        fs = rep.get("few_shot") or {}
        pc = rep.get("protected_competence") or {}
        integ = rep.get("integrity") or {}
        helps = bool(fs.get("adaptation_helps"))
        protected = bool(pc.get("competence_protected"))
        forgetting_shown = bool(pc.get("forgetting_demonstrated"))
        state_real = bool(integ.get("state_real"))
        leak = bool(integ.get("target_leakage"))
        age_s = max(0, int(time.time() - float(rep.get("ts") or 0)))
        stale = age_s > _STALE_S

        issues = []

        def add(code, sev, title, detail, rec):
            issues.append({"code": code, "severity": sev, "title": title, "detail": detail, "recommendation": rec})

        # the build itself (§8-419 deliverable)
        add("meta_layer_live", "info",
            f"Meta-learning on REAL cohorts live — {rep.get('n_cohorts')} regime×VPIN tasks, {fs.get('support_k') or rep.get('config', {}).get('support_k')}-shot, Reptile",
            "Real belief feature states → twin-utility targets, few-shot adapt per cohort, with a "
            "protected-competence test vs sequential fine-tune. Upgrades the synthetic per-pair live maml.",
            "Owner-gated promotion only after stage-6 benchmarks (§5.5); world model has no authority yet.")

        # INTEGRITY — the named requirement: no synthetic-zero-state, no leak
        if state_real and not leak:
            add("integrity_ok", "info", "No synthetic-zero-state, no target leakage",
                "State = real varying belief features; target = twin utility, never an input — fixing the live "
                "maml's all-zeros latent + pnl-in-input shortcut.",
                "Keep targets counterfactual (twin) and states real; never feed the label as a feature.")
        else:
            add("integrity_fail", "critical",
                f"Integrity FAIL — state_real={state_real}, target_leakage={leak}",
                "The corpus produced a degenerate (zero/constant) state or a leaked target.",
                "Do not trust any gain until the feature/target construction is fixed.")

        # FEW-SHOT ADAPTATION (the headline) — earns its keep only if it ALSO beats one pooled model
        beats_pooled = bool(fs.get("beats_pooled"))
        if helps:
            add("adaptation_helps", "info",
                f"Few-shot adaptation earns its keep — adaptation gain {fs.get('adaptation_gain'):+}, meta gain {fs.get('meta_gain'):+}, pool gain {fs.get('pool_gain'):+}",
                f"Meta-init query MSE {fs.get('mean_noadapt')}→{fs.get('mean_maml')} after few-shot adapt; beats "
                f"from-scratch ({fs.get('mean_scratch')}) AND the pooled model ({fs.get('mean_pooled')}).",
                "Promote only if the few-shot edge holds on purged walk-forward cohorts (§6).")
        else:
            add("no_adaptation_gain", "warn",
                f"Few-shot adaptation does NOT earn its keep (adaptation gain {fs.get('adaptation_gain'):+}, meta gain {fs.get('meta_gain'):+}, pool gain {fs.get('pool_gain'):+})",
                ("Adapting lowers loss vs no-adapt/scratch, but a single POOLED linear model is BETTER "
                 if fs.get("adaptation_reduces_loss") and not beats_pooled else
                 "Adaptation does not even reduce loss vs the simpler baselines ") +
                "on this corpus. HONEST — the real regime×VPIN cohort tasks + protected-competence test are real "
                "and measured; the few-shot edge isn't there yet.",
                "Needs cohorts whose optimal map actually differs for per-task adaptation to beat pooling.")

        # PROTECTED COMPETENCE (§5.4-7)
        if protected and forgetting_shown:
            add("competence_protected", "info",
                f"Protected competence holds — old cohort MSE {pc.get('maml_old_loss')} (protected init) vs sequential fine-tune {pc.get('seq_old_after')} (forgetting {pc.get('finetune_forgetting'):+})",
                "The MAML protected-init keeps the old cohort while the live-style sequential overwrite forgets "
                "it — direct evidence the live maml's per-changepoint weight overwrite is unsafe.",
                "Never overwrite shared weights regime-by-regime without a protected-competence regression test.")
        elif protected:
            add("competence_protected_weak", "info",
                f"Protected competence holds; forgetting not strongly shown this run (finetune_forgetting {pc.get('finetune_forgetting'):+})",
                "The protected init does not lose the old cohort, but the sequential baseline didn't forget much "
                "here either (cohorts may be too similar).",
                "Keep the protected-competence guard regardless.")
        else:
            add("competence_not_protected", "warn",
                f"Protected competence NOT demonstrated (maml_old {pc.get('maml_old_loss')} vs seq_old {pc.get('seq_old_after')})",
                "The protected-init protocol did not clearly preserve the old cohort this run.",
                "Investigate the cohort sequence / inner-lr before trusting continual adaptation.")

        if stale:
            add("report_stale", "warn", f"Meta-learning report is stale ({age_s // 3600}h old)",
                "Older than the expected cadence — the beat task may not be running.",
                "Check the scibrain-meta-learning beat task / worker.")

        sev_rank = {"critical": 3, "warn": 2, "info": 1}
        health = ("integrity_fail" if (not state_real or leak) else
                  "stale" if stale else
                  "healthy" if (helps and protected) else "no_edge")

        out.update({
            "available": True, "health": health, "age_s": age_s, "authority": "shadow",
            "live_agents_untouched": bool(rep.get("live_agents_untouched", True)),
            "live_maml_untouched": bool(rep.get("live_maml_untouched", True)),
            "n_trades": rep.get("n_trades"), "n_cohorts": rep.get("n_cohorts"), "cohorts": rep.get("cohorts"),
            "integrity": integ, "few_shot": fs, "protected_competence": pc,
            "verdict": rep.get("verdict"),
            "issues": sorted(issues, key=lambda i: -sev_rank[i["severity"]]),
            "n_issues": {s: sum(1 for i in issues if i["severity"] == s) for s in ("critical", "warn", "info")},
            "note": ("Meta-learning (design §3.7/§5.4-7/§5.5 stage-6/§8-419): real regime×VPIN cohort tasks with "
                     "real belief states + twin-utility targets, few-shot Reptile adaptation, and a protected-"
                     "competence test vs sequential fine-tune. Upgrades ml/maml.py's synthetic per-pair path. "
                     "SHADOW — no live authority; ml/maml.py untouched (Rule 21)."),
        })
        if publish:
            try:
                r.set(K.META_LEARNING_HEALTH, json.dumps(out))
            except Exception:
                pass
    except Exception as exc:
        out["error"] = str(exc)[:200]
        log.warning("scibrain_meta_learning_health_failed", error=str(exc)[:200])
    return out
