"""ChaosModule — PHYSICS (Domain 5: Chaos theory & nonlinear dynamical systems).

Estimates the **Hurst exponent** H of the price path via the structure-function method
(std of lag-differences scales as lag^H). H is the fingerprint of the dynamics:
  H > 0.5  → persistent / TRENDING  (momentum: trade WITH the recent move)
  H < 0.5  → anti-persistent / MEAN-REVERTING (fade the recent move)
  H ≈ 0.5  → random walk (no edge → abstain-ish, low conviction)

A short-horizon predictability proxy (lag-1 return autocorrelation) sharpens the call.
This is the module that tells the circuit whether momentum or reversion is the right lens
for a symbol right now. Pure numpy; abstains on insufficient data.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_TF = "5m"
_MIN_BARS = 50
_RET_LOOKBACK = 5
_HORIZON_MIN = 60


class ChaosModule(Module):
    name = "chaos"
    evidence_family = "persistence"  # §6g.330 family-correlation penalty
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        closes = frame.closes(_TF)
        if closes is None or len(closes) < _MIN_BARS:
            return ModuleOutput.abstain(self.name, "insufficient_bars", self.horizon_min)
        closes = closes[-120:]
        if not np.all(np.isfinite(closes)) or float(np.min(closes)) <= 0:
            return ModuleOutput.abstain(self.name, "bad_prices", self.horizon_min)

        logp = np.log(closes)
        H = _hurst(logp)
        if H is None:
            return ModuleOutput.abstain(self.name, "hurst_degenerate", self.horizon_min)

        rets = np.diff(logp)
        # short-horizon momentum sign
        recent = float(logp[-1] - logp[-1 - _RET_LOOKBACK])
        mom_sign = float(np.sign(recent)) or 1.0
        # lag-1 autocorrelation of returns (corroborates trend vs reversion)
        ac1 = _autocorr1(rets)

        bias = H - 0.5
        strength = float(min(abs(bias) * 2.0, 1.0))   # |H-0.5| in [0,0.5] → [0,1]
        if bias > 0.05:                                # trending
            direction = mom_sign * strength
            regime = "trending"
        elif bias < -0.05:                             # mean-reverting → fade
            direction = -mom_sign * strength
            regime = "mean_revert"
        else:
            direction = 0.0
            regime = "random"

        # predictability: |autocorr| adds confidence; near-random caps it
        conviction = float(min(strength * (0.6 + 0.4 * min(abs(ac1) * 3.0, 1.0)), 1.0))
        if regime == "random":
            conviction *= 0.3

        expl = (f"Chaos: Hurst H={H:.3f} ({regime}), lag1-autocorr={ac1:+.3f}, "
                f"recent {('+' if recent >= 0 else '')}{np.expm1(recent)*100:.2f}% → "
                f"{'long' if direction > 0 else 'short' if direction < 0 else 'flat'}")
        return ModuleOutput(
            module=self.name, direction=direction, conviction=conviction,
            expected_move_pct=None, horizon_min=self.horizon_min, regime_tag=regime,
            features={"tf": _TF, "hurst": round(H, 4), "autocorr1": round(ac1, 4),
                      "recent_ret_pct": round(float(np.expm1(recent) * 100.0), 4),
                      "strength": round(strength, 4)},
            explanation=expl)


def _hurst(logp: np.ndarray) -> float | None:
    """Structure-function Hurst: std(logp[t+lag]-logp[t]) ∝ lag^H. Slope of the
    log-log fit = H. Returns None if degenerate (flat series)."""
    n = len(logp)
    lags = np.arange(2, min(20, n // 2))
    if len(lags) < 4:
        return None
    tau = []
    for lag in lags:
        diff = logp[lag:] - logp[:-lag]
        s = float(np.std(diff))
        tau.append(s if s > 0 else 1e-12)
    tau = np.asarray(tau)
    if np.all(tau <= 1e-11):
        return None
    coeffs = np.polyfit(np.log(lags), np.log(tau), 1)
    H = float(coeffs[0])
    return float(max(0.0, min(1.0, H)))


def _autocorr1(x: np.ndarray) -> float:
    if len(x) < 3:
        return 0.0
    x = x - x.mean()
    denom = float(np.dot(x, x))
    if denom <= 0:
        return 0.0
    return float(np.dot(x[:-1], x[1:]) / denom)
