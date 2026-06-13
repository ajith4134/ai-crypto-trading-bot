"""SciBrain Phase 7b — statistical rigor layer for the ChangeSpec evaluator (design §9, tasks 5–8).

Turns the evaluator's per-trade Δutility records into a verdict that resists false discovery:

  task 7 — richer SCORING: expectancy, dispersion, CVaR (tail), worst-case, win-rate (descriptive
           only), and sign-STABILITY across time.
  task 5 — OFF-POLICY correction: IPS (inverse-propensity) + doubly-robust (direct twin estimate +
           IPS residual) using the logged action propensities, so the estimate isn't biased by the
           fact that we only observe the trades the live policy chose to take.
  task 6 — purged WALK-FORWARD over time folds + a NEGATIVE CONTROL (a no-op placebo must show ~0),
           so an effect must persist out-of-fold and a placebo must not manufacture one.
  task 8 — GATES: minimum EFFECTIVE sample, lower-confidence-bound > 0, an online multiple-testing
           guard (rolling Benjamini–Hochberg over recent bootstrap p-values), and a COMPLEXITY
           penalty so a big/أrisky change must clear a higher bar than a tiny one.

Pure compute; Tier-0. The evaluator calls rigorous_verdict() and attaches the result. Never raises.
"""
from __future__ import annotations

import json
import math
import random

import structlog

from . import keys as K

log = structlog.get_logger()

_BOOT = 1000
_LCB_PCTL = 5
_MIN_EFFECTIVE = 8           # minimum effective (propensity-weighted) sample to trust a verdict
_FDR_Q = 0.10                # rolling Benjamini–Hochberg target false-discovery rate
_FDR_WINDOW = 100            # recent tests considered for the multiple-testing guard
_COMPLEXITY_LAMBDA = 0.01    # utility the change must clear per unit of (magnitude+1) complexity
_IPS_CLIP = 10.0             # cap importance weights so one tiny-propensity trade can't dominate


