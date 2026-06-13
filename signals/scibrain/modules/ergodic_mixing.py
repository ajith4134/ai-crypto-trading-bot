"""ErgodicMixingModule — DYNAMICAL SYSTEMS (mixing time / ergodicity); emits an ALPHA-HALF-LIFE +
SAMPLE-TRUST gate (Hot-Pair, design §6e Tier-B, role=gate+horizon). SHADOW research.

UNIQUE EVIDENCE vs the bank (§6g.2): no existing module measures the TEMPORAL trustworthiness of its own
inputs. This one answers two questions the alpha modules silently ASSUME:
 1. ALPHA HALF-LIFE — how fast does the return autocorrelation DECAY (the mixing / decorrelation time, a
    Ruelle-resonance / spectral-gap proxy)? A short mixing time ⇒ any edge decays before a long horizon ⇒
    trade a SHORTER horizon. Distinct from Chaos (Hurst), Wavelet (scale energy), BOCPD (change-points).
 2. SAMPLE REPRESENTATIVENESS — is the window ERGODIC enough that its historical statistics transfer? An
    ergodicity-breaking statistic (dispersion of sub-window time-averages vs the pooled estimate) flags
    when the sample is NON-stationary/non-representative ⇒ the IC measured on it should be trusted less.
 + PERMUTATION ENTROPY (Bandt-Pompe ordinal complexity) — model-free predictability: ~1 = white/no
    structure, lower = exploitable order. evidence_family='ergodicity'.

ROLE = GATE+HORIZON (not direction): direction=0, conviction = SAMPLE_TRUST ∈ [floor,1] (the fusion size
damper). High permutation entropy (no structure) OR strong ergodicity-breaking (non-representative) OR a
mixing time far shorter than the trade horizon ⇒ low trust ⇒ damp; the estimated alpha half-life + valid
horizon ride in features.

ADMISSION (§6g + Rule 14): shadow_only=True — RECORDED + evaluable, NEVER applied to a live pick; stays
shadow / pruned unless it proves incremental IC. Pure numpy, deterministic, abstains on insufficient data.
"""
from __future__ import annotations

import math
from itertools import permutations

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_TF = "1h"
_MIN_BARS = 120
_WINDOW = 200
_PE_DIM = 3           # permutation-entropy embedding dimension (d! = 6 ordinal patterns)
_MAX_LAG = 30         # autocorrelation lags for the mixing-time estimate
_N_SUBWIN = 5         # sub-windows for the ergodicity-breaking statistic
_HORIZON_MIN = 60
_BAR_MIN = 60
_TRUST_FLOOR = 0.2


def _permutation_entropy(x: np.ndarray, d: int = _PE_DIM) -> float:
    """Normalized Bandt-Pompe permutation entropy ∈ [0,1] (1 = max disorder / unpredictable)."""
    n = x.size - d + 1
    if n < 10:
        return 1.0
    patt_index = {p: i for i, p in enumerate(permutations(range(d)))}
    counts = np.zeros(len(patt_index))
    for i in range(n):
        order = tuple(np.argsort(x[i:i + d], kind="stable"))
        counts[patt_index[order]] += 1
    p = counts[counts > 0] / counts.sum()
    h = -np.sum(p * np.log(p))
    return float(h / math.log(math.factorial(d)))


def _mixing_time(r: np.ndarray) -> tuple[float, float]:
    """Decorrelation/mixing time from the return ACF: integral time τ=1+2Σ|ρ(k)| (Ruelle/spectral-gap
    proxy) and the lag where |ρ| first falls below 1/e. Returns (integral_time, half_life)."""
    r0 = r - r.mean()
    var = float(np.dot(r0, r0))
    if var <= 1e-18:
        return 1.0, 1.0
    acf = []
    for k in range(1, min(_MAX_LAG, r.size - 2) + 1):
        acf.append(float(np.dot(r0[:-k], r0[k:]) / var))
    acf = np.asarray(acf)
    integral_time = float(1.0 + 2.0 * np.sum(np.abs(acf)))
    below = np.where(np.abs(acf) < math.exp(-1.0))[0]
    half_life = float(below[0] + 1) if below.size else float(acf.size)
    return integral_time, half_life


