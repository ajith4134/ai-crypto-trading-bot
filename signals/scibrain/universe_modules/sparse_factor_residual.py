"""SparseFactorResidual — robust low-rank + sparse decomposition of the universe return
matrix; trade the idiosyncratic residual (Universe Core, design §6e Tier-A).

UNIQUE EVIDENCE (§6g.2): a CROSS-SECTIONAL decomposition. The per-symbol bank (RMT is per-symbol
SSA, NoiseHarvest/Kalman are per-symbol) cannot see the universe factor structure, so it cannot
separate a name's move into "the whole market/sector moved" vs "this name moved on its own". This
module does Robust PCA (Principal Component Pursuit) on the standardized return matrix:

    M (T×S, columns z-scored)  =  L (low-rank: market/sector factor flow)  +  Sp (sparse: idiosyncratic shocks)

solved by inexact ALM (singular-value soft-threshold for L, element-wise soft-threshold for Sp).

FALSIFIABLE HYPOTHESIS (residual reversal — a classic statistical-arbitrage edge, distinct from
the bank's per-symbol trend/reversion): a large idiosyncratic shock that the market factor does NOT
explain tends to mean-REVERT. So the directional vote for symbol j is:

    direction_j = -tanh( z_idio_j / SCALE )     (fade the standardized recent idiosyncratic residual)
    conviction_j = clip(|z_idio_j|/CONV_SCALE, 0, 1) · idio_fraction_j

where z_idio_j standardizes the recent sparse residual across the cross-section and idio_fraction_j =
‖Sp[:,j]‖ / (‖L[:,j]‖+‖Sp[:,j]‖) is how idiosyncratic the name is (high = a cleaner orthogonal alpha,
low = it just rode the market factor → low conviction). evidence_family='cross_asset'.

ADMISSION (§6g + Rule 14): ships `shadow_only=True` — every vote is RECORDED + IC-evaluable but NEVER
applied to a live pick until it proves incremental out-of-sample IC conditional on the existing bank.
Deterministic, bounded, abstains on a too-thin frame. Budget: one economy SVD per ALM iter on a
(T≤lookback)×S matrix, capped iterations, once per cycle.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, UniverseFrame
from .base import UniverseModule

_MIN_T = 16          # need ≥ this many time rows to trust a decomposition
_MIN_S = 20          # ...and ≥ this many symbols (cross-section)
_MAX_ITER = 40       # ALM iteration cap (CPU budget §6g.5)
_TOL = 1e-3
_RECENT = 3          # bars of the sparse residual averaged into the "recent" idiosyncratic move
_DIR_SCALE = 1.5     # tanh scale on z_idio → direction
_CONV_SCALE = 3.0    # |z_idio| at which conviction saturates


def _soft_threshold(x: np.ndarray, tau: float) -> np.ndarray:
    """Element-wise soft-threshold (proximal op of the ℓ1 norm)."""
    return np.sign(x) * np.maximum(np.abs(x) - tau, 0.0)


def _svt(x: np.ndarray, tau: float):
    """Singular-value soft-threshold (proximal op of the nuclear norm) via economy SVD.
    Returns (L, rank, singular_values)."""
    u, s, vt = np.linalg.svd(x, full_matrices=False)
    s_thr = np.maximum(s - tau, 0.0)
    rank = int((s_thr > 0).sum())
    l = (u * s_thr) @ vt
    return l, rank, s


def robust_pca(m: np.ndarray, max_iter: int = _MAX_ITER, tol: float = _TOL):
    """Principal Component Pursuit (Candès et al. 2011) via inexact ALM.
    M = L + Sp with L low-rank, Sp sparse. Returns (L, Sp, rank, n_iter, converged)."""
    t, s = m.shape
    lam = 1.0 / np.sqrt(max(t, s))
    norm_2 = np.linalg.svd(m, compute_uv=False)[0]          # spectral norm
    norm_inf = np.abs(m).max() / lam
    dual_norm = max(norm_2, norm_inf)
    y = m / dual_norm if dual_norm > 0 else np.zeros_like(m)
    mu = 1.25 / (norm_2 + 1e-12)
    mu_bar = mu * 1e7
    rho = 1.5
    m_fro = np.linalg.norm(m, "fro") + 1e-12

    sp = np.zeros_like(m)
    l = np.zeros_like(m)
    rank = 0
    converged = False
    it = 0
    for it in range(1, max_iter + 1):
        l, rank, _sv = _svt(m - sp + y / mu, 1.0 / mu)
        sp = _soft_threshold(m - l + y / mu, lam / mu)
        z = m - l - sp
        y = y + mu * z
        mu = min(mu * rho, mu_bar)
        if np.linalg.norm(z, "fro") / m_fro < tol:
            converged = True
            break
    return l, sp, rank, it, converged


class SparseFactorResidualModule(UniverseModule):
    name = "sparse_factor_residual"
    horizon_min = 60

    def _compute(self, frame: UniverseFrame) -> dict[str, ModuleOutput]:
        symbols = frame.symbols
        ret = frame.returns_by_tf.get(frame.primary_tf)
        if ret is None or ret.shape[0] < _MIN_T or ret.shape[1] < _MIN_S:
            return {}                       # too-thin frame → whole module abstains (no votes)

        # standardize each column (per-symbol) so the residual is cross-sectionally comparable
        mu = ret.mean(axis=0)
        sd = ret.std(axis=0)
        good = sd > 1e-12
        m = np.zeros_like(ret)
        m[:, good] = (ret[:, good] - mu[good]) / sd[good]

        l, sp, rank, n_iter, converged = robust_pca(m)

        # per-symbol norms of the low-rank (factor) vs sparse (idiosyncratic) parts
        l_norm = np.linalg.norm(l, axis=0)
        sp_norm = np.linalg.norm(sp, axis=0)
        idio_fraction = sp_norm / (l_norm + sp_norm + 1e-12)     # [0,1]; high = name moves on its own

        # recent idiosyncratic move = mean of the last few sparse-residual rows, standardized
        # ACROSS the cross-section (the residual-reversal signal is a cross-sectional ranking)
        recent = sp[-_RECENT:, :].mean(axis=0) if sp.shape[0] >= _RECENT else sp[-1, :]
        rstd = recent[good].std() if good.any() else 0.0
        rstd = float(rstd) if rstd > 1e-12 else 1.0
        z_idio = recent / rstd

        ts = frame.ts
        out: dict[str, ModuleOutput] = {}
        for j, sym in enumerate(symbols):
            if not good[j]:                 # zero-variance column → nothing to decompose for it
                out[sym] = ModuleOutput.abstain(
                    self.name, "zero_variance", self.horizon_min,
                    role="direction", evidence_family="cross_asset", shadow_only=True)
                continue
            zj = float(z_idio[j])
            idio = float(idio_fraction[j])
            direction = float(-np.tanh(zj / _DIR_SCALE))     # FADE the idiosyncratic shock
            conviction = float(min(abs(zj) / _CONV_SCALE, 1.0) * idio)
            stance = "short" if direction < 0 else "long"
            out[sym] = ModuleOutput(
                module=self.name,
                direction=direction,
                conviction=conviction,
                expected_move_pct=None,                       # honest: not this module's strength
                horizon_min=self.horizon_min,
                regime_tag="mean_revert",
                features={
                    "z_idio_residual": round(zj, 4),
                    "idio_fraction": round(idio, 4),
                    "factor_fraction": round(1.0 - idio, 4),
                    "factor_rank": int(rank),
                    "alm_iters": int(n_iter),
                    "alm_converged": bool(converged),
                    "n_symbols": int(len(symbols)),
                },
                explanation=(f"sparse_factor_residual: idiosyncratic z={zj:+.2f} "
                             f"(idio_frac {idio:.2f}, factor rank {rank}) → fade → {stance}"),
                ok=True,
                role="direction",
                evidence_family="cross_asset",
                shadow_only=True,            # §6g/Rule 14: recorded + evaluated, NEVER a live vote yet
                ts=ts,
            )
        return out
