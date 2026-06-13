"""SciBrain Phase 6 — VS-V4 Learning Laboratory read-model (design §4.3 / §VS-V4).

Assembles ONE bounded, deterministic LearningGraphSnapshot from the REAL living-intelligence
artifacts so the dashboard can render the experiment system without re-deriving anything:

  • hypotheses  — the single experiment registry (scibrain:experiments:reg): each bounded typed
                  ChangeSpec, its validated-lifecycle status, the append-only transition HISTORY
                  (= genealogy/version lineage), and the latest matched-cohort EVALUATION
                  (= champion[current base] vs challenger[candidate] scorecard + confidence bound).
  • lifecycle   — the canonical promotion pipeline (STATUSES) + the legal-transition DAG +
                  by-status counts, so the frontend places each hypothesis on the real pipeline.
  • memory      — generalized hypothesis memory: NEGATIVE (rejected/demoted/rolled-back knob+
                  direction changes the council must not repeat) vs POSITIVE (retained laws).
  • competence  — the advisory per-module ablation verdict (KEEP/WATCH/DEMOTE/PRUNE/INSUFFICIENT)
                  joined with each module's rolling IC + sample count: the honest module-skill map.
  • authority   — the live authority switches + the Tier-0 fact that the registry RECORDS proposals
                  and has NO authority to apply any change (the Phase-7c gate owns application).

Pure Redis reads; Tier-0; never raises (returns {available:False,error} on failure). The heavy
per-trade ChangeSpec evaluation already ran in the beat — this only reads what it persisted.
"""
from __future__ import annotations

import json
import time

import structlog

from . import keys as K
from .changespec import LEGAL_TRANSITIONS, STATUSES

log = structlog.get_logger()

LEARNING_VIEW_VERSION = 1
# the terminal lifecycle states (no legal onward transition) — derived, not hard-coded
_TERMINAL = tuple(s for s in STATUSES if not LEGAL_TRANSITIONS.get(s))
# a hypothesis whose status reached one of these has actual authority to move a live parameter
_APPLIED_STATES = ("live", "retained")


