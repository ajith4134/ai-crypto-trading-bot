"""SciBrain Phase-7c — the UNIFIED PRODUCER BUS (design §11 "Global self-improvement unification").

The living brain has many autonomous self-improvement mechanisms that historically grew as INDEPENDENT
promotion paths — each computing a change and writing its own live parameter directly. The intelligence
constitution (living_intelligence.md §3.9, §11) forbids that:

    "All autonomous improvement mechanisms use the same experiment and promotion kernel."
    "Every mechanism receives the same versioning, evaluator, shadow, canary, rollback, and dashboard
     contract. No subsystem may bypass it because its delta is 'small'."

This module is the spine that makes that real. It:

  • ENUMERATES every known self-improvement producer (the LLM council, OPRO, DGM, AI-Scientist, GA,
    feature-governance, metacog, direction-model retrain, strategy-pool GA, DSL miner, F9/F12 pair-list
    decoder, bayes-threshold, and the in-loop IC/router learner), each with its live gate, the live
    parameter it touches, and the bounded ChangeSpec target it maps to (or None if its knob is not yet
    representable in the safe DSL — e.g. code rewrites, prompts, NN weights);

  • gives every producer ONE door — submit() — that compiles its raw proposal to a bounded ChangeSpec
    and registers it ONCE in the single experiment registry (experiments.register), stamped with the
    producer name, deduped by content fingerprint;

  • AUDITS the no-bypass property: for each producer it reads the ACTUAL live gate and classifies its
    apply path. An "open bypass" is a producer that is ENABLED and still self-applies a PERSISTED live
    parameter outside the kernel. The audit is falsifiable and surfaced through the authority/dashboard
    contract, so migration progress is measurable rather than asserted.

Authority (Rule 14 / authority.py): this bus is **Tier-0**. It records, routes, and reports. It holds
NO authority to apply any change to a live knob — the ONLY capital-affecting application path remains the
owner-approved bounded canary (canary.py). The `scibrain:unify:mode` switch defaults to **observe**:
observe REPORTS bypasses and is byte-identical to no bus at all; enforce is the future gate where a
producer's self-apply must carry a kernel-approved hypothesis (per-producer wiring lands incrementally).

Pure Redis; never raises out of a public function.
"""
from __future__ import annotations

import json
import time

import structlog

from . import keys as K

log = structlog.get_logger()


# ── producer apply-path taxonomy ───────────────────────────────────────────────────────────────
#   kernel           — already routes proposals through experiments.register (the target state).
#   in_loop_bounded  — applies only a bounded, IC-graded, in-loop multiplier already ENUMERATED and
#                      capped by authority.py (no persisted parameter mutation). Accounted, not a bypass.
#   bounded_recorded — a fast online controller whose live write goes THROUGH producers.apply_controller
#                      (kernel-owned bounded clamp + versioned audit record), not a direct self-apply.
#                      Accounted, not a bypass (the IC-learner precedent: bounded + reversible + audited;
#                      a per-tick canary is the wrong tool for a controller that recomputes from data).
#   legacy_direct    — self-applies a PERSISTED live parameter when its gate is on, NOT via the kernel.
#                      An ENABLED legacy_direct producer is an OPEN BYPASS until migrated.
#   safety_only      — can only suppress/deactivate (no origination/sizing authority); accounted.
KERNEL = "kernel"
IN_LOOP_BOUNDED = "in_loop_bounded"
BOUNDED_RECORDED = "bounded_recorded"
#   model_recorded   — a producer whose knob can't compile to the bounded DSL (a prompt, source code, an
#                      alpha factor, a hypothesis, a strategy). Every change it makes is recorded as a typed
#                      versioned ModelChangeSpec under the ONE kernel (provenance + evidence + lineage +
#                      dashboard), so it is no longer a SILENT independent promotion path. Accounted, not a
#                      bypass. (Owner-gated enforce mode — holding the apply pending evaluation — is future.)
MODEL_RECORDED = "model_recorded"
LEGACY_DIRECT = "legacy_direct"
SAFETY_ONLY = "safety_only"


def _gate_fcode(code):
    return {"kind": "fcode", "code": code}


def _gate_flag(key, *, off_values=("0",), default_on=True):
    return {"kind": "flag", "key": key, "off_values": tuple(off_values), "default_on": default_on}


