"""SciBrain Phase-7d — the Cognitive-OS spine's typed learning contracts (design §7 / §3.4).

The Cognitive Operating System (next_impl/scientist_brain_cognitive_os.md) adds a typed global
workspace, shared belief, learning signals, competence tracking, and bounded model changes. Before ANY
of those learners/workspace components are built, the design (§7, §9 step 2) mandates ONE set of typed,
versioned, bounded contracts so every specialist speaks the same language and every learning path can be
routed through the same experiment registry + promotion kernel (the Living-Intelligence/§11 kernel —
see producers.apply_model_change / changespec.ChangeSpec).

This module is JUST the contracts — frozen dataclasses with bounded-range validation and round-trip
serialization, the FinRL-X "one typed contract is the sole interface" principle (same as contracts.py).
They carry NO behavior and NO trading authority; they are the substrate the workspace (§3.4), thalamic
salience routing, metacognition, and the unified learners will publish/consume:

  • CognitiveMessage   — a specialist's typed, uncertain, salience-weighted contribution to the workspace.
  • BeliefState        — the shared, calibrated, uncertainty-aware belief broadcast to all consumers.
  • LearningSignal     — a typed learning driver (reward error / uncertainty / changepoint / …) with the
                          authority ladder so a signal can never exceed its blast radius.
  • CompetenceRecord   — honest per-component competence (calibration / utility / support / drift) — the
                          metacognitive map that gates whether a component may act or must abstain.
  • ModelChangeSpec    — a BROADER bounded model/data/objective/update change (the generalization of the
                          scalar/router changespec.ChangeSpec) with expected effect + falsifier, routed
                          through the same promotion kernel — no model change bypasses it.

All fields are plain JSON-serializable types (no numpy) so a contract round-trips through Redis/DB intact.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

COGNITIVE_SCHEMA_VERSION = 1

# ── controlled vocabularies (kept permissive — validate() WARNS via errors only on the hard ones) ──
ROLES = ("gate", "direction", "size", "exit", "meta", "evidence", "risk", "execution", "selection",
         "context", "allocator")   # context/allocator align with ModuleOutput.role (contracts.py §6g.3)
AUTHORITY_LADDER = ("observe", "advise", "bounded_canary", "live", "veto")
LEARNING_KINDS = ("reward_error", "uncertainty", "changepoint", "surprise", "patience_risk",
                  "homeostatic", "novelty")
MODEL_CHANGE_KINDS = ("model", "data", "objective", "update")


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _clip01(v, default: float = 0.0) -> float:
    return max(0.0, min(1.0, _f(v, default)))


def _list(v) -> list:
    return list(v) if isinstance(v, (list, tuple)) else ([] if v is None else [v])


def _dict(v) -> dict:
    return dict(v) if isinstance(v, dict) else {}


@dataclass(frozen=True)
class CognitiveMessage:
    """A specialist's typed contribution to the global workspace (design §3.4). Specialists COMPETE to
    publish; salience decides what is broadcast. `belief_delta` is the signed nudge to the shared belief;
    `uncertainty`/`support_distance` let consumers discount it; `source_version`+`evidence_ids` give the
    lineage so the message is auditable and replayable."""
    source: str
    role: str = "meta"
    evidence_family: str = "unspecified"
    belief_delta: float = 0.0
    uncertainty: float = 1.0            # [0,1] — 1 = no information
    salience: float = 0.0              # [0,1] — attention/broadcast weight
    horizon: float = 30.0              # minutes the message pertains to
    support_distance: float = 0.0      # >=0 — OOD distance from training support
    source_version: dict = field(default_factory=dict)
    evidence_ids: list = field(default_factory=list)
    ts: float = field(default_factory=lambda: round(time.time(), 3))
    schema_version: int = COGNITIVE_SCHEMA_VERSION

    def validate(self) -> list[str]:
        e: list[str] = []
        if not str(self.source).strip():
            e.append("source is required")
        if self.role not in ROLES:
            e.append(f"role '{self.role}' not in {ROLES}")
        if not (0.0 <= self.uncertainty <= 1.0):
            e.append("uncertainty must be in [0,1]")
        if not (0.0 <= self.salience <= 1.0):
            e.append("salience must be in [0,1]")
        if self.support_distance < 0:
            e.append("support_distance must be >= 0")
        if self.horizon <= 0:
            e.append("horizon must be > 0")
        return e

    def to_dict(self) -> dict:
        return {"source": self.source, "role": self.role, "evidence_family": self.evidence_family,
                "belief_delta": self.belief_delta, "uncertainty": self.uncertainty,
                "salience": self.salience, "horizon": self.horizon,
                "support_distance": self.support_distance, "source_version": self.source_version,
                "evidence_ids": self.evidence_ids, "ts": self.ts, "schema_version": self.schema_version}

    @staticmethod
    def from_dict(d: dict) -> "CognitiveMessage":
        d = d or {}
        return CognitiveMessage(
            source=str(d.get("source", "")), role=str(d.get("role", "meta")),
            evidence_family=str(d.get("evidence_family", "unspecified")),
            belief_delta=_f(d.get("belief_delta")), uncertainty=_clip01(d.get("uncertainty"), 1.0),
            salience=_clip01(d.get("salience")), horizon=_f(d.get("horizon"), 30.0),
            support_distance=max(0.0, _f(d.get("support_distance"))),
            source_version=_dict(d.get("source_version")), evidence_ids=_list(d.get("evidence_ids")),
            ts=_f(d.get("ts"), round(time.time(), 3)),
            schema_version=int(d.get("schema_version", COGNITIVE_SCHEMA_VERSION) or 1))


@dataclass(frozen=True)
class BeliefState:
    """The shared, calibrated, uncertainty-aware belief broadcast to planning/action/memory/risk/
    metacognition (design §3.4 BeliefState_t). The integration point of Global-Workspace theory's useful
    computational core — NOT a free-form chat; a bounded latent + calibrated posteriors + full lineage."""
    latent_mean: list = field(default_factory=list)            # shared latent vector
    latent_dispersion: float = 0.0                             # ensemble/covariance spread summary (>=0)
    regime_posterior: dict = field(default_factory=dict)       # {regime: prob} (calibrated)
    changepoint_posterior: float = 0.0                         # [0,1] P(recent changepoint)
    disagreement: float = 0.0                                  # [0,1] cross-source disagreement
    uncertainty_decomposition: dict = field(default_factory=dict)  # {epistemic, aleatoric}
    tail_state: dict = field(default_factory=dict)             # tail-risk summary
    liquidity_state: dict = field(default_factory=dict)
    portfolio_state: dict = field(default_factory=dict)
    execution_state: dict = field(default_factory=dict)
    active_goals: list = field(default_factory=list)
    active_hypotheses: list = field(default_factory=list)
    retrieved_episodes: list = field(default_factory=list)
    source_ids: list = field(default_factory=list)             # which specialists contributed
    versions: dict = field(default_factory=dict)               # code/config/model lineage
    ts: float = field(default_factory=lambda: round(time.time(), 3))
    schema_version: int = COGNITIVE_SCHEMA_VERSION

    def validate(self) -> list[str]:
        e: list[str] = []
        if not (0.0 <= self.changepoint_posterior <= 1.0):
            e.append("changepoint_posterior must be in [0,1]")
        if not (0.0 <= self.disagreement <= 1.0):
            e.append("disagreement must be in [0,1]")
        if self.latent_dispersion < 0:
            e.append("latent_dispersion must be >= 0")
        rp = self.regime_posterior or {}
        if rp:
            s = sum(_f(v) for v in rp.values())
            if not (0.80 <= s <= 1.20):   # calibrated posterior should ~sum to 1 (loose tolerance)
                e.append(f"regime_posterior should sum to ~1 (got {round(s, 3)})")
        return e

    def to_dict(self) -> dict:
        return {"latent_mean": self.latent_mean, "latent_dispersion": self.latent_dispersion,
                "regime_posterior": self.regime_posterior,
                "changepoint_posterior": self.changepoint_posterior, "disagreement": self.disagreement,
                "uncertainty_decomposition": self.uncertainty_decomposition, "tail_state": self.tail_state,
                "liquidity_state": self.liquidity_state, "portfolio_state": self.portfolio_state,
                "execution_state": self.execution_state, "active_goals": self.active_goals,
                "active_hypotheses": self.active_hypotheses, "retrieved_episodes": self.retrieved_episodes,
                "source_ids": self.source_ids, "versions": self.versions, "ts": self.ts,
                "schema_version": self.schema_version}

    @staticmethod
    def from_dict(d: dict) -> "BeliefState":
        d = d or {}
        return BeliefState(
            latent_mean=[_f(x) for x in _list(d.get("latent_mean"))],
            latent_dispersion=max(0.0, _f(d.get("latent_dispersion"))),
            regime_posterior=_dict(d.get("regime_posterior")),
            changepoint_posterior=_clip01(d.get("changepoint_posterior")),
            disagreement=_clip01(d.get("disagreement")),
            uncertainty_decomposition=_dict(d.get("uncertainty_decomposition")),
            tail_state=_dict(d.get("tail_state")), liquidity_state=_dict(d.get("liquidity_state")),
            portfolio_state=_dict(d.get("portfolio_state")), execution_state=_dict(d.get("execution_state")),
            active_goals=_list(d.get("active_goals")), active_hypotheses=_list(d.get("active_hypotheses")),
            retrieved_episodes=_list(d.get("retrieved_episodes")), source_ids=_list(d.get("source_ids")),
            versions=_dict(d.get("versions")), ts=_f(d.get("ts"), round(time.time(), 3)),
            schema_version=int(d.get("schema_version", COGNITIVE_SCHEMA_VERSION) or 1))


@dataclass(frozen=True)
class LearningSignal:
    """A typed driver of learning (design §7 / §3.7 neuromodulation). `kind` is the typed cause
    (reward_error / uncertainty / changepoint / surprise / patience_risk / homeostatic / novelty);
    `authority` pins the signal on the escalation ladder so an online signal can NEVER exceed its
    blast radius (observe/advise before any bounded_canary)."""
    kind: str
    magnitude: float = 0.0
    target: str = ""
    confidence: float = 0.0            # [0,1]
    evidence_ids: list = field(default_factory=list)
    authority: str = "observe"
    ts: float = field(default_factory=lambda: round(time.time(), 3))
    schema_version: int = COGNITIVE_SCHEMA_VERSION

    def validate(self) -> list[str]:
        e: list[str] = []
        if self.kind not in LEARNING_KINDS:
            e.append(f"kind '{self.kind}' not in {LEARNING_KINDS}")
        if not str(self.target).strip():
            e.append("target is required")
        if not (0.0 <= self.confidence <= 1.0):
            e.append("confidence must be in [0,1]")
        if self.authority not in AUTHORITY_LADDER:
            e.append(f"authority '{self.authority}' not in {AUTHORITY_LADDER}")
        return e

    def to_dict(self) -> dict:
        return {"kind": self.kind, "magnitude": self.magnitude, "target": self.target,
                "confidence": self.confidence, "evidence_ids": self.evidence_ids,
                "authority": self.authority, "ts": self.ts, "schema_version": self.schema_version}

    @staticmethod
    def from_dict(d: dict) -> "LearningSignal":
        d = d or {}
        return LearningSignal(
            kind=str(d.get("kind", "")), magnitude=_f(d.get("magnitude")),
            target=str(d.get("target", "")), confidence=_clip01(d.get("confidence")),
            evidence_ids=_list(d.get("evidence_ids")), authority=str(d.get("authority", "observe")),
            ts=_f(d.get("ts"), round(time.time(), 3)),
            schema_version=int(d.get("schema_version", COGNITIVE_SCHEMA_VERSION) or 1))


@dataclass(frozen=True)
class CompetenceRecord:
    """Honest per-component competence in a context (design §3.9 metacognition / §8.1). The metacognitive
    map: how well-calibrated a component is, its incremental utility, the sample support behind that, and
    its drift — the evidence that gates whether the component may act or must ABSTAIN."""
    component: str
    context: str = "global"
    task: str = ""
    calibration: float = 0.0           # [0,1] — 1 = perfectly calibrated
    utility_delta: float = 0.0         # incremental utility vs baseline (signed)
    support: int = 0                   # sample support behind the estimate
    drift: float = 0.0                 # >=0 drift indicator (0 = stable)
    version: dict = field(default_factory=dict)
    ts: float = field(default_factory=lambda: round(time.time(), 3))
    schema_version: int = COGNITIVE_SCHEMA_VERSION

    def validate(self) -> list[str]:
        e: list[str] = []
        if not str(self.component).strip():
            e.append("component is required")
        if not (0.0 <= self.calibration <= 1.0):
            e.append("calibration must be in [0,1]")
        if self.support < 0:
            e.append("support must be >= 0")
        if self.drift < 0:
            e.append("drift must be >= 0")
        return e

    def to_dict(self) -> dict:
        return {"component": self.component, "context": self.context, "task": self.task,
                "calibration": self.calibration, "utility_delta": self.utility_delta,
                "support": self.support, "drift": self.drift, "version": self.version,
                "ts": self.ts, "schema_version": self.schema_version}

    @staticmethod
    def from_dict(d: dict) -> "CompetenceRecord":
        d = d or {}
        return CompetenceRecord(
            component=str(d.get("component", "")), context=str(d.get("context", "global")),
            task=str(d.get("task", "")), calibration=_clip01(d.get("calibration")),
            utility_delta=_f(d.get("utility_delta")), support=int(_f(d.get("support"))),
            drift=max(0.0, _f(d.get("drift"))), version=_dict(d.get("version")),
            ts=_f(d.get("ts"), round(time.time(), 3)),
            schema_version=int(d.get("schema_version", COGNITIVE_SCHEMA_VERSION) or 1))


@dataclass(frozen=True)
class ModelChangeSpec:
    """A BOUNDED model/data/objective/update change (design §7) — the generalization of the scalar/router
    changespec.ChangeSpec to broader learning changes (a representation swap, a dataset/replay change, an
    objective/loss change, an online update). Like ChangeSpec it MUST be bounded, carry an expected_effect
    and a deterministic falsifier, and route through the SAME experiment registry + promotion kernel
    (producers.apply_model_change records it; it never self-applies). It holds NO trading authority."""
    change_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    kind: str = "update"               # model | data | objective | update
    target: str = ""
    intervention: dict = field(default_factory=dict)   # the bounded change description
    bounds: dict = field(default_factory=dict)         # the asserted bounds (proves boundedness)
    expected_effect: str = ""
    falsifier: str = ""
    evidence_ids: list = field(default_factory=list)
    proposer: str = "unknown"
    status: str = "proposed"
    versions: dict = field(default_factory=dict)
    ts: float = field(default_factory=lambda: round(time.time(), 3))
    schema_version: int = COGNITIVE_SCHEMA_VERSION

    def validate(self) -> list[str]:
        e: list[str] = []
        if self.kind not in MODEL_CHANGE_KINDS:
            e.append(f"kind '{self.kind}' not in {MODEL_CHANGE_KINDS}")
        if not str(self.target).strip():
            e.append("target is required")
        if not isinstance(self.intervention, dict) or not self.intervention:
            e.append("intervention (bounded change description) is required")
        if not isinstance(self.bounds, dict) or not self.bounds:
            e.append("bounds are required (a change must be bounded)")
        if not str(self.expected_effect).strip():
            e.append("expected_effect is required")
        if not str(self.falsifier).strip():
            e.append("falsifier is required (a change must be falsifiable)")
        return e

    def to_dict(self) -> dict:
        return {"change_id": self.change_id, "kind": self.kind, "target": self.target,
                "intervention": self.intervention, "bounds": self.bounds,
                "expected_effect": self.expected_effect, "falsifier": self.falsifier,
                "evidence_ids": self.evidence_ids, "proposer": self.proposer, "status": self.status,
                "versions": self.versions, "ts": self.ts, "schema_version": self.schema_version}

    @staticmethod
    def from_dict(d: dict) -> "ModelChangeSpec":
        d = d or {}
        return ModelChangeSpec(
            change_id=str(d.get("change_id") or uuid.uuid4().hex[:16]),
            kind=str(d.get("kind", "update")), target=str(d.get("target", "")),
            intervention=_dict(d.get("intervention")), bounds=_dict(d.get("bounds")),
            expected_effect=str(d.get("expected_effect", "")), falsifier=str(d.get("falsifier", "")),
            evidence_ids=_list(d.get("evidence_ids")), proposer=str(d.get("proposer", "unknown")),
            status=str(d.get("status", "proposed")), versions=_dict(d.get("versions")),
            ts=_f(d.get("ts"), round(time.time(), 3)),
            schema_version=int(d.get("schema_version", COGNITIVE_SCHEMA_VERSION) or 1))


# Registry of the contracts (for the workspace/dashboard + smoke round-trip coverage).
CONTRACTS = {
    "CognitiveMessage": CognitiveMessage, "BeliefState": BeliefState,
    "LearningSignal": LearningSignal, "CompetenceRecord": CompetenceRecord,
    "ModelChangeSpec": ModelChangeSpec,
}


def validate_any(obj) -> list[str]:
    """Validate any contract instance; returns the list of errors ([] = valid)."""
    return obj.validate() if hasattr(obj, "validate") else ["not a cognitive contract"]
