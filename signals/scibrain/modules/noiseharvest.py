"""NoiseHarvestModule — turn NOISE into edge (Domain 19: stochastic processes / OU).

Owner emphasis: "noise detection and using that noise to our advantage." A price path =
trend + noise. We strip the trend (moving average) and model the residual as an
Ornstein-Uhlenbeck mean-reverting process:  dr = θ(μ − r)dt + σ dW.

We fit OU via the AR(1) discretisation  r_t = a + b·r_{t-1} + ε  (b = e^{−θΔt}):
  - 0 < b < 1  → genuinely mean-reverting (half-life = −ln2/ln b). This is harvestable noise.
  - b ≥ 1      → not reverting (trend/random) → abstain.
When reverting and the residual is stretched (|z| large), we FADE it:
  residual below mean (z<0) → LONG; above mean (z>0) → SHORT. Strength scales with |z| and
  the reversion speed. The expected move back to the mean is a real % target.

Pure numpy; abstains when the noise isn't a reverting OU process.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_TF = "5m"
_MIN_BARS = 40
_MA_WIN = 20
_Z_ENTER = 1.0          # only act when the residual is >1σ from the mean
_HORIZON_MIN = 60


class NoiseHarvestModule(Module):
    name = "noiseharvest"
    evidence_family = "mean_reversion"  # §6g.330 family-correlation penalty
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        closes = frame.closes(_TF)
        if closes is None or len(closes) < _MIN_BARS:
            return ModuleOutput.abstain(self.name, "insufficient_bars", self.horizon_min)
        closes = closes[-120:]
        if not np.all(np.isfinite(closes)) or float(np.min(closes)) <= 0:
            return ModuleOutput.abstain(self.name, "bad_prices", self.horizon_min)

        logp = np.log(closes)
        # trend = rolling mean; residual = the noise around it
        ma = _rolling_mean(logp, _MA_WIN)
        resid = logp[_MA_WIN - 1:] - ma                  # aligned residual series
        if len(resid) < 15:
            return ModuleOutput.abstain(self.name, "short_residual", self.horizon_min)

        sd = float(np.std(resid))
        if sd <= 1e-9:
            return ModuleOutput.abstain(self.name, "no_noise", self.horizon_min)

        b, mu = _ar1(resid)
        if b is None or not (0.0 < b < 1.0):
            return ModuleOutput.abstain(self.name, "not_mean_reverting", self.horizon_min)
        half_life = float(-np.log(2.0) / np.log(b))       # bars to revert halfway

        z = float((resid[-1] - mu) / sd)
        if abs(z) < _Z_ENTER:
            return ModuleOutput.abstain(self.name, "within_band", self.horizon_min)

        # FADE the stretch: below mean → long, above → short
        direction = float(-np.sign(z) * min(abs(z) / 2.0, 1.0))
        # reversion speed → confidence; faster reversion (smaller half-life) = stronger
        speed = float(min(1.0, (1.0 - b) * 4.0))
        conviction = float(min(min(abs(z) / 2.0, 1.0) * (0.5 + 0.5 * speed), 1.0))
        # expected move = distance back to the mean (in %)
        exp_move_pct = float(np.expm1(abs(resid[-1] - mu)) * 100.0)

        expl = (f"NoiseHarvest(OU): z={z:+.2f}σ from mean, b={b:.3f} "
                f"(half-life {half_life:.1f} bars) → fade {'long' if direction > 0 else 'short'}, "
                f"revert ~{exp_move_pct:.2f}%")
        return ModuleOutput(
            module=self.name, direction=direction, conviction=conviction,
            expected_move_pct=exp_move_pct, horizon_min=self.horizon_min,
            regime_tag="mean_revert",
            features={"tf": _TF, "z": round(z, 4), "ou_b": round(b, 4),
                      "half_life_bars": round(half_life, 2), "resid_sd": round(sd, 6),
                      "reversion_speed": round(speed, 4)},
            explanation=expl)


def _rolling_mean(x: np.ndarray, win: int) -> np.ndarray:
    """Trailing simple moving average; output length = len(x)-win+1 (aligned to x[win-1:])."""
    c = np.cumsum(np.insert(x, 0, 0.0))
    return (c[win:] - c[:-win]) / float(win)


def _ar1(r: np.ndarray):
    """Fit r_t = a + b·r_{t-1}. Returns (b, mu=a/(1-b)) or (None, None) if degenerate."""
    x = r[:-1]
    y = r[1:]
    xc = x - x.mean()
    denom = float(np.dot(xc, xc))
    if denom <= 1e-12:
        return None, None
    b = float(np.dot(xc, y - y.mean()) / denom)
    a = float(y.mean() - b * x.mean())
    if abs(1.0 - b) < 1e-9:
        return None, None
    mu = a / (1.0 - b)
    return b, mu