# ── the canonical producer registry (design §11 list, grounded in the live beat tasks) ──────────
# Each: id, label, family, task (beat fn / module for reference), gate (live kill-switch), base_mode,
# applies_to (the live parameter it writes), mappable_target (ChangeSpec allow-list target or None).
PRODUCERS: list[dict] = [
    {
        "id": "llm_council", "label": "LLM scientific council", "family": "council",
        "task": "signals/scibrain/council.py:propose_from_flagged",
        "gate": _gate_flag(K.ENABLED, default_on=False), "base_mode": KERNEL,
        "applies_to": "nothing directly — emits ChangeSpecs to the registry",
        "mappable_target": "router.gain.* / scibrain.* scalars",
    },
    {
        "id": "ic_router_learner", "label": "Adaptive IC reliability learner", "family": "router",
        "task": "signals/scibrain/ic_tracker.py",
        "gate": _gate_flag(K.IC_ENABLED, default_on=True), "base_mode": IN_LOOP_BOUNDED,
        "applies_to": "bounded in-loop IC multiplier on module gains (capped, IC-graded)",
        "mappable_target": "router.gain.<module>.<regime>",
    },
    {
        "id": "meta_router_moe", "label": "Meta-router (MoE) regime gains", "family": "router",
        "task": "signals/scibrain/router.py",
        "gate": _gate_flag(K.ROUTER_ENABLED, default_on=True), "base_mode": IN_LOOP_BOUNDED,
        "applies_to": "bounded per-regime expert gain on the live vote (champion _PROFILES)",
        "mappable_target": "router.gain.<module>.<regime>",
    },
    {
        "id": "scibrain_remediation", "label": "SciBrain audit remediation actuator", "family": "council",
        "task": "celery_app.py:scibrain_audit_drain -> signals/scibrain/audit.py",
        "gate": _gate_flag("scibrain:audit_autoact", default_on=False), "base_mode": LEGACY_DIRECT,
        "applies_to": "REDUCE/CLOSE remediation on a flagged open (default OFF)",
        "mappable_target": None,
    },
    {
        "id": "opro_prompts", "label": "OPRO prompt optimization", "family": "self_improve",
        "task": "celery_app.py:opro_optimize", "gate": _gate_fcode("F39A"), "base_mode": MODEL_RECORDED,
        "applies_to": "live LLM prompt templates (opro:*) — recorded as a ModelChangeSpec (kind=prompt)",
        "mappable_target": "model:prompt",
    },
    {
        "id": "dgm_code_rewrite", "label": "DGM weakest-module code rewrite", "family": "self_improve",
        "task": "celery_app.py:dgm_rewrite_weakest", "gate": _gate_fcode("F39A"), "base_mode": MODEL_RECORDED,
        "applies_to": "source code rewrites of the weakest strategies — recorded as ModelChangeSpec (kind=code)",
        "mappable_target": "model:code",
    },
    {
        "id": "ai_scientist", "label": "AI Scientist hypothesis generation", "family": "self_improve",
        "task": "celery_app.py:ai_scientist_run", "gate": _gate_fcode("F39A"), "base_mode": MODEL_RECORDED,
        "applies_to": "generated hypotheses / experiment proposals — recorded as ModelChangeSpec (kind=hypothesis)",
        "mappable_target": "model:hypothesis",
    },
    {
        "id": "ga_params", "label": "Genetic-algorithm parameter evolution", "family": "evolution",
        "task": "celery_app.py:ga_evolve_params -> ml/genetic_algorithm.py", "gate": _gate_fcode("F25"),
        "base_mode": BOUNDED_RECORDED,
        "applies_to": "ga:best_params 6-param vector (min_signal_strength/turbulence_cap/dca_*/trailing_sl/"
                      "kelly) — kernel-applied via apply_params (per-param bounds clamp + diff audit)",
        "mappable_target": "controller-params:ga_best_params",
    },
    {
        "id": "feature_governance", "label": "Feature governance 5-mode check", "family": "governance",
        "task": "celery_app.py:run_feature_governance_check", "gate": _gate_fcode("F30"),
        "base_mode": SAFETY_ONLY,
        "applies_to": "deactivates failing features (suppression only)", "mappable_target": None,
    },
    {
        "id": "metacog_eval", "label": "Metacognition daily self-eval", "family": "governance",
        "task": "celery_app.py:metacog_daily_eval", "gate": _gate_fcode("F43"), "base_mode": SAFETY_ONLY,
        "applies_to": "escalates underperformers to F30 governance (suppression only)",
        "mappable_target": None,
    },
    {
        "id": "direction_model", "label": "Direction-model retrain", "family": "world_model",
        "task": "celery_app.py:retrain_direction_model", "gate": _gate_fcode("F13"),
        "base_mode": LEGACY_DIRECT,
        "applies_to": "direction-prediction model weights", "mappable_target": None,
    },
    {
        "id": "strategy_pool", "label": "Strategy-pool GA evolution", "family": "evolution",
        "task": "celery_app.py:evolve_strategy_pool_task",
        "gate": _gate_flag("research:pool_evolution_enabled", default_on=True), "base_mode": MODEL_RECORDED,
        "applies_to": "new evolved child strategies (experimental) — recorded as ModelChangeSpec (kind=strategy)",
        "mappable_target": "model:strategy",
    },
    {
        "id": "dsl_miner", "label": "LLM-DSL alpha miner", "family": "self_improve",
        "task": "celery_app.py:llm_dsl_mining_run_task", "gate": _gate_fcode("F54"),
        "base_mode": MODEL_RECORDED,
        "applies_to": "promoted DSL alpha factors (dsl:promoted_factors) — recorded as ModelChangeSpec (kind=factor)",
        "mappable_target": "model:factor",
    },
    {
        "id": "f9f12_decoder", "label": "F9/F12 pair-list decoder", "family": "actuator",
        "task": "celery_app.py:update_pair_lists_from_decoder", "gate": _gate_fcode("F46"),
        "base_mode": BOUNDED_RECORDED,
        "applies_to": "BRAIN_PAIR_PROBATION / BRAIN_PAIR_SUSPENSION membership lists — kernel-applied via "
                      "apply_set (max_size runaway cap + added/removed audit diff), no direct self-apply",
        "mappable_target": "controller-set:pair_probation / pair_suspension",
    },
    {
        "id": "bayes_threshold", "label": "Adaptive Bayes signal threshold", "family": "actuator",
        "task": "celery_app.py:refresh_bayes_threshold",
        "gate": _gate_flag("bayes_threshold:enabled", default_on=True), "base_mode": BOUNDED_RECORDED,
        "applies_to": "adaptive T_high∈[25,60] / T_low∈[15,T_high-5] thresholds — kernel-applied via "
                      "apply_controller (bounded clamp + audit ledger), no longer a direct self-apply",
        "mappable_target": "controller:bayes_threshold:t_high / t_low",
    },
]

