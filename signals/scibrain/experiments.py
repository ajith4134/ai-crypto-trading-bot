"""SciBrain Phase 7b — the single experiment registry (design §9/§11).

ONE durable place where every hypothesis lives, so the fragmented producers (LLM council, IC/router
learner, cohort analysis, OPRO, GA, DSL miner, F9/F12 actuator) become proposal producers under ONE
registry and promotion gate — never independent promotion paths. The registry:

  • compiles raw proposals to a bounded typed ChangeSpec (changespec.compile_proposal) — rejects
    anything that can't compile to the safe DSL;
  • DEDUPS by content fingerprint (same knob + context + candidate = the same hypothesis), so endless
    re-proposals don't manufacture duplicates;
  • advances each spec through the VALIDATED lifecycle (only legal transitions) with an append-only
    history;
  • is Tier-0 — it records and tracks hypotheses; it has NO authority to APPLY any change to a live
    parameter (that is the Evidence/Authority/Influence gate, Phase 7c). Pure Redis; never raises.

Storage (all under scibrain:experiments:*):
  reg   HASH  hypothesis_id -> {"spec": <ChangeSpec.to_dict>, "status", "history":[...], "updated_ts"}
  by_fp HASH  fingerprint   -> hypothesis_id            (dedup index)
  index ZSET  hypothesis_id -> created_ts               (recency)
"""
from __future__ import annotations

import json
import time

import structlog

from . import keys as K
from .changespec import (ChangeSpec, LEGAL_TRANSITIONS, STATUSES, compile_proposal)

log = structlog.get_logger()


def _jload(v, default):
    try:
        return json.loads(v) if isinstance(v, (str, bytes)) else (v if v is not None else default)
    except (TypeError, ValueError):
        return default


def register(r, proposal, *, proposer: str = "unknown") -> dict:
    """Compile (if raw) + register a hypothesis ONCE. Returns
    {ok, hypothesis_id, status, deduped, errors}.

    `proposal` may be a raw dict (compiled here) or an already-compiled ChangeSpec. Dedup is by the
    ChangeSpec content fingerprint: a re-proposal of the same knob+context+candidate returns the
    existing record (deduped=True) instead of creating a twin."""
    out = {"ok": False, "hypothesis_id": None, "status": None, "deduped": False, "errors": []}
    try:
        if isinstance(proposal, ChangeSpec):
            spec, errors = proposal, []
        else:
            spec, errors = compile_proposal(proposal, proposer=proposer)
        if spec is None:
            out["errors"] = errors
            log.info("scibrain_changespec_rejected", proposer=proposer, errors=errors[:6],
                     target=(proposal or {}).get("target") if isinstance(proposal, dict) else None)
            return out

        fp = spec.fingerprint()
        existing_id = r.hget(K.EXPERIMENTS_FP, fp)
        if existing_id:
            existing_id = existing_id.decode() if isinstance(existing_id, bytes) else existing_id
            rec = _jload(r.hget(K.EXPERIMENTS_REG, existing_id), None)
            out.update({"ok": True, "hypothesis_id": existing_id, "deduped": True,
                        "status": (rec or {}).get("status", "compiled")})
            return out

        hid = spec.hypothesis_id
        record = {
            "spec": spec.to_dict(),
            "status": spec.status,                 # 'compiled' from the compiler
            "history": [{"ts": round(time.time(), 3), "from": "proposed", "to": spec.status,
                         "note": f"compiled by {spec.proposer}"}],
            "evidence_ids": list(spec.evidence_ids),
            "updated_ts": round(time.time(), 3),
        }
        pipe = r.pipeline()
        pipe.hset(K.EXPERIMENTS_REG, hid, json.dumps(record))
        pipe.hset(K.EXPERIMENTS_FP, fp, hid)
        pipe.zadd(K.EXPERIMENTS_INDEX, {hid: spec.created_ts})
        pipe.execute()
        out.update({"ok": True, "hypothesis_id": hid, "status": spec.status})
        registry_summary(r)                        # keep the panel aggregate fresh
        log.info("scibrain_changespec_registered", hypothesis_id=hid, target=spec.target,
                 candidate=spec.intervention.get("candidate"), proposer=spec.proposer)
    except Exception as exc:
        out["errors"] = [str(exc)[:160]]
        log.warning("scibrain_experiment_register_failed", error=str(exc)[:160])
    return out


def get(r, hid: str) -> dict | None:
    """The full registry record (spec + status + history) for one hypothesis, or None."""
    try:
        return _jload(r.hget(K.EXPERIMENTS_REG, hid), None)
    except Exception:
        return None


