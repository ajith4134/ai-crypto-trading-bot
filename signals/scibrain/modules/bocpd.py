"""BOCPDModule — Bayesian Online Changepoint Detection (master notes BLOCK 6 / 14).

Adams & MacKay (2007): maintain a posterior over the "run length" r_t = time since
the last regime change. A Normal-Inverse-Gamma prior on each run's (mean, variance)
gives a Student-t predictive; a constant hazard H = 1/λ sets the prior change rate.
λ is matched to the holding horizon (BLOCK 14: λ ≈ horizon × 5 in bars).

This module is NOT directional — it is the circuit's **stability gate**. It emits:
  - direction ≈ 0 (it does not pick long/short)
  - conviction = stability = 1 - P(recent change)  → the Fusion ALU multiplies the
    directional modules' conviction by this, so a likely regime change DAMPENS bets.
  - regime_tag: 'regime_change' | 'settling' | 'stable'
  - features.p_change, features.expected_run_len for transparency.

Pure numpy; O(T²) with T capped, fast for the buffer. Abstains on insufficient data.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_TF = "5m"
_MIN_BARS = 30
_MAX_BARS = 120          # cap the O(T²) recursion
_RECENT_K = 3            # run lengths <= K count as a "recent" change
_HORIZON_MIN = 60

# Normal-Inverse-Gamma prior (weakly informative on standardised returns)
_MU0, _KAPPA0, _ALPHA0, _BETA0 = 0.0, 1.0, 1.0, 1.0


class BOCPDModule(Module):
    name = "bocpd"
    evidence_family = "changepoint"  # §6g.330 family-correlation penalty
    role = "gate"
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        closes = frame.closes(_TF)
        if closes is None or len(closes) < _MIN_BARS + 1:
            return ModuleOutput.abstain(self.name, "insufficient_bars", self.horizon_min)
        closes = closes[-(_MAX_BARS + 1):]
        if not np.all(np.isfinite(closes)) or float(np.min(closes)) <= 0:
            return ModuleOutput.abstain(self.name, "bad_prices", self.horizon_min)

        rets = np.diff(np.log(closes)) * 100.0          # % log-returns
        sd = float(np.std(rets)) or 1.0
        rets = rets / sd                                 # standardise → prior is scale-free
        if not np.all(np.isfinite(rets)):
            return ModuleOutput.abstain(self.name, "bad_returns", self.horizon_min)

        # hazard from the horizon: expected run length ≈ horizon_in_bars (BLOCK 14)
        lam_bars = max(10.0, float(self.horizon_min / 5.0) * 5.0)
        hazard = 1.0 / lam_bars

        rl_post = _bocpd_run_length(rets, hazard,
                                    _MU0, _KAPPA0, _ALPHA0, _BETA0)
        # rl_post is the run-length distribution AFTER the last observation
        p_change = float(rl_post[:_RECENT_K].sum())      # mass on very short runs
        run_idx = np.arange(len(rl_post))
        expected_run = float((run_idx * rl_post).sum())

        stability = float(max(0.0, min(1.0, 1.0 - p_change)))
        regime = ("regime_change" if p_change > 0.5
                  else "settling" if p_change > 0.2
                  else "stable")
        expl = (f"BOCPD: P(recent change)={p_change:.3f}, "
                f"E[run len]={expected_run:.1f} bars, hazard λ={lam_bars:.0f} → {regime} "
                f"(stability {stability:.3f})")
        return ModuleOutput(
            module=self.name,
            direction=0.0,                               # non-directional gate
            conviction=stability,
            expected_move_pct=None,
            horizon_min=self.horizon_min,
            regime_tag=regime,
            features={
                "tf": _TF, "p_change": p_change, "expected_run_len": expected_run,
                "hazard_lambda_bars": lam_bars, "ret_sd_pct": sd,
            },
            explanation=expl,
        )


def _bocpd_run_length(x: np.ndarray, hazard: float,
                      mu0: float, kappa0: float,
                      alpha0: float, beta0: float) -> np.ndarray:
    """Adams-MacKay forward recursion. Returns the run-length posterior P(r_t | x_{1:t})
    after the final observation, as a 1-D array indexed by run length 0..t."""
    T = len(x)
    # R[r] = P(run length = r) at the current step; grows by one each iteration.
    R = np.array([1.0])                                  # r_0 = 0 with prob 1
    mu = np.array([mu0]); kappa = np.array([kappa0])
    alpha = np.array([alpha0]); beta = np.array([beta0])

    for t in range(T):
        xt = x[t]
        # Student-t predictive prob of xt for every active run length
        pred = _student_t_pdf(xt, mu, kappa, alpha, beta)

        growth = R * pred * (1.0 - hazard)               # run continues (r -> r+1)
        cp = float(np.sum(R * pred * hazard))            # changepoint (r -> 0)

        new_R = np.empty(R.size + 1)
        new_R[0] = cp
        new_R[1:] = growth
        s = float(new_R.sum())
        if s <= 0 or not np.isfinite(s):
            # numerical underflow → treat as a forced change, reset
            new_R = np.zeros(R.size + 1); new_R[0] = 1.0
            s = 1.0
        R = new_R / s

        # Normal-Inverse-Gamma sufficient-statistic update (conjugate)
        new_mu = np.empty(mu.size + 1); new_kappa = np.empty(kappa.size + 1)
        new_alpha = np.empty(alpha.size + 1); new_beta = np.empty(beta.size + 1)
        new_mu[0], new_kappa[0] = mu0, kappa0
        new_alpha[0], new_beta[0] = alpha0, beta0
        new_kappa[1:] = kappa + 1.0
        new_mu[1:] = (kappa * mu + xt) / (kappa + 1.0)
        new_alpha[1:] = alpha + 0.5
        new_beta[1:] = beta + (kappa * (xt - mu) ** 2) / (2.0 * (kappa + 1.0))
        mu, kappa, alpha, beta = new_mu, new_kappa, new_alpha, new_beta

    return R


def _student_t_pdf(x: float, mu: np.ndarray, kappa: np.ndarray,
                   alpha: np.ndarray, beta: np.ndarray) -> np.ndarray:
    """Posterior-predictive Student-t density of the NIG model, vectorised over runs.
    df = 2α, loc = μ, scale² = β(κ+1)/(ακ)."""
    df = 2.0 * alpha
    scale2 = beta * (kappa + 1.0) / (alpha * kappa)
    scale2 = np.maximum(scale2, 1e-12)
    z = (x - mu) ** 2 / (df * scale2)
    # log pdf of Student-t (avoids scipy dependency, fully vectorised)
    from math import lgamma  # noqa: local import keeps module import cheap
    lg = np.vectorize(lgamma)
    log_norm = lg((df + 1.0) / 2.0) - lg(df / 2.0) \
        - 0.5 * np.log(df * np.pi * scale2)
    log_pdf = log_norm - ((df + 1.0) / 2.0) * np.log1p(z)
    return np.exp(log_pdf)