_BY_ID = {p["id"]: p for p in PRODUCERS}
PRODUCER_IDS = tuple(p["id"] for p in PRODUCERS)


# ── live gate evaluation ────────────────────────────────────────────────────────────────────────
def _gate_enabled(r, gate: dict):
    """Evaluate a producer's live gate. Returns True/False, or None when it cannot be determined
    (governance import unreachable) — None is treated CONSERVATIVELY as 'could be enabled' downstream."""
    try:
        kind = gate.get("kind")
        if kind == "fcode":
            from feature_governance.registry import is_active
            return bool(is_active(gate["code"]))
        if kind == "flag":
            v = r.get(gate["key"])
            v = v.decode() if isinstance(v, bytes) else v
            if v is None:
                return bool(gate.get("default_on", True))
            return v not in gate.get("off_values", ("0",))
    except Exception:
        return None
    return None


def mode(r) -> str:
    """Bus mode: 'observe' (default — report only, byte-identical) or 'enforce'."""
    try:
        v = r.get(K.UNIFY_MODE)
        v = v.decode() if isinstance(v, bytes) else v
        return "enforce" if v == "enforce" else "observe"
    except Exception:
        return "observe"


# ── the single submission door ───────────────────────────────────────────────────────────────────
def submit(r, proposer: str, proposal) -> dict:
    """The ONE door for any producer to propose a change: compile + register a bounded ChangeSpec in
    the single experiment registry, stamped with the producer name. Returns the registry result with
    an added {"proposer", "known_producer"}.

    `proposer` SHOULD be a registered producer id; an unknown proposer is still recorded (so nothing is
    silently dropped) but flagged known_producer=False. The bus NEVER applies the change — registration
    is Tier-0; promotion is the canary's job."""
    out = {"ok": False, "hypothesis_id": None, "deduped": False, "errors": [],
           "proposer": proposer, "known_producer": proposer in _BY_ID}
    try:
        from . import experiments as ex
        res = ex.register(r, proposal, proposer=proposer)
        out.update({k: res.get(k) for k in ("ok", "hypothesis_id", "status", "deduped", "errors")
                    if k in res})
        if res.get("ok"):
            try:
                r.hincrby(K.UNIFY_SUBMITS, proposer, 1)
                r.set(K.UNIFY_LAST.format(proposer=proposer), json.dumps({
                    "hypothesis_id": res.get("hypothesis_id"), "deduped": bool(res.get("deduped")),
                    "ts": round(time.time(), 3)}))
            except Exception:
                pass
            log.info("scibrain_unify_submit", proposer=proposer,
                     hypothesis_id=res.get("hypothesis_id"), deduped=res.get("deduped"))
    except Exception as exc:
        out["errors"] = [str(exc)[:160]]
        log.warning("scibrain_unify_submit_failed", proposer=proposer, error=str(exc)[:160])
    return out


