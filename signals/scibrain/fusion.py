"""SciBrain Fusion ALU — combine ModuleOutputs into one Decision.

VS-1 fusion (kept deliberately simple + interpretable; the information-geometry /
learned-IC weighting from the master notes is layered ON TOP in Phase 3, not now):

  1. Split modules into DIRECTIONAL (they vote a sign) and GATE modules (BOCPD-style
     stability, direction==0). A gate contributes a multiplicative damper, not a vote.
  2. Weight each directional vote by  w_i = conviction_i × max(reliability_ic_i, floor)
     × router_gain_i.  The router_gain (Phase 3 Meta-Router/MoE) selects + reweights the
     experts that fit the detected regime; gain 0 = the expert is deselected this regime.
     With no router passed (or gain 1.0) this is the original pure-conviction weighting.
  3. net = Σ w_i·direction_i / Σ w_i          → signed in [-1,1]
     stability = Π gate_conviction            → [0,1] damper from BOCPD et al.
  4. direction = long/short/None by a dead-band on (net × stability).
  5. size_frac = fractional-Kelly proxy: clip(|net|·stability·KELLY_BASE, 0, CAP).

Everything needed to audit the decision is carried in Decision.contributing + Decision.router.
"""
from __future__ import annotations

import numpy as np

from .contracts import Decision, ModuleOutput, SensorFrame
from .router import RouterState

# tunables (Phase-later: move to config.yaml / Redis per C6; defaults explicit here)
DEAD_BAND = 0.04          # |net×stability| below this → no trade (None)
IC_FLOOR = 1.0            # until reliability_ic is learned, weight by conviction alone
KELLY_BASE = 0.5          # fraction-of-Kelly scaler
SIZE_CAP = 0.25           # max size_frac a single candidate can request
FAMILY_PRIOR = 0.6        # §6g.330: structural same-evidence_family redundancy when no empirical HSIC yet


def _family_redundancy(directional: list[ModuleOutput], redundancy: dict | None,
                       strength: float) -> tuple[np.ndarray, dict]:
    """§6g.330 evidence-family correlation penalty.

    "Five trend-derived modules cannot count as five independent confirmations." Each voting
    module's weight is discounted by its *effective multiplicity* — how redundant it is with the
    OTHER voters. Similarity S_ij ∈ [0,1] blends two signals (HYBRID per owner 2026-06-11):
      • structural: same declared evidence_family ⇒ FAMILY_PRIOR (always-on, works in cold start);
      • empirical: normalized-HSIC from info_geometry's redundancy matrix (catches nonlinear
        cross-family redundancy once ≥30 co-voted samples mature). max(prior, hsic) wins.
    discount_i = 1 / (1 + strength · Σ_{j≠i} S_ij). A clique of k near-identical voters collapses
    toward counting ~once; an independent voter ≈ undiscounted. strength=0 ⇒ all discounts 1.0
    (exact no-op / instant rollback). Bounded (0,1], deterministic, fully auditable.
    """
    n = len(directional)
    discounts = np.ones(n, dtype=float)
    summary = {"strength": float(max(strength, 0.0)), "applied": False,
               "n_directional": n, "groups": {}, "max_discount": 0.0, "hsic_pairs_used": 0}
    if strength <= 0.0 or n <= 1:
        return discounts, summary

    fams = [o.evidence_family for o in directional]
    mods = [o.module for o in directional]
    # HSIC pairs published by info_geometry (only pairs ≥ its redundant-threshold appear)
    hsic: dict = {}
    for entry in (redundancy or {}).get("redundant_pairs") or []:
        try:
            hsic[frozenset((entry[0], entry[1]))] = float(entry[2])
        except (TypeError, ValueError, IndexError):
            continue
    voter_set = set(mods)
    hsic_used = sum(1 for k in hsic if k <= voter_set and len(k) == 2)

    neigh = np.zeros(n, dtype=float)
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            s = FAMILY_PRIOR if (fams[i] != "unspecified" and fams[i] == fams[j]) else 0.0
            h = hsic.get(frozenset((mods[i], mods[j])))
            if h is not None and h > s:
                s = h
            neigh[i] += min(max(s, 0.0), 1.0)
    discounts = 1.0 / (1.0 + strength * neigh)

    from collections import Counter
    groups = {f: c for f, c in Counter(f for f in fams if f != "unspecified").items() if c > 1}
    summary.update(applied=True, groups=groups,
                   max_discount=round(float(1.0 - discounts.min()), 4),
                   hsic_pairs_used=hsic_used)
    return discounts, summary


