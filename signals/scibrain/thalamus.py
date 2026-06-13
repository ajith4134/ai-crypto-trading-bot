"""SciBrain Phase-7d — the THALAMUS / salience router (design §3.3): sparse attention + bandwidth control.

"Decide which evidence reaches the shared workspace and which experts receive compute." This consumes the
read-only Global Workspace (workspace.py) and does two read-only jobs:

  1. SALIENCE — score every workspace CognitiveMessage with the §3.3 decomposition
         salience = expected_information_gain + decision_relevance + anomaly_or_changepoint
                  + risk_urgency + memory_match_value − compute_cost − redundancy_penalty
     then select a SPARSE, load-balanced EVIDENCE subset (sparse-MoE style: an evidence-family
     correlation penalty stops one family dominating, and a salience floor = abstention).

  2. BUDGETS — allocate bounded attention/compute budgets for the things the cognitive router gates beyond
     direction modules: MEMORY RETRIEVAL depth, PLANNING depth, AUDIT depth, and overall COMPUTE — each a
     bounded scalar derived from the BeliefState (disagreement, changepoint, tail/risk urgency, conviction)
     and scaled DOWN under real system load, so risk/execution is never starved (the §8.2 latency
     constraint).

Tier-0: PURE READ, no trading authority. It re-weights what the workspace already surfaced and writes only
its own snapshot key. Nothing here changes a live vote, size, or order; it is the attention plan the
planner / memory / audit subsystems (later tasks) will READ to spend their bounded compute well.
"""
from __future__ import annotations

import json
import os
import time

import structlog

from . import keys as K

log = structlog.get_logger()

# ── salience component weights (config-overridable via the redis keys below) ──
_W = {
    "info_gain": 0.9, "relevance": 1.0, "anomaly": 0.7, "risk_urgency": 0.8,
    "memory_match": 0.5, "compute_cost": 0.4, "redundancy": 0.6,
}
_SALIENCE_FLOOR = 0.12          # below this a message is ABSTAINED (not admitted to the workspace)
_MAX_PER_FAMILY = 2             # load balance: at most N admitted from one evidence family (sparse MoE)
_EVIDENCE_MIN, _EVIDENCE_MAX = 4, 12


def _flt(r, key: str, default: float) -> float:
    try:
        v = r.get(key)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _load_factor() -> float:
    """0..1 headroom: 1.0 = idle, →0 = saturated. Scales every compute-bearing budget so the thalamus
    spends LESS attention when the box is loaded (risk/execution must never be starved)."""
    try:
        ncpu = os.cpu_count() or 8
        load1 = os.getloadavg()[0]
        return max(0.15, min(1.0, 1.0 - (load1 / float(ncpu))))
    except Exception:
        return 0.6