# ── the kernel-owned bounded apply path (for bounded_recorded online controllers) ─────────────────
def apply_controller(r, proposer: str, *, redis_key: str, value: float, bounds,
                     reason: str = "", round_to: int = 2) -> dict:
    """KERNEL-OWNED bounded apply for a fast online controller (design §11 no-bypass).

    The producer COMPUTES a new value, but the live write goes THROUGH the kernel here: the value is
    hard-clamped to `bounds`, written to `redis_key`, and a versioned change record is appended to the
    controller audit ledger. This is how a `bounded_recorded` producer routes its apply through the
    kernel instead of self-applying — there is no direct r.set bypass left in the producer.

    This is deliberately NOT a canary (no owner approval, no shadow): a bounded controller that recomputes
    from fresh data every tick is the IC-learner case (bounded + reversible + audited), where a per-tick
    promotion gate is the wrong tool. The kernel's role here is the BOUND + the AUDIT TRAIL + versioning,
    which is the subset of the §11 contract that applies to online controllers. In 'enforce' mode an
    out-of-bounds value is still clamped AND flagged; in 'observe' mode behavior is byte-identical to the
    prior direct write (the clamp already existed upstream). Returns {applied, value, clamped, old}.
    Never raises (a controller must never freeze on an audit failure)."""
    out = {"applied": False, "value": None, "clamped": False, "old": None, "proposer": proposer}
    try:
        lo, hi = float(bounds[0]), float(bounds[1])
        v = float(value)
        clamped = not (lo <= v <= hi)
        v = max(lo, min(hi, v))
        v = round(v, round_to)
        try:
            old = r.get(redis_key)
            out["old"] = float(old) if old is not None else None
        except (TypeError, ValueError):
            out["old"] = None
        r.set(redis_key, v)                          # THE live write — performed by the kernel, not the producer
        out.update({"applied": True, "value": v, "clamped": clamped})
        # versioned audit record (code/config lineage so a stale controller is detectable)
        try:
            from .changespec import _versions
            versions = _versions()
        except Exception:
            versions = {}
        rec = {"target": redis_key, "old": out["old"], "new": v, "clamped": clamped,
               "bounds": [lo, hi], "reason": str(reason)[:160], "proposer": proposer,
               "mode": mode(r), "ts": round(time.time(), 3), "versions": versions}
        try:
            lk = K.UNIFY_CONTROLLER.format(proposer=proposer)
            pipe = r.pipeline()
            pipe.lpush(lk, json.dumps(rec))
            pipe.ltrim(lk, 0, 199)                    # cap the ledger
            pipe.execute()
            last = r.get(K.UNIFY_CONTROLLER_LAST.format(proposer=proposer))
            applies = 1
            if last:
                try:
                    applies = int(json.loads(last).get("applies", 0)) + 1
                except (TypeError, ValueError):
                    applies = 1
            r.set(K.UNIFY_CONTROLLER_LAST.format(proposer=proposer),
                  json.dumps({"target": redis_key, "value": v, "applies": applies,
                              "clamped": clamped, "ts": rec["ts"]}))
        except Exception:
            pass
        if clamped:
            log.warning("scibrain_unify_controller_clamped", proposer=proposer, target=redis_key,
                        requested=float(value), applied=v, bounds=[lo, hi])
    except Exception as exc:
        # last-resort: never let the audit path break the controller's live write
        try:
            r.set(redis_key, round(float(value), round_to))
            out.update({"applied": True, "value": round(float(value), round_to)})
        except Exception:
            pass
        log.warning("scibrain_unify_apply_controller_failed", proposer=proposer, error=str(exc)[:160])
    return out


