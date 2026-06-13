"""SciBrain Phase-7e — TRAINING HEALTH diagnostics (design §8 evaluation constitution).

A raw AUC number is NOT useful on its own — a 0.49 AUC on 20 test negatives means something very
different from a 0.49 on 5000. This module turns the shared-latent SSL train+eval report into an honest,
auto-diagnosed health view that POINTS OUT the issues that determine whether a result is trustworthy:

  • class imbalance + how many MINORITY test examples actually back the AUC;
  • the AUC 95% confidence interval (Hanley–McNeil) — does it even exclude chance (0.5)?  (statistical power)
  • whether the latent adds value over the raw baseline (the promotion criterion);
  • whether there is ANY out-of-sample signal at all (raw baseline ≈ chance ⇒ a data/feature issue, not a
    model bug);
  • a leakage read on the permutation control (contextualised by the wide CI when negatives are few);
  • corpus size adequacy for SSL, and report staleness.

Each issue is typed {code, severity, title, detail, recommendation}. severity ∈ info|warn|critical. The
overall `health` is the operator's one-word read: trustworthy | underpowered | issues | no_run. Pure read.
"""
from __future__ import annotations

import json
import math
import time

import structlog

from . import keys as K

log = structlog.get_logger()


def _hanley_mcneil_se(auc: float, n_pos: int, n_neg: int) -> float:
    """Standard error of an ROC-AUC (Hanley & McNeil 1982). Drives the CI / statistical-power read."""
    if n_pos < 1 or n_neg < 1:
        return float("nan")
    a = max(0.0, min(1.0, auc))
    q1 = a / (2 - a) if (2 - a) else 0.0
    q2 = 2 * a * a / (1 + a) if (1 + a) else 0.0
    var = (a * (1 - a) + (n_pos - 1) * (q1 - a * a) + (n_neg - 1) * (q2 - a * a)) / (n_pos * n_neg)
    return math.sqrt(max(0.0, var))


