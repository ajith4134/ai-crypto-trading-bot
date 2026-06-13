"""SpectralGraphContagion — spectral analysis of the directed/signed asset graph; trade the
shock that's propagating from leaders to followers but hasn't arrived yet (Universe Core, §6e Tier-A).

UNIQUE EVIDENCE (§6g.2, distinct from SparseFactorResidual's residual-reversal AND from the future
CausalLeadLag's sparse-Granger): pure SPECTRAL GRAPH tools on the cross-market graph the UniverseFrame
already carries —

  • Undirected signed graph  A = |correlation| (thresholded)  → Laplacian L = D − A → spectrum:
      - Fiedler value λ2 (algebraic connectivity) = how cohesive/fragmented the market is
      - Fiedler vector sign = a 2-community spectral clustering (which cluster each name sits in)
  • Eigenvector centrality on A (power iteration) = contagion-HUB score
  • Directed graph  W[i,j] = directed_lead_lag = corr(rᵢ[t-1], rⱼ[t])  (leader i → follower j):
      - out-strength = leader-ness, in-strength = follower-ness
      - HEAT/shock diffusion of the recent return vector along W: diffused = Σ_{k=1..K} αᵏ (Wᵀ)ᵏ r
        = the move flowing INTO each node from its leaders that has not propagated yet.

FALSIFIABLE HYPOTHESIS (directed contagion / lead-lag propagation, NOT own-momentum): a node that its
leaders predict will MOVE THE SAME WAY soon. So:

    direction_j  = tanh( z_diffused_j / SCALE )                 (follow the incoming diffused shock)
    conviction_j = clip(|z_diffused_j|/CONV_SCALE, 0, 1) · follower_weight_j

where follower_weight_j = normalized in-strength → a strong FOLLOWER is predictable from its leaders
(high conviction), a pure LEADER is not predicted by others (low conviction — correct). Context features
carry cluster / Fiedler value / centrality / leader-vs-follower so the panel can render the graph.

ADMISSION (§6g + Rule 14, VS-9 observe authority): ships shadow_only=True — RECORDED + IC-evaluable,
NEVER applied to a live pick until incremental IC is proven. Deterministic, bounded, abstains on a
too-thin frame. Budget: one symmetric eigendecomposition (Fiedler) + a few mat-vecs, once per
Universe-Core cycle.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, UniverseFrame
from .base import UniverseModule

_MIN_T = 16
_MIN_S = 20
_EDGE_THRESH = 0.30      # keep graph edges with |weight| ≥ this (sparsify out noise correlations)
_DIFF_ALPHA = 0.5        # diffusion decay per hop
_DIFF_STEPS = 3          # number of propagation hops
_DIR_SCALE = 1.5         # tanh scale on z_diffused → direction
_CONV_SCALE = 3.0        # |z_diffused| at which conviction saturates


def _fiedler(lap: np.ndarray, s: int):
    """Fiedler value λ2 + Fiedler vector (2nd-smallest eigenpair) of the Laplacian.

    Uses scipy eigsh for ONLY the 2 smallest eigenpairs — an O(n²·k) sparse iteration instead of a
    full O(n³) eigendecomposition (≈40× faster on a ~500-node graph; the rest of the spectrum is
    unused). Falls back to a full dense eigh for tiny graphs or if the sparse solver fails to converge."""
    if s <= 3:
        try:
            ev, evec = np.linalg.eigh(lap)
            return float(ev[1]) if s > 1 else 0.0, (evec[:, 1] if s > 1 else np.zeros(s))
        except np.linalg.LinAlgError:
            return 0.0, np.zeros(s)
    try:
        import scipy.sparse as sp
        import scipy.sparse.linalg as sla
        ev, evec = sla.eigsh(sp.csr_matrix(lap), k=2, which="SA", maxiter=2000)
        order = np.argsort(ev)                       # ascending: [0]≈0 (constant), [1]=Fiedler
        return float(ev[order[1]]), evec[:, order[1]]
    except Exception:
        try:
            ev, evec = np.linalg.eigh(lap)
            return float(ev[1]), evec[:, 1]
        except np.linalg.LinAlgError:
            return 0.0, np.zeros(s)


def _eigenvector_centrality(a: np.ndarray, iters: int = 50) -> np.ndarray:
    """Eigenvector centrality of a symmetric non-negative adjacency via power iteration.
    Returns a unit-sum non-negative vector (hub score). Stable + cheap."""
    s = a.shape[0]
    v = np.ones(s, dtype=float) / s
    for _ in range(iters):
        v_next = a @ v
        n = np.linalg.norm(v_next)
        if n < 1e-12:
            return np.ones(s, dtype=float) / s
        v_next = v_next / n
        if np.linalg.norm(v_next - v) < 1e-9:
            v = v_next
            break
        v = v_next
    v = np.abs(v)
    tot = v.sum()
    return v / tot if tot > 0 else np.ones(s, dtype=float) / s


class SpectralGraphContagionModule(UniverseModule):
    name = "spectral_graph_contagion"
    horizon_min = 60

    def _compute(self, frame: UniverseFrame) -> dict[str, ModuleOutput]:
        symbols = frame.symbols
        s = len(symbols)
        ret = frame.returns_by_tf.get(frame.primary_tf)
        if (ret is None or ret.shape[0] < _MIN_T or s < _MIN_S
                or frame.correlation.shape != (s, s)
                or frame.directed_lead_lag.shape != (s, s)):
            return {}

        # ── undirected signed graph → Laplacian spectrum (Fiedler clustering + connectivity) ──
        a = np.abs(frame.correlation).astype(float)
        np.fill_diagonal(a, 0.0)
        a[a < _EDGE_THRESH] = 0.0                  # sparsify: drop weak/noise edges
        deg = a.sum(axis=1)
        lap = np.diag(deg) - a                      # combinatorial Laplacian (symmetric PSD)
        fiedler_val, fiedler_vec = _fiedler(lap, s)
        cluster = (fiedler_vec >= 0).astype(int)    # 2-community spectral partition
        centrality = _eigenvector_centrality(a)     # contagion-hub score

        # ── directed graph W (leader i → follower j) → in/out strength + shock diffusion ──
        w = frame.directed_lead_lag.astype(float).copy()
        np.fill_diagonal(w, 0.0)
        w[np.abs(w) < _EDGE_THRESH] = 0.0
        out_strength = np.abs(w).sum(axis=1)        # leader-ness (i predicts many)
        in_strength = np.abs(w).sum(axis=0)         # follower-ness (predicted by many)
        # normalize W so the diffusion can't blow up (spectral radius bound via ∞-norm)
        max_rowsum = float(np.abs(w).sum(axis=1).max())
        w_norm = w / max_rowsum if max_rowsum > 1e-12 else w

        # recent return vector (last bar), standardized cross-sectionally
        r_recent = ret[-1, :].astype(float)
        rstd = r_recent.std()
        r_recent = r_recent / rstd if rstd > 1e-12 else r_recent

        # heat/shock diffusion along the DIRECTED graph: incoming to j = Σ_i W[i,j]·r_i = (Wᵀ r)
        diffused = np.zeros(s, dtype=float)
        prop = r_recent.copy()
        wt = w_norm.T
        for k in range(1, _DIFF_STEPS + 1):
            prop = wt @ prop
            diffused += (_DIFF_ALPHA ** k) * prop
        dstd = diffused.std()
        z_diff = diffused / dstd if dstd > 1e-12 else diffused

        # follower weight: a strong follower is predictable from its leaders (high conviction);
        # a pure leader is not predicted by others (low). Normalize in-strength to [0,1].
        max_in = float(in_strength.max())
        follower_w = in_strength / max_in if max_in > 1e-12 else np.zeros(s)

        ts = frame.ts
        out: dict[str, ModuleOutput] = {}
        for j, sym in enumerate(symbols):
            zj = float(z_diff[j])
            fw = float(follower_w[j])
            direction = float(np.tanh(zj / _DIR_SCALE))      # FOLLOW the incoming diffused shock
            conviction = float(min(abs(zj) / _CONV_SCALE, 1.0) * fw)
            role_kind = ("leader" if out_strength[j] > in_strength[j] else "follower")
            stance = "long" if direction > 0 else "short"
            out[sym] = ModuleOutput(
                module=self.name,
                direction=direction,
                conviction=conviction,
                expected_move_pct=None,
                horizon_min=self.horizon_min,
                regime_tag="trending",                        # contagion = momentum propagation
                features={
                    "z_diffused_shock": round(zj, 4),
                    "follower_weight": round(fw, 4),
                    "in_strength": round(float(in_strength[j]), 4),
                    "out_strength": round(float(out_strength[j]), 4),
                    "graph_role": role_kind,
                    "cluster": int(cluster[j]),
                    "eigen_centrality": round(float(centrality[j]), 6),
                    "fiedler_value": round(fiedler_val, 6),
                    "n_symbols": int(s),
                },
                explanation=(f"spectral_graph_contagion: incoming shock z={zj:+.2f} "
                             f"({role_kind}, follower_w {fw:.2f}, cluster {int(cluster[j])}) "
                             f"→ follow → {stance}"),
                ok=True,
                role="direction",
                evidence_family="contagion",                  # distinct family (§6g.1)
                shadow_only=True,                             # recorded + evaluated, NEVER a live vote yet
                ts=ts,
            )
        return out
