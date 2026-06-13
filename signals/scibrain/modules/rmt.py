"""RMTModule — Random Matrix Theory (origin: nuclear physics; Wigner/Marchenko-Pastur).

Classic RMT separates SIGNAL from NOISE in a covariance spectrum: for pure noise the
eigenvalues follow the Marchenko-Pastur (MP) law, bounded above by
  lambda_+ = sigma^2 (1 + sqrt(q))^2,   q = cols/rows.
Eigenvalues that poke ABOVE lambda_+ are genuine structure; everything inside the MP bulk
is unpredictable noise.

We apply this per-symbol via Singular Spectrum Analysis: build the symbol's own
trajectory (Hankel) matrix from log-returns, take the eigenspectrum of its lag-covariance,
keep only the eigen-modes above the MP edge, and reconstruct the DENOISED signal. The
slope of the denoised series is a noise-robust trend direction; the fraction of energy in
signal modes is the conviction (lots of structure above the noise floor => trust it).
Pure numpy; abstains on insufficient data.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_TF = "5m"
_MIN_BARS = 55         # sensor bus serves ~65 5m bars; L=16 still gives ~48 trajectory cols
_MAX_BARS = 220
_WINDOW = 16            # embedding dimension L (rows of the trajectory matrix)
_HORIZON_MIN = 60


class RMTModule(Module):
    name = "rmt"
    evidence_family = "trend_momentum"  # §6g.330 family-correlation penalty
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        closes = frame.closes(_TF)
        if closes is None or len(closes) < _MIN_BARS:
            return ModuleOutput.abstain(self.name, "insufficient_bars", self.horizon_min)
        closes = closes[-_MAX_BARS:].astype(float)
        if not np.all(np.isfinite(closes)) or float(np.min(closes)) <= 0:
            return ModuleOutput.abstain(self.name, "bad_prices", self.horizon_min)

        logp = np.log(closes)
        rets = np.diff(logp)
        ret_sd = float(np.std(rets))
        if ret_sd <= 0:
            return ModuleOutput.abstain(self.name, "degenerate_returns", self.horizon_min)

        # SSA is applied to the (centered) log-PRICE level — its trend/cycle structure is
        # what produces dominant eigenvalues above the noise floor. (Applying it to returns
        # finds nothing: returns are MP-noise by construction.)
        x = logp - float(np.mean(logp))
        L = _WINDOW
        K = x.size - L + 1                      # number of trajectory columns
        if K < L:
            return ModuleOutput.abstain(self.name, "short_series", self.horizon_min)
        traj = np.lib.stride_tricks.sliding_window_view(x, L).T   # L x K
        cov = (traj @ traj.T) / K              # L x L lag-covariance
        w, V = np.linalg.eigh(cov)
        order = np.argsort(w)[::-1]
        evals = np.clip(w[order], 0.0, None)   # descending, non-negative
        V = V[:, order]

        # Marchenko-Pastur split with an ITERATIVELY-FITTED noise variance: assume the
        # smaller eigenvalues are the MP noise bulk, set sigma^2 = their mean, recompute the
        # edge lambda_+ = sigma^2 (1+sqrt(q))^2, drop eigenvalues above it from the noise
        # set, and repeat to convergence. Eigenvalues remaining above the edge are signal.
        q = L / K
        mp_edge, n_signal = _mp_signal_count(evals, q)
        total_energy = float(np.sum(evals)) or 1e-12
        signal_frac = float(np.sum(evals[:n_signal]) / total_energy)

        if n_signal == 0:
            return ModuleOutput(
                module=self.name, direction=0.0, conviction=0.08,
                expected_move_pct=None, horizon_min=self.horizon_min,
                regime_tag="noise_dominated",
                features={"tf": _TF, "mp_edge": round(mp_edge, 6), "n_signal_modes": 0,
                          "signal_energy_frac": round(signal_frac, 4), "L": L, "K": K},
                explanation=(f"RMT: 0 modes above MP edge {mp_edge:.4f} -> all noise; flat"))

        # reconstruct the denoised log-price signal from the leading signal eigenvectors
        denoised = _ssa_reconstruct(traj, V[:, :n_signal], x.size, L, K)
        # direction = recent net move of the denoised path, normalized by per-bar vol
        span = min(20, denoised.size - 1)
        net = float(denoised[-1] - denoised[-1 - span])
        per_bar = net / span
        direction = float(np.clip(np.tanh(per_bar / (ret_sd + 1e-12)), -1.0, 1.0))
        # conviction tracks DECISIVENESS (how strong the denoised trend is), gated by how
        # much of the spectrum is genuine signal — not signal_frac alone (which is ~1 for
        # any price level since the trend mode always dominates).
        conviction = float(np.clip(abs(direction) * (0.55 + 0.45 * signal_frac), 0.0, 1.0))
        regime = "structured_trend" if signal_frac >= 0.6 else "partly_structured"

        expl = (f"RMT/SSA: {n_signal} signal mode(s) above MP edge {mp_edge:.4f} "
                f"(signal_frac={signal_frac:.2f}); denoised move={net*100:+.2f}% over "
                f"{span} bars -> {'long' if direction>0 else 'short' if direction<0 else 'flat'}")
        return ModuleOutput(
            module=self.name, direction=direction, conviction=conviction,
            expected_move_pct=None, horizon_min=self.horizon_min, regime_tag=regime,
            features={"tf": _TF, "mp_edge": round(mp_edge, 6), "n_signal_modes": n_signal,
                      "signal_energy_frac": round(signal_frac, 4),
                      "denoised_move_pct": round(net * 100.0, 4), "L": L, "K": K},
            explanation=expl)


def _mp_signal_count(evals: np.ndarray, q: float, iters: int = 12) -> tuple[float, int]:
    """Iteratively fit the Marchenko-Pastur noise variance and count signal eigenvalues.

    Start with all eigenvalues as the noise set; sigma^2 = mean(noise); edge =
    sigma^2 (1+sqrt(q))^2; keep only eigenvalues <= edge as noise; repeat until the noise
    set stabilizes. Returns (edge, n_signal = count above the converged edge)."""
    evals = np.sort(evals)[::-1]
    noise = evals.copy()
    edge = float(evals[0])
    for _ in range(iters):
        if noise.size == 0:
            break
        sigma2 = float(np.mean(noise))
        edge = sigma2 * (1.0 + np.sqrt(q)) ** 2
        new_noise = evals[evals <= edge]
        if new_noise.size == noise.size:
            break
        noise = new_noise
    n_signal = int(np.sum(evals > edge))
    return float(edge), n_signal


def _ssa_reconstruct(traj: np.ndarray, Vsig: np.ndarray, n: int, L: int, K: int) -> np.ndarray:
    """Reconstruct the time series from selected eigenvectors via diagonal averaging
    (Hankelization) of the grouped trajectory matrix."""
    # grouped matrix X_g = sum_i V_i V_i^T X    (projection onto signal subspace)
    Xg = Vsig @ (Vsig.T @ traj)                # L x K
    # diagonal averaging back to length-n series (vectorized anti-diagonal scatter):
    # element (i,j) of the trajectory matrix maps to series index i+j.
    ii, jj = np.indices((L, K))
    idx = (ii + jj).ravel()
    out = np.zeros(n)
    cnt = np.zeros(n)
    np.add.at(out, idx, Xg.ravel())
    np.add.at(cnt, idx, 1.0)
    cnt[cnt == 0] = 1.0
    return out / cnt


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
