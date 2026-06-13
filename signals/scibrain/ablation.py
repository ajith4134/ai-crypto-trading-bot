"""Automatic module ablation / prune / demote report (§6g.8 + §6g.10, VS-12) — ADVISORY.

The module-admission constitution (§6g) says a component must show incremental out-of-sample
IC/utility conditional on the bank (7), SURVIVE ablation — removing it must measurably reduce
performance/safety (8) — and be rejected/merged/DEMOTED if redundant, unstable, or economically
useless (10). This task judges the LIVE bank against those criteria every cycle and publishes a
verdict + the exact evidence per module.

It is ADVISORY: it never mutates a module's live authority on its own (there is no runtime demote
mechanism, and changing which modules vote is capital-affecting → Rule 14 owner-gated; actual action
is the job of the champion/challenger promotion kernel, VS-17). The "survive ablation" test (§6g.8)
is approximated here by a cheap, bounded proxy over substrates already computed each cycle — rolling
IC (ic_tracker) and the nonlinear-redundancy + drift matrix (info_geometry). True leave-one-out
replay-ablation is the heavier on-demand path in `evaluator` for candidate CHANGES.

Verdict precedence (worst-first): PRUNE > DEMOTE > WATCH > KEEP; INSUFFICIENT until IC matures.
Deterministic, bounded, abstain-safe. Reads only scibrain:* substrates; writes only the report.
"""
from __future__ import annotations

import json
import time

import structlog

from . import ic_tracker
from . import keys as K

log = structlog.get_logger()

# Thresholds (explicit defaults per C6; move to Redis config later). Aligned with the substrates:
# info_geometry's redundant-threshold is 0.55; IC maturity is ic_tracker's IC_MIN_SAMPLES (≥30).
IC_HARMFUL = -0.02       # mature IC below this ⇒ anti-predictive (actively hurting) → PRUNE
IC_USELESS = 0.02        # |mature IC| below this ⇒ no measurable edge → PRUNE (economically useless)
REDUNDANT_THRESH = 0.55  # max-HSIC at/above this ⇒ redundant with another voter
STABILITY_THRESH = 0.6   # ic_transferability below this ⇒ drifted/unstable → WATCH (trust IC less)


def _verdict(module: str, *, ic, in_ic_map: bool, max_redundancy: float,
             redundant_with, partner_ic, ic_transferability: float, n: int = 0) -> dict:
    """Pure, deterministic verdict for one module from its evidence. No I/O — unit-tested.

    `ic` is the rolling Pearson IC (vote vs realized return); `in_ic_map` is False until it matures
    (< IC_MIN_SAMPLES). `n` is the IC sample count, used for a SIGNIFICANCE gate so we never PRUNE on
    noise: pruning a module is capital-affecting (§6g.8 "measurably reduce performance"; Rule 14), so a
    PRUNE requires the IC to clear ~2 standard errors (SE≈1/√n). An IC that is statistically ~0 is
    inconclusive → KEEP (the safe default), not PRUNE. `partner_ic` is the IC of the redundant partner.
    """
    if not in_ic_map or ic is None:
        return {"status": "INSUFFICIENT", "reasons": ["ic_not_matured"],
                "ic": ic, "n": int(n), "max_redundancy": round(float(max_redundancy or 0.0), 4),
                "redundant_with": redundant_with, "ic_transferability": ic_transferability}

    ic = float(ic)
    red = float(max_redundancy or 0.0)
    transfer = float(ic_transferability if ic_transferability is not None else 1.0)
    se = (1.0 / (max(int(n), 2) ** 0.5))        # IC standard error under the null
    reasons: list[str] = []

    # PRUNE — harmful or economically useless, with statistical confidence (§6g.8/10, Rule 14):
    #  • harmful: IC significantly negative (ic < -2·SE) AND past the harmful floor.
    #  • useless: even the OPTIMISTIC bound (ic + 2·SE) is below the useful-edge floor ⇒ confidently no edge.
    if ic <= IC_HARMFUL and ic < -2.0 * se:
        reasons.append(f"harmful_ic({ic:+.3f}, <-2se={-2*se:+.3f}, n={n})")
        status = "PRUNE"
    elif (ic + 2.0 * se) < IC_USELESS:
        reasons.append(f"useless_ic(upper={ic + 2*se:+.3f}<{IC_USELESS}, n={n})")
        status = "PRUNE"
    # DEMOTE — redundant AND not the best representative of its redundancy cluster (§6g.10)
    elif red >= REDUNDANT_THRESH and partner_ic is not None and float(partner_ic) >= ic:
        reasons.append(f"redundant_with({redundant_with}@HSIC{red:.2f}, partner_ic{float(partner_ic):+.3f}>=ic{ic:+.3f})")
        status = "DEMOTE"
    # WATCH — predictive + not redundant-dominated, but drifted/unstable (trust its IC less)
    elif transfer < STABILITY_THRESH:
        reasons.append(f"unstable_drift(transfer{transfer:.2f}<{STABILITY_THRESH})")
        status = "WATCH"
    else:
        reasons.append(f"keep(ic{ic:+.3f}, red{red:.2f}, transfer{transfer:.2f})")
        status = "KEEP"

    # additive flag: a kept/demoted module that is ALSO unstable is worth surfacing
    if status in ("KEEP", "DEMOTE") and transfer < STABILITY_THRESH and "unstable_drift" not in str(reasons):
        reasons.append(f"also_unstable(transfer{transfer:.2f})")

    return {"status": status, "reasons": reasons, "ic": round(ic, 4), "n": int(n),
            "max_redundancy": round(red, 4), "redundant_with": redundant_with,
            "partner_ic": (round(float(partner_ic), 4) if partner_ic is not None else None),
            "ic_transferability": round(transfer, 4)}