def _jload(v, default):
    try:
        return json.loads(v) if isinstance(v, (str, bytes)) else (v if v is not None else default)
    except (TypeError, ValueError):
        return default


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _hypothesis_node(rec: dict) -> dict | None:
    """Trim one registry record to a genealogy node: the spec essentials, the full transition
    history (lineage), and ONLY the latest evaluation (the beat re-evaluates every pass, so the
    list can be hundreds long — we never ship them all)."""
    spec = rec.get("spec") or {}
    hid = spec.get("hypothesis_id")
    if not hid:
        return None
    interv = spec.get("intervention") or {}
    evals = rec.get("evaluations") or []
    latest = evals[-1] if evals else None
    scorecard = None
    if isinstance(latest, dict):
        ev = latest.get("evaluation") or {}
        rg = latest.get("rigor") or {}
        scorecard = {
            "verdict": latest.get("verdict"),
            "reason": latest.get("reason"),
            "ts": latest.get("ts"),
            "module": latest.get("module"),
            "regime": latest.get("regime"),
            # champion (current live base) vs challenger (proposed candidate)
            "champion_base": _f(latest.get("old_base"), _f(interv.get("old"))),
            "challenger_candidate": _f(latest.get("candidate"), _f(interv.get("candidate"))),
            # the matched-cohort effect estimate + its one-sided lower confidence bound (the falsifier)
            "n_cohort": ev.get("n_cohort"),
            "support_changed": ev.get("support_changed"),
            "replay_fidelity": ev.get("replay_fidelity"),
            "mean_delta_utility": ev.get("mean_delta_utility"),
            "lcb_delta_utility": ev.get("lcb_delta_utility"),
            "n_conviction_shifted": ev.get("n_conviction_shifted"),
            "mean_conviction_delta": ev.get("mean_conviction_delta"),
            "ablation_remove": latest.get("ablation_remove"),
            # rigor layer (off-policy IPS/DR + walk-forward + FDR + complexity), present once a
            # change has enough direction-changing support to be twin-graded
            "rigor": {k: rg.get(k) for k in ("verdict", "reasons", "ipw_lcb", "wf_consistent",
                                             "fdr_pass", "complexity_penalized")} if rg else None,
            # SPIBB baseline bootstrapping (sparse-support fallback): the supported sub-contexts where the
            # challenger may act, its pooled restricted-policy lower bound, and the champion-fallback frac.
            "baseline_bootstrap": (lambda bb: ({
                "applicable_contexts": bb.get("applicable_contexts"),
                "n_supported_total": bb.get("n_supported_total"),
                "n_changed_total": bb.get("n_changed_total"),
                "fallback_fraction": bb.get("fallback_fraction"),
                "bootstrapped_lcb": bb.get("bootstrapped_lcb"),
                "per_context": bb.get("per_context"),
            } if bb else None))(latest.get("baseline_bootstrap")),
            "applicable_contexts": latest.get("applicable_contexts"),   # set on a restricted (SPIBB) pass
            # Phase-7c per-change EVIDENCE REPORT: effective-sample / conditional-utility / ablation / risk
            # + the explicit "promotion rests on evidence, not an arbitrary cycle count" decision basis.
            "report": latest.get("change_report"),
        }
    return {
        "hypothesis_id": hid,
        "target": spec.get("target"),
        "role": spec.get("role"),
        "context_predicate": spec.get("context_predicate"),
        "kind": interv.get("kind"),
        "candidate": _f(interv.get("candidate")),
        "old": _f(interv.get("old")),
        "parameter_bounds": spec.get("parameter_bounds"),
        "expected_effect": spec.get("expected_effect"),
        "falsifier": spec.get("falsifier"),
        "complexity_cost": spec.get("complexity_cost"),
        "proposer": spec.get("proposer"),
        "created_ts": spec.get("created_ts"),
        "updated_ts": rec.get("updated_ts"),
        "status": rec.get("status"),
        "is_terminal": rec.get("status") in _TERMINAL,
        "is_applied": rec.get("status") in _APPLIED_STATES,
        "evidence_ids": (rec.get("evidence_ids") or spec.get("evidence_ids") or [])[:12],
        "versions": spec.get("versions"),     # code_fingerprint / code_commit / config_hash lineage
        "history": rec.get("history") or [],  # append-only lifecycle transitions = genealogy edges
        "n_evaluations": len(evals),
        "scorecard": scorecard,
    }


def _memory_bank(r, key: str, limit: int = 25) -> list[dict]:
    """All records from a generalized-memory hash (negative or positive), newest/most-repeated first."""
    out = []
    try:
        for _sig, raw in (r.hgetall(key) or {}).items():
            rec = _jload(raw, None)
            if isinstance(rec, dict):
                out.append({
                    "signature": rec.get("signature"),
                    "target": rec.get("target"),
                    "n": int(rec.get("n", 0) or 0),
                    "last_status": rec.get("last_status"),
                    "last_ts": rec.get("last_ts"),
                    "reasons": (rec.get("reasons") or [])[:3],
                    "hypothesis_ids": (rec.get("hypothesis_ids") or [])[:6],
                })
        out.sort(key=lambda x: (x.get("n", 0), x.get("last_ts") or 0), reverse=True)
    except Exception:
        return out
    return out[:limit]


