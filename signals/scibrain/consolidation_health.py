"""SciBrain Phase-7e — CONSOLIDATION HEALTH diagnostics (design §3.6 protected competence).

Turns the EWC slow-consolidation report into an honest health view that POINTS OUT the catastrophic-
forgetting issues: did EWC actually reduce forgetting vs the no-EWC ablation, did the protected old-
competence regression test ACCEPT or REJECT the update, and is the gate doing its job. Pure read.
"""
from __future__ import annotations

import json
import time

import structlog

from . import keys as K

log = structlog.get_logger()


def build_consolidation_health(r, *, publish: bool = True) -> dict:
    out = {"contract": "ConsolidationHealth", "available": False, "ts": round(time.time(), 3)}
    try:
        raw = r.get(K.CONSOLIDATION_REPORT)
        rep = json.loads(raw) if raw else None
        if not rep:
            out.update({"health": "no_run", "issues": [],
                        "note": "No slow-consolidation run yet (ml/consolidation)."})
            if publish:
                r.set(K.CONSOLIDATION_HEALTH, json.dumps(out))
            return out

        forg = rep.get("forgetting") or {}
        f_ewc = float(forg.get("with_ewc", 1.0)); f_no = float(forg.get("without_ewc", 1.0))
        thr = float(forg.get("threshold", 0.20))
        accepted = bool(rep.get("accepted"))
        helps = bool(rep.get("ewc_reduces_forgetting"))
        oc = rep.get("old_competence") or {}

        issues = []

        def add(code, sev, title, detail, rec):
            issues.append({"code": code, "severity": sev, "title": title, "detail": detail, "recommendation": rec})

        if not accepted:
            add("forgetting_rejected", "critical",
                f"Consolidation REJECTED — forgetting {f_ewc*100:.1f}% > threshold {thr*100:.0f}%",
                "Even with EWC the update degraded old competence past the gate; θ_old is kept (protected).",
                "Raise λ_ewc, add replay of old episodes, or use adapters/expert-expansion instead of in-place updates.")
        else:
            add("consolidation_accepted", "info",
                f"Consolidation ACCEPTED — forgetting {f_ewc*100:.1f}% < threshold {thr*100:.0f}%",
                "EWC kept old competence within the protected-competence gate.",
                "Anneal λ_ewc down as competence stabilises.")
        if helps:
            add("ewc_effective", "info",
                f"EWC reduces forgetting — {f_ewc*100:.1f}% with vs {f_no*100:.1f}% without",
                "Elastic-weight consolidation protects the params important to the old task.",
                "Keep EWC on consolidation; the ablation confirms its value.")
        else:
            add("ewc_ineffective", "warn",
                f"EWC NOT reducing forgetting — {f_ewc*100:.1f}% with vs {f_no*100:.1f}% without",
                "The Fisher penalty isn't protecting the right params (λ too low or Fisher mis-estimated).",
                "Raise λ_ewc / increase Fisher sample size; verify the OLD-task gradient is informative.")
        if f_no <= thr:
            add("ablation_did_not_forget", "info",
                f"No-EWC ablation also under threshold ({f_no*100:.1f}%) — weak forgetting pressure",
                "The new window is similar enough that even naive fine-tuning doesn't catastrophically forget.",
                "The EWC benefit will matter more as the new distribution diverges from the old.")
        age_h = (time.time() - float(rep.get("ts", 0) or 0)) / 3600.0
        if age_h > 24 * 7:
            add("stale_report", "warn", f"Consolidation report {age_h/24:.1f} days old",
                "Slow consolidation has not re-run on recent episodes.", "Re-run ml/consolidation.")

        sev_rank = {"critical": 3, "warn": 2, "info": 1}
        worst = max((sev_rank[i["severity"]] for i in issues), default=0)
        health = ("forgetting" if not accepted else "ewc_weak" if not helps
                  else "issues" if worst >= 2 else "healthy")

        out.update({
            "available": True, "health": health, "accepted": accepted,
            "ewc_reduces_forgetting": helps, "verdict": rep.get("verdict"),
            "forgetting": {"with_ewc": round(f_ewc, 4), "without_ewc": round(f_no, 4), "threshold": thr,
                           "reduction": round(f_no - f_ewc, 4)},
            "old_competence": oc, "new_task_loss": rep.get("new_task_loss"),
            "lambda_ewc": rep.get("lambda_ewc"), "split": rep.get("split"),
            "report_age_hours": round(age_h, 2),
            "issues": sorted(issues, key=lambda i: -sev_rank[i["severity"]]),
            "n_issues": {s: sum(1 for i in issues if i["severity"] == s) for s in ("critical", "warn", "info")},
            "note": ("Slow semantic consolidation health (design §3.6): EWC protects the params important to "
                     "the OLD task while learning the NEW window; the protected-competence regression test "
                     "ACCEPTS only if forgetting stays under threshold (else keeps θ_old). Health flags "
                     "whether EWC beats the no-EWC ablation and whether the gate accepted. Pure read."),
        })
        if publish:
            try:
                r.set(K.CONSOLIDATION_HEALTH, json.dumps(out))
            except Exception:
                pass
    except Exception as exc:
        out["error"] = str(exc)[:200]
        log.warning("scibrain_consolidation_health_failed", error=str(exc)[:200])
    return out