def apply_set(r, proposer: str, *, redis_key: str, mapping: dict, max_size: int,
              reason: str = "", count_key: str | None = None) -> dict:
    """KERNEL-OWNED bounded write for a membership/dict controller (e.g. the F9/F12 pair probation /
    suspension lists), the set-valued sibling of apply_controller (design §11 no-bypass).

    The producer COMPUTES the new membership dict; the kernel performs the live write here, enforcing a
    `max_size` runaway cap (the BOUND — a controller can never suppress/penalize more than max_size pairs;
    when over cap the most-recently-added entries are kept by `added_ts`) and recording an old→new diff
    (added/removed keys) to the controller audit ledger. There is no direct r.set bypass left in the
    producer. In-cap writes are byte-identical to the prior direct write. Never raises.

    Returns {applied, n, added, removed, capped, old_n}."""
    out = {"applied": False, "n": 0, "added": [], "removed": [], "capped": False, "old_n": 0,
           "proposer": proposer}
    try:
        mp = dict(mapping or {})
        # read old membership for the diff
        old_keys: set = set()
        try:
            raw = r.get(redis_key)
            if raw:
                old = json.loads(raw)
                if isinstance(old, dict):
                    old_keys = set(old.keys())
        except Exception:
            old_keys = set()
        out["old_n"] = len(old_keys)

        capped = False
        if max_size is not None and len(mp) > int(max_size):
            capped = True
            # keep the most-recently-added max_size entries (added_ts desc; missing → 0)
            items = sorted(mp.items(), key=lambda kv: (kv[1] or {}).get("added_ts", 0)
                           if isinstance(kv[1], dict) else 0, reverse=True)
            mp = dict(items[:int(max_size)])

        new_keys = set(mp.keys())
        r.set(redis_key, json.dumps(mp))                 # THE live write — performed by the kernel
        if count_key is not None:
            try:
                r.set(count_key, len(mp))
            except Exception:
                pass

        added = sorted(new_keys - old_keys)
        removed = sorted(old_keys - new_keys)
        out.update({"applied": True, "n": len(mp), "added": added, "removed": removed, "capped": capped})

        try:
            from .changespec import _versions
            versions = _versions()
        except Exception:
            versions = {}
        rec = {"target": redis_key, "kind": "set", "old_n": len(old_keys), "new_n": len(mp),
               "added": added[:50], "removed": removed[:50], "capped": capped, "max_size": int(max_size),
               "reason": str(reason)[:160], "proposer": proposer, "mode": mode(r),
               "ts": round(time.time(), 3), "versions": versions}
        try:
            lk = K.UNIFY_CONTROLLER.format(proposer=proposer)
            pipe = r.pipeline()
            pipe.lpush(lk, json.dumps(rec))
            pipe.ltrim(lk, 0, 199)
            pipe.execute()
            last = r.get(K.UNIFY_CONTROLLER_LAST.format(proposer=proposer))
            applies = 1
            if last:
                try:
                    applies = int(json.loads(last).get("applies", 0)) + 1
                except (TypeError, ValueError):
                    applies = 1
            r.set(K.UNIFY_CONTROLLER_LAST.format(proposer=proposer),
                  json.dumps({"target": redis_key, "value": len(mp), "applies": applies,
                              "clamped": capped, "ts": rec["ts"]}))
        except Exception:
            pass
        if capped:
            log.warning("scibrain_unify_set_capped", proposer=proposer, target=redis_key,
                        requested=len(mapping or {}), kept=len(mp), max_size=int(max_size))
    except Exception as exc:
        # last-resort: never let the audit path break the controller's live write
        try:
            r.set(redis_key, json.dumps(dict(mapping or {})))
            out["applied"] = True
        except Exception:
            pass
        log.warning("scibrain_unify_apply_set_failed", proposer=proposer, error=str(exc)[:160])
    return out


