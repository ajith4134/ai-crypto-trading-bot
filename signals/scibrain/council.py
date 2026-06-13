"""SciBrain Phase 7b — LLM scientific council (design §8): turns trade/cohort evidence into a
bounded, grounded ChangeSpec proposal, closing the hypothesis→evaluate loop.

The council plays five constrained roles in one structured pass (one LLM call to respect this box's
LLM budget — see finding-ollama-saturation; routed CLOUD-PRIMARY like the interrogator):
  1. Forensic Analyst   — summarizes the entry decision + realized/twin outcome from the evidence.
  2. Causal Skeptic     — attacks the proposed cause; names confounders.
  3. Experiment Designer— states the context, treatment, falsifier.
  4. Formula Engineer   — emits ONE bounded ChangeSpec against an ALLOW-LISTED target (never code).
  5. Independent Reviewer— checks grounding, leakage, and consistency with the deterministic evidence.

The LLM only PROPOSES. A deterministic gate (the §8 mandatory-rejection checks) then accepts or
rejects, and only an accepted proposal is COMPILED + REGISTERED + EVALUATED. The council has NO
authority to apply a change (Tier-0); applying one is the Phase-7c gate. Degrades honestly: if the
LLM is unavailable or returns nothing usable, the council produces NO proposal (never a fabricated
one). Never raises into a beat.
"""
from __future__ import annotations

import json
import re
import time

import structlog

from . import changespec as cs
from . import experiments as ex
from .outcome import _jload

log = structlog.get_logger()

COUNCIL_VERSION = 1


# ── evidence assembly (the deterministic grounding the reviewer is checked against) ───────────
def assemble_evidence(r, trade_id: str) -> dict | None:
    """Gather the grounded evidence for one closed scibrain trade: the decision (regime/direction/
    attribution), the realized + path-aware-twin outcome, and the ex-ante audit. None if the trade
    lacks the decision/outcome needed to reason (can't ground → the council must not invent)."""
    from db import db_conn
    try:
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT direction, signals_at_entry FROM trades
                            WHERE id=%s AND timeframe='scibrain'""", (str(trade_id),))
            row = cur.fetchone()
        if not row:
            return None
        actual_dir, prov_raw = row
        prov = _jload(prov_raw, {}) or {}
        dec = prov.get("decision_snapshot")
        pkt = prov.get("outcome_packet")
        if not isinstance(dec, dict) or not isinstance(pkt, dict):
            return None
        regime = (dec.get("router") or {}).get("regime") or prov.get("regime")
        attribution = dec.get("attribution") or []
        # modules that actually drove this decision (the only ones a change may be grounded on)
        attr_modules = [a.get("module") for a in attribution if isinstance(a, dict)]
        cf = pkt.get("counterfactual") or {}
        util = pkt.get("utility")
        util = util.get("utility") if isinstance(util, dict) else util
        audit = prov.get("audit") or {}
        return {
            "trade_id": str(trade_id),
            "regime": regime,
            "direction": actual_dir,
            "conviction": prov.get("conviction"),
            "primary_driver": prov.get("primary_driver") or dec.get("primary_driver"),
            "attribution": attribution[:6],
            "attr_modules": attr_modules,
            "realized_utility": util,
            "won": pkt.get("won"),
            "twin_fault_class": (cf.get("fault_class") if cf.get("status") == "ok" else None),
            "twin_confidence": cf.get("confidence"),
            "audit_wrong_dir_risk": audit.get("wrong_direction_risk"),
            "audit_responsible_factor": audit.get("responsible_factor"),
        }
    except Exception as exc:
        log.warning("scibrain_council_evidence_failed", trade_id=str(trade_id), error=str(exc)[:140])
        return None


# ── the LLM call (cloud-primary, json) ────────────────────────────────────────────────────────
def _llm_json(prompt: str, *, max_tokens: int = 700, timeout_s: int = 90) -> tuple[dict | None, dict]:
    """Cloud-primary JSON completion (local Ollama fallback). Returns (parsed_or_None, meta)."""
    text, meta = None, {"transport": None, "provider": None, "model": None}
    try:
        from llm.providers import call_chain, get_providers
        prov, text = call_chain(prompt, max_tokens=max_tokens, json_mode=True,
                                timeout=min(timeout_s, 90))
        if text and text.strip():
            model = next((m for (n, _u, m, _k) in get_providers() if n == prov), prov)
            meta = {"transport": "cloud", "provider": prov, "model": model}
    except Exception as exc:
        log.info("scibrain_council_cloud_unavailable", error=str(exc)[:140])
        text = None
    if not (text and text.strip()):
        try:
            from llm.ollama_client import chat_ollama
            from .interrogator import LEAD_MODEL
            text = chat_ollama(prompt, model=LEAD_MODEL, max_tokens=max_tokens, timeout_s=timeout_s)
            meta = {"transport": "local", "provider": "ollama", "model": LEAD_MODEL}
        except Exception as exc:
            log.info("scibrain_council_local_unavailable", error=str(exc)[:140])
            return None, meta
    return _extract_json(text), meta


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)        # first balanced-ish object
    if m:
        try:
            return json.loads(m.group(0))
        except (TypeError, ValueError):
            return None
    return None


def _council_prompt(evidence: dict) -> str:
    """The five-role structured prompt. The Formula Engineer is constrained to the bounded target
    menu for THIS trade's regime + the allow-listed scalars, so output is forced into the typed DSL."""
    regime = evidence.get("regime") or "neutral"
    targets = [f"router.gain.{m}.{regime}" for m in evidence.get("attr_modules", []) if m]
    return (
        "You are a five-role quantitative research council reviewing ONE trade from a crypto "
        "trading bot's modular decision circuit. Roles: Forensic Analyst, Causal Skeptic, Experiment "
        "Designer, Formula Engineer, Independent Reviewer.\n\n"
        f"EVIDENCE (deterministic, do not contradict):\n{json.dumps(evidence, default=str)}\n\n"
        "fault_class meaning: 'direction'=the opposite direction would have won on the real path; "
        "'selection'=both directions lose (should have abstained); 'none'=the trade was fine.\n\n"
        "TASK: propose AT MOST ONE bounded change to a per-regime module gain that, IF it had been "
        "in effect, would plausibly have improved outcomes for trades LIKE this one. You may ONLY "
        f"target one of these allow-listed knobs (gain is a multiplier, hard bounds [0, 1.6]):\n"
        f"{json.dumps(targets)}\n"
        "Rules: the target module MUST appear in the evidence attribution; do NOT propose a change "
        "that only fixes this one path; if no defensible change exists, return proposal=null.\n\n"
        "Respond with STRICT JSON only:\n"
        "{\"forensic\":\"...\",\"skeptic\":\"...\",\"designer\":\"...\","
        "\"proposal\":{\"target\":\"router.gain.<module>." + regime + "\",\"candidate\":<float 0..1.6>,"
        "\"context_predicate\":\"router.regime == " + regime + "\",\"role\":\"direction\","
        "\"expected_effect\":\"...\",\"falsifier\":\"LCB(delta_utility) <= 0\"} or null,"
        "\"reviewer\":{\"grounded\":true,\"leakage_free\":true,\"consistent\":true,"
        "\"verdict\":\"accept|reject\",\"reason\":\"...\"}}")


