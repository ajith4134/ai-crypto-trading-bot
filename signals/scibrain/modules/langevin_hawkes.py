"""LangevinHawkesModule — PHYSICS (Langevin/Fokker-Planck + Hawkes self-excitation).

Two complementary dynamical pictures of the price path:

  Langevin (overdamped):  dX = -k (X - mu) dt + sigma dW
    A mean-reverting drift toward a slow fair value mu with diffusion sigma. We fit it as
    an AR(1) on log-price: X_{t+1} = a + b X_t + eps  =>  k = -ln(b) (reversion speed),
    mu = a/(1-b), sigma = std(eps). The drift term -k(X-mu) gives a reversion-to-fair
    direction: below mu => up-drift, above mu => down-drift.

  Hawkes (self-exciting point process): large moves are "events" whose past arrivals
    raise the present intensity (volatility clusters; jumps beget jumps). We estimate the
    branching ratio / clustering of |return| exceedances. High intensity NOW means we are
    inside a cluster where CONTINUATION of the latest jump dominates over slow reversion.

Direction blend: when Hawkes intensity is high (clustered/trending regime) follow the
latest jump (continuation); otherwise follow the Langevin reversion toward fair value.
Pure numpy; abstains on insufficient data.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_TF = "5m"
_MIN_BARS = 60
_MAX_BARS = 240
_HORIZON_MIN = 60


class LangevinHawkesModule(Module):
    name = "langevin_hawkes"
    evidence_family = "flow_excitation"  # §6g.330 family-correlation penalty
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        closes = frame.closes(_TF)
        if closes is None or len(closes) < _MIN_BARS:
            return ModuleOutput.abstain(self.name, "insufficient_bars", self.horizon_min)
        closes = closes[-_MAX_BARS:]
        if not np.all(np.isfinite(closes)) or float(np.min(closes)) <= 0:
            return ModuleOutput.abstain(self.name, "bad_prices", self.horizon_min)

        logp = np.log(closes)
        rets = np.diff(logp)
        if float(np.std(rets)) <= 0:
            return ModuleOutput.abstain(self.name, "degenerate_returns", self.horizon_min)

        # ---- Langevin via AR(1) fit on log-price ----
        x0 = logp[:-1]
        x1 = logp[1:]
        b, a = np.polyfit(x0, x1, 1)          # x1 = b*x0 + a
        b = float(b)
        if not np.isfinite(b) or b <= 0 or b >= 1.0:
            # no stable mean-reversion (random walk / explosive) -> Langevin abstains
            k = 0.0
            mu = float(logp[-1])
            reversion_dir = 0.0
            mu_dev = 0.0
        else:
            k = float(-np.log(b))             # reversion speed (>0)
            mu = float(a / (1.0 - b))
            sigma_ou = float(np.std(x1 - (b * x0 + a)))
            dev = float(logp[-1] - mu)        # how far above/below fair value
            mu_dev = dev / (sigma_ou + 1e-12) if sigma_ou > 0 else 0.0
            reversion_dir = float(-np.sign(dev)) * float(np.clip(abs(mu_dev) / 2.0, 0.0, 1.0))

        # ---- Hawkes self-excitation on |return| exceedances ----
        absr = np.abs(rets)
        thr = float(np.mean(absr) + np.std(absr))
        events = (absr > thr).astype(float)
        intensity, branching = _hawkes_intensity(events)
        last_jump_sign = float(np.sign(rets[-1])) or 1.0

        # ---- blend ----
        # weight toward continuation when clustered, toward reversion when calm
        w_cont = float(np.clip(intensity, 0.0, 1.0))
        continuation_dir = last_jump_sign * w_cont
        direction = float(np.clip((1.0 - w_cont) * reversion_dir + w_cont * continuation_dir,
                                  -1.0, 1.0))

        # conviction: stronger when reversion signal is decisive OR clustering is clear
        conv_rev = float(np.clip(abs(reversion_dir), 0.0, 1.0))
        conviction = float(np.clip(max((1.0 - w_cont) * conv_rev, w_cont * 0.8), 0.0, 1.0))

        regime = ("clustered_jump" if w_cont >= 0.6 else
                  "mean_reverting" if (k > 0 and conv_rev >= 0.3) else "diffusive")

        expl = (f"Langevin k={k:.3f} mu_dev={mu_dev:+.2f}sigma (rev->{('up' if reversion_dir>0 else 'down' if reversion_dir<0 else 'flat')}); "
                f"Hawkes intensity={intensity:.2f} branching={branching:.2f} "
                f"({regime}) -> {'long' if direction>0 else 'short' if direction<0 else 'flat'}")
        return ModuleOutput(
            module=self.name, direction=direction, conviction=conviction,
            expected_move_pct=None, horizon_min=self.horizon_min, regime_tag=regime,
            features={"tf": _TF, "ou_k": round(k, 5), "mu_dev_sigma": round(float(mu_dev), 4),
                      "reversion_dir": round(reversion_dir, 4),
                      "hawkes_intensity": round(float(intensity), 4),
                      "branching_ratio": round(float(branching), 4),
                      "cont_weight": round(w_cont, 4)},
            explanation=expl)


def _hawkes_intensity(events: np.ndarray) -> tuple[float, float]:
    """Estimate present self-excitation from an event sequence (0/1 exceedances).

    branching ratio ~ lag-1 autocorrelation of the event indicator (events clustering in
    time). present intensity ~ exponentially-weighted recent event density relative to the
    baseline rate. Both returned in [0,1]-ish (intensity clipped to [0,1]).
    """
    n = events.size
    base_rate = float(np.mean(events))
    if n < 10 or base_rate <= 0:
        return 0.0, 0.0
    # branching: do events beget events? lag-1 autocorr of the indicator.
    e = events - base_rate
    d = float(np.dot(e, e))
    branching = float(np.clip(np.dot(e[:-1], e[1:]) / d, 0.0, 1.0)) if d > 0 else 0.0
    # present intensity: EWMA of recent events vs baseline
    halflife = 8.0
    w = np.exp(-np.log(2.0) * np.arange(n)[::-1] / halflife)
    recent_rate = float(np.dot(w, events) / np.sum(w))
    intensity = float(np.clip((recent_rate / base_rate - 1.0) * 0.5 + branching, 0.0, 1.0))
    return intensity, branching
