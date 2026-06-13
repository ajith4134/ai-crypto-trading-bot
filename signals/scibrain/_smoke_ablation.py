"""§6g.8/§6g.10 module ablation/prune/demote — verdict invariants.

Run: docker compose exec -T brain python -m signals.scibrain._smoke_ablation
Pure synthetic; exercises the deterministic _verdict (no Redis).
"""
from __future__ import annotations

from .ablation import _verdict


def _v(**kw):
    base = dict(ic=0.1, in_ic_map=True, max_redundancy=0.0, redundant_with=None,
                partner_ic=None, ic_transferability=1.0, n=1000)
    base.update(kw)
    return _verdict("m", **base)["status"]


def test_insufficient():
    assert _v(in_ic_map=False, ic=None) == "INSUFFICIENT"
    print("  PASS immature → INSUFFICIENT")


def test_prune_harmful_significant():
    # significantly negative (n large enough that ic < -2·SE) → PRUNE
    assert _v(ic=-0.08, n=1000) == "PRUNE"
    print("  PASS significantly anti-predictive IC → PRUNE")


def test_noise_not_pruned():
    # the CORE significance guard: a small negative IC on FEW samples is noise → must NOT prune
    assert _v(ic=-0.08, n=30) == "KEEP", "must not prune on statistically-insignificant IC"
    assert _v(ic=-0.01, n=30) == "KEEP"
    print("  PASS small IC on few samples → KEEP (no pruning on noise)")


def test_prune_useless_confident():
    # confidently no edge: even optimistic upper bound below the useful floor (needs large n)
    assert _v(ic=0.001, n=20000) == "PRUNE"
    print("  PASS confidently no-edge IC → PRUNE (economically useless)")


def test_demote_redundant_worse():
    assert _v(ic=0.05, n=1000, max_redundancy=0.7, redundant_with="partner", partner_ic=0.12) == "DEMOTE"
    print("  PASS redundant + worse-than-partner → DEMOTE")


def test_keeper_of_redundant_pair_not_demoted():
    assert _v(ic=0.12, n=1000, max_redundancy=0.7, redundant_with="partner", partner_ic=0.05) == "KEEP"
    print("  PASS best representative of a redundant cluster is KEPT")


def test_watch_unstable():
    assert _v(ic=0.1, n=1000, ic_transferability=0.4) == "WATCH"
    print("  PASS predictive-but-drifted → WATCH")


def test_keep():
    assert _v(ic=0.1, n=1000, max_redundancy=0.2, ic_transferability=0.95) == "KEEP"
    print("  PASS predictive + distinct + stable → KEEP")


def test_precedence_useless_beats_redundant():
    assert _v(ic=0.001, n=20000, max_redundancy=0.9, redundant_with="p", partner_ic=0.2) == "PRUNE"
    print("  PASS precedence: useless+redundant → PRUNE (not DEMOTE)")


if __name__ == "__main__":
    test_insufficient()
    test_prune_harmful_significant()
    test_noise_not_pruned()
    test_prune_useless_confident()
    test_demote_redundant_worse()
    test_keeper_of_redundant_pair_not_demoted()
    test_watch_unstable()
    test_keep()
    test_precedence_useless_beats_redundant()
    print("ALL §6g ABLATION VERDICT INVARIANTS PASS")
