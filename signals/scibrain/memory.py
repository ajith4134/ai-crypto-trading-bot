"""SciBrain Phase 7b — hypothesis memory so failed ideas are not repeated (design §10 / §8).

The registry dedups EXACT ChangeSpecs (same knob + context + candidate). But "boost wavelet in
mean_revert to 1.5" failing should also discourage "boost wavelet in mean_revert to 1.4" — the same
DIRECTION of change on the same knob. This module keeps that generalized memory:

  negative memory — knob+direction changes that were rejected / demoted / rolled back, with reasons,
                    so the council can refuse to re-propose them (§8: "duplicates a rejected hypothesis").
  positive memory — knob+direction changes that were RETAINED (validated conditional laws).

Keyed by a GENERALIZED signature: (target, increase|decrease|zero) relative to the knob's current
value. Pure Redis; Tier-0 (records evidence about hypotheses, changes nothing live). Never raises.
"""
from __future__ import annotations

import json
import time

import structlog

from . import changespec as cs
from . import keys as K

log = structlog.get_logger()

_NEG_STATUSES = ("rejected", "demoted", "rolled_back")
_POS_STATUSES = ("retained",)
_MAX_REASONS = 5
_MAX_IDS = 10


def signature(target: str, candidate, current) -> str | None:
    """Generalized memory key: '<target>|<increase|decrease|zero>' relative to the knob's current
    value. None if the candidate isn't numeric (nothing to remember)."""
    try:
        cand = float(candidate)
        cur = float(current)
    except (TypeError, ValueError):
        return None
    if abs(cand) < 1e-9:
        direction = "zero"
    elif cand > cur + 1e-9:
        direction = "increase"
    elif cand < cur - 1e-9:
        direction = "decrease"
    else:
        direction = "noop"
    return f"{target}|{direction}"


def _sig_for_spec(spec: dict) -> str | None:
    target = spec.get("target", "")
    res = cs.resolve_target(target)
    if not res.get("valid"):
        return None
    candidate = (spec.get("intervention") or {}).get("candidate")
    return signature(target, candidate, res.get("current"))


def record_terminal(r, spec: dict, status: str, reason: str = "") -> None:
    """Record a terminal-state hypothesis into the negative or positive bank under its generalized
    signature. No-op for non-terminal statuses. Idempotent-ish: accumulates count + recent reasons."""
    try:
        bank = (K.MEMORY_NEG if status in _NEG_STATUSES else
                K.MEMORY_POS if status in _POS_STATUSES else None)
        if bank is None:
            return
        sig = _sig_for_spec(spec)
        if sig is None:
            return
        existing = r.hget(bank, sig)
        rec = json.loads(existing) if existing else {
            "signature": sig, "target": spec.get("target"), "n": 0,
            "reasons": [], "hypothesis_ids": [], "first_ts": round(time.time(), 3)}
        rec["n"] = int(rec.get("n", 0)) + 1
        hid = spec.get("hypothesis_id")
        if hid and hid not in rec["hypothesis_ids"]:
            rec["hypothesis_ids"] = ([hid] + rec["hypothesis_ids"])[:_MAX_IDS]
        if reason:
            rec["reasons"] = ([f"{status}: {reason[:120]}"] + rec.get("reasons", []))[:_MAX_REASONS]
        rec["last_status"] = status
        rec["last_ts"] = round(time.time(), 3)
        r.hset(bank, sig, json.dumps(rec))
        memory_summary(r)
        log.info("scibrain_memory_recorded", bank=("neg" if bank == K.MEMORY_NEG else "pos"),
                 signature=sig, n=rec["n"])
    except Exception as exc:
        log.warning("scibrain_memory_record_failed", error=str(exc)[:140])


def recall_negative(r, target: str, candidate, current) -> dict | None:
    """The negative-memory record for this knob+direction change, or None. The council consults this
    to refuse re-proposing a previously-failed direction of change (not just an exact duplicate)."""
    try:
        sig = signature(target, candidate, current)
        if sig is None:
            return None
        raw = r.hget(K.MEMORY_NEG, sig)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def memory_summary(r) -> dict:
    """Counts + the most-repeated negative signatures, for the dashboard. Published to MEMORY_SUMMARY."""
    summary = {"n_negative": 0, "n_positive": 0, "top_negative": [], "positive": [],
               "updated_ts": round(time.time(), 3),
               "note": "generalized hypothesis memory (knob+direction): negative = rejected/demoted/"
                       "rolled-back changes the council must not repeat; positive = retained laws."}
    try:
        neg = r.hgetall(K.MEMORY_NEG) or {}
        pos = r.hgetall(K.MEMORY_POS) or {}
        nrecs = []
        for _s, raw in neg.items():
            try:
                nrecs.append(json.loads(raw))
            except (TypeError, ValueError):
                continue
        nrecs.sort(key=lambda x: x.get("n", 0), reverse=True)
        summary["n_negative"] = len(nrecs)
        summary["top_negative"] = [{"signature": x.get("signature"), "n": x.get("n"),
                                    "last_reason": (x.get("reasons") or [None])[0]} for x in nrecs[:10]]
        precs = []
        for _s, raw in pos.items():
            try:
                precs.append(json.loads(raw))
            except (TypeError, ValueError):
                continue
        summary["n_positive"] = len(precs)
        summary["positive"] = [{"signature": x.get("signature"), "n": x.get("n")} for x in precs[:10]]
        r.set(K.MEMORY_SUMMARY, json.dumps(summary))
    except Exception as exc:
        log.warning("scibrain_memory_summary_failed", error=str(exc)[:140])
    return summary
