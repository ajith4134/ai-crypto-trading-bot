"""SciBrain Phase 7b — deterministic matched-cohort evaluator for a ChangeSpec (design §8/§9).

Estimates the effect of a bounded router-gain ChangeSpec WITHOUT touching anything live, by replaying
the real historical decisions with the candidate gain and mapping every decision that CHANGES through
the path-aware counterfactual twin to a realized-utility delta:

  cohort   = closed scibrain trades in the ChangeSpec's target regime that carry a decision_snapshot
             (the exact per-module ModuleOutputs at open) + a realized OutcomePacket (+ twin).
  replay   = rebuild the ModuleOutputs, recover each trade's historical IC context from its stored
             gain, recompute the target module's gain under the candidate profile base, and re-fuse.
  Δutility = per trade: unchanged decision → 0; direction FLIP → twin.opposite − realized;
             becomes ABSTAIN → 0 − realized (the change would have prevented the trade).
  verdict  = bootstrap lower-confidence-bound on mean Δutility + a minimum support floor; the
             ChangeSpec's falsifier (LCB ≤ 0) decides pass/fail. Ablation (candidate→0, full removal)
             and the decision-change breakdown give marginal contribution + interaction context.

Pure compute + DB/Redis reads; NO trading authority (Tier-0). Never raises into a beat.
"""
from __future__ import annotations

import json
import random
import time

import structlog

from .contracts import ModuleOutput
from .fusion import fuse
from .outcome import _jload
from .router import GAIN_CEIL, GATE_EXPERTS, RouterState

log = structlog.get_logger()

EVALUATOR_VERSION = 1
_MIN_SUPPORT = 8          # need at least this many decision-changing trades to trust a FULL-cohort verdict
_BOOT = 1000              # bootstrap resamples for the LCB
_LCB_PCTL = 5             # 5th percentile = one-sided 95% lower confidence bound
# SPIBB baseline bootstrapping (arXiv:1712.06924) — when global support is sparse, the challenger may act
# only in sub-contexts with enough local evidence and falls back to the champion elsewhere (safe).
_MIN_CONTEXT_SUPPORT = 3  # min direction-changing samples for ONE sub-context to be "supported"
_MIN_SUPPORT_SPIBB = 6    # min total supported samples for a baseline-bootstrap (restricted) verdict


def _scalar_util(packet: dict):
    """The realized scalar utility from an OutcomePacket (utility is {terms,lambdas,utility})."""
    u = (packet or {}).get("utility")
    if isinstance(u, dict):
        u = u.get("utility")
    try:
        return float(u)
    except (TypeError, ValueError):
        return None


def _twin_util(packet: dict, leg: str):
    """The path-aware twin utility for a counterfactual leg ('opposite'|'abstain'), or None."""
    cf = (packet or {}).get("counterfactual") or {}
    if cf.get("status") != "ok":
        return None
    node = cf.get(leg) or {}
    try:
        return float(node.get("utility"))
    except (TypeError, ValueError):
        return None


def _recover_ic_mult(old_gain: float, old_base: float, strength: float) -> float:
    """Back out the IC multiplier that produced the stored gain, given the known profile base:
        gain = 1 + (base·ic_mult − 1)·strength   ⇒   ic_mult = ((gain−1)/strength + 1)/base.
    Falls back to 1.0 when base≈0 or strength≈0 (cannot be inverted)."""
    if old_base is None or abs(old_base) < 1e-9 or strength < 1e-9:
        return 1.0
    try:
        return (((old_gain - 1.0) / strength) + 1.0) / old_base
    except ZeroDivisionError:
        return 1.0


def _apply_gain(base: float, ic_mult: float, strength: float) -> float:
    """The router's gain formula, clipped to [0, GAIN_CEIL]."""
    g = 1.0 + (base * ic_mult - 1.0) * strength
    return max(0.0, min(float(GAIN_CEIL), g))


