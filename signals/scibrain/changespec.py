"""SciBrain Phase 7b — typed bounded ChangeSpec + safe intervention DSL (design §7).

The living brain's constitution: a trade/analysis can CREATE a hypothesis; it can never directly
mutate a live formula or parameter. Every LLM-or-algorithm proposal must compile to a **bounded,
typed ChangeSpec** against an allow-listed target with hard parameter bounds — arbitrary Python /
unrestricted equations are rejected by construction. This module is the contract + the deterministic
compiler/validator; it has ZERO trading authority (it only describes a proposed change). Application
and promotion live behind the Evidence/Authority/Influence gate (Phase 7c), never here.

A ChangeSpec targets one of a small, explicit set of safe knobs:
  • router.gain.<module>.<regime>   — a Meta-Router per-regime expert gain (bounds [0, GAIN_CEIL])
  • scibrain.<scalar>               — an allow-listed scalar control (each with its own hard bounds)
  • fusion.<scalar>                 — an allow-listed fusion scalar
The intervention proposes a new value within BOTH its own parameter_bounds AND the target's hard
bounds; anything outside, unknown, or non-numeric fails to compile (returns errors, not an exception).
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger()

CHANGESPEC_SCHEMA_VERSION = 1

# ── lifecycle (design §9). proposed → compiled → tested → walk_forward → shadow → canary → live,
# with terminal retained/demoted/rolled_back/rejected. This module only ever produces 'proposed'
# (raw) and 'compiled' (validated); later phases own the rest. Transitions are validated centrally. ──
STATUSES = ("proposed", "compiled", "unit_tested", "historical_replay", "walk_forward",
            "shadow", "canary", "live", "retained", "demoted", "rolled_back", "rejected")
_TERMINAL = ("retained", "demoted", "rolled_back", "rejected")
LEGAL_TRANSITIONS: dict[str, tuple] = {
    "proposed": ("compiled", "rejected"),
    "compiled": ("unit_tested", "rejected"),
    "unit_tested": ("historical_replay", "rejected"),
    "historical_replay": ("walk_forward", "rejected"),
    "walk_forward": ("shadow", "rejected"),
    "shadow": ("canary", "demoted", "rejected"),
    "canary": ("live", "rolled_back", "demoted"),
    "live": ("retained", "rolled_back", "demoted"),
    # terminal states have no onward transitions
    **{s: () for s in _TERMINAL},
}

_INTERVENTION_KINDS = ("gain_multiplier", "param_set")


# ── target registry: the ONLY knobs a ChangeSpec may name, each with hard bounds ──────────────
# Router gains are resolved dynamically (module × regime); scalars are explicit with per-knob bounds.
_CANON_REGIMES = ("trending", "mean_revert", "turbulent", "neutral")

# explicit allow-listed scalar controls: target -> (redis_key, lo, hi, default)
_SCALAR_TARGETS: dict[str, tuple] = {
    "scibrain.router_strength":    ("scibrain:router_strength", 0.0, 1.0, 1.0),
    "scibrain.wrong_dir_threshold":("scibrain:wrong_dir_threshold", 0.30, 0.90, 0.60),
    "scibrain.ic_horizon_min":     ("scibrain:ic_horizon_min", 5, 240, 30),
    "scibrain.router_enabled":     ("scibrain:router_enabled", 0, 1, 1),
    "scibrain.ic_enabled":         ("scibrain:ic_enabled", 0, 1, 1),
}


def resolve_target(target: str) -> dict:
    """Resolve a target string to its intervention kind + hard numeric bounds (+ current value when
    cheaply known). Returns {"valid": bool, "kind", "bounds":[lo,hi], "current", "error"}.

    router.gain.<module>.<regime> is checked against the live ROSTER + canonical regimes and the
    router GAIN_CEIL; scalars come from the explicit allow-list. Anything else is invalid (bounded)."""
    if not isinstance(target, str) or not target:
        return {"valid": False, "error": "target must be a non-empty string"}

    if target.startswith("router.gain."):
        rest = target[len("router.gain."):]
        parts = rest.split(".")
        if len(parts) != 2:
            return {"valid": False, "error": "router gain target must be router.gain.<module>.<regime>"}
        module, regime = parts
        try:
            from .cohort import ROSTER
            from .router import GAIN_CEIL, _PROFILES
        except Exception as exc:
            return {"valid": False, "error": f"router import failed: {str(exc)[:80]}"}
        if module not in ROSTER:
            return {"valid": False, "error": f"unknown module '{module}' (not in roster)"}
        if regime not in _CANON_REGIMES:
            return {"valid": False, "error": f"unknown regime '{regime}'"}
        current = float(_PROFILES.get(regime, {}).get(module, 1.0))
        return {"valid": True, "kind": "gain_multiplier", "bounds": [0.0, float(GAIN_CEIL)],
                "current": current}

    if target in _SCALAR_TARGETS:
        _key, lo, hi, default = _SCALAR_TARGETS[target]
        return {"valid": True, "kind": "param_set", "bounds": [float(lo), float(hi)],
                "current": float(default), "redis_key": _key}

    return {"valid": False, "error": f"target '{target}' is not in the allow-list"}


def allowed_targets() -> dict:
    """The full bounded target surface (for the dashboard / proposers). Router-gain targets are
    enumerated lazily as <module>×<regime>; scalars are listed with their hard bounds."""
    try:
        from .cohort import ROSTER
        from .router import GAIN_CEIL
        gains = {f"router.gain.{m}.{rg}": {"kind": "gain_multiplier", "bounds": [0.0, float(GAIN_CEIL)]}
                 for m in ROSTER for rg in _CANON_REGIMES}
    except Exception:
        gains = {}
    scalars = {t: {"kind": "param_set", "bounds": [float(v[1]), float(v[2])]}
               for t, v in _SCALAR_TARGETS.items()}
    return {**gains, **scalars}


@dataclass(frozen=True)
class ChangeSpec:
    """A bounded, typed proposal to change ONE allow-listed knob in ONE context (design §7).

    Immutable; carries its own provenance + a content fingerprint for dedup. `status` advances only
    through experiments.transition() (validated). This object has NO authority to apply itself."""
    hypothesis_id: str
    target: str
    role: str                              # gate | direction | size | exit | meta (descriptive)
    context_predicate: str                 # when the change applies (e.g. "router.regime == turbulent")
    intervention: dict                     # {"kind", "old", "candidate"}
    parameter_bounds: list                 # [lo, hi] the proposer asserts (∩ target hard bounds)
    evidence_ids: list                     # trade/cohort ids supporting the hypothesis
    expected_effect: str
    falsifier: str                         # the deterministic condition that would KILL this change
    complexity_cost: int = 1
    status: str = "proposed"
    proposer: str = "unknown"              # which producer created it (council/ic/cohort/manual/...)
    schema_version: int = CHANGESPEC_SCHEMA_VERSION
    created_ts: float = field(default_factory=lambda: round(time.time(), 3))
    versions: dict = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Stable content hash (target + context + intervention kind/candidate) for dedup — two
        proposals to set the same knob in the same context to the same value are the SAME hypothesis."""
        iv = self.intervention or {}
        basis = json.dumps({
            "t": self.target, "c": self.context_predicate, "k": iv.get("kind"),
            "cand": iv.get("candidate"),
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id, "target": self.target, "role": self.role,
            "context_predicate": self.context_predicate, "intervention": self.intervention,
            "parameter_bounds": self.parameter_bounds, "evidence_ids": self.evidence_ids,
            "expected_effect": self.expected_effect, "falsifier": self.falsifier,
            "complexity_cost": self.complexity_cost, "status": self.status,
            "proposer": self.proposer, "schema_version": self.schema_version,
            "created_ts": self.created_ts, "versions": self.versions,
            "fingerprint": self.fingerprint(),
        }

    @staticmethod
    def from_dict(d: dict) -> "ChangeSpec":
        return ChangeSpec(
            hypothesis_id=str(d.get("hypothesis_id") or uuid.uuid4().hex),
            target=str(d.get("target", "")), role=str(d.get("role", "meta")),
            context_predicate=str(d.get("context_predicate", "")),
            intervention=dict(d.get("intervention") or {}),
            parameter_bounds=list(d.get("parameter_bounds") or []),
            evidence_ids=list(d.get("evidence_ids") or []),
            expected_effect=str(d.get("expected_effect", "")),
            falsifier=str(d.get("falsifier", "")),
            complexity_cost=int(d.get("complexity_cost", 1) or 1),
            status=str(d.get("status", "proposed")),
            proposer=str(d.get("proposer", "unknown")),
            schema_version=int(d.get("schema_version", CHANGESPEC_SCHEMA_VERSION) or 1),
            created_ts=float(d.get("created_ts", time.time()) or time.time()),
            versions=dict(d.get("versions") or {}),
        )


def _versions() -> dict:
    """Version lineage pinned onto every compiled ChangeSpec (so a stale proposal is detectable)."""
    try:
        from .snapshot import _code_fingerprint, _code_commit, _config_hash
        return {"code_fingerprint": _code_fingerprint(), "code_commit": _code_commit(),
                "config_hash": _config_hash(), "changespec_schema": CHANGESPEC_SCHEMA_VERSION}
    except Exception:
        return {"changespec_schema": CHANGESPEC_SCHEMA_VERSION}


def compile_proposal(raw: dict, *, proposer: str = "unknown") -> tuple:
    """Deterministically validate a raw proposal dict into a COMPILED ChangeSpec (design §7/§8).

    Returns (spec_or_None, errors[]). The deterministic mandatory-rejection checks enforced here:
      • target must be on the bounded allow-list;
      • intervention.kind must be supported and match the target's kind;
      • candidate must be numeric and inside BOTH parameter_bounds AND the target's hard bounds;
      • parameter_bounds must be a valid [lo, hi] inside the target's hard bounds;
      • a falsifier and an expected_effect must be stated (no unfalsifiable changes).
    Council/grounding/leakage checks (§8) layer on top later; this is the typed-DSL gate."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return None, ["proposal is not a dict"]

    target = raw.get("target")
    res = resolve_target(target)
    if not res.get("valid"):
        errors.append(f"target: {res.get('error')}")

    iv = raw.get("intervention") or {}
    kind = iv.get("kind")
    if kind not in _INTERVENTION_KINDS:
        errors.append(f"intervention.kind '{kind}' not in {_INTERVENTION_KINDS}")
    elif res.get("valid") and kind != res.get("kind"):
        errors.append(f"intervention.kind '{kind}' != target kind '{res.get('kind')}'")

    candidate = iv.get("candidate")
    try:
        candidate = float(candidate)
        cand_ok = True
    except (TypeError, ValueError):
        errors.append("intervention.candidate must be numeric")
        cand_ok = False

    pbounds = raw.get("parameter_bounds")
    if not (isinstance(pbounds, (list, tuple)) and len(pbounds) == 2):
        errors.append("parameter_bounds must be [lo, hi]")
        pbounds = None
    else:
        try:
            plo, phi = float(pbounds[0]), float(pbounds[1])
            if plo > phi:
                errors.append("parameter_bounds lo > hi")
            pbounds = [plo, phi]
        except (TypeError, ValueError):
            errors.append("parameter_bounds must be numeric")
            pbounds = None

    # candidate must satisfy BOTH the proposer's bounds and the target's hard bounds
    if cand_ok and res.get("valid"):
        hlo, hhi = res["bounds"]
        if not (hlo <= candidate <= hhi):
            errors.append(f"candidate {candidate} outside target hard bounds [{hlo}, {hhi}]")
        if pbounds is not None:
            if not (pbounds[0] >= hlo and pbounds[1] <= hhi):
                errors.append(f"parameter_bounds {pbounds} not inside target hard bounds [{hlo}, {hhi}]")
            if not (pbounds[0] <= candidate <= pbounds[1]):
                errors.append(f"candidate {candidate} outside parameter_bounds {pbounds}")

    if not str(raw.get("falsifier", "")).strip():
        errors.append("falsifier is required (a change must be falsifiable)")
    if not str(raw.get("expected_effect", "")).strip():
        errors.append("expected_effect is required")

    if errors:
        return None, errors

    # normalize intervention: record the resolved current value as 'old' for staleness detection
    intervention = {"kind": kind, "candidate": candidate,
                    "old": iv.get("old", res.get("current"))}
    spec = ChangeSpec(
        hypothesis_id=str(raw.get("hypothesis_id") or uuid.uuid4().hex),
        target=str(target), role=str(raw.get("role", "meta")),
        context_predicate=str(raw.get("context_predicate", "")),
        intervention=intervention, parameter_bounds=pbounds,
        evidence_ids=list(raw.get("evidence_ids") or []),
        expected_effect=str(raw.get("expected_effect")),
        falsifier=str(raw.get("falsifier")),
        complexity_cost=int(raw.get("complexity_cost", 1) or 1),
        status="compiled", proposer=str(proposer or raw.get("proposer", "unknown")),
        versions=_versions(),
    )
    return spec, []
