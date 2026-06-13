"""EVTLargeDeviationTail — Peaks-Over-Threshold / Generalized Pareto tail fit; turn "fat-tail
warning" into a CALIBRATED adverse-tail probability + risk envelope (Hot-Pair, design §6e Tier-A,
RISK plane, role=risk → gate+size-cap).

UNIQUE EVIDENCE (§6g.1/2): the bank has crash *early-warning* heuristics (StatPhysSOC power-law tail,
Chaos/Wavelet) but NO explicitly calibrated tail PROBABILITY — design §6d names this gap directly
("tail probability is not explicitly calibrated, despite real-money operation"). Extreme Value Theory
is the right tool: above a high threshold the exceedances of ANY heavy-tailed return process converge
to the Generalized Pareto Distribution (Pickands–Balkema–de Haan), so a POT/GPD fit gives a principled
P(adverse move > m) and Expected Shortfall instead of an ad-hoc "looks risky". evidence_family='tail'.

ROLE = RISK, NOT DIRECTION. Unlike the 15 directional/context modules, this emits direction=0 and a
conviction = tail_SAFETY ∈ [floor,1] — i.e. it is a GATE: the existing fusion already turns a
direction-0/conviction>0 output into a multiplicative size damper (stability = Π gate_conviction). A
heavy adverse tail ⇒ low safety ⇒ size capped. (It also computes BOTH tails so the asymmetry/ES are
available to a future direction-aware size/allocator consumer.)

COMPUTATION (deterministic, closed-form — no iterative MLE; §6g.4/5):
  • returns r over a window; losses L=-r (down/adverse tail) and gains G=+r (up tail).
  • POT: threshold u = quantile(side, _THRESH_Q); exceedances y = side[side>u]-u.
  • GPD(ξ,σ) by METHOD OF MOMENTS on y:  ξ = ½(1 − m²/s²),  σ = ½·m·(1 + m²/s²)  (m,s² = mean,var of y;
    parameterization F(y)=1−(1+ξy/σ)^(−1/ξ); ξ>0 ⇒ heavy/fat tail). ξ clipped to a sane range.
  • adverse-tail prob of a reference move m*:  P(L>m*) = (Nu/N)·[1+ξ(m*−u)/σ]^(−1/ξ), horizon-scaled.
  • VaR_p = u + (σ/ξ)[((N/Nu)(1−p))^(−ξ) − 1];  ES_p = (VaR_p + σ − ξu)/(1−ξ)  (ξ<1).
  • rare-event rate λ = Nu/N; tail asymmetry = ξ_L − ξ_R; extremal index θ (runs estimator;
    θ<1 ⇒ exceedances CLUSTER = volatility clustering, which AMPLIFIES realized tail risk).
  • Hill index reported as a cross-check heavy-tail estimate.

GATE: severity = clip(P_horizon·(1+max(ξ_adverse,0))/θ, 0,1) over the worse of the two tails;
tail_safety = clip(1 − severity, _SAFETY_FLOOR, 1) = conviction.

ADMISSION (§6g + Rule 14, design VS-11 "risk shadow authority"): shadow_only=True — the full envelope
is RECORDED on every scored symbol (visible + evaluable) but NEVER caps live size until promoted by the
owner. Deterministic, bounded, abstains when too few exceedances to fit. NOTE: the tail-CALIBRATION
evaluation (predicted P vs realized exceedance, Brier/reliability) is a MAIN-PROCESS concern — this
module runs in the per-symbol fork workers, so it must not run a settle sweep here; calibration is wired
in the risk-plane evaluation step (pairs with the allocator, VS-11 / Phase 7c promotion gate).
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_TF = "1h"             # the only SensorFrame TF that carries enough history (200 bars vs ≤65 on ≤30m)
_MIN_BARS = 120        # need a decent sample for a tail fit
_WINDOW = 200          # bars of returns used (≈8 days on 1h)
_THRESH_Q = 0.90       # POT threshold = this quantile of the side (loss/gain) magnitudes
_MIN_EXC = 10          # need ≥ this many exceedances to fit a GPD, else abstain
_XI_LO, _XI_HI = -0.5, 0.95
_ADVERSE_K = 3.0       # reference adverse move m* = _ADVERSE_K × window return std
_VAR_P = 0.99          # VaR/ES quantile
_HORIZON_MIN = 60      # risk-plane refresh / forward horizon the adverse prob is scaled to (=1 bar @1h)
_BAR_MIN = 60          # _TF minutes (for horizon scaling)
_SAFETY_FLOOR = 0.15   # a tail-risky name is damped, not fully vetoed


def _fit_gpd_mom(y: np.ndarray):
    """GPD(ξ,σ) by method of moments on exceedances y. Returns (xi, sigma) with xi clipped.
    MoM is closed-form/deterministic (no MLE iteration); valid while the exceedance variance is
    finite — clipping ξ guards the heavy-tail regime where MoM would otherwise blow up."""
    m = float(y.mean())
    s2 = float(y.var())
    if m <= 0 or s2 <= 1e-18:
        return 0.0, max(m, 1e-9)
    ratio = m * m / s2
    xi = 0.5 * (1.0 - ratio)
    sigma = 0.5 * m * (1.0 + ratio)
    xi = float(np.clip(xi, _XI_LO, _XI_HI))
    sigma = float(max(sigma, 1e-9))
    return xi, sigma


def _hill(side_sorted_desc: np.ndarray, k: int) -> float:
    """Hill tail-index estimate from the top-k order statistics (unambiguous heavy-tail cross-check)."""
    if k < 3 or side_sorted_desc[k - 1] <= 0:
        return 0.0
    top = side_sorted_desc[:k]
    top = top[top > 0]
    if top.size < 3:
        return 0.0
    return float(np.mean(np.log(top)) - np.log(side_sorted_desc[k - 1]))


def _extremal_index(mask: np.ndarray) -> float:
    """Runs estimator of the extremal index θ = #clusters/#exceedances ∈ (0,1].
    θ→1 isolated exceedances; θ<1 ⇒ clustering (consecutive exceedances)."""
    n_exc = int(mask.sum())
    if n_exc == 0:
        return 1.0
    # a cluster starts at each exceedance not immediately preceded by an exceedance
    prev = np.concatenate([[False], mask[:-1]])
    n_clusters = int(np.sum(mask & ~prev))
    return float(np.clip(n_clusters / n_exc, 1e-3, 1.0))


def _tail_stats(side: np.ndarray, ret_std: float):
    """POT/GPD risk envelope for one side (losses or gains, both as POSITIVE magnitudes).
    Returns a dict or None if too few exceedances to fit."""
    n = side.size
    u = float(np.quantile(side, _THRESH_Q))
    mask = side > u
    y = side[mask] - u
    n_exc = int(y.size)
    if n_exc < _MIN_EXC or u <= 0:
        return None
    xi, sigma = _fit_gpd_mom(y)
    lam = n_exc / float(n)                                   # rare-event (exceedance) rate
    theta = _extremal_index(mask)

    # reference adverse move and its POT tail probability (per bar)
    m_star = max(_ADVERSE_K * ret_std, u * 1.0001)
    z = 1.0 + xi * (m_star - u) / sigma
    p_bar = lam * (z ** (-1.0 / xi)) if (xi != 0 and z > 0) else lam * np.exp(-(m_star - u) / sigma)
    p_bar = float(np.clip(p_bar, 0.0, 1.0))
    # horizon scaling (independent-bar approximation, then clustering inflation via 1/θ)
    n_bars_h = max(1.0, _HORIZON_MIN / _BAR_MIN)
    p_horizon = 1.0 - (1.0 - p_bar) ** n_bars_h
    p_horizon = float(np.clip(p_horizon * (1.0 + (1.0 - theta)), 0.0, 1.0))

    # VaR / ES at _VAR_P from the POT-GPD tail
    xi_s = xi if abs(xi) > 1e-6 else 1e-6
    var_p = u + (sigma / xi_s) * (((n / max(n_exc, 1)) * (1.0 - _VAR_P)) ** (-xi_s) - 1.0)
    es_p = (var_p + sigma - xi_s * u) / (1.0 - xi_s) if xi_s < 1.0 else var_p
    hill = _hill(np.sort(side)[::-1], n_exc)

    return {
        "u": u, "xi": xi, "sigma": sigma, "n_exc": n_exc, "lambda": lam, "theta": theta,
        "m_star": m_star, "p_bar": p_bar, "p_horizon": p_horizon,
        "var": float(max(var_p, 0.0)), "es": float(max(es_p, 0.0)), "hill": hill,
    }


class EVTLargeDeviationTailModule(Module):
    name = "evt_large_deviation_tail"
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        arr = frame.candles.get(_TF)
        if arr is None or len(arr) < _MIN_BARS:
            return ModuleOutput.abstain(self.name, "insufficient_bars", self.horizon_min,
                                        role="risk", evidence_family="tail", shadow_only=True)
        c = arr[-_WINDOW:, 4]
        if not np.all(np.isfinite(c)) or float(np.min(c)) <= 0:
            return ModuleOutput.abstain(self.name, "bad_prices", self.horizon_min,
                                        role="risk", evidence_family="tail", shadow_only=True)
        r = np.diff(np.log(c))
        r = r[np.isfinite(r)]
        ret_std = float(r.std())
        if r.size < _MIN_BARS - 1 or ret_std <= 1e-12:
            return ModuleOutput.abstain(self.name, "flat_returns", self.horizon_min,
                                        role="risk", evidence_family="tail", shadow_only=True)

        loss = -r            # adverse (down) tail magnitudes (positive)
        gain = r             # up tail
        left = _tail_stats(loss, ret_std)     # adverse / downside
        right = _tail_stats(gain, ret_std)    # upside
        if left is None and right is None:
            return ModuleOutput.abstain(self.name, "too_few_exceedances", self.horizon_min,
                                        role="risk", evidence_family="tail", shadow_only=True)

        # downside is the primary adverse tail; if it couldn't fit, fall back to the upside fit
        adverse = left or right
        xi_l = left["xi"] if left else float("nan")
        xi_r = right["xi"] if right else float("nan")
        asymmetry = (xi_l - xi_r) if (left and right) else 0.0

        # GATE severity from the WORSE of the two fitted tails (a violently two-sided name is risky to
        # hold whichever way we trade it). Driven by the CALIBRATED EVT outputs that actually
        # differentiate names — tail HEAVINESS (ξ) and Expected Shortfall magnitude — not just the rare
        # 1-bar exceedance prob (which is ~1% for everything and gives a useless flat gate). Clustering
        # (low θ) amplifies realized tail risk.
        sev_candidates = []
        for t in (left, right):
            if not t:
                continue
            heaviness = np.clip(max(t["xi"], 0.0) / 0.5, 0.0, 1.0)          # ξ≥0.5 ⇒ full
            es_mag = np.clip(t["es"] / (_ADVERSE_K * ret_std * 2.0 + 1e-12), 0.0, 1.0)
            rare = np.clip(t["p_horizon"] / 0.10, 0.0, 1.0)                 # 1-bar adverse prob, 10% ⇒ full
            base = 0.5 * heaviness + 0.3 * es_mag + 0.2 * rare
            clustering_amp = 1.0 + (1.0 - t["theta"])                       # θ<1 ⇒ inflate
            sev_candidates.append(float(np.clip(base * clustering_amp, 0.0, 1.0)))
        severity = float(max(sev_candidates)) if sev_candidates else 0.0
        tail_safety = float(np.clip(1.0 - severity, _SAFETY_FLOOR, 1.0))

        heavy = "heavy" if adverse["xi"] > 0.2 else ("light" if adverse["xi"] < 0 else "moderate")
        clustered = "clustered" if adverse["theta"] < 0.7 else "isolated"
        return ModuleOutput(
            module=self.name,
            direction=0.0,                       # RISK gate — no directional opinion
            conviction=tail_safety,              # [floor,1] size damper when promoted (1=safe)
            expected_move_pct=None,
            horizon_min=self.horizon_min,
            regime_tag=f"tail_{heavy}",
            features={
                "tf": _TF,
                "adverse_tail_prob": round(adverse["p_horizon"], 5),   # P(adverse move > m*) over horizon
                "tail_safety": round(tail_safety, 4),
                "severity": round(severity, 4),
                "xi_loss": (None if left is None else round(xi_l, 4)),   # GPD shape, down tail (>0 heavy)
                "xi_gain": (None if right is None else round(xi_r, 4)),
                "tail_asymmetry": round(float(asymmetry), 4),           # ξ_loss − ξ_gain (>0 = downside heavier)
                "expected_shortfall": round(adverse["es"], 5),         # ES/CVaR @ _VAR_P, adverse tail
                "var": round(adverse["var"], 5),
                "rare_event_rate": round(adverse["lambda"], 4),        # exceedance frequency
                "extremal_index": round(adverse["theta"], 4),          # <1 ⇒ clustering
                "hill_index": round(adverse["hill"], 4),
                "n_exceedances": int(adverse["n_exc"]),
                "ret_std": round(ret_std, 6),
            },
            explanation=(f"evt_pot_gpd: {heavy} adverse tail ξ={adverse['xi']:+.2f}, "
                         f"P(adverse>m*)={adverse['p_horizon']:.1%} over {self.horizon_min}m, "
                         f"ES={adverse['es']:.3f}, exceedances {clustered} (θ={adverse['theta']:.2f}) "
                         f"→ tail_safety {tail_safety:.2f}"),
            ok=True,
            role="risk",
            evidence_family="tail",
            shadow_only=True,
        )
