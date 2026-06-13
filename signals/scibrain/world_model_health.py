"""SciBrain Phase-7f — WORLD-MODEL health (RSSM on rich ledger sequences; design §3.6/§3.9/§9).

Reads the world-model training+imagination report (scibrain:world_model:report, written by
ml/world_model_train) and diagnoses the ONE honest capability question (§3.9): does the world model's
open-loop multi-step IMAGINATION predict realized reward better than a constant baseline?

  • CALIBRATION — normalized model error = imagined-reward MSE / constant-baseline MSE (= 1−R²) in symlog
    space, per horizon (k=1..K). This REPLACES the degenerate sign-accuracy metric (a ~91% win rate makes a
    trivial "always-win" score ~0.91, so sign-accuracy cannot separate the model from it).
  • PLANNING AUTHORITY — planning_weight = clip(1 − normalized_model_error − epistemic, 0, 1). Model error
    CAPS how much a planner may trust imagined futures. If the model does not beat the baseline, authority is
    correctly WITHHELD (planning_weight ≈ 0) — a SAFE, EXPECTED state, not a fault.
  • UPGRADE — confirms the sparse trade-close-reward-only training was replaced by rich cross-trade sequences
    (full multimodal obs + typed action + reward + continue) training the WHOLE RSSM.

Tier-0: pure read of an already-computed report; never raises; no trading authority.
"""
from __future__ import annotations

import json
import time

import structlog

from . import keys as K

log = structlog.get_logger()

_STALE_S = 36 * 3600          # a report older than this is flagged stale (train cadence is ~daily)


