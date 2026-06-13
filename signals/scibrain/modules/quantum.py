"""QuantumModule — PhD-level QUANTUM physics (Domain 8: quantum info & QML, quantum-inspired).

Two quantum tools, computed classically on CPU (upgradeable to a real QPU later):

1. **Von Neumann entropy** of the spectral density. The normalised power spectrum p_i =
   P_i/ΣP_i is treated as the eigenvalue distribution of a density matrix ρ; its Von Neumann
   entropy  S = −Σ p_i ln p_i  (normalised to [0,1]) measures DISORDER. S→1 = white noise
   (no structure, unpredictable → gate conviction down); S→0 = a single dominant cycle exists.

2. **Quantum Fourier Transform** (the FFT is the classical QFT) extracts the dominant cycle.
   Its phase tells us WHERE in the cycle we are; the slope of the dominant mode at "now" gives
   a directional vote (on the up-leg of the cycle → long).

Vote strength = cycle dominance × (1 − entropy). Pure numpy; abstains on insufficient data.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_TF = "5m"
_MIN_BARS = 48
_HORIZON_MIN = 60


class QuantumModule(Module):
    name = "quantum"
    evidence_family = "cycle_spectral"  # §6g.330 family-correlation penalty
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        closes = frame.closes(_TF)
        if closes is None or len(closes) < _MIN_BARS:
            return ModuleOutput.abstain(self.name, "insufficient_bars", self.horizon_min)
        closes = closes[-128:]
        if not np.all(np.isfinite(closes)) or float(np.min(closes)) <= 0:
            return ModuleOutput.abstain(self.name, "bad_prices", self.horizon_min)

        # detrend (work on returns) so the spectrum reflects oscillation, not the trend DC
        rets = np.diff(np.log(closes))
        rets = rets - rets.mean()
        n = len(rets)
        if n < 16 or float(np.std(rets)) <= 1e-9:
            return ModuleOutput.abstain(self.name, "no_signal", self.horizon_min)

        # QFT (FFT) → one-sided power spectrum (drop DC)
        fft = np.fft.rfft(rets * np.hanning(n))
        power = (np.abs(fft) ** 2)[1:]                  # skip DC bin
        freqs = np.fft.rfftfreq(n)[1:]
        total = float(power.sum())
        if total <= 0 or len(power) < 3:
            return ModuleOutput.abstain(self.name, "empty_spectrum", self.horizon_min)

        p = power / total
        # Von Neumann / Shannon spectral entropy, normalised to [0,1]
        S = float(-np.sum(p * np.log(p + 1e-12)) / np.log(len(p)))
        S = max(0.0, min(1.0, S))

        k = int(np.argmax(power))                        # dominant cycle bin
        dominance = float(power[k] / total)              # share of energy in the top mode
        f0 = float(freqs[k])
        period_bars = (1.0 / f0) if f0 > 0 else float("inf")

        # reconstruct the dominant sinusoid's slope at "now" (last sample) for direction
        phase = float(np.angle(fft[k + 1]))              # +1: fft index incl. DC
        # d/dt cos(2πf t + φ) ∝ −sin(2πf (n-1) + φ); positive slope ⇒ rising ⇒ long
        slope = -np.sin(2.0 * np.pi * f0 * (n - 1) + phase)
        order = float(1.0 - S)                            # 1 = ordered/cyclic, 0 = noise

        direction = float(np.sign(slope) * order * min(dominance * 3.0, 1.0))
        conviction = float(order * min(dominance * 3.0, 1.0))
        regime = "cyclic" if S < 0.7 else "noise"

        expl = (f"Quantum: VonNeumann entropy S={S:.3f} ({regime}), dominant cycle "
                f"~{period_bars:.1f} bars (energy {dominance*100:.0f}%), phase-slope "
                f"{'up→long' if slope >= 0 else 'down→short'}")
        return ModuleOutput(
            module=self.name, direction=direction, conviction=conviction,
            expected_move_pct=None, horizon_min=self.horizon_min, regime_tag=regime,
            features={"tf": _TF, "vonneumann_entropy": round(S, 4),
                      "dominant_period_bars": round(period_bars, 2) if np.isfinite(period_bars) else None,
                      "cycle_dominance": round(dominance, 4), "order": round(order, 4)},
            explanation=expl)