def _competence_map(r) -> dict:
    """Honest per-module skill map straight from the advisory ablation report's full per-module
    `verdicts` map: each module's status (KEEP/WATCH/DEMOTE/PRUNE/INSUFFICIENT), rolling IC + sample
    count, redundancy + IC-transferability (the InfoGeo drift link), evidence family, role, and its
    CURRENT live authority. NO fabricated regime×symbol×horizon grid — only what is actually measured.
    Falls back to the raw ic:map / ic:samples hashes if the ablation report hasn't been published."""
    report = _jload(r.get(K.ABLATION_REPORT), {}) or {}
    verdicts = report.get("verdicts")
    by_mod: dict = {}
    if isinstance(verdicts, dict) and verdicts:
        for m, rc in verdicts.items():
            if not isinstance(rc, dict):
                continue
            by_mod[m] = {
                "module": m, "status": rc.get("status"),
                "ic": _f(rc.get("ic")), "samples": rc.get("n"),
                "max_redundancy": _f(rc.get("max_redundancy")),
                "redundant_with": rc.get("redundant_with"),
                "ic_transferability": _f(rc.get("ic_transferability")),
                "evidence_family": rc.get("evidence_family"),
                "role": rc.get("role"),
                "current_authority": rc.get("current_authority"),
                "reasons": (rc.get("reasons") or [])[:3],
            }
    else:
        # fallback: the ablation report isn't published yet — show raw IC + sample counts only
        try:
            ic = {(k.decode() if isinstance(k, bytes) else k): _f(v)
                   for k, v in (r.hgetall(K.IC_MAP) or {}).items()}
            samples = {(k.decode() if isinstance(k, bytes) else k): int(_f(v, 0) or 0)
                       for k, v in (r.hgetall(K.IC_SAMPLES) or {}).items()}
            for m, icv in ic.items():
                by_mod[m] = {"module": m, "status": None, "ic": icv,
                             "samples": samples.get(m), "reasons": []}
        except Exception:
            pass
    # sort matured (has IC) first, by descending skill; unmatured modules sink to the bottom
    modules = sorted(by_mod.values(),
                     key=lambda x: (x.get("ic") if x.get("ic") is not None else -9), reverse=True)
    return {
        "advisory_only": True,
        "summary": report.get("summary") or {},
        "n_modules": report.get("n_modules"),
        "n_matured": report.get("n_matured"),
        "ts": report.get("ts"),
        "modules": modules,
        "note": ("Module skill = rolling IC (Pearson of the module's directional vote vs the realized "
                 "forward return) with its sample count; the ablation verdict (KEEP/WATCH/DEMOTE/PRUNE) "
                 "is ADVISORY — it never auto-acts. No regime/symbol/horizon competence grid is shown "
                 "because that breakdown is not yet measured (honest gap, not a fabricated heatmap)."),
    }


def _self_improvement(r) -> dict:
    """VS-V4 self-improvement unification surface (design §11 / §4.3): the unified producer-bus state
    (every autonomous self-improvement mechanism, its kernel mode, and whether any still bypasses) plus
    the newest-first LINEAGE of actual kernel-routed changes (controller writes + ModelChangeSpec records).
    This is the 'state + lineage' the Living-Intelligence dashboard needs. Pure read; never raises."""
    try:
        from . import producers as P
        audit = P.audit_bypass(r)
        producers = [{
            "id": row["id"], "label": row["label"], "family": row["family"],
            "effective_mode": row["effective_mode"], "enabled": row["enabled"],
            "open_bypass": row["open_bypass"], "applies_to": row["applies_to"],
            "last": row.get("controller_last") or row.get("model_last"),
        } for row in audit.get("producers", [])]
        return {
            "available": audit.get("available", False),
            "mode": audit.get("mode"),
            "n_producers": audit.get("n_producers"),
            "n_open_bypasses": audit.get("n_open_bypasses"),
            "open_bypasses": audit.get("open_bypasses", []),
            "by_effective_mode": audit.get("by_effective_mode", {}),
            "producers": producers,
            "recent_changes": P.recent_changes(r, limit=30),
            "note": ("Every autonomous self-improvement mechanism is a proposal/recorded producer under "
                     "ONE kernel (no silent independent promotion path). bounded_recorded = kernel-owned "
                     "bounded write + audit; model_recorded = typed ModelChangeSpec recorded; kernel = "
                     "registry-routed. open_bypasses=0 means none self-applies outside the kernel."),
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)[:160]}


