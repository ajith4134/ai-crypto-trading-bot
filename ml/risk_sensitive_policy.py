"""SciBrain Phase-7f — DISTRIBUTIONAL / CVaR objective + realistic costs + SAFETY PROJECTION, twin-evaluated
in shadow (design §3.7 reward / §3.10 tail reflex / §8-416 / step 11).

Builds on the offline-RL challengers (task 5): instead of optimizing the MEAN return, model the full return
DISTRIBUTION per (state, action) and optimize a risk-sensitive CVaR objective, with explicit trading costs and
a Control-Barrier-Function-style SAFETY PROJECTION that can only veto/reduce a risky action (never enlarge it).

  • DISTRIBUTIONAL — per state-bucket × action, the full distribution of deterministic digital-twin utilities
    (full counterfactual feedback for entry options), summarized by mean, std, CVaR, and worst-case.
  • CVaR OBJECTIVE — pick the action maximizing CVaR_α (the mean of the worst α-tail) rather than the mean →
    risk-averse. Compared head-to-head with the mean-optimal policy on the TAIL, not just the average.
  • REALISTIC COSTS — the twin utility is already net of fees/slippage/funding (path-aware sim); on top of that
    an explicit λ_turnover penalty charges churn, so abstaining is preferred when the edge can't pay the cost
    (§3.7 r = Δutility − λ_tail·tail − λ_dd·dd − λ_turnover·turnover − …).
  • SAFETY PROJECTION (§3.10 / CBF) — after the policy picks an action, project it onto the SAFE SET: if the
    action's tail (CVaR_0.1) is worse than a cap, veto to abstain. One-directional — it can only REDUCE risk,
    never invent a larger position. Plus a support-aware baseline fallback on sparse buckets.
  • SHADOW EVAL — on the held-out twin: mean / CVaR / worst-case / max-drawdown / turnover for mean-policy vs
    CVaR-policy vs CVaR+safety vs baseline vs realized → the honest mean-vs-tail tradeoff.

SHADOW / read-only: no live authority; the live agents/funnel are untouched (Rule 21). Promotion owner-gated.
Run: python -m ml.risk_sensitive_policy
"""
from __future__ import annotations

import json
import time
from collections import defaultdict

import numpy as np

from ml.hierarchical_controllers import _load_controller_corpus, _ofi_bucket

ENTRY = {"abstain": 2, "enter_long": 0, "enter_short": 1}   # index into twin u=(u_long,u_short,u_abstain)
ALPHA_FIT = 0.25       # CVaR tail fraction used in the objective
ALPHA_TAIL = 0.10      # tighter tail for reporting + the safety cap
LAMBDA_TURN = 0.004    # explicit per-trade turnover/cost penalty (on top of the twin's net-of-fees utility)
SAFETY_CAP = -0.20     # CBF safe set: veto an entry whose CVaR_0.1 tail is worse than this
TAU_SUPPORT = 8


def _bucket(b, sent_med, vpin_med):
    return (b["regime"], _ofi_bucket(b["ofi"]),
            "hi" if b["vpin"] >= vpin_med else "lo",
            "hi" if b["sentiment"] >= sent_med else "lo")


def _cvar(xs, alpha):
    """CVaR_alpha = mean of the worst alpha-fraction (lower tail) of xs."""
    a = np.sort(np.asarray(xs, dtype=np.float64))
    if a.size == 0:
        return 0.0
    k = max(1, int(np.ceil(alpha * a.size)))
    return float(a[:k].mean())


def _maxdd(util_seq):
    """Max drawdown of the equity curve formed by the chronological per-trade utilities."""
    eq = np.cumsum(util_seq)
    peak = np.maximum.accumulate(eq)
    return float(np.max(peak - eq)) if eq.size else 0.0


