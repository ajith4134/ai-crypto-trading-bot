"""SciBrain Phase-7f — RISK-SENSITIVE POLICY health (distributional/CVaR + costs + safety projection;
§3.7/§3.10/§8-416).

Reads the shadow risk-sensitive policy report (scibrain:risk_policy:report, written by
ml/risk_sensitive_policy) and diagnoses the design's acceptance — "higher net utility AND lower tail risk":

  • TAIL vs MEAN TRADEOFF — does the CVaR objective improve the tail (CVaR_0.1 / worst / drawdown) over the
    mean-optimal policy, and at what cost to the mean? (The honest risk-sensitivity question.)
  • SAFETY PROJECTION — the CBF reduce-only veto rate (it can only make an action safer, never larger, §3.10).
  • COST AWARENESS — turnover under the cost-penalized policy vs the always-trade realized policy.

Tier-0 pure read; never raises. SHADOW — no live authority (Rule 21).
"""
from __future__ import annotations

import json
import time

import structlog

from . import keys as K

log = structlog.get_logger()

_STALE_S = 36 * 3600


def build_risk_policy_health(r, *, publish: bool = True) -> dict:
    """Diagnose the risk-sensitive policy report into a typed health/issues view. Pure read; never raises."""
    out = {"contract": "RiskPolicyHealth", "available": False, "ts": round(time.time(), 3)}
    try:
        raw = r.get(K.RISK_POLICY_REPORT)
        rep = json.loads(raw) if raw else None
        if not rep:
            out.update({"health": "cold", "issues": [],
                        "note": "Risk-sensitive policy has not run yet — no report. Run "
                                "ml.risk_sensitive_policy (or wait for the beat task)."})
            if publish:
                try:
                    r.set(K.RISK_POLICY_HEALTH, json.dumps(out))
                except Exception:
                    pass
            return out

        ev = rep.get("eval") or {}
        td = rep.get("tradeoff") or {}
        obj = rep.get("objective") or {}
        helps = bool(td.get("risk_sensitive_helps"))
        tail_gain = td.get("tail_gain_cvar10_vs_mean")
        mean_cost = td.get("mean_cost_vs_mean")
        dd_red = td.get("drawdown_reduction_safety")
        proj_rate = td.get("safety_projection_rate")
        age_s = max(0, int(time.time() - float(rep.get("ts") or 0)))
        stale = age_s > _STALE_S

        issues = []

        def add(code, sev, title, detail, rec):
            issues.append({"code": code, "severity": sev, "title": title, "detail": detail, "recommendation": rec})

        # the build itself (§3.7/§3.10 deliverable)
        add("risk_layer_live", "info",
            f"Risk-sensitive layer live — distributional/CVaR (α_fit {obj.get('cvar_alpha_fit')}) + turnover "
            f"cost (λ {obj.get('lambda_turnover')}) + CBF safety projection (cap {obj.get('safety_cap_cvar10')})",
            "Per-action twin-utility distributions → CVaR objective, explicit churn cost, and a reduce-only "
            "safety projection onto the safe set, with support-aware fallback.",
            "Owner-gated promotion only if it lowers tail risk without losing net utility out-of-sample (§6).")

        # TAIL vs MEAN tradeoff (the headline)
        if helps:
            add("tail_improved", "info",
                f"CVaR lowers TAIL risk — tail gain {tail_gain:+} (CVaR_0.1), drawdown reduction {dd_red:+}",
                f"for a mean cost {mean_cost:+}: the risk-sensitive policy trades a little average return for a "
                "better worst case — the intended distributional behavior.",
                "Promote only if the tail improvement holds on purged walk-forward (§6).")
        else:
            add("no_tail_gain", "warn",
                f"CVaR does NOT improve the tail here (tail gain {tail_gain:+}, drawdown reduction {dd_red:+})",
                "The mean and CVaR policies pick alike on this corpus, so risk-sensitivity changes little. "
                "HONEST — the distributional/CVaR + safety machinery is real, the tail edge isn't there yet.",
                "Needs a corpus/state where actions actually differ in tail risk to bite.")

        # SAFETY PROJECTION (§3.10 reduce-only)
        sp = (ev.get("cvar_safety_policy") or {}).get("safety_projections")
        add("safety_projection", "info",
            f"CBF safety projection active — {sp} vetoes ({proj_rate} rate), reduce-only",
            "After the policy picks, a risky-tail entry (CVaR_0.1 below the cap) is projected to abstain; the "
            "projection can only make an action safer, never enlarge it (§3.10).",
            "Keep the projection as the last-line tail guard regardless of the learned policy.")

        if stale:
            add("report_stale", "warn", f"Risk-policy report is stale ({age_s // 3600}h old)",
                "Older than the expected cadence — the beat task may not be running.",
                "Check the scibrain-risk-policy beat task / worker.")

        sev_rank = {"critical": 3, "warn": 2, "info": 1}
        health = "stale" if stale else ("healthy" if helps else "no_tail_gain")

        out.update({
            "available": True, "health": health, "age_s": age_s, "authority": "shadow",
            "live_agents_untouched": bool(rep.get("live_agents_untouched", True)),
            "n_trades": rep.get("n_trades"), "n_test": rep.get("n_test"),
            "objective": obj, "eval": ev, "tradeoff": td,
            "verdict": rep.get("verdict"),
            "issues": sorted(issues, key=lambda i: -sev_rank[i["severity"]]),
            "n_issues": {s: sum(1 for i in issues if i["severity"] == s) for s in ("critical", "warn", "info")},
            "note": ("Risk-sensitive policy (design §3.7/§3.10/§8-416): distributional CVaR objective + explicit "
                     "turnover cost + CBF safety projection (reduce-only) + support-aware fallback, twin-"
                     "evaluated on the mean-vs-tail tradeoff. SHADOW — no live authority (Rule 21)."),
        })
        if publish:
            try:
                r.set(K.RISK_POLICY_HEALTH, json.dumps(out))
            except Exception:
                pass
    except Exception as exc:
        out["error"] = str(exc)[:200]
        log.warning("scibrain_risk_policy_health_failed", error=str(exc)[:200])
    return out
