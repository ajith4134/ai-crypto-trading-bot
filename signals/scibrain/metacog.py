"""SciBrain Phase-7d — the METACORTEX / metacognition competence maps (design §3.11): calibrated ignorance.

"Estimate what the brain knows, where it is weak, and whether it should act." This builds, per component
(every module/expert), a typed CompetenceRecord plus the derived metacognitive facets the design demands —
calibration, EPISTEMIC vs ALEATORIC uncertainty, support distance / OOD, and abstention utility — and the
key metacognitive output for the current decision:

    P(action_supported | belief, evidence, model_versions)

It reads ONLY what the live circuit already measured: per-module rolling IC + sample support (the §6g IC
tracker), the advisory ablation report (utility / redundancy / transferability), the global ex-ante audit
calibration (Brier), and the current workspace/belief. It then re-expresses that as honest competence:

  • calibration   — does the component's vote track outcomes, with enough evidence? (|IC|/target × support)
  • epistemic unc — REDUCIBLE ignorance: 1 − sample-sufficiency (more data shrinks it).
  • aleatoric unc — IRREDUCIBLE noise: 1 − |IC|/target (even with data, a low-IC signal is noisy).
  • support_distance / OOD — below the IC trust gate (or shadow) ⇒ out of support.
  • abstention_utility — the value of NOT acting on a weak/unsupported component.

The constitution: REWARD correct abstention, PENALIZE confident unsupported action. Tier-0: pure read, no
trading authority — it produces the self-model the planner/action-selector READ to decide whether to act.
"""
from __future__ import annotations

import json
import statistics
import time

import structlog

from . import keys as K
from .cognitive_contracts import CompetenceRecord

log = structlog.get_logger()

_TARGET_IC = 0.05               # |IC| at/above which a module is treated as fully skill-calibrated


def _jload(v, default=None):
    try:
        return json.loads(v) if isinstance(v, (str, bytes)) else (v if v is not None else default)
    except (TypeError, ValueError):
        return default


def _clip(v, lo=0.0, hi=1.0):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return lo


def _hash(r, key):
    try:
        return {(k.decode() if isinstance(k, bytes) else k): v for k, v in (r.hgetall(key) or {}).items()}
    except Exception:
        return {}