# ── deterministic §8 mandatory-rejection gate (layered on top of the LLM) ──────────────────────
def grounding_checks(proposal: dict, evidence: dict) -> list[str]:
    """The deterministic mandatory-rejection checks (design §8). Returns errors[]; empty = passes."""
    errors: list[str] = []
    if not isinstance(proposal, dict):
        return ["no proposal"]
    target = proposal.get("target", "")
    res = cs.resolve_target(target)
    if not res.get("valid"):
        errors.append(f"target not in DSL allow-list: {res.get('error')}")
        return errors
    # target must be a module that was actually MEASURED in this decision's attribution (§8)
    parts = target.split(".")
    module = parts[2] if len(parts) >= 4 else None
    regime = parts[3] if len(parts) >= 4 else None
    if module not in (evidence.get("attr_modules") or []):
        errors.append(f"target module '{module}' was not measured in the evidence attribution")
    if regime != evidence.get("regime"):
        errors.append(f"target regime '{regime}' != evidence regime '{evidence.get('regime')}'")
    # candidate must be an actual change vs current
    try:
        if abs(float(proposal.get("candidate")) - float(res.get("current"))) < 1e-9:
            errors.append("candidate equals the current value (no-op)")
    except (TypeError, ValueError):
        errors.append("candidate not numeric")
    return errors