def _rollback_history(r, limit: int = 20) -> dict:
    """VS-V4 rollback lineage (design §4.3): the bounded-canary event history — request / approve /
    rollback / promote — the only capital-affecting promotion path, with its auto-rollback events. Plus
    the current active canary (if any). Pure read; never raises."""
    try:
        raw = r.lrange(K.CANARY_HISTORY, 0, max(0, int(limit) - 1)) or []
        events = [e for e in (_jload(x, None) for x in raw) if isinstance(e, dict)]
        try:
            active = _jload(r.get(K.CANARY_ACTIVE), None)
        except Exception:
            active = None
        return {
            "active_canary": active,
            "events": events,
            "n_events": len(events),
            "note": ("The bounded canary is the ONLY capital-affecting promotion path (owner-approved, one "
                     "(module,regime) at a time, auto-rollback). These are its request/approve/rollback/"
                     "promote events — the rollback lineage for any applied change."),
        }
    except Exception as exc:
        return {"available": False, "error": str(exc)[:160]}


def build_learning_snapshot(r, *, limit: int = 50) -> dict:
    """Assemble the VS-V4 Learning Laboratory snapshot from the live registry + memory + competence +
    authority state. Bounded to `limit` hypotheses (newest first). Pure read; never raises."""
    snap: dict = {"available": False, "schema_version": LEARNING_VIEW_VERSION,
                  "ts": round(time.time(), 3)}
    try:
        from . import experiments as ex
        lim = max(1, min(200, int(limit)))
        recs = ex.list_specs(r, limit=lim)
        hyps = [n for n in (_hypothesis_node(rc) for rc in recs) if n is not None]

        # lifecycle counts straight from the registry aggregate (recomputed, drift-proof)
        summary = _jload(r.get(K.EXPERIMENTS_SUMMARY), {}) or {}
        by_status = summary.get("by_status") or {}

        # the rolling FDR/multiple-testing guard context (online BH on bootstrap p-values)
        try:
            raw_p = r.lrange(K.EXPERIMENTS_PVALS, 0, 49) or []
            pvals = [p for p in (_f(x) for x in raw_p) if p is not None]
        except Exception:
            pvals = []

        # authority — always visible (design §4.3). Phase-7c: use the CANONICAL authority reconciliation
        # (signals/scibrain/authority.py) so there is ONE source of truth for the live observe→advise→
        # bounded_canary→live(+veto) map + the invariant checks — not a partial ad-hoc switch list. The
        # banner keys (origination/router/ic/promotion_gate) are preserved; the full per-component
        # reconciliation + invariants ride alongside for the Safety & Authority surface.
        from .authority import reconcile_authority
        applied = sum(int(c) for s, c in by_status.items() if s in _APPLIED_STATES)
        authority = reconcile_authority(r)
        authority.update({
            "router_strength": _f(r.get(K.ROUTER_STRENGTH), 1.0),
            "registry_tier": 0,
            "hypotheses_applied_live": applied,
            "note": ("The experiment registry is Tier-0: it RECORDS + tracks hypotheses through the "
                     "validated lifecycle but has NO authority to apply any change to a live parameter. "
                     "Applying a passed hypothesis is the Phase-7c Evidence/Authority/Influence gate's "
                     "job (not yet active), so no hypothesis here can move a live knob."),
        })

        snap.update({
            "available": True,
            "lifecycle": {
                "states": list(STATUSES),
                "transitions": {k: list(v) for k, v in LEGAL_TRANSITIONS.items()},
                "terminal": list(_TERMINAL),
                "by_status": by_status,
                "n": summary.get("n", len(hyps)),
            },
            "hypotheses": hyps,
            "memory": {
                "negative": _memory_bank(r, K.MEMORY_NEG),
                "positive": _memory_bank(r, K.MEMORY_POS),
                "note": "knob+direction generalization: a rejected change blocks nearby re-proposals.",
            },
            "competence": _competence_map(r),
            "fdr": {"n_pvals": len(pvals), "recent_pvals": pvals[:20],
                    "note": "online Benjamini–Hochberg guard against multiple-testing false discoveries."},
            "authority": authority,
            "self_improvement": _self_improvement(r),
            "rollback": _rollback_history(r),
        })
    except Exception as exc:
        snap["error"] = str(exc)[:200]
        log.warning("scibrain_learning_view_failed", error=str(exc)[:200])
    return snap