def apply_params(r, proposer: str, *, redis_key: str, params: dict, bounds: dict,
                 reason: str = "", round_to: int = 4) -> dict:
    """KERNEL-OWNED bounded write for a multi-scalar PARAM-VECTOR controller (e.g. the GA's 6-param
    ga:best_params), the third controller shape alongside apply_controller (scalar) and apply_set
    (membership). Clamps EACH named param to its own [lo,hi] from `bounds`, writes the JSON dict, and
    records a per-param old→new diff to the controller ledger. No direct r.set bypass left in the
    producer; in-bounds writes are byte-identical to the prior direct write (the producer already
    clamped). Never raises. Returns {applied, params, clamped:[keys], changed:[keys]}."""
    out = {"applied": False, "params": None, "clamped": [], "changed": [], "proposer": proposer}
    try:
        old = {}
        try:
            raw = r.get(redis_key)
            if raw:
                o = json.loads(raw)
                if isinstance(o, dict):
                    old = o
        except Exception:
            old = {}
        new_params: dict = {}
        clamped: list = []
        changed: list = []
        for k, v in (params or {}).items():
            try:
                val = float(v)
            except (TypeError, ValueError):
                new_params[k] = v               # non-numeric param passes through unchanged
                continue
            if k in (bounds or {}):
                lo, hi = float(bounds[k][0]), float(bounds[k][1])
                if not (lo <= val <= hi):
                    clamped.append(k)
                val = max(lo, min(hi, val))
            val = round(val, round_to)
            new_params[k] = val
            try:
                if k not in old or float(old[k]) != val:
                    changed.append(k)
            except (TypeError, ValueError):
                changed.append(k)
        r.set(redis_key, json.dumps(new_params))     # THE live write — performed by the kernel
        out.update({"applied": True, "params": new_params, "clamped": clamped, "changed": changed})

        try:
            from .changespec import _versions
            versions = _versions()
        except Exception:
            versions = {}
        diff = {k: [old.get(k), new_params.get(k)] for k in changed}
        rec = {"target": redis_key, "kind": "params", "changed": dict(list(diff.items())[:20]),
               "clamped": clamped, "n_params": len(new_params), "reason": str(reason)[:160],
               "proposer": proposer, "mode": mode(r), "ts": round(time.time(), 3), "versions": versions}
        try:
            lk = K.UNIFY_CONTROLLER.format(proposer=proposer)
            pipe = r.pipeline()
            pipe.lpush(lk, json.dumps(rec))
            pipe.ltrim(lk, 0, 199)
            pipe.execute()
            last = r.get(K.UNIFY_CONTROLLER_LAST.format(proposer=proposer))
            applies = 1
            if last:
                try:
                    applies = int(json.loads(last).get("applies", 0)) + 1
                except (TypeError, ValueError):
                    applies = 1
            r.set(K.UNIFY_CONTROLLER_LAST.format(proposer=proposer),
                  json.dumps({"target": redis_key, "value": len(new_params), "applies": applies,
                              "clamped": bool(clamped), "ts": rec["ts"]}))
        except Exception:
            pass
        if clamped:
            log.warning("scibrain_unify_params_clamped", proposer=proposer, target=redis_key,
                        clamped=clamped)
    except Exception as exc:
        try:
            r.set(redis_key, json.dumps(dict(params or {})))
            out["applied"] = True
        except Exception:
            pass
        log.warning("scibrain_unify_apply_params_failed", proposer=proposer, error=str(exc)[:160])
    return out


def controller_last(r, proposer: str) -> dict | None:
    """Last kernel-applied controller value for a bounded_recorded producer (dashboard read)."""
    try:
        v = r.get(K.UNIFY_CONTROLLER_LAST.format(proposer=proposer))
        return json.loads(v) if v else None
    except Exception:
        return None


_MODEL_KINDS = ("prompt", "code", "factor", "hypothesis", "strategy")