def run_council(r, trade_id: str, *, register: bool = True, evaluate: bool = True) -> dict:
    """Run the council on one trade: assemble evidence → LLM proposes → deterministic gate → compile
    + register + evaluate (if it passes). Returns the full transcript + decision. No trading effect."""
    out = {"council_version": COUNCIL_VERSION, "trade_id": str(trade_id), "ts": round(time.time(), 3),
           "registered": False, "hypothesis_id": None, "verdict": "no_proposal", "errors": []}
    evidence = assemble_evidence(r, trade_id)
    if evidence is None:
        out["verdict"], out["errors"] = "ungroundable", ["trade lacks decision_snapshot/outcome"]
        return out
    out["evidence"] = evidence
    if not evidence.get("attr_modules"):
        out["verdict"], out["errors"] = "ungroundable", ["no attribution modules to ground a change"]
        return out

    parsed, meta = _llm_json(_council_prompt(evidence))
    out["llm_meta"] = meta
    if not isinstance(parsed, dict):
        out["verdict"], out["errors"] = "llm_unavailable", ["no usable LLM JSON response"]
        return out
    out["transcript"] = {k: parsed.get(k) for k in ("forensic", "skeptic", "designer", "reviewer")}
    proposal = parsed.get("proposal")
    reviewer = parsed.get("reviewer") or {}
    if not isinstance(proposal, dict):
        out["verdict"] = "council_declined"           # the council itself found no defensible change
        return out
    if str(reviewer.get("verdict", "")).lower() != "accept" or not reviewer.get("grounded", False):
        out["verdict"], out["errors"] = "reviewer_rejected", [reviewer.get("reason", "reviewer did not accept")]
        return out

    errors = grounding_checks(proposal, evidence)
    if errors:
        out["verdict"], out["errors"] = "grounding_failed", errors
        return out

    # semantic-consistency gate (§8): reject changes that contradict the deterministic evidence
    # (e.g. boosting a module that voted WITH the losing direction).
    from .validator import semantic_consistency
    sem_errors = semantic_consistency(proposal, evidence)
    if sem_errors:
        out["verdict"], out["errors"] = "semantically_inconsistent", sem_errors
        return out

    # negative-memory gate (§8: don't repeat a rejected hypothesis) — a coarser block than the
    # registry's exact-fingerprint dedup: the same DIRECTION of change on the same knob that already
    # failed is refused.
    from . import memory
    _cur = cs.resolve_target(proposal.get("target")).get("current")
    prior = memory.recall_negative(r, proposal.get("target"), proposal.get("candidate"), _cur)
    if prior and int(prior.get("n", 0)) >= 1:
        out["verdict"] = "repeats_failed_hypothesis"
        out["errors"] = [f"{prior.get('signature')} previously failed {prior.get('n')}× "
                         f"({(prior.get('reasons') or ['?'])[0]})"]
        return out

    # build the raw ChangeSpec proposal from the accepted council proposal
    raw = {
        "target": proposal.get("target"), "role": proposal.get("role", "direction"),
        "context_predicate": proposal.get("context_predicate", f"router.regime == {evidence['regime']}"),
        "intervention": {"kind": "gain_multiplier", "candidate": proposal.get("candidate")},
        "parameter_bounds": [0.0, 1.6],
        "evidence_ids": [str(trade_id)],
        "expected_effect": proposal.get("expected_effect", ""),
        "falsifier": proposal.get("falsifier", "LCB(delta_utility) <= 0"),
    }
    spec, cerrors = cs.compile_proposal(raw, proposer="llm_council")
    if spec is None:
        out["verdict"], out["errors"] = "did_not_compile", cerrors
        return out
    out["proposal"] = spec.to_dict()
    if not register:
        out["verdict"] = "compiled_not_registered"
        return out

    reg = ex.register(r, spec, proposer="llm_council")
    out["registered"], out["hypothesis_id"], out["deduped"] = reg["ok"], reg["hypothesis_id"], reg.get("deduped")
    out["verdict"] = "duplicate" if reg.get("deduped") else "registered"
    if evaluate and reg["ok"] and not reg.get("deduped"):
        from . import evaluator as ev
        ev_res = ev.evaluate_registered(r, reg["hypothesis_id"])
        out["evaluation_verdict"] = ev_res.get("verdict")
        out["evaluation_reason"] = ev_res.get("reason")
    log.info("scibrain_council_run", trade_id=str(trade_id), verdict=out["verdict"],
             hypothesis_id=out["hypothesis_id"], eval=out.get("evaluation_verdict"))
    return out


def propose_from_flagged(r, *, limit: int = 3) -> dict:
    """Drive the council from the most-recent wrong-direction-flagged trades (the richest evidence):
    run the council on each, registering+evaluating any grounded, reviewer-accepted proposals."""
    summary = {"ran": 0, "registered": 0, "verdicts": {}}
    try:
        ids = list(r.zrevrange("scibrain:wrong_direction_trades", 0, max(0, int(limit) - 1)) or [])
        for tid in ids:
            tid = tid.decode() if isinstance(tid, bytes) else tid
            res = run_council(r, tid)
            summary["ran"] += 1
            v = res.get("verdict", "error")
            summary["verdicts"][v] = summary["verdicts"].get(v, 0) + 1
            if res.get("registered"):
                summary["registered"] += 1
    except Exception as exc:
        log.warning("scibrain_council_drive_error", error=str(exc)[:160])
    if summary["ran"]:
        log.info("scibrain_council_drive", **summary)
    return summary