def fuse(symbol: str, outputs: list[ModuleOutput],
         frame: SensorFrame | None = None,
         router: RouterState | None = None,
         redundancy: dict | None = None,
         penalty_strength: float = 0.0) -> Decision:
    gains = router.gains if router is not None else {}
    router_dict = router.to_dict() if router is not None else None

    # shadow_only modules are EXCLUDED from the live vote/stability (§5a): they remain in
    # `outputs`/Decision.contributing so they're recorded + evaluable, but never applied.
    directional = [o for o in outputs if o.ok and o.conviction > 0 and o.direction != 0.0
                   and gains.get(o.module, 1.0) > 0.0 and not o.shadow_only]
    gates = [o for o in outputs if o.ok and o.direction == 0.0 and o.conviction > 0
             and not o.shadow_only]

    # stability damper = product of gate convictions (BOCPD stability, etc.)
    stability = 1.0
    for g in gates:
        stability *= float(g.conviction)
    stability = float(max(0.0, min(1.0, stability)))

    if not directional:
        regime = _vote_regime(outputs)
        return Decision(symbol=symbol, direction=None, conviction=0.0,
                        expected_move_pct=None, size_frac=0.0, regime=regime,
                        contributing=outputs, attribution=[], primary_driver=None,
                        router=router_dict)

    weights = np.array([o.conviction * max(o.reliability_ic or IC_FLOOR, 0.0)
                        * float(gains.get(o.module, 1.0))
                        for o in directional], dtype=float)
    # §6g.330 evidence-family correlation penalty: discount redundant voters so a clique of
    # same-family / empirically-correlated modules can't stack as independent confirmations.
    discounts, family_penalty = _family_redundancy(directional, redundancy, penalty_strength)
    weights = weights * discounts
    dirs = np.array([o.direction for o in directional], dtype=float)
    wsum = float(weights.sum()) or 1.0
    net = float((weights * dirs).sum() / wsum)          # [-1, 1]

    # ── attribution: which factor is responsible? (computed, not inferred) ───────
    # each module's signed share of `net`; shares sum to net. The aligned module with
    # the largest share is the primary driver (the EDENUSDT "OFI was primary" answer).
    contrib = (weights * dirs) / wsum
    attribution = sorted(
        [{"module": o.module, "share": round(float(s), 4),
          "vote": round(float(o.direction), 4), "conviction": round(float(o.conviction), 4),
          "family": o.evidence_family, "redundancy_discount": round(float(disc), 4),
          "aligned": bool((s > 0) == (net > 0)) if net != 0 else False}
         for o, s, disc in zip(directional, contrib, discounts)],
        key=lambda a: abs(a["share"]), reverse=True)
    aligned_shares = [(o.module, s) for o, s in zip(directional, contrib)
                      if net != 0 and (s > 0) == (net > 0)]
    primary_driver = (max(aligned_shares, key=lambda kv: abs(kv[1]))[0]
                      if aligned_shares else None)

    score = net * stability
    if score > DEAD_BAND:
        direction = "long"
    elif score < -DEAD_BAND:
        direction = "short"
    else:
        direction = None

    conviction = float(min(abs(score), 1.0))

    # expected move: conviction-weighted mean of modules that forecast one, signed
    em_vals, em_w = [], []
    for o in directional:
        if o.expected_move_pct is not None:
            em_vals.append(abs(o.expected_move_pct))
            em_w.append(o.conviction)
    expected_move = (float(np.average(em_vals, weights=em_w))
                     if em_vals and sum(em_w) > 0 else None)

    size_frac = (float(min(conviction * KELLY_BASE, SIZE_CAP))
                 if direction is not None else 0.0)
    # canonical regime = the Meta-Router's verdict when present, else the descriptive
    # majority-vote tag (keeps the funnel/heartbeat regime meaningful + auditable).
    regime = router.regime if router is not None else _vote_regime(outputs)

    return Decision(symbol=symbol, direction=direction, conviction=conviction,
                    expected_move_pct=expected_move, size_frac=size_frac,
                    regime=regime, contributing=outputs,
                    attribution=attribution, primary_driver=primary_driver,
                    router=router_dict, family_penalty=family_penalty)


def _vote_regime(outputs: list[ModuleOutput]) -> str:
    """Majority regime tag across modules that emitted one (transparency only)."""
    tags: dict[str, float] = {}
    for o in outputs:
        if o.regime_tag and o.ok:
            tags[o.regime_tag] = tags.get(o.regime_tag, 0.0) + max(o.conviction, 0.05)
    if not tags:
        return "unknown"
    return max(tags.items(), key=lambda kv: kv[1])[0]