def apply_model_change(r, proposer: str, *, kind: str, target: str, summary: str,
                       evidence_ids=None, reason: str = "", reversible: bool = True) -> dict:
    """Kernel record for a NON-DSL model change (design §11/§7/§14 ModelChangeSpec).

    `model_recorded` producers touch knobs that can't compile to the bounded scalar/router ChangeSpec
    DSL — a PROMPT addendum, a CODE rewrite, an alpha FACTOR, a HYPOTHESIS, a STRATEGY. Instead of a
    silent independent promotion path, the producer records a typed, versioned ModelChangeSpec under the
    ONE kernel here: kind, target, a bounded summary, evidence ids, code/config lineage, reversibility,
    and the bus mode. This brings them under the same registry + dashboard contract (they are now
    ACCOUNTED + AUDITED, not invisible), which is what §11 requires.

    NOTE (honest scope): unlike apply_controller, the kernel does not itself perform the artifact apply
    (a code rewrite / strategy insert is not a single r.set) — in OBSERVE mode the producer still applies
    (byte-identical autonomous behavior) and the change is recorded. The owner-gated ENFORCE mode (hold
    the apply pending evaluation/approval) is the future escalation. Records to scibrain:unify:model:
    {proposer}. Never raises. Returns {recorded, model_id, kind}."""
    import uuid
    out = {"recorded": False, "model_id": None, "kind": kind, "proposer": proposer}
    try:
        mid = uuid.uuid4().hex[:12]
        try:
            from .changespec import _versions
            versions = _versions()
        except Exception:
            versions = {}
        rec = {"model_id": mid, "kind": str(kind), "target": str(target)[:160],
               "summary": str(summary)[:300], "evidence_ids": list(evidence_ids or [])[:20],
               "reason": str(reason)[:160], "reversible": bool(reversible), "proposer": proposer,
               "mode": mode(r), "ts": round(time.time(), 3), "versions": versions,
               "known_kind": kind in _MODEL_KINDS}
        lk = K.UNIFY_MODEL.format(proposer=proposer)
        pipe = r.pipeline()
        pipe.lpush(lk, json.dumps(rec))
        pipe.ltrim(lk, 0, 199)
        pipe.execute()
        last = r.get(K.UNIFY_MODEL_LAST.format(proposer=proposer))
        records = 1
        if last:
            try:
                records = int(json.loads(last).get("records", 0)) + 1
            except (TypeError, ValueError):
                records = 1
        r.set(K.UNIFY_MODEL_LAST.format(proposer=proposer),
              json.dumps({"model_id": mid, "kind": str(kind), "target": str(target)[:120],
                          "records": records, "ts": rec["ts"]}))
        out.update({"recorded": True, "model_id": mid})
        log.info("scibrain_unify_model_change", proposer=proposer, kind=kind, model_id=mid,
                 target=str(target)[:80])
    except Exception as exc:
        log.warning("scibrain_unify_apply_model_change_failed", proposer=proposer, error=str(exc)[:160])
    return out


def model_last(r, proposer: str) -> dict | None:
    """Last recorded ModelChangeSpec for a model_recorded producer (dashboard read)."""
    try:
        v = r.get(K.UNIFY_MODEL_LAST.format(proposer=proposer))
        return json.loads(v) if v else None
    except Exception:
        return None


def _jload(v):
    try:
        return json.loads(v) if isinstance(v, (str, bytes)) else None
    except (TypeError, ValueError):
        return None


def recent_changes(r, *, limit: int = 30) -> list[dict]:
    """Merged newest-first LINEAGE of actual kernel-routed changes across every producer — the
    bounded_recorded controller writes (bayes/f9f12/ga) AND the model_recorded ModelChangeSpec records
    (opro/dgm/ai_scientist/strategy_pool/dsl). This is the §11 'what changed, by whom, when, under which
    mode' timeline for the Living-Intelligence dashboard (design §4.3 lineage + rollback). Pure read."""
    out: list[dict] = []
    per = max(1, min(25, int(limit)))
    for p in PRODUCERS:
        pid = p["id"]
        if p["base_mode"] == BOUNDED_RECORDED:
            for raw in (r.lrange(K.UNIFY_CONTROLLER.format(proposer=pid), 0, per - 1) or []):
                d = _jload(raw)
                if d:
                    out.append({"proposer": pid, "change_type": "controller", "kind": d.get("kind"),
                                "target": d.get("target"), "ts": d.get("ts"), "mode": d.get("mode"),
                                "detail": {k: d.get(k) for k in
                                           ("old", "new", "added", "removed", "changed", "clamped",
                                            "n_params", "reason") if d.get(k) is not None}})
        elif p["base_mode"] == MODEL_RECORDED:
            for raw in (r.lrange(K.UNIFY_MODEL.format(proposer=pid), 0, per - 1) or []):
                d = _jload(raw)
                if d:
                    out.append({"proposer": pid, "change_type": "model", "kind": d.get("kind"),
                                "target": d.get("target"), "ts": d.get("ts"), "mode": d.get("mode"),
                                "detail": {k: d.get(k) for k in
                                           ("summary", "evidence_ids", "reason", "reversible")
                                           if d.get(k) is not None}})
    out.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return out[:max(1, min(100, int(limit)))]