def build_training_health(r, *, publish: bool = True) -> dict:
    """Diagnose the latest shared-latent training report into typed issues + a health verdict. Pure read."""
    out = {"contract": "TrainingHealth", "available": False, "ts": round(time.time(), 3)}
    try:
        raw = r.get(K.PERCEPTION_REPORT)
        rep = json.loads(raw) if raw else None
        if not rep:
            out.update({"health": "no_run", "issues": [],
                        "note": "No shared-latent training run yet (ml/train_shared_latent)."})
            if publish:
                try:
                    r.set(K.TRAINING_HEALTH, json.dumps(out))
                except Exception:
                    pass
            return out

        auc = rep.get("downstream_auc") or {}
        a_lat = float(auc.get("latent", 0.5) or 0.5)
        a_base = float(auc.get("raw_baseline", 0.5) or 0.5)
        a_perm = float(auc.get("permutation_control", 0.5) or 0.5)
        n = int(rep.get("n", 0))
        split = rep.get("split") or {}
        test_n = int(split.get("test", 0))
        base_rate = float(rep.get("base_rate_win", 0.5) or 0.5)
        minority = min(base_rate, 1.0 - base_rate)
        n_test_pos = int(round(test_n * base_rate))
        n_test_neg = max(0, test_n - n_test_pos)
        # AUC for the minority-as-positive is symmetric; CI uses the two class counts on the test split.
        se = _hanley_mcneil_se(a_lat, max(n_test_pos, 1), max(n_test_neg, 1))
        ci = [round(a_lat - 1.96 * se, 4), round(a_lat + 1.96 * se, 4)] if se == se else [None, None]
        delta = round(a_lat - a_base, 4)

        issues: list[dict] = []

        def add(code, sev, title, detail, rec):
            issues.append({"code": code, "severity": sev, "title": title, "detail": detail, "recommendation": rec})

        # 1) class imbalance
        if minority < 0.15:
            add("class_imbalance", "critical" if minority < 0.10 else "warn",
                f"Severe class imbalance — minority class is {minority * 100:.1f}%",
                f"Win rate {base_rate:.3f}; the test split holds only ~{n_test_neg} minority (loss) examples.",
                "Use balanced sampling / collect more loss examples; report AUC + PR-AUC, never raw accuracy.")
        # 2) too few minority test examples
        if n_test_neg < 30:
            add("low_test_minority", "warn",
                f"Only ~{n_test_neg} minority test examples back the AUC",
                "AUC estimated on very few negatives is high-variance — a noisy point estimate.",
                "Grow the corpus or use time-blocked CV to average AUC over folds.")
        # 3) underpowered — CI includes chance
        if ci[0] is not None and ci[0] <= 0.5 <= ci[1]:
            add("auc_ci_includes_chance", "warn",
                f"AUC 95% CI [{ci[0]}, {ci[1]}] includes 0.5 — underpowered",
                f"latent AUC {a_lat} is statistically indistinguishable from chance at this sample size.",
                "This is a POWER problem, not necessarily a bad model — need more (balanced) test data.")
        # 4) representation adds no value
        if a_lat <= a_base:
            add("no_representation_gain", "info",
                f"Latent does not beat the raw baseline (Δ={delta})",
                f"latent {a_lat} ≤ raw baseline {a_base}; the SSL representation adds no measurable value.",
                "Correctly NOT promoted (design §3.2). Revisit objectives/features once more data exists.")
        # 5) no out-of-sample signal at all
        if abs(a_lat - 0.5) < 0.05 and abs(a_base - 0.5) < 0.05:
            add("no_oos_signal", "warn",
                "No out-of-sample signal — raw features ALSO ≈ chance",
                "Neither the raw module snapshot nor the latent predicts realized win/loss out-of-sample.",
                "A DATA/FEATURE issue, not a model bug: the entry-time snapshot may not encode the outcome; "
                "try richer features (lifetrace/path), a different label (forward return, not just win/loss), "
                "or the live cross-universe corpus.")
        # 6) leakage read on the permutation control
        perm_off = abs(a_perm - 0.5)
        if perm_off > 0.15 and n_test_neg >= 30:
            add("possible_leakage", "critical",
                f"Permutation control {a_perm} is far from 0.5 — possible leakage",
                "Shuffled labels should give AUC≈0.5; a large deviation suggests information leaked into the split.",
                "Audit the train/test purge + standardization; ensure no future info in features.")
        else:
            add("leakage_ok", "info",
                f"No leakage evidence (permutation control {a_perm})",
                f"Within the wide CI expected from ~{n_test_neg} negatives; deviation {perm_off:.3f} is noise.",
                "Keep the embargo + TRAIN-only standardization on future runs.")
        # 7) small corpus for SSL
        if n < 1000:
            add("small_corpus", "info",
                f"Small corpus for self-supervised learning (N={n})",
                "SSL representations usually need 10³–10⁴+ samples to learn transferable structure.",
                "Accumulate more closed-trade snapshots, or pretrain on the unlabeled live universe snapshot.")
        # 8) staleness
        age_h = (time.time() - float(rep.get("ts", 0) or 0)) / 3600.0
        if age_h > 24 * 7:
            add("stale_report", "warn", f"Training report is {age_h / 24:.1f} days old",
                "The representation has not been re-evaluated on recent data.", "Re-run ml/train_shared_latent.")

        sev_rank = {"critical": 3, "warn": 2, "info": 1}
        worst = max((sev_rank[i["severity"]] for i in issues), default=0)
        underpowered = any(i["code"] in ("auc_ci_includes_chance", "low_test_minority") for i in issues)
        leak = any(i["code"] == "possible_leakage" for i in issues)
        health = ("leakage" if leak else "underpowered" if underpowered
                  else "issues" if worst >= 2 else "trustworthy")

        out.update({
            "available": True, "health": health,
            "promoted": bool(rep.get("utility_positive")),
            "verdict": rep.get("verdict"),
            "auc": {"latent": a_lat, "baseline": a_base, "permutation": a_perm, "delta_vs_baseline": delta,
                    "latent_ci95": ci, "latent_se": round(se, 4) if se == se else None},
            "corpus": {"n": n, "train": split.get("train"), "embargo": split.get("embargo"),
                       "test": test_n, "base_rate_win": round(base_rate, 4),
                       "n_test_pos": n_test_pos, "n_test_neg": n_test_neg,
                       "minority_frac": round(minority, 4), "epochs": rep.get("epochs"),
                       "n_params": rep.get("n_params"), "report_age_hours": round(age_h, 2)},
            "issues": sorted(issues, key=lambda i: -sev_rank[i["severity"]]),
            "n_issues": {s: sum(1 for i in issues if i["severity"] == s) for s in ("critical", "warn", "info")},
            "note": ("Auto-diagnosed training health (design §8): the AUC is contextualised by class balance, "
                     "the number of minority test examples, and its Hanley–McNeil confidence interval, so an "
                     "operator can tell an UNDERPOWERED/DATA-LIMITED result apart from a genuinely bad model "
                     "or a leak. Pure read; the representation is a shadow with no authority."),
        })
        if publish:
            try:
                r.set(K.TRAINING_HEALTH, json.dumps(out))
            except Exception:
                pass
    except Exception as exc:
        out["error"] = str(exc)[:200]
        log.warning("scibrain_training_health_failed", error=str(exc)[:200])
    return out