def build_world_model_health(r, *, publish: bool = True) -> dict:
    """Diagnose the world-model report into a typed health/issues view. Pure read; never raises."""
    out = {"contract": "WorldModelHealth", "available": False, "ts": round(time.time(), 3)}
    try:
        raw = r.get(K.WORLD_MODEL_REPORT)
        rep = json.loads(raw) if raw else None
        if not rep:
            out.update({"health": "cold", "issues": [],
                        "note": "World model has not trained yet — no report. Run ml.world_model_train "
                                "(or wait for the scibrain-world-model beat task)."})
            if publish:
                try:
                    r.set(K.WORLD_MODEL_HEALTH, json.dumps(out))
                except Exception:
                    pass
            return out

        cal = rep.get("calibration") or {}
        pa = rep.get("planning_authority") or {}
        deg = rep.get("secondary_degenerate") or {}
        unc = rep.get("uncertainty") or {}
        plan = rep.get("planning") or {}
        nme = cal.get("normalized_model_error")
        r2 = cal.get("r2")
        beats = bool(cal.get("beats_baseline"))
        pw = pa.get("planning_weight")
        epistemic = unc.get("epistemic")
        aleatoric = unc.get("aleatoric")
        cov90 = cal.get("reward_coverage_90")
        cov_gap = cal.get("coverage_gap_90")
        safe_h = pa.get("safe_planning_horizon")
        n_members = rep.get("n_members")
        age_s = max(0, int(time.time() - float(rep.get("ts") or 0)))
        stale = age_s > _STALE_S

        issues = []

        def add(code, sev, title, detail, rec):
            issues.append({"code": code, "severity": sev, "title": title, "detail": detail, "recommendation": rec})

        # the upgrade itself — rich sequences (task 1) + ensemble + multi-horizon calibration (task 2)
        add("rich_sequences_upgrade", "info",
            f"Rich-sequence ENSEMBLE live — {n_members}-member RSSM on {rep.get('n_sequences')} windows "
            f"(T={rep.get('T')}) from {rep.get('n_trades')} ordered trades",
            "Sparse trade-close-reward-only training was replaced by full multimodal obs + typed action + "
            "reward + continue SEQUENCES, now with an ensemble for epistemic uncertainty + multi-horizon "
            "reward/latent/continuation calibration.",
            "Next (task 3): bounded sequence planning gated by planning_weight + the safe horizon.")

        # ENSEMBLE EPISTEMIC UNCERTAINTY (§3.9/§3.11) — disagreement drives the horizon-shortening gate
        if epistemic is not None:
            if unc.get("epistemic_gt_threshold"):
                add("epistemic_high", "warn",
                    f"Ensemble disagreement HIGH — epistemic {epistemic} (aleatoric {aleatoric})",
                    "The members disagree on the imagined reward → the model is under-determined here; per "
                    "§3.9 high disagreement shortens the planning horizon and increases baseline fallback.",
                    "Reduce epistemic with richer/longer obs + more data, not more epochs (that won't help).")
            else:
                add("epistemic_decomposed", "info",
                    f"Uncertainty decomposed — epistemic {epistemic} vs aleatoric {aleatoric}",
                    "Epistemic (reducible ensemble disagreement) is separated from aleatoric (irreducible "
                    "trade-PnL noise); planning trusts the model only where epistemic is low.",
                    "Watch epistemic fall as the obs/data improve — that is what earns planning horizon.")

        # MULTI-HORIZON REWARD-INTERVAL CALIBRATION (§8 'multi-horizon … calibration')
        if cov90 is not None:
            if (cov_gap or 0) > 0.15:
                add("reward_intervals_miscalibrated", "warn",
                    f"Reward intervals MISCALIBRATED — 90% interval covers {cov90} (gap {cov_gap} vs 0.90)",
                    "The ensemble+aleatoric predictive interval does not cover the realized reward at the "
                    "nominal rate — over/under-confident; planning on these intervals would be unsafe.",
                    "Re-estimate aleatoric / widen via conformal before granting interval-based authority.")
            else:
                add("reward_intervals_calibrated", "info",
                    f"Reward intervals calibrated — 90% interval covers {cov90} (gap {cov_gap})",
                    "The predictive interval covers the realized reward near the nominal rate across horizons.",
                    "Calibrated intervals are a prerequisite for risk-aware planning.")

        # the honest capability verdict
        if beats:
            add("imagination_beats_baseline", "info",
                f"Imagination BEATS the constant baseline (R²={r2}) — planning_weight {pw}",
                "Open-loop multi-step imagined reward predicts realized reward better than a constant; "
                "modest learned dynamics.",
                "Proceed to bounded sequence planning (task 3) gated by planning_weight.")
        else:
            add("no_predictive_dynamics", "warn",
                f"Imagination does NOT beat the constant baseline (R²={r2}, normalized model error {nme})",
                "Trade-PnL dynamics are not yet learnable from this corpus — the imagined reward is no better "
                "than predicting the mean. This is HONEST and EXPECTED on a small noisy corpus.",
                "Withhold planning authority (done: planning_weight≈0); the levers are ensemble uncertainty "
                "+ richer/longer sequences + costs/CVaR objectives (Phase-7f tasks 2–6), not more epochs.")

        # BOUNDED PLANNING vs the deterministic DIGITAL TWIN (task 3, §3.9/§6)
        nd = plan.get("n_decisions") or 0
        if nd:
            tu = plan.get("twin_utility") or {}
            lift_b = plan.get("planning_lift_vs_baseline")
            mix = plan.get("planner_action_mix") or {}
            collapsed = max(mix.values()) if mix else 0
            if pa.get("planning_helps"):                  # meaningful lift (threshold-gated in the trainer)
                add("planner_beats_baseline", "info",
                    f"Planner BEATS the baseline on the twin — lift {lift_b:+} over {nd} decisions",
                    f"planner utility {tu.get('planner')} vs baseline {tu.get('baseline_const_action')} "
                    f"(realized {tu.get('realized')}, oracle {tu.get('oracle')}).",
                    "Candidate for bounded planning authority — confirm it holds out-of-sample (§6).")
            else:
                add("planning_no_lift", "warn",
                    f"Planner provides NO lift over baseline ({lift_b:+}) → deterministic baseline fallback",
                    f"Over {nd} twin-replayed decisions the planner ({tu.get('planner')}) does not beat the "
                    f"fixed best-constant-action baseline ({tu.get('baseline_const_action')}); oracle "
                    f"agreement {plan.get('oracle_action_agreement')}. Expected with no learned edge (§3.9).",
                    "Fallback to the deterministic baseline (done); earn lift via richer obs + offline RL.")
            # Rule-12 honesty: a non-discriminating world model collapses the planner to a constant action
            if collapsed >= nd:
                dom = max(mix, key=mix.get)
                add("planner_collapsed_constant_action", "warn",
                    f"Planner COLLAPSED to a constant action ({dom} ×{collapsed}/{nd}) — no state-dependent planning",
                    "The world model can't discriminate states, so argmax-over-imagined-reward picks the same "
                    "action every time — which is why it equals the best-constant-action baseline.",
                    "This is the honest symptom of R²≤0; state-dependent planning needs a discriminating model.")

        # planning authority gate (§3.9) — surfaced explicitly so the SAFE withholding is visible
        if not pa.get("granted"):
            add("planning_authority_withheld", "info",
                f"Planning authority WITHHELD — planning_weight {pw}, fallback={pa.get('fallback')}",
                "Per §3.9 authority needs the model to beat baseline AND low disagreement AND positive planning "
                "lift on the digital twin; until then the planner falls back to the deterministic baseline.",
                "Authority is earned automatically once all three conditions hold.")

        # Rule-18 transparency: the degenerate metric we replaced
        if deg.get("k_step_sign_accuracy") is not None:
            add("degenerate_metric_replaced", "info",
                "Sign-accuracy is degenerate here — replaced by normalized model error",
                f"{deg.get('why_degenerate')}. Reported for transparency only "
                f"(sign-acc {deg.get('k_step_sign_accuracy')} vs base {deg.get('mean_baseline_sign_accuracy')}, "
                f"corr {deg.get('reward_correlation')}).",
                "Judge the world model by calibration (1−R²), not sign-accuracy.")

        if stale:
            add("report_stale", "warn",
                f"World-model report is stale ({age_s // 3600}h old)",
                "The training report is older than the expected cadence — the beat task may not be running.",
                "Check the scibrain-world-model beat task / worker load.")

        sev_rank = {"critical": 3, "warn": 2, "info": 1}
        worst = max((sev_rank[i["severity"]] for i in issues), default=0)
        # NOTE: 'no_authority' is a SAFE/expected state (model honestly can't plan yet) — it is NOT a fault,
        # so brain_view keeps it in the healthy set. 'stale' (the pipeline stopped) is the real watch state.
        health = "stale" if stale else ("healthy" if beats else "no_authority")

        out.update({
            "available": True, "health": health, "age_s": age_s,
            "n_trades": rep.get("n_trades"), "n_sequences": rep.get("n_sequences"), "n_members": n_members,
            "T": rep.get("T"), "K_imagined": rep.get("K_imagined"), "n_params": rep.get("n_params"),
            "calibration": {"normalized_model_error": nme, "r2": r2, "beats_baseline": beats,
                            "reward_coverage_90": cov90, "coverage_gap_90": cov_gap,
                            "per_horizon": cal.get("per_horizon") or []},
            "uncertainty": {"epistemic": epistemic, "aleatoric": aleatoric,
                            "epistemic_gt_threshold": unc.get("epistemic_gt_threshold")},
            "planning_authority": {"planning_weight": pw, "granted": bool(pa.get("granted")),
                                   "epistemic_uncertainty": pa.get("epistemic_uncertainty"),
                                   "safe_planning_horizon": safe_h, "fallback": pa.get("fallback"),
                                   "planning_helps": bool(pa.get("planning_helps")), "formula": pa.get("formula")},
            "planning": {"n_decisions": plan.get("n_decisions"), "twin_utility": plan.get("twin_utility"),
                         "planning_lift_vs_baseline": plan.get("planning_lift_vs_baseline"),
                         "planning_lift_vs_realized": plan.get("planning_lift_vs_realized"),
                         "oracle_action_agreement": plan.get("oracle_action_agreement"),
                         "planner_action_mix": plan.get("planner_action_mix"),
                         "baseline_action": plan.get("baseline_action")},
            "secondary_degenerate": deg,
            "verdict": rep.get("verdict"),
            "issues": sorted(issues, key=lambda i: -sev_rank[i["severity"]]),
            "n_issues": {s: sum(1 for i in issues if i["severity"] == s) for s in ("critical", "warn", "info")},
            "note": ("World-model health (design §3.6/§3.9): the RSSM trained on RICH cross-trade sequences "
                     "from the immutable ledger (not sparse trade-only state), judged by an honest open-loop "
                     "multi-step CALIBRATION metric (normalized model error = 1−R²), with planning authority "
                     "gated by that error (planning_weight). When the model can't beat a constant, authority "
                     "is correctly withheld — a safe, honest default. Pure read; no trading authority."),
        })
        if publish:
            try:
                r.set(K.WORLD_MODEL_HEALTH, json.dumps(out))
            except Exception:
                pass
    except Exception as exc:
        out["error"] = str(exc)[:200]
        log.warning("scibrain_world_model_health_failed", error=str(exc)[:200])
    return out