def _bootstrap_lcb(deltas: list[float]) -> float | None:
    """One-sided lower confidence bound on the mean of `deltas` via percentile bootstrap."""
    n = len(deltas)
    if n == 0:
        return None
    if n == 1:
        return float(deltas[0])
    rng = random.Random(1234567)               # fixed seed → deterministic, reproducible verdict
    means = []
    for _ in range(_BOOT):
        s = 0.0
        for _ in range(n):
            s += deltas[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    idx = max(0, min(len(means) - 1, int(_LCB_PCTL / 100.0 * len(means))))
    return round(means[idx], 6)


def _replay_one(row_modules: list, stored_router: dict, module: str, candidate_base: float,
                old_base: float):
    """Replay fusion for one trade with the candidate gain. Returns (old_decision, new_decision)
    Decisions, reconstructing the historical IC context from the stored gain so only the target
    module's gain moves."""
    outputs = [ModuleOutput.from_dict(m) for m in row_modules if isinstance(m, dict)]
    stored_gains = {k: float(v) for k, v in (stored_router.get("gains") or {}).items()}
    strength = float(stored_router.get("strength", 1.0) or 1.0)
    regime = stored_router.get("regime", "neutral")

    def _rs(gains):
        return RouterState(
            regime=regime, confidence=float(stored_router.get("confidence", 0.0) or 0.0),
            change_point_prob=float(stored_router.get("change_point_prob", 0.0) or 0.0),
            crash_warning=float(stored_router.get("crash_warning", 0.0) or 0.0),
            trend_score=float(stored_router.get("trend_score", 0.0) or 0.0),
            hmm_regime=stored_router.get("hmm_regime"), gains=gains,
            deactivated=[m for m, g in gains.items() if g <= 1e-6], strength=strength)

    old_gain = stored_gains.get(module)
    if module in GATE_EXPERTS or old_gain is None:
        # target isn't a directional gain on this trade (gate expert / didn't participate) → no change
        old_dec = fuse("_eval", outputs, router=_rs(stored_gains))
        return old_dec, old_dec
    ic_mult = _recover_ic_mult(old_gain, old_base, strength)
    cand_gain = _apply_gain(candidate_base, ic_mult, strength)
    new_gains = dict(stored_gains)
    new_gains[module] = cand_gain
    old_dec = fuse("_eval", outputs, router=_rs(stored_gains))
    new_dec = fuse("_eval", outputs, router=_rs(new_gains))
    return old_dec, new_dec


def _delta_for(old_dec, new_dec, realized_u, opp_u, abs_u):
    """Map an (old→new) decision change to a realized-utility delta + a change-type label.
    Returns (delta_or_None, change_type). None delta = unmodeled (excluded from the aggregate)."""
    od, nd = old_dec.direction, new_dec.direction
    if od == nd:
        return 0.0, "unchanged"                # direction preserved → outcome sign unchanged
    if nd is None:                             # the change would ABSTAIN this trade
        if realized_u is None:
            return None, "to_abstain_unmodeled"
        return (abs_u if abs_u is not None else 0.0) - realized_u, "to_abstain"
    if od is None:                             # change turns a no-trade into a trade — no twin exists
        return None, "to_trade_unmodeled"
    # genuine direction flip (long↔short) → the twin's opposite-policy realized utility
    if opp_u is None or realized_u is None:
        return None, "flip_unmodeled"
    return opp_u - realized_u, "flip"


_CONV_EPS = 0.02          # |Δconviction| above this counts as a material sizing shift


def _aggregate(deltas: list, change_types: dict, n_cohort: int, fidelity: float,
               conv_deltas: list, size_deltas: list) -> dict:
    modeled = [d for d in deltas if d is not None]
    changed = [d for d in deltas if d is not None and abs(d) > 1e-12]
    support = change_types.get("flip", 0) + change_types.get("to_abstain", 0)
    mean_delta = (round(sum(changed) / len(changed), 6) if changed else 0.0)
    lcb = _bootstrap_lcb(changed) if changed else 0.0
    # interaction test: among DIRECTION-PRESERVING trades, how much does conviction/size move? (the
    # common effect of a gain tweak — it rescales position size rather than flipping direction.)
    conv_shifted = [c for c in conv_deltas if abs(c) > _CONV_EPS]
    return {
        "n_cohort": n_cohort,
        "n_modeled": len(modeled),
        "support_changed": support,            # decisions the change altered DIRECTION (flip/abstain)
        "change_types": change_types,
        "mean_delta_utility": mean_delta,      # over the direction-changing trades (twin-grounded)
        "lcb_delta_utility": lcb,              # one-sided 95% bootstrap lower bound
        "replay_fidelity": round(fidelity, 4), # frac where stored-gain replay reproduced actual dir
        # interaction / sizing sensitivity (not folded into the Δutility verdict — size→utility is
        # the separate incremental-utility task; surfaced so a change is never reported as "no effect")
        "n_conviction_shifted": len(conv_shifted),
        "mean_conviction_delta": (round(sum(conv_deltas) / len(conv_deltas), 6) if conv_deltas else 0.0),
        "mean_abs_size_delta": (round(sum(abs(s) for s in size_deltas) / len(size_deltas), 6)
                                if size_deltas else 0.0),
    }


def _context_key(old_dec) -> str:
    """A coarse, interpretable sub-context for SPIBB support buckets: the old (champion) decision's
    conviction band × its direction. Low-cardinality so each bucket can accrue enough support to trust."""
    try:
        c = float(old_dec.conviction)
    except (TypeError, ValueError):
        c = 0.0
    band = "hi" if c >= 0.67 else ("mid" if c >= 0.34 else "lo")
    return f"{band}_conv/{old_dec.direction or 'abstain'}"


def _baseline_bootstrap(changed: list) -> dict:
    """SPIBB-style baseline bootstrapping (design §9; arXiv:1712.06924). Partition the direction-changing
    trades by sub-context, mark contexts with ≥ _MIN_CONTEXT_SUPPORT changed samples as SUPPORTED, and
    treat the challenger as a RESTRICTED policy that acts ONLY in supported contexts and falls back to the
    champion (Δ=0) everywhere else — so it can never do worse than the champion outside its support. Reports
    per-context evidence, the restricted-policy bootstrap LCB, the applicable contexts, and the fallback
    fraction. Pure compute."""
    from collections import defaultdict
    buckets: dict[str, list] = defaultdict(list)
    for c in changed:
        buckets[c.get("context", "?")].append(float(c["delta"]))
    per_context, supported, supported_deltas = [], [], []
    for ctx, ds in buckets.items():
        n = len(ds)
        is_sup = n >= _MIN_CONTEXT_SUPPORT
        per_context.append({"context": ctx, "n": n,
                            "mean_delta": round(sum(ds) / n, 6) if n else 0.0,
                            "lcb": _bootstrap_lcb(ds), "supported": is_sup})
        if is_sup:
            supported.append(ctx)
            supported_deltas.extend(ds)
    per_context.sort(key=lambda x: (-x["n"], x["context"]))
    n_changed = sum(len(v) for v in buckets.values())
    n_sup = len(supported_deltas)
    return {
        "method": "spibb_baseline_bootstrap", "min_context_support": _MIN_CONTEXT_SUPPORT,
        "per_context": per_context, "applicable_contexts": sorted(supported),
        "n_supported_total": n_sup, "n_changed_total": n_changed,
        "fallback_fraction": round((n_changed - n_sup) / n_changed, 4) if n_changed else 1.0,
        "mean_supported_delta": round(sum(supported_deltas) / n_sup, 6) if n_sup else 0.0,
        "bootstrapped_lcb": _bootstrap_lcb(supported_deltas) if supported_deltas else None,
    }


def evaluate_changespec(r, spec: dict, *, limit: int = 400) -> dict:
    """Evaluate one ChangeSpec dict against its matched historical cohort. Returns a structured
    result with a pass/fail verdict (the falsifier: LCB(Δutility) > 0 AND enough support)."""
    out = {"evaluator_version": EVALUATOR_VERSION, "target": spec.get("target"),
           "supported": False, "verdict": "unevaluated", "reason": None, "ts": round(time.time(), 3)}
    try:
        target = spec.get("target", "")
        if not target.startswith("router.gain."):
            out["reason"] = f"evaluator v1 supports router.gain.* only (got '{target}')"
            return out
        _, _, module, regime = target.split(".", 3) if target.count(".") >= 3 else (None, None, None, None)
        if module is None:
            out["reason"] = "malformed router.gain target"
            return out
        candidate = float((spec.get("intervention") or {}).get("candidate"))
        from .router import _PROFILES
        old_base = float(_PROFILES.get(regime, {}).get(module, 1.0))
        out["supported"] = True
        out["module"], out["regime"], out["candidate"], out["old_base"] = module, regime, candidate, old_base

        from db import db_conn
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id, direction,
                          signals_at_entry::jsonb->'decision_snapshot'->'modules',
                          signals_at_entry::jsonb->'decision_snapshot'->'router',
                          signals_at_entry::jsonb->'outcome_packet',
                          (signals_at_entry::jsonb->'entry_snapshot'->'action_space'->>'chosen_propensity')::float,
                          extract(epoch from exit_time)
                     FROM trades
                    WHERE timeframe='scibrain' AND status='closed'
                      AND signals_at_entry::jsonb->'decision_snapshot' ? 'modules'
                      AND signals_at_entry::jsonb ? 'outcome_packet'
                      AND (signals_at_entry::jsonb->'decision_snapshot'->'router'->>'regime') = %s
                    ORDER BY exit_time DESC NULLS LAST
                    LIMIT %s""", (regime, int(max(1, limit))))
            rows = cur.fetchall()

        def _run(cand_base):
            deltas, ctypes, fid_hit, fid_tot = [], {}, 0, 0
            conv_deltas, size_deltas = [], []
            changed = []                       # per-trade records for the rigor layer (off-policy/folds)
            for _id, actual_dir, mods, router_d, pkt, propensity, exit_ts in rows:
                mods = _jload(mods, None); router_d = _jload(router_d, None); pkt = _jload(pkt, None)
                if not (isinstance(mods, list) and isinstance(router_d, dict)):
                    continue
                old_dec, new_dec = _replay_one(mods, router_d, module, cand_base, old_base)
                # fidelity: does the stored-gain replay reproduce the actual opened direction?
                fid_tot += 1
                if old_dec.direction == (actual_dir if actual_dir in ("long", "short") else None):
                    fid_hit += 1
                ru = _scalar_util(pkt)
                d, ct = _delta_for(old_dec, new_dec, ru, _twin_util(pkt, "opposite"),
                                   _twin_util(pkt, "abstain"))
                deltas.append(d)
                ctypes[ct] = ctypes.get(ct, 0) + 1
                if ct == "unchanged":          # direction preserved → measure the sizing sensitivity
                    conv_deltas.append(float(new_dec.conviction) - float(old_dec.conviction))
                    size_deltas.append(float(new_dec.size_frac) - float(old_dec.size_frac))
                elif d is not None and abs(d) > 1e-12:   # a real direction change with a twin-graded Δ
                    changed.append({"delta": float(d), "propensity": propensity, "exit_ts": exit_ts,
                                    "context": _context_key(old_dec)})   # SPIBB sub-context bucket
            fidelity = (fid_hit / fid_tot) if fid_tot else 0.0
            agg = _aggregate(deltas, ctypes, fid_tot, fidelity, conv_deltas, size_deltas)
            return agg, changed

        primary, changed = _run(candidate)
        ablation, _ = _run(0.0) if abs(candidate) > 1e-9 else (primary, changed)  # full-removal reference
        out["evaluation"] = primary
        out["ablation_remove"] = {"candidate": 0.0, "mean_delta_utility": ablation["mean_delta_utility"],
                                  "lcb_delta_utility": ablation["lcb_delta_utility"],
                                  "support_changed": ablation["support_changed"]}

        # SPIBB baseline bootstrapping — always computed (surfaced as evidence); only the SPARSE-support
        # verdict path below depends on it. The challenger restricted to its supported contexts is SAFE.
        out["baseline_bootstrap"] = bb = _baseline_bootstrap(changed)
        sup_set = set(bb["applicable_contexts"])

        # verdict: direction-changing support is required (the twin only grounds Δutility on flips/
        # abstains); then the RIGOR layer (off-policy IPS/DR + walk-forward + FDR + complexity) decides.
        if primary["support_changed"] < _MIN_SUPPORT:
            # SPARSE global support: instead of a flat reject, try the baseline-bootstrapped RESTRICTED
            # policy (acts only where locally supported, champion elsewhere). The SPIBB safety guarantee
            # — never deviating outside support — is what justifies acting on less GLOBAL evidence than the
            # regime-wide _MIN_SUPPORT/effective-8 gate. Falsifier: enough total supported samples, a
            # strictly positive POOLED bootstrap LCB over the supported contexts, AND every supported
            # context individually non-harmful (its own LCB ≥ 0) so no single context carries a net-negative
            # one. This advances only to unit_tested (shadow); live promotion still needs the full gate +
            # owner approval (Phase-7c canary task), and applicable_contexts scopes any future application.
            spibb_passed = False
            ctx_lcbs = [c["lcb"] for c in bb["per_context"] if c["supported"] and c["lcb"] is not None]
            if (primary["replay_fidelity"] >= 0.6 and bb["n_supported_total"] >= _MIN_SUPPORT_SPIBB
                    and (bb["bootstrapped_lcb"] or 0.0) > 0.0 and all(l >= 0 for l in ctx_lcbs)):
                out["verdict"] = "baseline_bootstrap_pass"
                out["applicable_contexts"] = bb["applicable_contexts"]
                out["reason"] = (
                    f"sparse global support ({primary['support_changed']} dir-changes < {_MIN_SUPPORT}); "
                    f"SPIBB restricted policy improves on {bb['n_supported_total']} samples across "
                    f"{len(sup_set)} supported context(s) {bb['applicable_contexts']} (pooled bootstrap LCB "
                    f"{bb['bootstrapped_lcb']:+.4f} > 0, every context LCB ≥ 0); champion fallback on "
                    f"{bb['fallback_fraction']:.0%} of deviations (safe by construction)")
                spibb_passed = True
            if not spibb_passed:
                sized = primary["n_conviction_shifted"]
                if sized >= _MIN_SUPPORT:
                    out["verdict"], out["reason"] = "size_effect_only", (
                        f"flips no direction ({primary['support_changed']} flips) but rescales sizing on "
                        f"{sized} trades (mean Δconv {primary['mean_conviction_delta']:+.3f}); size→utility "
                        f"is the incremental-utility task, not twin-gradable here")
                else:
                    out["verdict"], out["reason"] = "insufficient_support", (
                        f"only {primary['support_changed']} direction-changing + {sized} sizing-shifted "
                        f"trades (< {_MIN_SUPPORT}); baseline-bootstrap also short (supported "
                        f"{bb['n_supported_total']}/{_MIN_SUPPORT_SPIBB}, bootstrap LCB {bb['bootstrapped_lcb']})")
        elif primary["replay_fidelity"] < 0.6:
            out["verdict"], out["reason"] = "low_fidelity", (
                f"replay reproduced only {primary['replay_fidelity']:.0%} of actual directions")
        else:
            from . import rigor
            deltas = [c["delta"] for c in changed]
            rv = rigor.rigorous_verdict(r, deltas, changed,
                                        direct=primary["mean_delta_utility"],
                                        candidate=candidate, current=old_base)
            out["rigor"] = rv
            out["verdict"], out["reason"] = rv["verdict"], "; ".join(rv["reasons"])

        # Phase-7c — consolidate everything above into the normalized per-change EVIDENCE REPORT
        # (effective-sample / conditional-utility / ablation / risk + the "no cycle count" decision basis).
        from .change_report import build_change_report
        out["change_report"] = build_change_report(out, spec)
    except Exception as exc:
        out["reason"] = f"error: {str(exc)[:160]}"
        log.warning("scibrain_evaluator_error", error=str(exc)[:160])
    return out


def evaluate_registered(r, hid: str) -> dict:
    """Evaluate a registered hypothesis, attach the result to its registry record, and advance the
    lifecycle: pass → unit_tested, fail (falsifier) → rejected, otherwise leave it for re-evaluation."""
    from . import experiments as ex
    rec = ex.get(r, hid)
    if rec is None:
        return {"ok": False, "error": f"unknown hypothesis '{hid}'"}
    result = evaluate_changespec(r, rec.get("spec") or {})
    # persist the evaluation onto the record (immutable evidence)
    rec.setdefault("evaluations", []).append(result)
    rec["updated_ts"] = round(time.time(), 3)
    from . import keys as K
    r.hset(K.EXPERIMENTS_REG, hid, json.dumps(rec))
    verdict = result.get("verdict")
    if verdict == "pass" and rec.get("status") == "compiled":
        ex.transition(r, hid, "unit_tested", note=f"cohort eval pass: {result.get('reason')}")
    elif verdict == "baseline_bootstrap_pass" and rec.get("status") == "compiled":
        # RESTRICTED pass (SPIBB): advances like a pass but acts ONLY in its supported contexts and falls
        # back to the champion elsewhere — record the applicable contexts so any future application is scoped.
        rec["applicable_contexts"] = result.get("applicable_contexts") or []
        rec["restricted"] = True
        r.hset(K.EXPERIMENTS_REG, hid, json.dumps(rec))
        ex.transition(r, hid, "unit_tested",
                      note=f"baseline-bootstrap restricted pass ({rec['applicable_contexts']}): {result.get('reason')}")
    elif verdict in ("fail", "fail_fdr", "fail_negative_control", "unstable") and rec.get("status") == "compiled":
        # a falsified / multiple-testing-failed / non-robust change is rejected (→ negative memory)
        ex.transition(r, hid, "rejected", note=f"{verdict}: {result.get('reason')}")
    else:
        ex.registry_summary(r)                 # transient (insufficient/size-only) → re-evaluate later
    return {"ok": True, "hypothesis_id": hid, "verdict": verdict, "reason": result.get("reason"),
            "evaluation": result.get("evaluation")}


def evaluate_pending(r, *, limit: int = 50) -> dict:
    """Evaluate every COMPILED hypothesis in the registry against its fresh matched cohort (evidence
    accrues as more trades close). Idempotent recompute: pass→unit_tested, fail→rejected, others stay
    compiled and re-evaluate next pass. Returns a verdict tally. Never raises."""
    from . import experiments as ex
    summary = {"evaluated": 0, "verdicts": {}}
    try:
        for rec in ex.list_specs(r, status="compiled", limit=int(max(1, limit))):
            hid = (rec.get("spec") or {}).get("hypothesis_id")
            if not hid:
                continue
            res = evaluate_registered(r, hid)
            v = res.get("verdict", "error")
            summary["verdicts"][v] = summary["verdicts"].get(v, 0) + 1
            summary["evaluated"] += 1
    except Exception as exc:
        log.warning("scibrain_evaluate_pending_error", error=str(exc)[:160])
    if summary["evaluated"]:
        log.info("scibrain_evaluate_pending", **summary)
    return summary