def main() -> int:
    t0 = time.time()
    rows = _load_controller_corpus()
    if len(rows) < 150:
        print(f"corpus too small ({len(rows)})"); return 1
    n = len(rows); i_tr = int(0.70 * n)
    train, test = rows[:i_tr], rows[i_tr:]
    sent_med = float(np.median([r["belief"]["sentiment"] for r in train]))
    vpin_med = float(np.median([r["belief"]["vpin"] for r in train]))

    # ── per-bucket × action return DISTRIBUTION on TRAIN (full-feedback twin utilities) ──
    dist = defaultdict(lambda: {"enter_long": [], "enter_short": [], "abstain": []})
    for r in train:
        b = _bucket(r["belief"], sent_med, vpin_med)
        uL, uS, uA = r["u"]
        dist[b]["enter_long"].append(uL); dist[b]["enter_short"].append(uS); dist[b]["abstain"].append(uA)
    stats = {}                                          # bucket -> action -> {mean, cvar_fit, cvar_tail, n}
    for b, d in dist.items():
        stats[b] = {a: {"mean": float(np.mean(v)), "cvar_fit": _cvar(v, ALPHA_FIT),
                        "cvar_tail": _cvar(v, ALPHA_TAIL), "n": len(v)} for a, v in d.items() if v}
    cov = {b: stats[b]["enter_long"]["n"] for b in stats}
    g_long = float(np.mean([r["u"][0] for r in train])); g_short = float(np.mean([r["u"][1] for r in train]))
    base_global = "enter_long" if g_long >= max(g_short, 0.0) else ("enter_short" if g_short >= 0.0 else "abstain")

    def turn(a):
        return 0.0 if a == "abstain" else LAMBDA_TURN

    def mean_policy(b):
        if b not in stats or cov[b] < TAU_SUPPORT:
            return base_global
        return max(stats[b], key=lambda a: stats[b][a]["mean"] - turn(a))

    def cvar_policy(b):
        if b not in stats or cov[b] < TAU_SUPPORT:
            return base_global
        return max(stats[b], key=lambda a: stats[b][a]["cvar_fit"] - turn(a))

    def project_safety(a, b):
        """CBF-style safe-set projection: veto a risky-tail ENTRY to abstain (reduce-only)."""
        if a == "abstain" or b not in stats or a not in stats[b]:
            return a, False
        if stats[b][a]["cvar_tail"] < SAFETY_CAP:        # tail too heavy → project onto the safe set
            return "abstain", True
        return a, False

    # ── shadow eval on the held-out twin ──
    def util(a, r):
        return r["u"][ENTRY[a]] if a in ENTRY else r["u"][2]

    def evaluate(policy, with_safety=False):
        useq, n_proj, n_enter = [], 0, 0
        for r in test:
            b = _bucket(r["belief"], sent_med, vpin_med)
            a = policy(b)
            if with_safety:
                a, proj = project_safety(a, b); n_proj += int(proj)
            useq.append(util(a, r)); n_enter += int(a != "abstain")
        useq = np.asarray(useq)
        return {"mean": round(float(useq.mean()), 5), "cvar_25": round(_cvar(useq, 0.25), 5),
                "cvar_10": round(_cvar(useq, 0.10), 5), "worst": round(float(useq.min()), 5),
                "max_drawdown": round(_maxdd(useq), 5), "turnover": round(n_enter / len(useq), 4),
                "safety_projections": n_proj}

    ev_mean = evaluate(mean_policy)
    ev_cvar = evaluate(cvar_policy)
    ev_cvar_safe = evaluate(cvar_policy, with_safety=True)
    ev_base = evaluate(lambda b: base_global)
    real_seq = np.asarray([(r["u"][0] if r["dir"] == "long" else r["u"][1]) for r in test])
    ev_real = {"mean": round(float(real_seq.mean()), 5), "cvar_25": round(_cvar(real_seq, 0.25), 5),
               "cvar_10": round(_cvar(real_seq, 0.10), 5), "worst": round(float(real_seq.min()), 5),
               "max_drawdown": round(_maxdd(real_seq), 5), "turnover": 1.0, "safety_projections": 0}

    # the honest tail-vs-mean tradeoff: does CVaR improve the tail (cvar_10/worst/dd) vs the mean policy?
    tail_gain = round(ev_cvar["cvar_10"] - ev_mean["cvar_10"], 5)
    mean_cost = round(ev_cvar["mean"] - ev_mean["mean"], 5)
    dd_gain = round(ev_mean["max_drawdown"] - ev_cvar_safe["max_drawdown"], 5)   # +ve = safer
    risk_sensitive_helps = bool(tail_gain > 0.001 or dd_gain > 0.001)

    report = {
        "ts": round(time.time(), 3), "n_trades": n, "n_test": len(test), "n_state_buckets": len(stats),
        "objective": {"cvar_alpha_fit": ALPHA_FIT, "cvar_alpha_tail": ALPHA_TAIL,
                      "lambda_turnover": LAMBDA_TURN, "safety_cap_cvar10": SAFETY_CAP, "support_tau": TAU_SUPPORT,
                      "note": "distributional CVaR objective + explicit turnover cost + CBF safety projection "
                              "(reduce-only) + support-aware fallback; reward = path-aware twin utility "
                              "(already net of fees/slippage/funding)"},
        "eval": {"mean_policy": ev_mean, "cvar_policy": ev_cvar, "cvar_safety_policy": ev_cvar_safe,
                 "baseline": ev_base, "realized": ev_real},
        "tradeoff": {"tail_gain_cvar10_vs_mean": tail_gain, "mean_cost_vs_mean": mean_cost,
                     "drawdown_reduction_safety": dd_gain,
                     "safety_projection_rate": round(ev_cvar_safe["safety_projections"] / max(1, len(test)), 4),
                     "risk_sensitive_helps": risk_sensitive_helps,
                     "note": "CVaR trades mean for tail: tail_gain = CVaR_0.1(cvar) − CVaR_0.1(mean); "
                             "mean_cost = mean(cvar) − mean(mean); drawdown_reduction from the safety projection"},
        "authority": "shadow", "live_agents_untouched": True, "train_secs": round(time.time() - t0, 2),
        "verdict": (
            (f"Risk-sensitive CVaR policy IMPROVES the tail — CVaR_0.1 {ev_cvar['cvar_10']} vs mean-policy "
             f"{ev_mean['cvar_10']} (tail gain {tail_gain:+}), max-drawdown {ev_cvar_safe['max_drawdown']} vs "
             f"{ev_mean['max_drawdown']} after {ev_cvar_safe['safety_projections']} safety projections, "
             f"for a mean cost {mean_cost:+}."
             if risk_sensitive_helps else
             f"Risk-sensitive CVaR policy does NOT improve the tail here (tail gain {tail_gain:+}, dd reduction "
             f"{dd_gain:+}); the mean and CVaR policies pick alike on this corpus. HONEST — the distributional/"
             "CVaR machinery + CBF safety projection are real and measured, the tail edge isn't there yet.") +
            f" Turnover {ev_cvar['turnover']} (cost-aware) vs realized 1.0. SHADOW — no live authority (Rule 21)."),
    }
    print("\n=== RISK-SENSITIVE POLICY REPORT ===")
    print(json.dumps(report, indent=2))
    try:
        import redis_client
        from signals.scibrain import keys as _K
        r = redis_client.get()
        r.set(_K.RISK_POLICY_REPORT, json.dumps(report))
        print("saved", _K.RISK_POLICY_REPORT)
        try:
            from signals.scibrain import risk_policy_health
            risk_policy_health.build_risk_policy_health(r)
            print("saved", _K.RISK_POLICY_HEALTH)
        except Exception as hx:
            print("health warning:", str(hx)[:140])
    except Exception as exc:
        print("save warning:", str(exc)[:140])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
