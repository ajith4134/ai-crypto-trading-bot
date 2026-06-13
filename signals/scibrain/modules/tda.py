"""TDAModule — Topological Data Analysis (persistent homology of the price path).

Persistent homology summarizes the SHAPE of data across all scales. For a 1-D price
series the relevant invariant is H0 (connected components) of the sublevel-set
filtration: sweeping a water level upward, a new component is BORN at each local minimum
(a valley) and DIES when it merges with a deeper basin at a local maximum (a ridge). Each
component's PERSISTENCE = death - birth = the topological depth of that valley — a
scale-free measure of how structurally significant a support level is, immune to noise.

We compute the exact H0 persistence diagram via a union-find sweep (pure numpy, no
external TDA library), then:
  - persistence_entropy: low => one dominant structure (a clean range) => trust it;
    high => fragmented/choppy => low conviction.
  - the most persistent valley/ridge define the dominant range; the price's position in
    that range gives a structure-aware mean-reversion vote (near a deep support => long,
    near a dominant resistance => short), gated by how dominant the structure is.
Abstains on insufficient data.
"""
from __future__ import annotations

import numpy as np

from ..contracts import ModuleOutput, SensorFrame
from .base import Module

_TF = "5m"
_MIN_BARS = 60
_MAX_BARS = 200
_HORIZON_MIN = 90


class TDAModule(Module):
    name = "tda"
    evidence_family = "topology"  # §6g.330 family-correlation penalty
    horizon_min = _HORIZON_MIN

    def _compute(self, frame: SensorFrame) -> ModuleOutput:
        closes = frame.closes(_TF)
        if closes is None or len(closes) < _MIN_BARS:
            return ModuleOutput.abstain(self.name, "insufficient_bars", self.horizon_min)
        closes = closes[-_MAX_BARS:].astype(float)
        if not np.all(np.isfinite(closes)) or float(np.min(closes)) <= 0:
            return ModuleOutput.abstain(self.name, "bad_prices", self.horizon_min)

        f = np.log(closes)
        persistences = _h0_persistence(f)
        rng = float(f.max() - f.min())
        if rng <= 0:
            return ModuleOutput.abstain(self.name, "flat_series", self.horizon_min)

        if persistences.size == 0:
            max_pers = rng
            pers_entropy = 1.0
            n_feat = 0
        else:
            max_pers = float(np.max(persistences))
            n_feat = int(np.sum(persistences > 0.15 * rng))   # structurally significant valleys
            pers_entropy = _persistence_entropy(persistences)

        # dominant range position of the current price
        lo, hi = float(f.min()), float(f.max())
        pos = float((f[-1] - lo) / (hi - lo))                 # 0=at support, 1=at resistance
        # structure dominance: a single deep valley (low entropy, high max persistence)
        dominance = float(np.clip((max_pers / rng) * (1.0 - pers_entropy), 0.0, 1.0))

        # structure-aware mean reversion: near deep support -> long, near resistance -> short
        direction = float(np.clip((0.5 - pos) * 2.0 * (0.4 + 0.6 * dominance), -1.0, 1.0))
        conviction = float(np.clip(dominance * (0.5 + 0.5 * abs(pos - 0.5) * 2.0), 0.0, 1.0))

        # breakout guard: a fresh extreme beyond the structure is a trend, not a fade
        if pos >= 0.98 or pos <= 0.02:
            conviction *= 0.5
            regime = "range_edge_breakout_risk"
        elif dominance >= 0.5:
            regime = "dominant_range"
        else:
            regime = "fragmented"

        expl = (f"TDA: H0 max_persistence={max_pers/rng:.2f} of range, n_features={n_feat}, "
                f"entropy={pers_entropy:.2f}, dominance={dominance:.2f}; price at "
                f"{pos*100:.0f}% of range -> "
                f"{'long' if direction>0 else 'short' if direction<0 else 'flat'}")
        return ModuleOutput(
            module=self.name, direction=direction, conviction=conviction,
            expected_move_pct=None, horizon_min=self.horizon_min, regime_tag=regime,
            features={"tf": _TF, "max_persistence_frac": round(max_pers / rng, 4),
                      "n_features": n_feat, "persistence_entropy": round(pers_entropy, 4),
                      "range_position": round(pos, 4), "dominance": round(dominance, 4)},
            explanation=expl)


def _h0_persistence(f: np.ndarray) -> np.ndarray:
    """Exact 0-dim sublevel-set persistence of a 1-D function via union-find.

    Vertices = samples, edges connect consecutive samples. Process vertices by increasing
    f: each starts a component (birth=f[v]); an edge merging two live components kills the
    YOUNGER one (higher birth) at the current level => persistence = level - younger_birth.
    The single oldest component (global min) never dies and is excluded.
    """
    n = f.size
    order = np.argsort(f, kind="mergesort")
    parent = np.full(n, -1, dtype=int)        # -1 = not yet added
    birth = np.zeros(n)
    root_birth: dict[int, float] = {}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pers: list[float] = []
    for v in order:
        parent[v] = v
        birth[v] = f[v]
        root_birth[v] = f[v]
        for nb in (v - 1, v + 1):
            if 0 <= nb < n and parent[nb] != -1:
                rv, rn = find(v), find(nb)
                if rv == rn:
                    continue
                bv, bn = root_birth[rv], root_birth[rn]
                # younger = higher birth value -> it dies now at level f[v]
                if bv >= bn:
                    young, old = rv, rn
                else:
                    young, old = rn, rv
                pers.append(float(f[v] - root_birth[young]))
                parent[young] = old
                root_birth[old] = min(root_birth[old], root_birth[young])
    return np.asarray([p for p in pers if p > 0.0], dtype=float)


def _persistence_entropy(p: np.ndarray) -> float:
    """Normalized Shannon entropy of the persistence distribution, in [0,1].
    0 => one structure dominates; 1 => many equal-weight structures (fragmented)."""
    p = p[p > 0]
    if p.size <= 1:
        return 0.0
    w = p / np.sum(p)
    h = -float(np.sum(w * np.log(w)))
    return float(h / np.log(p.size))