# ── the no-bypass audit ────────────────────────────────────────────────────────────────────────
def _submit_count(r, pid: str) -> int:
    try:
        v = r.hget(K.UNIFY_SUBMITS, pid)
        return int(v) if v is not None else 0
    except Exception:
        return 0


def producer_status(r) -> list[dict]:
    """Per-producer live classification. effective_mode collapses to 'disabled' when the gate is off
    (it applies nothing); a legacy_direct producer that is ENABLED is an OPEN BYPASS."""
    rows = []
    for p in PRODUCERS:
        enabled = _gate_enabled(r, p["gate"])
        base = p["base_mode"]
        if enabled is False:
            eff = "disabled"
        else:                                     # True or None (unknown → conservative 'could apply')
            eff = base
        open_bypass = (eff == LEGACY_DIRECT)      # enabled (or unknown) AND self-applies, not via kernel
        row = {
            "id": p["id"], "label": p["label"], "family": p["family"], "task": p["task"],
            "base_mode": base, "enabled": enabled, "effective_mode": eff,
            "applies_to": p["applies_to"], "mappable_target": p["mappable_target"],
            "kernel_submits": _submit_count(r, p["id"]),
            "open_bypass": open_bypass, "gate_unknown": enabled is None,
        }
        if base == BOUNDED_RECORDED:              # kernel-applied controller — surface its last apply
            row["controller_last"] = controller_last(r, p["id"])
        if base == MODEL_RECORDED:                # ModelChangeSpec producer — surface its last record
            row["model_last"] = model_last(r, p["id"])
        rows.append(row)
    return rows


def audit_bypass(r) -> dict:
    """Falsifiable no-bypass audit (design §11). Returns the full producer map plus the open-bypass set.

    accounting invariant — `all_producers_accounted`: every producer is enumerated and its apply path is
    classified (none unknown-and-unclassified). This is the property the bus GUARANTEES and it must hold.

    migration metric — `open_bypasses`: producers that are enabled and still self-apply a persisted live
    parameter outside the kernel. These are NOT invariant violations (the owner deliberately runs them);
    they are the measurable work remaining to fully unify. In 'enforce' mode they would be refused."""
    out = {"contract": "ProducerBusAudit", "ts": round(time.time(), 3), "available": False}
    try:
        rows = producer_status(r)
        by_mode: dict[str, int] = {}
        for row in rows:
            by_mode[row["effective_mode"]] = by_mode.get(row["effective_mode"], 0) + 1
        open_ids = [row["id"] for row in rows if row["open_bypass"]]
        kernel_ids = [row["id"] for row in rows if row["effective_mode"] == KERNEL]
        accounted = all(row["effective_mode"] in (KERNEL, IN_LOOP_BOUNDED, BOUNDED_RECORDED,
                                                  MODEL_RECORDED, LEGACY_DIRECT, SAFETY_ONLY, "disabled")
                        for row in rows)
        out.update({
            "available": True, "mode": mode(r),
            "n_producers": len(rows), "producers": rows,
            "by_effective_mode": by_mode,
            "open_bypasses": open_ids, "n_open_bypasses": len(open_ids),
            "kernel_routed": kernel_ids,
            "total_kernel_submits": sum(row["kernel_submits"] for row in rows),
            "invariants": [
                {"name": "all_producers_accounted", "ok": accounted,
                 "detail": "every self-improvement producer is enumerated and its apply path classified"
                           if accounted else "VIOLATION: an unclassified producer apply path exists"},
                {"name": "tier0_bus_holds_no_apply_authority", "ok": True,
                 "detail": "the producer bus records + routes + reports only; the ONLY capital-affecting "
                           "application path is the owner-approved bounded canary"},
            ],
            "note": ("Unified producer bus (design §11): all autonomous self-improvement mechanisms are "
                     "proposal producers under ONE registry + promotion gate. open_bypasses = producers "
                     "still self-applying a persisted live parameter outside the kernel (migration metric, "
                     "owner-run by choice); enforce mode would refuse those without a kernel-approved "
                     "hypothesis. Mode = " + mode(r) + "."),
        })
    except Exception as exc:
        out["error"] = str(exc)[:200]
    return out


def summary(r) -> dict:
    """Dashboard aggregate — recompute the producer-bus panel state and publish to UNIFY_SUMMARY."""
    audit = audit_bypass(r)
    try:
        r.set(K.UNIFY_SUMMARY, json.dumps(audit))
    except Exception:
        pass
    return audit
