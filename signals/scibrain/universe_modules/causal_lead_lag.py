"""CausalLeadLag — sparse CONDITIONAL lead-lag (Granger/PCMCI-style) from liquid leaders, with
stability selection; trade the causally-predicted move flowing from a target's stable drivers
(Universe Core, §6e Tier-A).

UNIQUE EVIDENCE (§6g.2) — what makes this NOT a renamed existing signal:
  • vs SpectralGraphContagion: that uses the BIVARIATE lag-1 cross-correlation graph; this is
    CONDITIONAL — each leader→target coefficient controls for the common market factor AND the other
    leaders (a multivariate regression), so it distinguishes true directed predictive flow from a
    pair of names that merely co-move with the market.
  • vs InfoTheory's transfer entropy: that is BIVARIATE volume→price TE on one symbol; this is a
    multivariate cross-asset causal graph.
  • vs SparseFactorResidual: that fades the idiosyncratic residual (reversal); this FOLLOWS a stable
    causal driver (directed flow).

METHOD (fully vectorized — matmuls + a small linear solve, runs once per Universe-Core cycle):
  1. Restrict to the top-K most LIQUID symbols as candidate leaders/drivers (liquidity = frame.liquidity).
  2. Residualize the leaders' lagged returns AND every target's next return against the cross-sectional
     market factor f[t] (regress out f) → everything is factor-neutral ⇒ coefficients are CONDITIONAL
     on the common factor. Standardize.
  3. Conditional multivariate regression (shared design X = factor-residualized leader-lag matrix):
     B = (XᵀX + ridge·I)⁻¹ Xᵀ Y  →  B[k,j] = lead-lag of leader k → target j controlling for the
     OTHER leaders + the factor. One solve gives the whole (K×S) causal matrix.
  4. STABILITY SELECTION: refit B on many row-subsamples; keep an edge (k,j) only if |B| clears a
     robust threshold in ≥ π of subsamples. Unstable/spurious edges are dropped ⇒ a SPARSE graph.
     Self-edges (k==j) are zeroed (own-lag is the per-symbol modules' job, not cross-asset flow).
  5. Vote for target j: predicted_j = Σ_k B_stable[k,j]·x_leaderₖ(recent) = the move its STABLE causal
     leaders predict. direction = tanh(z(predicted)), conviction ∝ |z|·driver_strength. A target with
     NO stable driver ABSTAINS (conviction 0) — the module is honestly sparse.

ADMISSION (§6g + Rule 14): shadow_only=True (observe authority) — recorded + IC-evaluable, never a live
vote until incremental IC is proven; promotion is owner-approved. Deterministic (fixed RNG seed for the
subsampling), bounded, abstains on a too-thin frame.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, UniverseFrame
from .base import UniverseModule

_MIN_T = 24          # conditional regression needs more rows than the per-symbol modules
_MIN_S = 30
_MAX_LEADERS = 30    # K — top-liquidity candidate drivers (CPU + interpretability bound)
_RIDGE = 1e-2        # ridge on XᵀX for a stable solve
_N_SUBSAMPLE = 20    # stability-selection refits
_SUBSAMPLE_FRAC = 0.7
_STABILITY_PI = 0.5  # keep an edge that is a top-N parent in ≥ this fraction of subsamples
_N_PARENTS = 3       # PCMCI-style: per target, only its few strongest conditional drivers are candidates
                     # each subsample → forces a SPARSE causal graph (not the dense median-threshold trap)
_DIR_SCALE = 1.5
_CONV_SCALE = 3.0
_SEED = 12345        # fixed → deterministic stability selection


def _residualize(mat: np.ndarray, f: np.ndarray) -> np.ndarray:
    """Regress out the common factor f (T-vector) from each column of mat (T×n): mat - f·βᵀ where
    β = (fᵀmat)/(fᵀf). Makes every series factor-neutral ⇒ downstream coefficients are conditional."""
    ff = float(f @ f)
    if ff < 1e-12:
        return mat
    beta = (f @ mat) / ff               # (n,)
    return mat - np.outer(f, beta)


def _standardize(mat: np.ndarray) -> np.ndarray:
    sd = mat.std(axis=0)
    sd = np.where(sd > 1e-12, sd, 1.0)
    return (mat - mat.mean(axis=0)) / sd


def _solve_beta(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Ridge multivariate least squares: B = (XᵀX + ridge·I)⁻¹ XᵀY  → (K×S)."""
    k = x.shape[1]
    xtx = x.T @ x + _RIDGE * np.eye(k)
    return np.linalg.solve(xtx, x.T @ y)


