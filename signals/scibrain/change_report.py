"""SciBrain Phase-7c — the per-change EVIDENCE REPORT (design §9 evaluation requirements).

Consolidates everything the matched-cohort evaluator + rigor layer + baseline-bootstrap already computed
for ONE ChangeSpec into a single normalized report along the four mandated dimensions, with an explicit
decision basis that rests on EVIDENCE, not an arbitrary clock-cycle count (design §9: "minimum effective
sample size, not merely 50 clock cycles"):

  1. effective_sample    — propensity-weighted Kish ESS + direction-changing support vs the floor (NOT a
                           fixed cycle count).
  2. conditional_utility — the change's incremental utility CONDITIONAL on its context: the off-policy
                           IPS/DR mean, the complexity-adjusted LCB, and the per-sub-context law
                           ("module X adds value in context C"), incl. the SPIBB supported contexts.
  3. ablation            — marginal contribution from removing the candidate (→0) + the direction-vs-sizing
                           decomposition (does the change flip decisions or just rescale size?).
  4. risk                — tail of the Δutility distribution (CVaR / worst), walk-forward stability, the
                           multiple-testing + negative-control guards, and the spec's own falsifier; flags
                           whether the change WORSENS the tail (the §9 falsifier "CVaR worsens").

Pure compute, never raises, Tier-0. Attached to the evaluation so it flows to the registry + dashboard.
"""
from __future__ import annotations

from typing import Any, Optional

REPORT_VERSION = 1
_MIN_EFFECTIVE = 8        # mirrors rigor._MIN_EFFECTIVE — the evidence floor (effective sample, not cycles)
_TAIL_WORSEN_EPS = 0.02   # CVaR of Δutility below this (negative) = the change has an adverse tail


