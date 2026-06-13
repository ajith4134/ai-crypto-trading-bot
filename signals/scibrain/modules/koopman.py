"""KoopmanModule — PHYSICS (Domain 7: Koopman operator & ergodic theory).

The Koopman operator turns a nonlinear dynamical system into an infinite-dimensional
LINEAR operator acting on observables; its eigenfunctions are the system's natural
modes. Dynamic Mode Decomposition (DMD), originally from fluid dynamics, numerically
approximates the Koopman operator's eigenvalues/modes from data.

We run **Hankel-DMD** (time-delay embedding → DMD) on a timeframe's close series:
  - eigenvalues λ of the reduced operator give each mode's growth rate |λ| and
    frequency arg(λ). |λ|>1 ⇒ growing/trending mode, |λ|<1 ⇒ decaying/mean-reverting.
  - a one-step Koopman push of the latest state gives a model-based next-bar forecast
    → the directional vote + expected move.
  - the reduced-space reconstruction error measures how well a LINEAR operator explains
    the dynamics: low error ⇒ coherent/predictable regime; high error ⇒ chaotic/noisy
    transition (this is the DMD regime-transition detector, arXiv:1904.09082).

Pure numpy; no IO. Abstains (conviction 0) when there isn't enough data.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_TF = "5m"          # primary timeframe (less noisy than 1m, reactive enough for the buffer)
_MIN_BARS = 40       # need a decent window for a meaningful embedding
_DELAY = 12          # Hankel delay dimension (embedding rows)
_SV_TOL = 1e-10      # singular-value truncation tolerance
_HORIZON_MIN = 60    # the holding horizon this module speaks to


class KoopmanModule(Module):
    name = "koopman"
    evidence_family = "dynamics_spectral"  # Koopman/DMD spectral dynamics (§6g.330)
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        closes = frame.closes(_TF)
        if closes is None or len(closes) < _MIN_BARS:
            return ModuleOutput.abstain(self.name, "insufficient_bars", self.horizon_min)
        closes = closes[-64:]                      # cap window for O(1) cost
        if not np.all(np.isfinite(closes)) or float(np.min(closes)) <= 0:
            return ModuleOutput.abstain(self.name, "bad_prices", self.horizon_min)

        # work in log-price so multiplicative moves are linear (geometric returns)
        x = np.log(closes)
        last_log = float(x[-1])

        res = _hankel_dmd(x, delay=_DELAY, sv_tol=_SV_TOL)
        if res is None:
            return ModuleOutput.abstain(self.name, "dmd_degenerate", self.horizon_min)
        pred_next_log, dom_growth, dom_freq, recon_err, rank, mode_energy = res

        # One-step Koopman forecast → next-bar % move. We report the HONEST one-step
        # forecast (not a multi-bar sum): a mean-reverting/oscillatory mode reverts, so
        # linearly accumulating the one-bar move across the horizon is wrong and blows up.
        # Bound the forecast to recent volatility (a one-step prediction physically can't
        # exceed a few σ) so a pathological reconstruction can't dominate the circuit.
        rets = np.diff(x)
        sigma = float(np.std(rets[-30:])) if rets.size >= 5 else float(np.std(rets) or 0.0)
        sigma = max(sigma, 1e-6)
        step_move = float(np.clip(pred_next_log - last_log, -3.0 * sigma, 3.0 * sigma))
        exp_move_pct = float(np.expm1(step_move) * 100.0)

        # directional vote = sign of the next-bar push, strength from mode coherence
        coherence = float(max(0.0, 1.0 - recon_err))        # 1 = perfectly linear/predictable
        strength = coherence * float(min(mode_energy, 1.0))
        direction = float(np.sign(step_move) * strength)

        # conviction: coherent dynamics + a non-trivial predicted move, capped
        move_sig = float(min(abs(exp_move_pct) / 1.0, 1.0))  # 1% move → full weight
        conviction = float(coherence * (0.5 + 0.5 * move_sig))

        regime = _regime(dom_growth, recon_err)
        expl = (f"Koopman/DMD: dominant |λ|={dom_growth:.3f} (freq~{dom_freq:.3f}), "
                f"recon_err={recon_err:.3f}, rank={rank} → {regime}, "
                f"next-bar {('+' if step_move >= 0 else '')}{np.expm1(step_move)*100:.3f}%")
        return ModuleOutput(
            module=self.name,
            direction=direction,
            conviction=conviction,
            expected_move_pct=exp_move_pct,
            horizon_min=self.horizon_min,
            regime_tag=regime,
            features={
                "tf": _TF, "dominant_growth": dom_growth, "dominant_freq": dom_freq,
                "recon_err": recon_err, "rank": rank, "mode_energy": mode_energy,
                "next_bar_pct": float(np.expm1(step_move) * 100.0), "coherence": coherence,
            },
            explanation=expl,
        )


def _regime(dom_growth: float, recon_err: float) -> str:
    """Label the dynamical regime from the dominant eigenvalue + reconstruction error."""
    if recon_err > 0.45:
        return "chaotic"          # a linear operator poorly explains the dynamics
    if dom_growth > 1.015:
        return "trending"         # dominant mode is growing
    if dom_growth < 0.985:
        return "mean_revert"      # dominant mode is decaying toward the attractor
    return "oscillatory"          # |λ|≈1 → persistent cycle / drift


def _hankel_dmd(x: np.ndarray, *, delay: int, sv_tol: float):
    """Hankel time-delay DMD on a 1-D series `x`.

    Returns (pred_next_value, dominant_growth |λ|, dominant_freq |arg λ|/π,
    recon_err in [0,1], rank, dominant_mode_energy in [0,1]) or None if degenerate.
    """
    n = len(x)
    d = min(delay, n // 3)
    if d < 3:
        return None
    m = n - d + 1                                  # number of snapshot columns
    if m < d + 2:
        return None
    # Hankel embedding: column j is the window x[j : j+d]
    H = np.empty((d, m), dtype=float)
    for i in range(d):
        H[i, :] = x[i:i + m]
    X = H[:, :-1]                                   # snapshots t = 0 .. m-2
    Xp = H[:, 1:]                                   # snapshots t = 1 .. m-1 (advanced by one)

    # center each delay-coordinate so DMD models the dynamics, not the DC level
    mean = X.mean(axis=1, keepdims=True)
    Xc = X - mean
    Xpc = Xp - mean

    try:
        U, S, Vh = np.linalg.svd(Xc, full_matrices=False)
    except np.linalg.LinAlgError:
        return None
    if S.size == 0 or S[0] <= 0:
        return None
    r = int(np.sum(S > max(sv_tol, S[0] * 1e-6)))   # numerical rank
    r = max(1, min(r, d - 1))
    Ur, Sr, Vr = U[:, :r], S[:r], Vh[:r, :].conj().T

    # reduced Koopman operator  Ã = Urᴴ Xp' Vr Sr⁻¹
    Atil = Ur.conj().T @ Xpc @ Vr @ np.diag(1.0 / Sr)

    # reconstruction error in the reduced subspace: how linear is the evolution?
    Z = Ur.conj().T @ Xc                            # reduced states (r × m-1)
    Zp = Ur.conj().T @ Xpc
    denom = float(np.linalg.norm(Zp)) or 1.0
    recon_err = float(np.linalg.norm(Zp - Atil @ Z) / denom)
    recon_err = max(0.0, min(recon_err, 1.0))

    eigvals = np.linalg.eigvals(Atil)
    if eigvals.size == 0:
        return None
    # dominant mode = the eigenvalue whose amplitude in the data is largest, weighted
    # by persistence (|λ|). Approx amplitude via projection of the latest reduced state.
    z_last = Ur.conj().T @ (H[:, -1:] - mean)        # newest window in reduced coords
    z_next = Atil @ z_last                           # one-step Koopman push
    x_next_window = (Ur @ z_next) + mean             # back to delay coords
    pred_next_value = float(x_next_window[-1, 0])    # newest delay coord = future value

    idx = int(np.argmax(np.abs(eigvals)))
    lam = eigvals[idx]
    dom_growth = float(np.abs(lam))
    dom_freq = float(np.abs(np.angle(lam)) / np.pi)  # 0 = pure growth, 1 = Nyquist

    # dominant-mode energy fraction (singular-value spectrum)
    energy = float((Sr[0] ** 2) / (np.sum(Sr ** 2) or 1.0))

    if not np.isfinite(pred_next_value):
        return None
    return pred_next_value, dom_growth, dom_freq, recon_err, r, energy
