"""KalmanModule — state estimation (Domain 2: Kalman filter / optimal Bayesian filtering).

A constant-velocity Kalman filter on log-price estimates two hidden states the raw price
hides under noise: the smoothed LEVEL (fair value) and the VELOCITY (instantaneous drift).
  - velocity sign → directional vote (the de-noised trend), strength from velocity vs its
    own recent scale.
  - the innovation (price − predicted level) flags mispricing/over-extension and damps
    conviction when the filter is being surprised (regime break).

This is the circuit's clean momentum estimate — it sees through tick noise that fools a raw
return. Pure numpy; abstains on insufficient data.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_TF = "5m"
_MIN_BARS = 40
_HORIZON_MIN = 60


class KalmanModule(Module):
    name = "kalman"
    evidence_family = "trend_momentum"  # §6g.330 family-correlation penalty
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        closes = frame.closes(_TF)
        if closes is None or len(closes) < _MIN_BARS:
            return ModuleOutput.abstain(self.name, "insufficient_bars", self.horizon_min)
        closes = closes[-120:]
        if not np.all(np.isfinite(closes)) or float(np.min(closes)) <= 0:
            return ModuleOutput.abstain(self.name, "bad_prices", self.horizon_min)

        logp = np.log(closes)
        level, vel, innov = _cv_kalman(logp)
        if level is None:
            return ModuleOutput.abstain(self.name, "filter_degenerate", self.horizon_min)

        # scale velocity by the recent per-bar return volatility → unit-free signal
        ret_sd = float(np.std(np.diff(logp))) or 1e-6
        vel_z = float(vel / ret_sd)                       # velocity in σ-per-bar units
        direction = float(np.tanh(vel_z))                 # smooth squashing into [-1,1]

        # innovation surprise damps conviction (filter being surprised = unstable regime)
        innov_z = abs(innov) / (ret_sd + 1e-9)
        stability = float(max(0.2, 1.0 - min(innov_z / 3.0, 0.8)))
        conviction = float(min(abs(direction), 1.0) * stability)

        # one-bar fair-value projection from velocity → expected % move
        exp_move_pct = float(np.expm1(vel) * 100.0)

        regime = ("trending" if abs(vel_z) > 0.5 else "drifting")
        expl = (f"Kalman: velocity={vel_z:+.2f}σ/bar (fair-value drift), "
                f"innovation={innov_z:.2f}σ → {'long' if direction > 0 else 'short'}, "
                f"next-bar {exp_move_pct:+.3f}%")
        return ModuleOutput(
            module=self.name, direction=direction, conviction=conviction,
            expected_move_pct=exp_move_pct, horizon_min=self.horizon_min,
            regime_tag=regime,
            features={"tf": _TF, "velocity_sigma": round(vel_z, 4),
                      "innovation_sigma": round(float(innov_z), 4),
                      "stability": round(stability, 4)},
            explanation=expl)


def _cv_kalman(z: np.ndarray):
    """Constant-velocity Kalman filter on 1-D series z. State x=[level, velocity].
    Returns (final_level, final_velocity, final_innovation) or (None,None,None)."""
    n = len(z)
    if n < 5:
        return None, None, None
    dt = 1.0
    F = np.array([[1.0, dt], [0.0, 1.0]])            # state transition (const velocity)
    H = np.array([[1.0, 0.0]])                       # observe level only
    # noise: measurement r from short-term variance; process q smaller (smooth velocity)
    r = float(np.var(np.diff(z)[:10])) or 1e-8
    q = r * 1e-2
    Q = q * np.array([[dt**3 / 3, dt**2 / 2], [dt**2 / 2, dt]])
    R = np.array([[r]])

    x = np.array([z[0], 0.0])
    P = np.eye(2) * r
    innov = 0.0
    for k in range(1, n):
        # predict
        x = F @ x
        P = F @ P @ F.T + Q
        # update
        y = float(z[k] - (H @ x)[0])                 # innovation
        S = (H @ P @ H.T + R)[0, 0]
        if S <= 0 or not np.isfinite(S):
            return None, None, None
        K = (P @ H.T) / S                            # Kalman gain (2x1)
        x = x + (K[:, 0] * y)
        P = (np.eye(2) - K @ H) @ P
        innov = y
    if not np.all(np.isfinite(x)):
        return None, None, None
    return float(x[0]), float(x[1]), float(innov)
