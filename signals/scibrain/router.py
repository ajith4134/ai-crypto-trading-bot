"""SciBrain Meta-Router (Layer 2, MoE) — regime detection + per-module gating.

This is the "Mixture-of-Experts" layer of BLOCK 16: between the math module bank
(Layer 1) and the Fusion ALU (Layer 3) it decides WHICH experts to trust for THIS
pair right now, by a canonical market regime, and emits a per-module *gain* the fusion
multiplies into each vote's weight. gain 0 = the expert is deselected (not in the
active subset); gain >1 = boosted; <1 = damped.

Two independent inputs combine into each gain:
  1. REGIME PROFILE (Task 1+2) — a canonical regime is detected from the modules that
     already speak to regime: BOCPD (change-point prob), HMM (bull/bear/turbulent),
     StatPhysSOC (crash/criticality), confirmed by Chaos-Hurst + Kalman-velocity. Each
     regime has a profile that boosts the experts whose math fits it and damps the rest:
       trending     → trust trend/continuation experts, fade mean-reversion
       mean_revert  → trust reversion/fair-value experts, fade trend-following
       turbulent    → deselect the aggressive directional experts; lean on the gate
       neutral      → no opinion, all experts weighted 1.0
  2. ADAPTIVE IC (Task 3) — a rolling Information-Coefficient multiplier per module
     (ic_tracker) folds in how predictive each expert has actually been lately, so the
     router self-tunes its weights from realized outcomes, not just the static profile.

gain_i = clip( regime_profile_i × ic_multiplier_i , 0, GAIN_CEIL ), then blended toward
1.0 by `router_strength` (0 = router off → all 1.0, 1 = full strength).

Pure compute (no Redis) except the optional ic_map passed in; deterministic + auditable.
The full RouterState is attached to the Decision for the live dashboard.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .contracts import ModuleOutput

# ── module discipline groups (which expert does what) ──────────────────────────────
TREND_EXPERTS = ("koopman", "kalman", "langevin_hawkes", "wavelet", "info_theory")
REVERT_EXPERTS = ("noiseharvest", "tda", "ising")
CONTEXT_EXPERTS = ("rmt", "quantum", "statphys_soc", "hmm_regime")
DUAL_EXPERTS = ("chaos",)            # self-adapts (Hurst trend vs revert) → kept near-neutral
GATE_EXPERTS = ("bocpd",)            # non-directional stability damper (never gated by regime)

GAIN_CEIL = 1.6                      # a single expert can't be boosted past this
IC_FLOOR_MULT = 0.35                 # worst an anti-predictive (IC≤0) expert is damped to
IC_CEIL_MULT = 1.5                   # best a strongly-predictive expert is boosted to

# Per-regime gain profile: module -> multiplier. Modules absent from a profile get 1.0.
# Grounded in each expert's math: trend-followers up in trends, reversion up in ranges,
# everything aggressive damped when the change-point/crisis detectors fire.
_PROFILES: dict[str, dict[str, float]] = {
    "trending": {
        "koopman": 1.35, "kalman": 1.30, "langevin_hawkes": 1.25, "wavelet": 1.20,
        "info_theory": 1.15, "chaos": 1.10,
        "noiseharvest": 0.45, "tda": 0.60, "ising": 0.70,
    },
    "mean_revert": {
        "noiseharvest": 1.40, "tda": 1.30, "ising": 1.20, "rmt": 1.10, "chaos": 1.10,
        "koopman": 0.55, "kalman": 0.65, "langevin_hawkes": 0.55, "info_theory": 0.80,
    },
    "turbulent": {
        # crisis / change-point: TRUE-DESELECT the two pure trend-chasers (gain 0 ⇒ their vote is
        # zeroed, not just damped — owner-approved 2026-06-10), heavily damp all other directional.
        # Koopman (DMD trend extrapolation) + Langevin-Hawkes (self-exciting continuation) are the
        # modules that chase INTO a crash; in turbulence they get no vote at all.
        "koopman": 0.0, "langevin_hawkes": 0.0, "kalman": 0.45, "wavelet": 0.45,
        "info_theory": 0.40, "noiseharvest": 0.45, "tda": 0.55, "ising": 0.50,
        "chaos": 0.45, "quantum": 0.55,
        "statphys_soc": 1.20, "rmt": 1.10, "hmm_regime": 1.10,
    },
    "neutral": {},   # no regime conviction → trust the bank as-is
}


@dataclass(frozen=True)
class RouterState:
    """The Meta-Router verdict for one symbol (attached to the Decision for visibility)."""
    regime: str                         # canonical: trending|mean_revert|turbulent|neutral
    confidence: float                   # [0,1] how strongly the regime was detected
    change_point_prob: float            # BOCPD P(recent change)
    crash_warning: float                # StatPhysSOC early-warning [0,1]
    trend_score: float                  # signed persistence proxy [-1,1] (Hurst+velocity)
    hmm_regime: Optional[str]           # bull|bear|turbulent|unknown|None
    gains: dict = field(default_factory=dict)        # module -> applied gain
    deactivated: list = field(default_factory=list)  # modules the router zeroed out
    strength: float = 1.0               # router_strength blend actually applied

    def to_dict(self) -> dict:
        return {
            "regime": self.regime,
            "confidence": round(float(self.confidence), 4),
            "change_point_prob": round(float(self.change_point_prob), 4),
            "crash_warning": round(float(self.crash_warning), 4),
            "trend_score": round(float(self.trend_score), 4),
            "hmm_regime": self.hmm_regime,
            "gains": {k: round(float(v), 4) for k, v in self.gains.items()},
            "deactivated": list(self.deactivated),
            "strength": round(float(self.strength), 4),
        }


def _by_module(outputs: list[ModuleOutput]) -> dict[str, ModuleOutput]:
    return {o.module: o for o in outputs}


def _feat(o: Optional[ModuleOutput], key: str, default: float = 0.0) -> float:
    if o is None or not o.ok:
        return default
    try:
        v = o.features.get(key)
        return float(v) if v is not None else default
    except (TypeError, ValueError, AttributeError):
        return default


def detect_regime(outputs: list[ModuleOutput]) -> tuple[str, float, dict]:
    """Canonical regime from the bank's regime-speaking experts (Task 1).

    Priority: a firing change-point / crisis detector overrides everything (turbulent),
    else a clear trend vs mean-revert split from Hurst + Kalman velocity, else neutral.
    Returns (regime, confidence, evidence dict)."""
    m = _by_module(outputs)

    p_change = _feat(m.get("bocpd"), "p_change", 0.0)
    crash = _feat(m.get("statphys_soc"), "crash_warning", 0.0)
    criticality = _feat(m.get("statphys_soc"), "criticality", 0.0)
    hmm = m.get("hmm_regime")
    hmm_regime = hmm.regime_tag if (hmm is not None and hmm.ok) else None
    hmm_conf = _feat(hmm, "posterior_conf", 0.0)

    hurst = _feat(m.get("chaos"), "hurst", 0.5)
    vel_z = _feat(m.get("kalman"), "velocity_sigma", 0.0)

    # signed persistence proxy: (Hurst-0.5) sets trend-vs-revert, Kalman velocity sets sign.
    # |trend_score| → trending strength; Hurst<0.5 with small velocity → mean-revert.
    persistence = (hurst - 0.5) * 2.0                       # [-1,1], >0 trending memory
    import math
    trend_score = max(-1.0, min(1.0, math.tanh(vel_z) * max(0.0, persistence) * 2.0
                                if persistence > 0 else math.tanh(vel_z) * 0.3))

    evidence = {"p_change": round(p_change, 4), "crash_warning": round(crash, 4),
                "criticality": round(criticality, 4), "hmm": hmm_regime,
                "hurst": round(hurst, 4), "velocity_sigma": round(vel_z, 4)}

    # 1) crisis / change-point dominates — protect capital before chasing direction
    turb = max(p_change, crash, (hmm_conf if hmm_regime == "turbulent" else 0.0),
               criticality if criticality >= 0.6 else 0.0)
    if turb >= 0.5:
        return "turbulent", float(min(1.0, turb)), evidence

    # 2) trend vs mean-revert from persistence
    if persistence >= 0.10 and abs(vel_z) >= 0.5:
        return "trending", float(min(1.0, abs(trend_score))), evidence
    if persistence <= -0.10:
        # anti-persistent memory → mean-reverting; confidence from how sub-0.5 Hurst is
        return "mean_revert", float(min(1.0, abs(persistence))), evidence

    return "neutral", 0.0, evidence


def route(outputs: list[ModuleOutput], *, ic_map: Optional[dict] = None,
          strength: float = 1.0, canary: Optional[dict] = None) -> RouterState:
    """Detect the regime and compute the per-module gain (Task 2 + Task 3).

    gain = clip(regime_profile × ic_multiplier, 0, GAIN_CEIL), blended toward 1.0 by
    `strength`. Returns a RouterState whose .gains the Fusion ALU multiplies into the
    directional weights. Gate experts (BOCPD) are never regime-gated.

    `canary` (Phase-7c): an owner-approved bounded-canary override {module, regime, candidate}. When the
    DETECTED regime matches the canary's regime, the canary module's profile base is replaced by the
    candidate — ONE bounded (module, regime) context only. None (default) ⇒ byte-identical to no canary."""
    regime, confidence, evidence = detect_regime(outputs)
    profile = _PROFILES.get(regime, {})
    strength = max(0.0, min(1.0, float(strength)))
    ic_map = ic_map or {}
    # canary override applies ONLY in its own regime context, to its ONE module (bounded blast radius)
    cov_module, cov_base = None, None
    if canary and canary.get("regime") == regime and canary.get("module"):
        try:
            cov_base = float(canary["candidate"])
            cov_module = str(canary["module"])
        except (TypeError, ValueError, KeyError):
            cov_module, cov_base = None, None

    gains: dict[str, float] = {}
    deactivated: list[str] = []
    for o in outputs:
        name = o.module
        if name in GATE_EXPERTS:
            gains[name] = 1.0                    # the stability gate is regime-agnostic
            continue

        base = profile.get(name, 1.0)            # regime profile (Task 2)
        if name == cov_module and cov_base is not None:
            base = cov_base                      # bounded-canary override (owner-approved, one context)
        ic_mult = _ic_multiplier(ic_map.get(name))   # adaptive IC (Task 3)
        raw = base * ic_mult
        # blend toward 1.0 by router strength (0 = router off)
        g = 1.0 + (raw - 1.0) * strength
        g = max(0.0, min(GAIN_CEIL, g))
        gains[name] = g
        if g <= 1e-6 and o.direction != 0.0 and o.ok:
            deactivated.append(name)

    return RouterState(
        regime=regime, confidence=confidence,
        change_point_prob=evidence["p_change"], crash_warning=evidence["crash_warning"],
        trend_score=round(float(_trend_from_evidence(evidence)), 4),
        hmm_regime=evidence["hmm"], gains=gains, deactivated=deactivated,
        strength=strength,
    )


def _trend_from_evidence(evidence: dict) -> float:
    import math
    persistence = (float(evidence.get("hurst", 0.5)) - 0.5) * 2.0
    vel = float(evidence.get("velocity_sigma", 0.0))
    return max(-1.0, min(1.0, math.tanh(vel) * max(0.0, persistence) * 2.0
                         if persistence > 0 else math.tanh(vel) * 0.3))


def _ic_multiplier(ic: Optional[float]) -> float:
    """Map a module's rolling IC to a weight multiplier in [IC_FLOOR_MULT, IC_CEIL_MULT].

    IC is the correlation between the module's past votes and realized forward returns:
      IC ≤ 0   → not (anti-)predictive lately → damp toward the floor (don't trust it)
      IC ≈ 0.1 → ~neutral (1.0)
      IC ≥ 0.3 → strongly predictive → boost toward the ceil
    None (not enough samples yet) → 1.0 (no adjustment until the window fills)."""
    if ic is None:
        return 1.0
    try:
        ic = float(ic)
    except (TypeError, ValueError):
        return 1.0
    if ic != ic:                                 # NaN
        return 1.0
    if ic <= 0.0:
        # linearly damp from 1.0 at IC=0 down to the floor at IC=-0.2
        frac = max(0.0, min(1.0, (-ic) / 0.2))
        return 1.0 + (IC_FLOOR_MULT - 1.0) * frac
    # boost from 1.0 at IC=0 up to the ceil at IC=0.3
    frac = max(0.0, min(1.0, ic / 0.3))
    return 1.0 + (IC_CEIL_MULT - 1.0) * frac