def build_competence_map(r, *, symbol: str | None = None, publish: bool = True) -> dict:
    """Per-component competence + the derived uncertainty facets + P(action_supported) for the current
    decision. Pure read; never raises. Publishes to K.METACOG when `publish`."""
    out = {"contract": "Metacognition", "available": False, "ts": round(time.time(), 3)}
    try:
        try:
            min_samples = int(r.get(K.IC_MIN_SAMPLES) or 30)
        except (TypeError, ValueError):
            min_samples = 30
        ic_map = {k: _clip(_f(v), -1.0, 1.0) for k, v in _hash(r, K.IC_MAP).items()}
        ic_samples = {k: int(_f(v)) for k, v in _hash(r, K.IC_SAMPLES).items()}
        ablation = _jload(r.get(K.ABLATION_REPORT), {}) or {}
        verdicts = ablation.get("verdicts") if isinstance(ablation.get("verdicts"), dict) else {}
        audit_calib = _jload(r.get(K.AUDIT_CALIBRATION), {}) or {}

        # union of all components we have ANY signal for
        comps = set(ic_map) | set(ic_samples) | set(verdicts)
        rows = []
        for c in sorted(comps):
            ic = ic_map.get(c)
            n = ic_samples.get(c, 0)
            v = verdicts.get(c) if isinstance(verdicts.get(c), dict) else {}
            support_suff = _clip(n / max(1, min_samples))                  # 0..1 evidence sufficiency
            skill = _clip(abs(ic) / _TARGET_IC) if ic is not None else 0.0  # 0..1 IC-skill
            calibration = round(_clip(skill * support_suff), 4)
            epistemic = round(_clip(1.0 - support_suff), 4)               # reducible (need more data)
            aleatoric = round(_clip(1.0 - skill), 4)                      # irreducible (noisy signal)
            support_distance = round(_clip(1.0 - support_suff), 4)
            ood = bool(n < min_samples)
            # value of abstaining: high when poorly calibrated AND unsupported (don't act on it)
            abstention_utility = round(_clip((1.0 - calibration) * (0.5 + 0.5 * support_distance)), 4)
            utility_delta = v.get("mean_delta_utility")
            if utility_delta is None:
                utility_delta = (v.get("utility_delta") if isinstance(v.get("utility_delta"), (int, float))
                                 else 0.0)
            drift = _clip(1.0 - _clip(_f(v.get("ic_transferability"), 1.0))) if v else 0.0

            rec = CompetenceRecord(
                component=c, context="global", task="direction",
                calibration=calibration, utility_delta=_f(utility_delta), support=int(n),
                drift=round(drift, 4),
                version={"status": v.get("status")} if v.get("status") else {})
            row = rec.to_dict()
            row.update({
                "ic": ic, "epistemic_uncertainty": epistemic, "aleatoric_uncertainty": aleatoric,
                "support_distance": support_distance, "ood": ood,
                "abstention_utility": abstention_utility, "status": v.get("status"),
                "_record_valid": not rec.validate(),
            })
            rows.append(row)

        # global aggregates
        cals = [x["calibration"] for x in rows if x["support"] > 0]
        glob = {
            "n_components": len(rows),
            "n_ood": sum(1 for x in rows if x["ood"]),
            "mean_calibration": round(statistics.mean(cals), 4) if cals else 0.0,
            "mean_epistemic": round(statistics.mean([x["epistemic_uncertainty"] for x in rows]), 4) if rows else 1.0,
            "mean_aleatoric": round(statistics.mean([x["aleatoric_uncertainty"] for x in rows]), 4) if rows else 1.0,
            "audit_calibration": {"brier": audit_calib.get("brier"), "n": audit_calib.get("n"),
                                  "base_rate": audit_calib.get("base_rate")},
        }

        # ── P(action_supported) for the CURRENT decision: the drivers' calibration, discounted by the
        # belief's disagreement and the drivers' support distance (the §3.11 key output) ──
        p_supported, abstain_reco, decision_ctx = _p_action_supported(r, rows, symbol)

        out.update({
            "available": True,
            "components": sorted(rows, key=lambda x: (x["calibration"], x["support"]), reverse=True),
            "global": glob,
            "p_action_supported": p_supported,
            "abstention_recommended": abstain_reco,
            "decision_context": decision_ctx,
            "note": ("Metacortex self-model (design §3.11): per-component calibration (|IC|·support) with "
                     "epistemic (reducible) vs aleatoric (irreducible) uncertainty, support distance / OOD, "
                     "and abstention utility; the key output P(action_supported|belief,evidence,versions) for "
                     "the live decision discounts driver calibration by cross-module disagreement + support "
                     "distance. Reward correct abstention; penalize confident unsupported action. Pure read."),
        })
        if publish:
            try:
                r.set(K.METACOG, json.dumps(out))
            except Exception:
                pass
    except Exception as exc:
        out["error"] = str(exc)[:200]
        log.warning("scibrain_metacog_build_failed", error=str(exc)[:200])
    return out


def _p_action_supported(r, rows: list, symbol):
    """P(action_supported) for the latest (or given) decision: salience-weighted driver calibration ×
    (1−disagreement) × (1−mean driver support distance). Returns (p, abstain_recommended, context)."""
    try:
        from . import workspace as ws_mod
        ws = ws_mod.build_workspace(r, symbol=symbol, publish=False)
        if not ws.get("available"):
            return None, None, {"available": False}
        belief = ws.get("belief") or {}
        disagreement = _clip(belief.get("disagreement", 0.0))
        by_comp = {x["component"]: x for x in rows}
        drivers = ws.get("broadcast") or [m["source"] for m in (ws.get("messages") or [])[:6]]
        cal, sd = [], []
        for d in drivers:
            rec = by_comp.get(d)
            if rec:
                cal.append(rec["calibration"])
                sd.append(rec["support_distance"])
        mean_cal = statistics.mean(cal) if cal else 0.0
        mean_sd = statistics.mean(sd) if sd else 1.0
        p = round(_clip(mean_cal * (1.0 - disagreement) * (1.0 - mean_sd)), 4)
        thresh = _f(r.get("scibrain:metacog:support_threshold"), 0.15)
        return p, bool(p < thresh), {
            "available": True, "symbol": ws.get("symbol"), "direction": (ws.get("decision") or {}).get("direction"),
            "drivers": drivers, "disagreement": disagreement,
            "mean_driver_calibration": round(mean_cal, 4), "mean_driver_support_distance": round(mean_sd, 4),
            "threshold": thresh}
    except Exception:
        return None, None, {"available": False}


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
