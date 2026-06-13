"""§6g.330 evidence-family correlation penalty — invariants + adversarial tests.

Run: docker compose exec -T brain python -m signals.scibrain._smoke_family_penalty
Pure synthetic; no Redis/market. Proves the penalty does what §6g.330 specifies before it
touches the live vote (Rule 14 admission §6g.6).
"""
from __future__ import annotations

from .contracts import ModuleOutput
from .fusion import fuse
from .modules.base import Module


def _mk(name, direction, conv, family):
    return ModuleOutput(module=name, direction=direction, conviction=conv,
                        expected_move_pct=None, horizon_min=60, regime_tag=None,
                        features={}, explanation="", evidence_family=family)


def _net(dec):
    # reconstruct signed net from attribution shares (sum of shares == net)
    return round(sum(a["share"] for a in dec.attribution), 6)


def test_clique_collapse():
    # 5 same-family trend voters (long) vs 1 distinct reversion voter (short)
    outs = [_mk(f"trend{i}", 1.0, 0.8, "trend_momentum") for i in range(5)]
    outs.append(_mk("rev", -1.0, 0.8, "mean_reversion"))
    base = fuse("T", outs, penalty_strength=0.0)
    pen = fuse("T", outs, penalty_strength=1.0)
    nb, npn = _net(base), _net(pen)
    print(f"  clique: net no-penalty={nb}  with-penalty={npn}  (max_discount={pen.family_penalty['max_discount']})")
    assert abs(npn) < abs(nb), "penalty must REDUCE the redundant clique's net dominance"
    assert pen.family_penalty["groups"].get("trend_momentum") == 5
    # the lone independent reversion voter stays undiscounted
    rev = next(a for a in pen.attribution if a["module"] == "rev")
    assert rev["redundancy_discount"] == 1.0, "independent module must NOT be discounted"
    print("  PASS clique-collapse")


def test_strength_zero_is_noop():
    outs = [_mk(f"trend{i}", 1.0, 0.8, "trend_momentum") for i in range(4)]
    a = fuse("T", outs, penalty_strength=0.0)
    assert all(x["redundancy_discount"] == 1.0 for x in a.attribution)
    assert a.family_penalty["applied"] is False
    print("  PASS strength=0 exact no-op (instant rollback path)")


def test_independents_undiscounted():
    outs = [_mk("a", 1.0, 0.8, "trend_momentum"),
            _mk("b", 1.0, 0.8, "mean_reversion"),
            _mk("c", 1.0, 0.8, "topology")]
    pen = fuse("T", outs, penalty_strength=1.0)
    assert all(x["redundancy_discount"] == 1.0 for x in pen.attribution), \
        "distinct-family voters with no HSIC must be undiscounted"
    print("  PASS independents-undiscounted")


def test_hsic_crosses_family():
    # two DIFFERENT-family modules that are empirically redundant (HSIC 0.9) must be discounted
    outs = [_mk("a", 1.0, 0.8, "topology"), _mk("b", 1.0, 0.8, "crowding")]
    report = {"redundant_pairs": [["a", "b", 0.9]]}
    pen = fuse("T", outs, redundancy=report, penalty_strength=1.0)
    discs = {x["module"]: x["redundancy_discount"] for x in pen.attribution}
    print(f"  hsic cross-family discounts: {discs}  hsic_pairs_used={pen.family_penalty['hsic_pairs_used']}")
    assert discs["a"] < 1.0 and discs["b"] < 1.0, "HSIC must discount cross-family empirical redundancy"
    assert pen.family_penalty["hsic_pairs_used"] == 1
    print("  PASS hsic-cross-family")


class _ShadowClamp(Module):
    name = "shadow_clamp"
    evidence_family = "tail"
    role = "risk"

    def _compute(self, frame):
        # deliberately out-of-range conviction to force the base.py clamp/rebuild path
        return ModuleOutput(module=self.name, direction=0.5, conviction=1.7,
                            expected_move_pct=None, horizon_min=60, regime_tag=None,
                            features={}, explanation="", shadow_only=True)


def test_shadow_preserved_through_clamp():
    out = _ShadowClamp().evaluate(frame=None)
    print(f"  clamped: conviction={out.conviction} shadow_only={out.shadow_only} "
          f"role={out.role} family={out.evidence_family}")
    assert out.conviction == 1.0, "conviction must be clamped to 1.0"
    assert out.shadow_only is True, "REGRESSION: shadow_only dropped on clamp → live-vote leak!"
    assert out.role == "risk" and out.evidence_family == "tail", "identity must survive clamp"
    print("  PASS shadow-preserved-through-clamp (base.py leak fix)")


def test_identity_stamp_default():
    # a module declaring family at class level but building a plain ModuleOutput gets it stamped
    class _K(Module):
        name = "k"
        evidence_family = "dynamics_spectral"

        def _compute(self, frame):
            return ModuleOutput(module="k", direction=1.0, conviction=0.5, expected_move_pct=None,
                                horizon_min=60, regime_tag=None, features={}, explanation="")
    out = _K().evaluate(frame=None)
    assert out.evidence_family == "dynamics_spectral", "class-declared family must be stamped"
    print("  PASS identity-stamp-from-class")


if __name__ == "__main__":
    test_clique_collapse()
    test_strength_zero_is_noop()
    test_independents_undiscounted()
    test_hsic_crosses_family()
    test_shadow_preserved_through_clamp()
    test_identity_stamp_default()
    print("ALL §6g.330 FAMILY-PENALTY INVARIANTS PASS")