def _clip(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def route_salience(r, workspace: dict | None = None, *, publish: bool = True) -> dict:
    """Score + route the workspace's messages into a sparse evidence subset + bounded budgets. Pure read;
    never raises. Publishes to K.THALAMUS when `publish`."""
    out = {"contract": "Thalamus", "available": False, "ts": round(time.time(), 3)}
    try:
        if workspace is None:
            from . import workspace as ws_mod
            workspace = ws_mod.build_workspace(r, publish=False)
        if not workspace or not workspace.get("available"):
            out["error"] = (workspace or {}).get("error", "no workspace")
            return out

        belief = workspace.get("belief") or {}
        msgs = workspace.get("messages") or []
        changepoint = _clip(float(belief.get("changepoint_posterior", 0.0) or 0.0))
        disagreement = _clip(float(belief.get("disagreement", 0.0) or 0.0))
        tail = belief.get("tail_state") or {}
        regime_post = belief.get("regime_posterior") or {}
        turbulence = _clip(float(regime_post.get("turbulent", 0.0) or 0.0))
        # a tail-severity proxy from the statphys_soc features if present (crash pressure / hazard)
        tfeat = tail.get("features") or {}
        tail_sev = _clip(max(_flt_dict(tfeat, "crash_pressure"), _flt_dict(tfeat, "hazard"),
                             _flt_dict(tfeat, "soc_pressure")))
        risk_urgency_global = _clip(0.5 * turbulence + 0.3 * tail_sev + 0.2 * disagreement)

        # config overrides (optional)
        w = dict(_W)
        for k in w:
            w[k] = _flt(r, f"scibrain:thalamus:w_{k}", w[k])
        floor = _flt(r, "scibrain:thalamus:salience_floor", _SALIENCE_FLOOR)

        # ── per-family crowding (redundancy / evidence-family correlation penalty) ──
        fam_count: dict[str, int] = {}
        for m in msgs:
            fam = str(m.get("evidence_family", "unspecified"))
            fam_count[fam] = fam_count.get(fam, 0) + 1
        n_fam_max = max(fam_count.values()) if fam_count else 1

        ranked = []
        for m in msgs:
            bd = abs(float(m.get("belief_delta", 0.0) or 0.0))
            unc = _clip(float(m.get("uncertainty", 1.0) or 1.0))
            sd = _clip(float(m.get("support_distance", 0.0) or 0.0))
            fam = str(m.get("evidence_family", "unspecified"))
            shadow = sd >= 1.0
            # components (each ~[0,1])
            info_gain = bd * (1.0 - unc) * (1.0 - sd)          # confident, supported, contributing
            relevance = bd                                       # contribution to the fused belief
            anomaly = changepoint
            risk_urgency = risk_urgency_global
            memory_match = 0.0                                   # stub: episodic retrieval not built yet
            compute_cost = 0.5 if shadow else 0.15               # shadow/unproven experts cost more
            redundancy = (fam_count.get(fam, 1) - 1) / max(1, n_fam_max)
            score = (w["info_gain"] * info_gain + w["relevance"] * relevance + w["anomaly"] * anomaly
                     + w["risk_urgency"] * risk_urgency + w["memory_match"] * memory_match
                     - w["compute_cost"] * compute_cost - w["redundancy"] * redundancy)
            ranked.append({
                "source": m.get("source"), "evidence_family": fam, "role": m.get("role"),
                "salience": round(score, 5),
                "components": {"info_gain": round(info_gain, 4), "relevance": round(relevance, 4),
                               "anomaly": round(anomaly, 4), "risk_urgency": round(risk_urgency, 4),
                               "compute_cost": round(compute_cost, 4), "redundancy": round(redundancy, 4)},
                "abstain": score < floor,
            })
        ranked.sort(key=lambda x: x["salience"], reverse=True)

        # ── sparse, load-balanced EVIDENCE selection (admit to the workspace broadcast) ──
        load = _load_factor()
        evidence_budget = int(round(_EVIDENCE_MIN + (_EVIDENCE_MAX - _EVIDENCE_MIN) * load))
        per_fam: dict[str, int] = {}
        evidence_selected = []
        for row in ranked:
            if row["abstain"]:
                continue
            fam = row["evidence_family"]
            if per_fam.get(fam, 0) >= _MAX_PER_FAMILY:
                continue                                         # family load balance
            evidence_selected.append(row["source"])
            per_fam[fam] = per_fam.get(fam, 0) + 1
            if len(evidence_selected) >= evidence_budget:
                break

        # ── bounded attention/compute BUDGETS (scaled by stakes AND load) ──
        top_conv = 1.0 - (ranked[0]["components"]["relevance"] if ranked else 0.0)  # low top relevance ⇒ low conviction
        budgets = {
            "evidence": evidence_budget,                         # # messages admitted to the workspace
            # retrieve MORE memory when surprising/changing/risky (capped, load-scaled)
            "memory_retrieval_depth": int(round(_clip(0.5 * changepoint + 0.3 * disagreement
                                                       + 0.2 * risk_urgency_global) * 8 * load)),
            # plan DEEPER when the decision is high-relevance AND coherent (low disagreement); shallow when incoherent
            "planning_depth": int(round(_clip((ranked[0]["components"]["relevance"] if ranked else 0.0)
                                              * (1.0 - disagreement)) * 5 * load)),
            # audit DEEPER when modules disagree / risk is high / conviction is low
            "audit_depth": int(round(_clip(0.5 * disagreement + 0.3 * risk_urgency_global
                                           + 0.2 * top_conv) * 5)),
            # overall compute fraction — bounded by real load headroom (never starve risk/execution)
            "compute_fraction": round(load, 3),
        }

        out.update({
            "available": True, "symbol": workspace.get("symbol"),
            "load_factor": round(load, 3),
            "drivers": {"changepoint": changepoint, "disagreement": disagreement,
                        "risk_urgency": round(risk_urgency_global, 4), "turbulence": round(turbulence, 4),
                        "tail_severity": round(tail_sev, 4)},
            "n_messages": len(ranked), "n_abstained": sum(1 for x in ranked if x["abstain"]),
            "ranked": ranked,
            "evidence_selected": evidence_selected,             # sparse, family-balanced broadcast set
            "budgets": budgets,
            "note": ("Thalamic salience routing (design §3.3): each message scored by info-gain + relevance "
                     "+ anomaly + risk-urgency − compute-cost − family-redundancy; a sparse load-balanced "
                     "subset is admitted (the rest abstain), and memory/planning/audit/compute budgets are "
                     "allocated by stakes AND scaled by real system load so risk/execution is never starved. "
                     "Tier-0: pure read, no trading authority."),
        })
        if publish:
            try:
                r.set(K.THALAMUS, json.dumps(out))
            except Exception:
                pass
    except Exception as exc:
        out["error"] = str(exc)[:200]
        log.warning("scibrain_thalamus_route_failed", error=str(exc)[:200])
    return out


def _flt_dict(d: dict, key: str, default: float = 0.0) -> float:
    try:
        v = d.get(key)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default