def transition(r, hid: str, new_status: str, *, note: str = "", evidence_ids=None) -> dict:
    """Advance a hypothesis to `new_status` iff the transition is legal (design §9 lifecycle).

    Records an append-only history entry; never applies any change to a live parameter. Returns
    {ok, status, error}. Illegal/unknown transitions are refused (the registry is the guardrail)."""
    out = {"ok": False, "status": None, "error": None}
    try:
        if new_status not in STATUSES:
            out["error"] = f"unknown status '{new_status}'"
            return out
        rec = get(r, hid)
        if rec is None:
            out["error"] = f"unknown hypothesis '{hid}'"
            return out
        cur = rec.get("status", "compiled")
        if new_status not in LEGAL_TRANSITIONS.get(cur, ()):
            out["error"] = f"illegal transition {cur} -> {new_status} (legal: {LEGAL_TRANSITIONS.get(cur, ())})"
            out["status"] = cur
            return out
        rec["status"] = new_status
        rec.setdefault("history", []).append(
            {"ts": round(time.time(), 3), "from": cur, "to": new_status, "note": note[:200]})
        if evidence_ids:
            ev = set(rec.get("evidence_ids") or []) | set(evidence_ids)
            rec["evidence_ids"] = sorted(ev)
        rec["updated_ts"] = round(time.time(), 3)
        r.hset(K.EXPERIMENTS_REG, hid, json.dumps(rec))
        out.update({"ok": True, "status": new_status})
        registry_summary(r)                        # keep the panel aggregate fresh
        # terminal outcomes feed the generalized hypothesis memory so the council won't repeat them
        if new_status in ("rejected", "demoted", "rolled_back", "retained"):
            try:
                from . import memory
                memory.record_terminal(r, rec.get("spec") or {}, new_status, note)
            except Exception:
                pass
        log.info("scibrain_changespec_transition", hypothesis_id=hid, **{"from": cur, "to": new_status})
    except Exception as exc:
        out["error"] = str(exc)[:160]
        log.warning("scibrain_experiment_transition_failed", hypothesis_id=hid, error=str(exc)[:160])
    return out


def list_specs(r, *, status: str | None = None, limit: int = 50) -> list[dict]:
    """Registry records, newest first, optionally filtered by status."""
    try:
        ids = list(r.zrevrange(K.EXPERIMENTS_INDEX, 0, max(0, int(limit) * 3 - 1)) or [])
        out = []
        for hid in ids:
            hid = hid.decode() if isinstance(hid, bytes) else hid
            rec = get(r, hid)
            if rec is None:
                continue
            if status is not None and rec.get("status") != status:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def registry_summary(r) -> dict:
    """Recompute the panel aggregate from the registry: total, counts per lifecycle status, and the
    most-recent few. Pure recompute (no counter drift). Also published to EXPERIMENTS_SUMMARY."""
    summary = {"n": 0, "by_status": {}, "recent": [], "updated_ts": round(time.time(), 3)}
    try:
        all_recs = r.hgetall(K.EXPERIMENTS_REG) or {}
        by_status: dict = {s: 0 for s in STATUSES}
        recs = []
        for _hid, raw in all_recs.items():
            rec = _jload(raw, None)
            if not isinstance(rec, dict):
                continue
            st = rec.get("status", "compiled")
            by_status[st] = by_status.get(st, 0) + 1
            recs.append(rec)
        recs.sort(key=lambda x: (x.get("spec") or {}).get("created_ts", 0.0), reverse=True)
        summary["n"] = len(recs)
        summary["by_status"] = {s: c for s, c in by_status.items() if c > 0}
        summary["recent"] = [{
            "hypothesis_id": (x.get("spec") or {}).get("hypothesis_id"),
            "target": (x.get("spec") or {}).get("target"),
            "candidate": ((x.get("spec") or {}).get("intervention") or {}).get("candidate"),
            "status": x.get("status"),
            "proposer": (x.get("spec") or {}).get("proposer"),
            "expected_effect": (x.get("spec") or {}).get("expected_effect"),
        } for x in recs[:10]]
        summary["note"] = ("Tier-0 hypothesis registry: bounded typed ChangeSpecs, deduped by content "
                           "fingerprint, advanced through the validated lifecycle. RECORDS proposals; "
                           "applying one is the Evidence/Authority/Influence gate's job (Phase 7c).")
        try:
            r.set(K.EXPERIMENTS_SUMMARY, json.dumps(summary))
        except Exception:
            pass
    except Exception as exc:
        log.warning("scibrain_experiment_summary_failed", error=str(exc)[:160])
    return summary
