"""SciBrain typed interface contracts — the single coupling between every layer.

This is the FinRL-X principle (arXiv:2603.21330): one typed contract is the sole
interface, so any module (math / physics / quantum / ML / LLM) is swappable without
touching the rest of the circuit. Every math module returns exactly a `ModuleOutput`;
the Fusion ALU returns a `Decision`. Frozen dataclasses → values can't be mutated
after a module emits them (auditability).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class SensorFrame:
    """Layer-0 snapshot of everything known about one symbol at one instant.

    `candles[tf]` is an (N, 6) float array of the last N CLOSED candles for that
    timeframe, columns [t, o, h, l, c, v], OLDEST-first (row -1 = newest). The
    SensorBus reverses Redis' newest-first list so modules can treat it as a normal
    forward time series. Missing inputs are left as None / empty so modules decide
    how to degrade.
    """
    symbol: str
    candles: dict[str, np.ndarray]
    ofi: Optional[float] = None
    vpin: Optional[float] = None
    funding: Optional[float] = None
    oi_change_5m: Optional[float] = None
    oi_change_z: Optional[float] = None
    sentiment: Optional[float] = None
    cn_forecasts: dict[str, dict] = field(default_factory=dict)
    last_price: Optional[float] = None
    ts: float = field(default_factory=time.time)

    def closes(self, tf: str) -> Optional[np.ndarray]:
        """1-D float array of closes for `tf` (oldest-first), or None if absent."""
        arr = self.candles.get(tf)
        if arr is None or len(arr) == 0:
            return None
        return arr[:, 4].astype(float)

    def has(self, tf: str, n: int) -> bool:
        arr = self.candles.get(tf)
        return arr is not None and len(arr) >= n


@dataclass(frozen=True)
class ModuleOutput:
    """The universal output every SciBrain module emits.

    direction:  signed directional vote in [-1, +1] (sign = long/short, |.| = strength)
    conviction: the module's own confidence in [0, 1]
    A module that cannot compute returns direction=0, conviction=0 (it abstains) —
    it NEVER raises into the pipeline.
    """
    module: str
    direction: float
    conviction: float
    expected_move_pct: Optional[float]
    horizon_min: int
    regime_tag: Optional[str]
    features: dict
    explanation: str
    reliability_ic: Optional[float] = None
    ok: bool = True
    ts: float = field(default_factory=time.time)
    # §5a expansion contract — explicit semantics so unlike components can't be mixed
    # accidentally. Appended with defaults so EVERY existing producer/reader keeps working
    # (the 14-module bank constructs these by keyword and never sets them → safe defaults).
    role: str = "direction"               # direction|gate|risk|context|allocator|exit (§6g.3)
    evidence_family: str = "unspecified"  # trend|reversion|topology|cross_asset|tail|microstructure|...
    shadow_only: bool = False             # research/shadow: RECORDED + evaluated, NEVER applied to a live vote

    @staticmethod
    def abstain(module: str, reason: str, horizon_min: int = 60, *,
                role: str = "direction", evidence_family: str = "unspecified",
                shadow_only: bool = False) -> "ModuleOutput":
        """Graceful no-vote (insufficient/bad data). Conviction 0 → ignored by fusion.
        Carries role/evidence_family/shadow_only so an abstaining tagged module keeps its identity."""
        return ModuleOutput(
            module=module, direction=0.0, conviction=0.0, expected_move_pct=None,
            horizon_min=horizon_min, regime_tag=None, features={"reason": reason},
            explanation=f"{module}: abstain ({reason})", ok=False,
            role=role, evidence_family=evidence_family, shadow_only=shadow_only,
        )

    def to_dict(self) -> dict:
        d = {
            "module": self.module,
            "direction": round(float(self.direction), 6),
            "conviction": round(float(self.conviction), 6),
            "expected_move_pct": (None if self.expected_move_pct is None
                                  else round(float(self.expected_move_pct), 6)),
            "horizon_min": self.horizon_min,
            "regime_tag": self.regime_tag,
            "features": _round_floats(self.features),
            "explanation": self.explanation,
            "reliability_ic": (None if self.reliability_ic is None
                               else round(float(self.reliability_ic), 6)),
            "ok": self.ok,
            "ts": round(self.ts, 3),
            "role": self.role,
            "evidence_family": self.evidence_family,
            "shadow_only": bool(self.shadow_only),
        }
        return d

    @staticmethod
    def from_dict(d: dict) -> "ModuleOutput":
        """Rebuild a ModuleOutput from its to_dict() form (used to reconstruct a snapshot
        decision at ex-ante decision-risk audit time). Tolerant of missing keys."""
        return ModuleOutput(
            module=str(d.get("module", "?")),
            direction=float(d.get("direction", 0.0) or 0.0),
            conviction=float(d.get("conviction", 0.0) or 0.0),
            expected_move_pct=(None if d.get("expected_move_pct") is None
                               else float(d["expected_move_pct"])),
            horizon_min=int(d.get("horizon_min", 60) or 60),
            regime_tag=d.get("regime_tag"),
            features=dict(d.get("features") or {}),
            explanation=str(d.get("explanation", "")),
            reliability_ic=(None if d.get("reliability_ic") is None
                            else float(d["reliability_ic"])),
            ok=bool(d.get("ok", True)),
            ts=float(d.get("ts", time.time()) or time.time()),
            # tolerant of OLD snapshots (pre-§5a) that lack these keys → safe defaults
            role=str(d.get("role") or "direction"),
            evidence_family=str(d.get("evidence_family") or "unspecified"),
            shadow_only=bool(d.get("shadow_only", False)),
        )


@dataclass(frozen=True)
class Decision:
    """Fusion ALU output — the launchpad candidate verdict.

    `attribution` answers "WHICH factor is responsible?" deterministically: a list of
    {module, share, aligned} where share = the module's signed fraction of the net
    directional vote (sums to ~1 over directional modules). `primary_driver` is the
    aligned module with the largest share — the computed responsible driver (the
    EDENUSDT "OFI was the primary signal" answer, but exact, not inferred).
    """
    symbol: str
    direction: Optional[str]          # 'long' | 'short' | None
    conviction: float                 # [0, 1]
    expected_move_pct: Optional[float]
    size_frac: float                  # fractional-Kelly position size in [0, cap]
    regime: str
    contributing: list[ModuleOutput]
    attribution: list = field(default_factory=list)
    primary_driver: Optional[str] = None
    router: Optional[dict] = None     # Meta-Router (MoE) state: canonical regime + per-module gains
    family_penalty: Optional[dict] = None  # §6g.330 evidence-family correlation penalty summary
    ts: float = field(default_factory=time.time)

    def influence_manifest(self) -> dict:
        """Normalized, honest account of every module present at decision time.

        `applied` and `gate_applied` are the only causal statuses. Suppressed and
        abstained modules remain visible so an opened trade shows the full circuit
        without pretending that non-applied evidence caused the action.
        """
        gains = (self.router or {}).get("gains") or {}
        attribution = {
            str(a.get("module")): a for a in self.attribution if isinstance(a, dict)
        }
        influences = []
        counts = {
            "applied": 0, "gate_applied": 0, "suppressed": 0,
            "abstained": 0, "advised": 0, "counterfactual_only": 0,
        }
        for module in self.contributing:
            gain = float(gains.get(module.module, 1.0) or 0.0)
            attr = attribution.get(module.module, {})
            if module.shadow_only:
                # §5a/§6g: shadow/research module — RECORDED + evaluated, never applied
                status = "counterfactual_only"
                authority = "observe"
                effect_kind = "none"
                effect_value = 0.0
            elif not module.ok or module.conviction <= 0:
                status = "abstained"
                authority = "observe"
                effect_kind = "none"
                effect_value = 0.0
            elif module.direction == 0.0:
                status = "gate_applied"
                authority = "live"
                effect_kind = "stability_multiplier"
                effect_value = float(module.conviction)
            elif gain <= 0.0:
                status = "suppressed"
                authority = "observe"
                effect_kind = "none"
                effect_value = 0.0
            else:
                status = "applied"
                authority = "live"
                effect_kind = "signed_fusion_share"
                effect_value = float(attr.get("share", 0.0) or 0.0)
            counts[status] += 1
            reliability = max(float(module.reliability_ic or 1.0), 0.0)
            # §6g.330: the family-correlation penalty's per-module discount (1.0 = undiscounted)
            # so effective_weight is HONEST about what actually fed the vote, not the pre-penalty value.
            discount = float(attr.get("redundancy_discount", 1.0) or 1.0)
            influences.append({
                "source": module.module,
                "authority": authority,
                "status": status,
                # behavioral label kept; declared role/family surfaced for the family penalty
                "role": "gate" if status == "gate_applied" else module.role,
                "evidence_family": module.evidence_family,
                "shadow_only": bool(module.shadow_only),
                "raw_vote": round(float(module.direction), 6),
                "conviction": round(float(module.conviction), 6),
                "router_gain": round(gain, 6),
                "reliability_factor": round(reliability, 6),
                "redundancy_discount": round(discount, 6),
                "effective_weight": round(
                    float(module.conviction) * reliability * gain * discount, 6
                ),
                "effect_kind": effect_kind,
                "actual_effect": round(effect_value, 6),
                "aligned": bool(attr.get("aligned", False)),
                "regime_tag": module.regime_tag,
                "ok": bool(module.ok),
                "explanation": module.explanation,
            })
        return {
            "schema_version": 1,
            "origin": "scibrain",
            "decision": {
                "direction": self.direction,
                "conviction": round(float(self.conviction), 6),
                "regime": self.regime,
                "primary_driver": self.primary_driver,
                "ts": round(float(self.ts), 3),
            },
            "summary": counts,
            "influences": influences,
        }

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "conviction": round(float(self.conviction), 6),
            "expected_move_pct": (None if self.expected_move_pct is None
                                  else round(float(self.expected_move_pct), 6)),
            "size_frac": round(float(self.size_frac), 6),
            "regime": self.regime,
            "primary_driver": self.primary_driver,
            "attribution": self.attribution,
            "router": self.router,
            "family_penalty": self.family_penalty,
            "modules": [m.to_dict() for m in self.contributing],
            "influence_manifest": self.influence_manifest(),
            "ts": round(self.ts, 3),
        }

    @staticmethod
    def from_dict(d: dict) -> "Decision":
        """Rebuild a Decision from its to_dict() form. Used by the ex-ante decision-risk auditor
        to interrogate the EXACT decision that opened a trade (snapshot), not a later re-score."""
        return Decision(
            symbol=str(d.get("symbol", "?")),
            direction=d.get("direction"),
            conviction=float(d.get("conviction", 0.0) or 0.0),
            expected_move_pct=(None if d.get("expected_move_pct") is None
                               else float(d["expected_move_pct"])),
            size_frac=float(d.get("size_frac", 0.0) or 0.0),
            regime=str(d.get("regime", "unknown")),
            contributing=[ModuleOutput.from_dict(m) for m in (d.get("modules") or [])],
            attribution=list(d.get("attribution") or []),
            primary_driver=d.get("primary_driver"),
            router=(d.get("router") if isinstance(d.get("router"), dict) else None),
            family_penalty=(d.get("family_penalty") if isinstance(d.get("family_penalty"), dict) else None),
            ts=float(d.get("ts", time.time()) or time.time()),
        )


@dataclass(frozen=True)
class UniverseFrame:
    """Read-only cross-market snapshot built ONCE per funnel cycle and shared by every
    Universe-Core module (design §4a/§5a). Per-symbol Hot-Core modules still see only their
    own SensorFrame; the Universe Core reads THIS shared substrate to compute factor flow,
    contagion graphs, lead-lag, and transport distance WITHOUT each module re-reading the
    whole universe (kills the per-symbol Redis round-trip storm Phase 5 flagged).

    All matrices are COLUMN-aligned to `symbols` (column j ↔ symbols[j]) and rows are
    OLDEST→newest. `primary_tf` is the timeframe the correlation / lead-lag / digest are
    computed on. Anything that could not be measured is an empty array — never fabricated.
    Frozen: a downstream module can read but never mutate the shared frame (auditability).
    """
    symbols: list[str]
    primary_tf: str
    returns_by_tf: dict[str, np.ndarray]   # tf -> (T, S) time-aligned log-returns, oldest-first
    feature_matrix: np.ndarray             # (S, F) per-symbol summary features
    feature_names: tuple[str, ...]
    correlation: np.ndarray                # (S, S) Pearson corr on primary_tf returns
    directed_lead_lag: np.ndarray          # (S, S) lag-1 directed cross-corr: L[i,j]=corr(r_i[t-1], r_j[t])
    liquidity: np.ndarray                  # (S,) recent log dollar-volume per symbol
    digest: dict                           # cheap market-state observables (breadth, crowding, factor share)
    ts: float = field(default_factory=time.time)

    def index_of(self, symbol: str) -> Optional[int]:
        """Column index of `symbol` in the aligned matrices, or None if not in the frame."""
        try:
            return self.symbols.index(symbol)
        except ValueError:
            return None

    @property
    def n_symbols(self) -> int:
        return len(self.symbols)

    def to_digest_dict(self) -> dict:
        """The compact, JSON-safe market-state payload the dashboard/consumers read (the
        full matrices stay in-RAM for the in-process Universe modules). Pure read of the
        already-computed digest plus shape metadata — never recomputes."""
        return {
            "ts": round(float(self.ts), 3),
            "primary_tf": self.primary_tf,
            "n_symbols": self.n_symbols,
            "tfs": sorted(self.returns_by_tf.keys()),
            "feature_names": list(self.feature_names),
            **{k: (round(float(v), 6) if isinstance(v, (int, float)) else v)
               for k, v in self.digest.items()},
        }


def _round_floats(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, float):
            out[k] = round(v, 6)
        elif isinstance(v, (np.floating,)):
            out[k] = round(float(v), 6)
        elif isinstance(v, (np.integer,)):
            out[k] = int(v)
        else:
            out[k] = v
    return out