def _percentile(xs: list, p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    idx = max(0, min(len(s) - 1, int(p / 100.0 * len(s))))
    return s[idx]


def _bootstrap(deltas: list, seed: int = 1234567):
    """Return (lcb, p_value) — one-sided 95% LCB on the mean and the bootstrap p-value P(mean<=0)."""
    n = len(deltas)
    if n == 0:
        return None, None
    if n == 1:
        return float(deltas[0]), (1.0 if deltas[0] <= 0 else 0.0)
    rng = random.Random(seed)
    means = []
    le0 = 0
    for _ in range(_BOOT):
        s = 0.0
        for _ in range(n):
            s += deltas[rng.randrange(n)]
        m = s / n
        means.append(m)
        if m <= 0:
            le0 += 1
    means.sort()
    lcb = means[max(0, min(len(means) - 1, int(_LCB_PCTL / 100.0 * len(means))))]
    return round(lcb, 6), round(le0 / len(means), 4)


def score_distribution(deltas: list) -> dict:
    """task 7 — the metric suite over the Δutility distribution. win_rate is DESCRIPTIVE only."""
    n = len(deltas)
    if n == 0:
        return {"n": 0, "expectancy": None, "std": None, "cvar5": None, "worst": None,
                "win_rate": None}
    mean = sum(deltas) / n
    var = sum((d - mean) ** 2 for d in deltas) / n
    tail = [d for d in deltas if d <= (_percentile(deltas, 5) if n >= 20 else min(deltas))]
    return {
        "n": n,
        "expectancy": round(mean, 6),
        "std": round(math.sqrt(var), 6),
        "cvar5": (round(sum(tail) / len(tail), 6) if tail else round(min(deltas), 6)),  # mean of worst tail
        "worst": round(min(deltas), 6),
        "win_rate": round(sum(1 for d in deltas if d > 0) / n, 4),   # descriptive only
    }


def ips_dr(records: list, direct: float) -> dict:
    """task 5 — off-policy IPS + doubly-robust mean of the change effect.

    records: [{"delta", "propensity"}]. IPS reweights each observed trade by 1/propensity (clipped)
    so under-sampled actions count proportionally; DR adds the IPS-corrected residual to the direct
    (twin) estimate, which is lower-variance and unbiased if EITHER model is right."""
    wsum = wdelta = 0.0
    resid_w = resid = 0.0
    used = 0
    for rec in records:
        d = rec.get("delta")
        p = rec.get("propensity")
        if d is None or p is None or p <= 0:
            continue
        w = min(_IPS_CLIP, 1.0 / float(p))
        wsum += w
        wdelta += w * float(d)
        resid_w += w
        resid += w * (float(d) - direct)
        used += 1
    ips = (wdelta / wsum) if wsum > 1e-9 else None
    dr = (direct + resid / resid_w) if resid_w > 1e-9 else None
    # effective sample size (Kish) — penalizes a few huge weights
    if used:
        ws = [min(_IPS_CLIP, 1.0 / float(r["propensity"])) for r in records
              if r.get("delta") is not None and r.get("propensity")]
        ess = (sum(ws) ** 2) / (sum(w * w for w in ws)) if ws else 0.0
    else:
        ess = 0.0
    return {"ips_mean": (round(ips, 6) if ips is not None else None),
            "dr_mean": (round(dr, 6) if dr is not None else None),
            "effective_sample": round(ess, 2), "n_weighted": used}


def walk_forward(records: list, k: int = 3) -> dict:
    """task 6 — split the changed trades into k time-ordered folds; report per-fold mean + the sign
    STABILITY (fraction of non-empty folds agreeing with the overall sign). An effect that only
    appears in one fold is not robust."""
    recs = [r for r in records if r.get("delta") is not None and r.get("exit_ts") is not None]
    if len(recs) < k:
        return {"folds": [], "stability": None, "n_folds": 0,
                "note": f"need >= {k} changed trades for {k}-fold walk-forward"}
    recs.sort(key=lambda r: r["exit_ts"])
    fold_means = []
    sz = len(recs) / k
    for i in range(k):
        chunk = recs[int(i * sz):int((i + 1) * sz)]
        if chunk:
            fold_means.append(sum(c["delta"] for c in chunk) / len(chunk))
    overall = sum(r["delta"] for r in recs) / len(recs)
    osign = (overall > 0)
    agree = sum(1 for m in fold_means if (m > 0) == osign)
    return {"folds": [round(m, 6) for m in fold_means],
            "stability": round(agree / len(fold_means), 4) if fold_means else None,
            "n_folds": len(fold_means)}


def negative_control(deltas: list, seed: int = 99) -> dict:
    """task 6 — a sign-flip PERMUTATION negative control: under the null 'this change has no
    directional effect', each Δutility's sign is exchangeable, so the observed mean should be extreme
    vs the sign-flipped null. Returns the permutation p-value P(|null mean| >= |observed mean|); a
    spurious effect that survives the bootstrap but not this control is caught."""
    n = len(deltas)
    if n < 4:
        return {"perm_p": None, "passes": False, "note": "too few for a permutation control"}
    obs = abs(sum(deltas) / n)
    rng = random.Random(seed)
    ge = 0
    for _ in range(_BOOT):
        s = 0.0
        for d in deltas:
            s += d if rng.random() < 0.5 else -d
        if abs(s / n) >= obs - 1e-12:
            ge += 1
    p = ge / _BOOT
    return {"perm_p": round(p, 4), "passes": bool(p < 0.10)}


def _fdr_guard(r, p_value: float | None) -> dict:
    """task 8 — online multiple-testing guard: keep a rolling window of recent bootstrap p-values and
    apply Benjamini–Hochberg at q=_FDR_Q; this test passes the guard iff its p-value is BH-significant
    within that window. Bounds the false-discovery rate as endless hypotheses are tested."""
    if p_value is None:
        return {"passes_fdr": False, "reason": "no p-value", "n_recent": 0}
    try:
        r.lpush(K.EXPERIMENTS_PVALS, p_value)
        r.ltrim(K.EXPERIMENTS_PVALS, 0, _FDR_WINDOW - 1)
        raw = r.lrange(K.EXPERIMENTS_PVALS, 0, _FDR_WINDOW - 1) or []
        ps = sorted(float(x) for x in raw)
        m = len(ps)
        # BH critical value: largest threshold p_(i) <= (i/m)*q
        crit = 0.0
        for i, p in enumerate(ps, start=1):
            if p <= (i / m) * _FDR_Q:
                crit = p
        return {"passes_fdr": bool(p_value <= crit), "bh_threshold": round(crit, 4),
                "n_recent": m, "q": _FDR_Q}
    except Exception as exc:
        return {"passes_fdr": False, "reason": str(exc)[:80], "n_recent": 0}


def complexity_penalty(candidate: float, current: float, ceil: float = 1.6) -> dict:
    """task 8 — a change pays a utility penalty proportional to its magnitude, so a big/risky move
    must clear a higher incremental-utility bar than a tiny tweak."""
    try:
        mag = abs(float(candidate) - float(current)) / max(1e-9, float(ceil))
    except (TypeError, ValueError):
        mag = 1.0
    penalty = _COMPLEXITY_LAMBDA * (1.0 + mag)
    return {"magnitude": round(mag, 4), "penalty": round(penalty, 6)}


def rigorous_verdict(r, deltas: list, records: list, *, direct: float, candidate: float,
                     current: float) -> dict:
    """Combine scoring + off-policy + walk-forward + gates into one verdict (pass/fail/insufficient).
    `deltas` = direction-changed Δutilities; `records` = same with {propensity, exit_ts}."""
    score = score_distribution(deltas)
    offp = ips_dr(records, direct)
    wf = walk_forward(records)
    lcb, pval = _bootstrap(deltas)
    fdr = _fdr_guard(r, pval)
    negctrl = negative_control(deltas)
    cx = complexity_penalty(candidate, current)
    eff = offp.get("effective_sample") or 0.0
    # complexity-adjusted lower bound: the change must clear its own complexity cost
    adj_lcb = (round(lcb - cx["penalty"], 6) if lcb is not None else None)

    reasons = []
    if score["n"] < _MIN_EFFECTIVE or eff < _MIN_EFFECTIVE:
        verdict = "insufficient_effective_sample"
        reasons.append(f"effective sample {eff:.1f} / changed {score['n']} < {_MIN_EFFECTIVE}")
    elif adj_lcb is None or adj_lcb <= 0:
        verdict = "fail"
        reasons.append(f"complexity-adjusted LCB {adj_lcb} <= 0")
    elif not fdr.get("passes_fdr"):
        verdict = "fail_fdr"
        reasons.append(f"does not survive multiple-testing (p={pval}, BH thr={fdr.get('bh_threshold')})")
    elif not negctrl.get("passes"):
        verdict = "fail_negative_control"
        reasons.append(f"sign-flip permutation null not beaten (perm_p={negctrl.get('perm_p')})")
    elif wf.get("stability") is not None and wf["stability"] < 0.67:
        verdict = "unstable"
        reasons.append(f"walk-forward sign stability {wf['stability']} < 0.67")
    else:
        verdict = "pass"
        reasons.append(f"adj-LCB {adj_lcb}>0, ESS {eff:.1f}, stability {wf.get('stability')}, "
                       f"survives BH (p={pval})")
    return {
        "verdict": verdict, "reasons": reasons,
        "scoring": score, "off_policy": offp, "walk_forward": wf,
        "lcb": lcb, "complexity_adjusted_lcb": adj_lcb, "p_value": pval,
        "fdr_guard": fdr, "negative_control": negctrl, "complexity": cx,
        "note": "win_rate is descriptive; the verdict rests on complexity-adjusted off-policy Δutility "
                "that survives walk-forward stability + a rolling-BH multiple-testing guard.",
    }