def _ergodicity_breaking(r: np.ndarray) -> float:
    """EB statistic ∈ [0,1): dispersion of sub-window VARIANCES (time-averaged 2nd moment) about their
    mean. 0 ⇒ ergodic/stationary (every sub-window agrees); large ⇒ non-representative sample."""
    parts = np.array_split(r, _N_SUBWIN)
    v = np.array([float(p.var()) for p in parts if p.size > 3])
    if v.size < 3 or v.mean() <= 1e-18:
        return 0.0
    cv = float(v.std() / v.mean())                    # coefficient of variation of sub-window variances
    return float(np.clip(cv / (1.0 + cv), 0.0, 1.0))   # squashed to [0,1)


class ErgodicMixingModule(Module):
    name = "ergodic_mixing"
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        arr = frame.candles.get(_TF)
        if arr is None or len(arr) < _MIN_BARS:
            return ModuleOutput.abstain(self.name, "insufficient_bars", self.horizon_min,
                                        role="gate", evidence_family="ergodicity", shadow_only=True)
        c = arr[-_WINDOW:, 4]
        if not np.all(np.isfinite(c)) or float(np.min(c)) <= 0:
            return ModuleOutput.abstain(self.name, "bad_prices", self.horizon_min,
                                        role="gate", evidence_family="ergodicity", shadow_only=True)
        r = np.diff(np.log(c))
        r = r[np.isfinite(r)]
        if r.size < _MIN_BARS - 1 or r.std() <= 1e-12:
            return ModuleOutput.abstain(self.name, "flat_returns", self.horizon_min,
                                        role="gate", evidence_family="ergodicity", shadow_only=True)

        pe = _permutation_entropy(r)
        integral_time, half_life = _mixing_time(r)
        eb = _ergodicity_breaking(r)
        # valid horizon (bars) ≈ the decorrelation half-life; in minutes:
        valid_horizon_min = float(half_life * _BAR_MIN)
        # does the edge survive to our trade horizon? ratio<1 ⇒ edge decays first ⇒ damp
        horizon_cover = float(np.clip(valid_horizon_min / _HORIZON_MIN, 0.0, 1.0))

        # SAMPLE-TRUST gate: penalize no-structure (high PE), non-ergodic sample (high EB), and an edge
        # that decays before the horizon (low horizon_cover).
        trust = float(np.clip(
            (1.0 - 0.5 * pe) * (1.0 - 0.6 * eb) * (0.5 + 0.5 * horizon_cover),
            _TRUST_FLOOR, 1.0))
        regime = ("mixing_fast" if half_life <= 2 else
                  "mixing_slow" if half_life >= 8 else "mixing_moderate")
        ergo = "non_ergodic" if eb > 0.4 else "ergodic"

        return ModuleOutput(
            module=self.name,
            direction=0.0,                                # gate — no directional opinion
            conviction=trust,                             # [floor,1] sample-trust size damper
            expected_move_pct=None,
            horizon_min=self.horizon_min,
            regime_tag=f"{regime}_{ergo}",
            features={
                "tf": _TF,
                "permutation_entropy": round(pe, 4),         # 1 = unpredictable
                "mixing_integral_time": round(integral_time, 4),
                "alpha_half_life_bars": round(half_life, 4),
                "valid_horizon_min": round(valid_horizon_min, 1),
                "horizon_cover": round(horizon_cover, 4),
                "ergodicity_breaking": round(eb, 4),         # >0.4 ⇒ non-representative window
                "sample_trust": round(trust, 4),
                "n_returns": int(r.size),
            },
            explanation=(f"ergodic_mixing: PE={pe:.2f}, half-life {half_life:.1f} bars "
                         f"(~{valid_horizon_min:.0f}m), EB={eb:.2f} ({ergo}) → sample_trust {trust:.2f}"),
            ok=True,
            role="gate",
            evidence_family="ergodicity",
            shadow_only=True,
        )