def _g(d: Optional[dict], *path, default=None):
    cur: Any = d
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def build_change_report(ev: dict, spec: Optional[dict] = None) -> dict:
    """Normalize an evaluate_changespec() result `ev` (+ its ChangeSpec) into the four-dimension evidence
    report + decision basis. Works whether or not the rigor layer ran (sparse changes have no rigor block);
    falls back to the cohort/baseline-bootstrap evidence. Never raises."""
    spec = spec or {}
    rep: dict = {"report_version": REPORT_VERSION, "ts": _g(ev, "ts")}
    try:
        evaluation = ev.get("evaluation") or {}
        rigor = ev.get("rigor") or {}
        bb = ev.get("baseline_bootstrap") or {}
        scoring = rigor.get("scoring") or {}
        offp = rigor.get("off_policy") or {}
        wf = rigor.get("walk_forward") or {}
        verdict = ev.get("verdict")
        module, regime = ev.get("module"), ev.get("regime")
        context = spec.get("context_predicate") or (f"router.regime == {regime}" if regime else None)

        rep["change"] = {
            "target": ev.get("target"), "module": module, "regime": regime,
            "context_predicate": context,
            "champion_base": ev.get("old_base"), "challenger_candidate": ev.get("candidate"),
            "kind": _g(spec, "intervention", "kind"),
            "complexity_cost": spec.get("complexity_cost"),
        }

        # 1 ── EFFECTIVE SAMPLE (evidence, not cycle count) ─────────────────────────────────────────
        ess = offp.get("effective_sample")
        support = evaluation.get("support_changed")
        rep["effective_sample"] = {
            "n_cohort": evaluation.get("n_cohort"),
            "n_modeled": evaluation.get("n_modeled"),
            "n_direction_changed": support,
            "effective_sample_size": ess,          # Kish ESS over the direction-changing trades
            "min_required": _MIN_EFFECTIVE,
            "replay_fidelity": evaluation.get("replay_fidelity"),
            "sufficient": bool((ess is not None and ess >= _MIN_EFFECTIVE)
                               or (bb.get("n_supported_total") or 0) >= 6),
            "basis": "evidence-based effective sample (Kish ESS over twin-graded direction changes), "
                     "NOT a fixed clock-cycle count",
        }

        # 2 ── CONDITIONAL UTILITY (incremental utility given context) ──────────────────────────────
        per_ctx = bb.get("per_context") or []
        supported = bb.get("applicable_contexts") or []
        rep["conditional_utility"] = {
            "context": context,
            "mean_delta_utility": evaluation.get("mean_delta_utility"),
            "lcb_delta_utility": evaluation.get("lcb_delta_utility"),
            "complexity_adjusted_lcb": rigor.get("complexity_adjusted_lcb"),
            "off_policy_ips_mean": offp.get("ips_mean"),
            "off_policy_dr_mean": offp.get("dr_mean"),
            "per_sub_context": per_ctx,                 # "module adds value in context C" — per bucket
            "supported_contexts": supported,
            "restricted": bool(ev.get("applicable_contexts")),
            "conditional_law": _conditional_law(module, regime, ev.get("candidate"),
                                                evaluation.get("mean_delta_utility"), supported,
                                                bool(ev.get("applicable_contexts"))),
        }

        # 3 ── ABLATION (marginal contribution + direction-vs-sizing) ───────────────────────────────
        abl = ev.get("ablation_remove") or {}
        rep["ablation"] = {
            "remove_candidate_to_zero": {
                "mean_delta_utility": abl.get("mean_delta_utility"),
                "lcb_delta_utility": abl.get("lcb_delta_utility"),
                "support_changed": abl.get("support_changed"),
            },
            "direction_vs_sizing": {
                "n_conviction_shifted": evaluation.get("n_conviction_shifted"),
                "mean_conviction_delta": evaluation.get("mean_conviction_delta"),
                "mean_abs_size_delta": evaluation.get("mean_abs_size_delta"),
            },
            "interpretation": ("the change flips decision DIRECTION (twin-gradable)"
                               if (support or 0) > 0 else
                               "the change only rescales SIZE/conviction (no direction flips) — "
                               "its utility effect is the separate incremental-utility task"),
        }

        # 4 ── RISK (tail / stability / controls / falsifier) ───────────────────────────────────────
        cvar = scoring.get("cvar5")
        tail_worsens = (cvar is not None and cvar < -_TAIL_WORSEN_EPS)
        # for sparse changes with no rigor scoring, use the worst supported-context LCB as a tail proxy
        ctx_lcbs = [c.get("lcb") for c in per_ctx if c.get("supported") and c.get("lcb") is not None]
        worst_ctx_lcb = (min(ctx_lcbs) if ctx_lcbs else None)
        rep["risk"] = {
            "cvar5_delta_utility": cvar,               # mean of the worst-5% Δutilities (tail loss)
            "worst_delta": scoring.get("worst"),
            "std_delta": scoring.get("std"),
            "worst_supported_context_lcb": worst_ctx_lcb,
            "walk_forward_stability": wf.get("stability"),
            "fdr_pass": _g(rigor, "fdr_guard", "passes_fdr"),
            "negative_control_pass": _g(rigor, "negative_control", "passes"),
            "tail_worsens": tail_worsens,
            "falsifier": spec.get("falsifier", "LCB(Δutility) <= 0 or CVaR worsens"),
            "risk_ok": bool(not tail_worsens and (worst_ctx_lcb is None or worst_ctx_lcb >= 0)),
        }

        # ── DECISION BASIS — evidence, not cycle count ──────────────────────────────────────────────
        rep["decision_basis"] = {
            "verdict": verdict, "reason": ev.get("reason"),
            "no_arbitrary_cycle_count": True,
            "gates": {
                "effective_sample": rep["effective_sample"]["sufficient"],
                "positive_complexity_adjusted_lcb": (rigor.get("complexity_adjusted_lcb") is not None
                                                     and rigor["complexity_adjusted_lcb"] > 0),
                "walk_forward_stable": (wf.get("stability") is None or wf["stability"] >= 0.67),
                "survives_multiple_testing": bool(_g(rigor, "fdr_guard", "passes_fdr", default=True)),
                "negative_control": bool(_g(rigor, "negative_control", "passes", default=True)),
                "tail_not_worsened": not tail_worsens,
            },
            "promotion_rule": ("Rule-14 Evidence/Authority/Influence Gate: promotion scales with risk and "
                               "requires effective sample + positive complexity-adjusted off-policy LCB + "
                               "walk-forward stability + multiple-testing survival + bounded tail — there is "
                               "NO fixed cycle-count gate. A sparse-support challenger may pass only as a "
                               "context-restricted (baseline-bootstrapped) policy that falls back to the "
                               "champion outside its support."),
        }
    except Exception as exc:  # pure read — never break the evaluator
        rep["error"] = str(exc)[:200]
    return rep


def _conditional_law(module, regime, candidate, mean_delta, supported, restricted) -> Optional[str]:
    """A human-readable conditional law: 'challenger C for module M in regime R adds ΔU …'."""
    if module is None:
        return None
    try:
        md = f"{float(mean_delta):+.4f}" if mean_delta is not None else "n/a"
    except (TypeError, ValueError):
        md = "n/a"
    base = f"gain base {candidate} for '{module}' in regime '{regime}' → mean ΔU {md}"
    if restricted and supported:
        return base + f"; acts ONLY in supported sub-contexts {supported}, champion fallback elsewhere"
    if supported:
        return base + f"; locally supported in {supported}"
    return base