def _current_authority(r) -> dict:
    """{module: status} from the most-recent decision's influence_manifest (applied|gate_applied|
    counterfactual_only|abstained|suppressed). Best-effort, one read; absent → {}."""
    try:
        latest = r.zrevrange(K.LAST_DECISIONS, 0, 0)
        if not latest:
            return {}
        sym = latest[0].decode() if isinstance(latest[0], bytes) else latest[0]
        raw = r.get(K.sym_key(K.DECISION, sym))
        if not raw:
            return {}
        man = json.loads(raw).get("influence_manifest") or {}
        return {i["source"]: i.get("status") for i in man.get("influences", []) if i.get("source")}
    except Exception:
        return {}


def assess(r, now: float | None = None) -> dict:
    """Compute + publish the advisory ablation report. Never raises; returns the report dict."""
    now = now or time.time()
    try:
        health = json.loads(r.get(K.INFOGEO_HEALTH) or "{}")
    except Exception:
        health = {}
    mod_health = health.get("modules") or {}
    ic_map = ic_tracker.get_ic_map(r)            # only matured modules appear
    authority = _current_authority(r)

    # the declared bank (name → family/role); shadow modules included so the report covers them too
    try:
        from .modules import MODULES
        bank = {m.name: {"evidence_family": getattr(m, "evidence_family", "unspecified"),
                         "role": getattr(m, "role", "direction")} for m in MODULES}
    except Exception:
        bank = {}

    # union of every module we have any evidence for
    names = set(bank) | set(mod_health) | set(ic_map)
    verdicts: dict[str, dict] = {}
    for name in sorted(names):
        h = mod_health.get(name, {})
        partner = h.get("redundant_with")
        # true IC sample count for the significance gate (one bounded LLEN per module, main process)
        try:
            n_ic = int(r.llen(K.mod_key(K.IC_WIN, name)))
        except Exception:
            n_ic = int(h.get("n", 0) or 0)
        v = _verdict(
            name,
            ic=ic_map.get(name),
            in_ic_map=(name in ic_map),
            max_redundancy=h.get("max_redundancy", 0.0),
            redundant_with=partner,
            partner_ic=ic_map.get(partner) if partner else None,
            ic_transferability=h.get("ic_transferability"),
            n=n_ic,
        )
        v["evidence_family"] = bank.get(name, {}).get("evidence_family", "unspecified")
        v["role"] = bank.get(name, {}).get("role", "direction")
        v["current_authority"] = authority.get(name)
        verdicts[name] = v

    summary: dict[str, int] = {}
    for v in verdicts.values():
        summary[v["status"]] = summary.get(v["status"], 0) + 1
    recommendations = [
        {"module": m, "status": v["status"], "reasons": v["reasons"],
         "current_authority": v["current_authority"], "evidence_family": v["evidence_family"]}
        for m, v in verdicts.items() if v["status"] in ("PRUNE", "DEMOTE")
    ]

    report = {
        "ts": round(now, 2),
        "advisory_only": True,   # never auto-acts; action is owner-gated via the promotion kernel
        "n_modules": len(verdicts),
        "n_matured": len(ic_map),
        "summary": summary,
        "recommendations": recommendations,
        "thresholds": {"ic_harmful": IC_HARMFUL, "ic_useless": IC_USELESS,
                       "redundant": REDUNDANT_THRESH, "stability": STABILITY_THRESH},
        "verdicts": verdicts,
    }
    try:
        pipe = r.pipeline(transaction=False)
        pipe.setex(K.ABLATION_REPORT, K.HOT_TTL, json.dumps(report, separators=(",", ":")))
        pipe.incr(K.ABLATION_RUNS)
        pipe.execute()
    except Exception as exc:
        log.warning("scibrain_ablation_publish_failed", error=str(exc)[:120])
    return report
