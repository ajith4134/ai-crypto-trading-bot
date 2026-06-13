"""WaveletSpectralModule — MATH/SIGNAL (multi-scale wavelet energy + noise floor).

The Fourier transform assumes stationarity; markets are not. The wavelet transform
localizes energy in BOTH time and scale, so it can say "the action right now is at the
30-minute scale, and below the 10-minute scale it's just noise." We run a pure-numpy Haar
discrete wavelet transform on the log-price:

  - Detail energy per scale E_j tells us which horizon currently carries the move
    (dominant scale).
  - The finest-scale details give a Donoho noise-floor estimate
    sigma = median(|d_1|)/0.6745; comparing total detail energy to the expected noise
    energy yields a signal-to-noise ratio (SNR) — the conviction gate.
  - The deepest APPROXIMATION band is the denoised trend; its recent slope is the
    direction (noise-robust momentum).
Abstains on insufficient data.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_TF = "5m"
_MIN_BARS = 48
_MAX_BARS = 256
_HORIZON_MIN = 60
_TREND_LEVEL = 3        # approximation band used as the denoised trend


class WaveletSpectralModule(Module):
    name = "wavelet"
    evidence_family = "multiscale_energy"  # §6g.330 family-correlation penalty
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        closes = frame.closes(_TF)
        if closes is None or len(closes) < _MIN_BARS:
            return ModuleOutput.abstain(self.name, "insufficient_bars", self.horizon_min)
        closes = closes[-_MAX_BARS:].astype(float)
        if not np.all(np.isfinite(closes)) or float(np.min(closes)) <= 0:
            return ModuleOutput.abstain(self.name, "bad_prices", self.horizon_min)

        logp = np.log(closes)
        approx_bands, detail_bands = _haar_decompose(logp, max_level=5)
        if not detail_bands:
            return ModuleOutput.abstain(self.name, "decompose_failed", self.horizon_min)

        # energy per detail scale
        energies = np.asarray([float(np.sum(d ** 2)) for d in detail_bands])
        total_e = float(np.sum(energies)) or 1e-12
        dominant_scale = int(np.argmax(energies)) + 1     # 1-based level

        # Donoho noise floor from finest details (level 1)
        d1 = detail_bands[0]
        sigma = float(np.median(np.abs(d1)) / 0.6745) if d1.size else 0.0
        # expected total noise energy across the detail coefficients (sigma^2 per coeff)
        n_detail = int(sum(d.size for d in detail_bands))
        noise_e = float(sigma ** 2 * n_detail)
        snr = float(np.clip((total_e - noise_e) / total_e, 0.0, 1.0))

        # denoised trend = slope of the deepest available approximation band
        lvl = min(_TREND_LEVEL, len(approx_bands)) - 1
        trend_band = approx_bands[lvl]
        slope = _recent_slope(trend_band, span=min(16, trend_band.size))
        direction = float(np.clip(np.tanh(slope * 50.0), -1.0, 1.0))
        conviction = float(np.clip(snr * (0.5 + 0.5 * min(abs(slope) * 50.0, 1.0)), 0.0, 1.0))

        regime = ("trend_clean" if snr >= 0.5 and abs(direction) >= 0.3 else
                  "noisy" if snr < 0.3 else "mixed")

        expl = (f"Wavelet: dominant_scale=L{dominant_scale} (~{2**dominant_scale}bar), "
                f"SNR={snr:.2f}, sigma={sigma:.4f}; denoised slope={slope:+.4f} -> "
                f"{'long' if direction>0 else 'short' if direction<0 else 'flat'}")
        return ModuleOutput(
            module=self.name, direction=direction, conviction=conviction,
            expected_move_pct=None, horizon_min=self.horizon_min, regime_tag=regime,
            features={"tf": _TF, "dominant_scale_level": dominant_scale,
                      "snr": round(snr, 4), "noise_sigma": round(sigma, 6),
                      "n_scales": len(detail_bands), "trend_slope": round(float(slope), 6),
                      "scale_energy_frac": [round(float(e / total_e), 4) for e in energies]},
            explanation=expl)


def _haar_decompose(x: np.ndarray, max_level: int) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Iterative Haar DWT. Returns (approximation_bands, detail_bands), one entry per level
    (level 1 = finest). At each level the signal is split into low-pass (approx) and
    high-pass (detail) and the approx is recursed. Odd-length bands drop the last sample."""
    approx_bands: list[np.ndarray] = []
    detail_bands: list[np.ndarray] = []
    a = x.astype(float)
    inv_sqrt2 = 1.0 / np.sqrt(2.0)
    for _ in range(max_level):
        if a.size < 4:
            break
        if a.size % 2:
            a = a[:-1]
        even = a[0::2]
        odd = a[1::2]
        approx = (even + odd) * inv_sqrt2
        detail = (even - odd) * inv_sqrt2
        approx_bands.append(approx)
        detail_bands.append(detail)
        a = approx
    return approx_bands, detail_bands


def _recent_slope(y: np.ndarray, span: int) -> float:
    y = y[-span:]
    if y.size < 3:
        return 0.0
    t = np.arange(y.size, dtype=float)
    t -= t.mean()
    denom = float(np.dot(t, t))
    if denom <= 0:
        return 0.0
    return float(np.dot(t, y - y.mean()) / denom)
