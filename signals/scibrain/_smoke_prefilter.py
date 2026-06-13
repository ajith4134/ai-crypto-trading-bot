"""Phase 5 top-K prefilter — selection invariants (Rule 14 coverage).

Run: docker compose exec -T brain python -m signals.scibrain._smoke_prefilter
Patches the volatility scorer so the test is deterministic (no Redis).
"""
from __future__ import annotations

from . import gate


def _patch_scores(scores):
    gate._cheap_vol_scores = lambda r, syms, *a, **k: {s: scores.get(s, 0.0) for s in syms}


def test_topk_picks_highest_vol():
    syms = [f"P{i}" for i in range(10)]
    _patch_scores({f"P{i}": float(i) for i in range(10)})   # P9 highest … P0 lowest
    sel, meta = gate._prefilter_select(None, syms, k=3, rotate=0, cycle=0)
    assert sel == ["P9", "P8", "P7"], sel
    assert meta["n_topk"] == 3 and meta["n_rotate"] == 0
    print("  PASS top-K selects highest-volatility pairs")


def test_rotation_covers_the_rest_over_cycles():
    syms = [f"P{i}" for i in range(10)]
    _patch_scores({f"P{i}": float(i) for i in range(10)})   # top-3 = P9,P8,P7; rest = P6..P0 (7 pairs)
    seen_rotated = set()
    for c in range(8):                                       # rotate=2 over the 7 'rest' pairs
        sel, _ = gate._prefilter_select(None, syms, k=3, rotate=2, cycle=c)
        assert sel[:3] == ["P9", "P8", "P7"]                # top-K stable every cycle
        seen_rotated.update(sel[3:])
    rest = {f"P{i}" for i in range(7)}                       # P0..P6
    assert rest <= seen_rotated, f"rotation must eventually cover every non-topK pair, missed {rest - seen_rotated}"
    print("  PASS rotation covers all non-topK pairs within a few cycles (no starvation)")


def test_no_duplicates_and_small_universe():
    syms = [f"P{i}" for i in range(5)]
    _patch_scores({s: 1.0 for s in syms})
    sel, meta = gate._prefilter_select(None, syms, k=10, rotate=5, cycle=0)  # k >= len ⇒ all
    assert sorted(sel) == sorted(syms) and len(sel) == len(set(sel))
    print("  PASS universe<=k returns all, no duplicates")


def test_rotation_no_dup_when_window_overlaps_topk_boundary():
    syms = [f"P{i}" for i in range(6)]
    _patch_scores({f"P{i}": float(i) for i in range(6)})
    sel, _ = gate._prefilter_select(None, syms, k=2, rotate=10, cycle=0)     # rotate bigger than rest
    assert len(sel) == len(set(sel)), "no duplicates even when rotate exceeds rest"
    assert set(sel) == set(syms), "with big rotation all pairs covered in one cycle"
    print("  PASS no duplicates when rotation window is large")


if __name__ == "__main__":
    test_topk_picks_highest_vol()
    test_rotation_covers_the_rest_over_cycles()
    test_no_duplicates_and_small_universe()
    test_rotation_no_dup_when_window_overlaps_topk_boundary()
    print("ALL PHASE-5 PREFILTER INVARIANTS PASS")