class CausalLeadLagModule(UniverseModule):
    name = "causal_lead_lag"
    horizon_min = 60

    def _compute(self, frame: UniverseFrame) -> dict[str, ModuleOutput]:
        symbols = frame.symbols
        s = len(symbols)
        ret = frame.returns_by_tf.get(frame.primary_tf)
        if ret is None or ret.shape[0] < _MIN_T or s < _MIN_S:
            return {}
        t_full = ret.shape[0]

        # ── liquid leaders (top-K by liquidity) ──
        liq = frame.liquidity if frame.liquidity.shape[0] == s else ret.std(axis=0)
        k = int(min(_MAX_LEADERS, max(5, s // 8)))
        leader_idx = np.argsort(liq)[::-1][:k]

        # ── lagged design + factor control ──
        f = ret.mean(axis=1)                     # cross-sectional market factor per bar
        x_lead = ret[:-1, leader_idx]            # leaders at t-1   (T-1 × K)
        y_all = ret[1:, :]                        # all targets at t (T-1 × S)
        f_lag = f[:-1]                            # factor at t-1   (T-1,)

        x = _standardize(_residualize(x_lead, f_lag))      # factor-neutral, standardized leaders
        y = _residualize(y_all, f_lag)                     # factor-neutral targets
        y = _standardize(y)
        n = x.shape[0]
        if n < _MIN_T:
            return {}

        # ── conditional multivariate causal matrix (one solve for all targets) ──
        b_full = _solve_beta(x, y)               # (K × S)

        # ── stability selection over row subsamples (PCMCI-style top-N parents) ──
        # Each subsample marks ONLY each target's _N_PARENTS strongest conditional drivers; an edge is
        # "stable" only if it lands in that target's top-N in ≥ π of subsamples → a genuinely SPARSE
        # graph (the earlier median threshold kept ~half the edges, which is not a causal graph).
        rng = np.random.default_rng(_SEED)
        m = max(_MIN_T // 2, int(n * _SUBSAMPLE_FRAC))
        sel_count = np.zeros_like(b_full)
        nth = max(0, k - _N_PARENTS)                         # index of the N-th largest per column
        for _ in range(_N_SUBSAMPLE):
            idx = rng.choice(n, size=m, replace=False)
            absb = np.abs(_solve_beta(x[idx], y[idx]))       # (K×S)
            if k > _N_PARENTS:
                kth = np.partition(absb, nth, axis=0)[nth, :]   # per-target N-th largest |coef|
                sel_count += (absb >= kth[None, :]) & (kth[None, :] > 0)
            else:
                sel_count += absb > 0
        sel_freq = sel_count / _N_SUBSAMPLE
        stable = sel_freq >= _STABILITY_PI                  # boolean (K×S) sparse support

        # zero self-edges (a leader predicting itself = own-lag, not cross-asset flow)
        pos_of = {int(li): r for r, li in enumerate(leader_idx)}
        for j_global, r in pos_of.items():
            stable[r, j_global] = False

        b_stable = np.where(stable, b_full, 0.0)            # sparse conditional causal graph
        n_drivers = stable.sum(axis=0)                      # stable causal in-edges per target

        # ── predicted move from each target's STABLE leaders ──
        x_recent = x[-1, :]                                  # leaders' most-recent factor-neutral return
        predicted = b_stable.T @ x_recent                    # (S,)
        active = n_drivers > 0
        pstd = predicted[active].std() if active.any() else 0.0
        pstd = float(pstd) if pstd > 1e-12 else 1.0
        z_pred = predicted / pstd

        # driver strength = total stable |coef| into the target (normalized) → conviction weight
        drive = np.abs(b_stable).sum(axis=0)
        max_drive = float(drive.max())
        drive_w = drive / max_drive if max_drive > 1e-12 else np.zeros(s)

        ts = frame.ts
        leader_syms = [symbols[i] for i in leader_idx]
        out: dict[str, ModuleOutput] = {}
        for j, sym in enumerate(symbols):
            nd = int(n_drivers[j])
            if nd == 0:                                      # no stable causal driver → honest abstain
                out[sym] = ModuleOutput.abstain(
                    self.name, "no_stable_driver", self.horizon_min,
                    role="direction", evidence_family="causal_flow", shadow_only=True)
                continue
            zj = float(z_pred[j])
            dw = float(drive_w[j])
            direction = float(np.tanh(zj / _DIR_SCALE))      # FOLLOW the causally-predicted move
            conviction = float(min(abs(zj) / _CONV_SCALE, 1.0) * dw)
            # name the strongest stable driver for transparency
            col = b_stable[:, j]
            top_r = int(np.argmax(np.abs(col)))
            top_driver = leader_syms[top_r] if abs(col[top_r]) > 0 else None
            stance = "long" if direction > 0 else "short"
            out[sym] = ModuleOutput(
                module=self.name,
                direction=direction,
                conviction=conviction,
                expected_move_pct=None,
                horizon_min=self.horizon_min,
                regime_tag="trending",
                features={
                    "z_causal_pred": round(zj, 4),
                    "n_stable_drivers": nd,
                    "driver_weight": round(dw, 4),
                    "top_driver": top_driver,
                    "top_driver_beta": round(float(col[top_r]), 4),
                    "n_leaders": int(k),
                    "n_symbols": int(s),
                },
                explanation=(f"causal_lead_lag: {nd} stable driver(s) (top {top_driver}) → "
                             f"conditional predicted z={zj:+.2f} → follow → {stance}"),
                ok=True,
                role="direction",
                evidence_family="causal_flow",               # distinct family (§6g.1)
                shadow_only=True,                            # recorded + evaluated, NEVER a live vote yet
                ts=ts,
            )
        return out
