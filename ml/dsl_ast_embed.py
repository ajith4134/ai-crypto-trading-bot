"""
F60 — AST embedding + novelty / complexity regularisers (cont. 56, 2026-05-28).

Lightweight ML-free implementation of AlphaAgent's (arXiv:2502.16789) AST
regularisers, layered on top of F54's LLM-DSL miner. We deliberately avoid
the GNN-embedder approach because:
  - The promoted-factor pool tops out at 30 (F54's MAX_PROMOTED) — a learned
    embedder is overkill for ~30 trees.
  - Sub-tree frequency vectors (a.k.a. "bag of node-types + edges") reproduce
    AlphaAgent's headline numbers in their own ablation table.

Public API:
  - `subtree_vector(node)`     — sparse dict[str, int] of subtree counts.
  - `cosine_distance(a, b)`    — 1 - cos_sim over the two vectors.
  - `min_distance_to_pool(node, pool_exprs)` — nearest-neighbour distance to
    the set of already-promoted factors. Higher = more novel.
  - `complexity(node)`         — node count + window-diversity penalty.
  - `score_novelty(node, pool_exprs)` — combined novelty + simplicity score
    ∈ [0, 1]. Used by the F54 promoter as a tie-breaker.

Configuration via env:
  DSL_NOVELTY_MIN_DISTANCE   default 0.30 — reject if min_distance < this.
  DSL_NOVELTY_COMPLEXITY_CAP default 30   — reject if complexity > this.
"""
from __future__ import annotations

import math
import os
from collections import Counter
from typing import Iterable

import structlog
from feature_governance.registry import register

from ml.dsl_grammar import Const, Terminal, Op, Node, parse, DSLParseError

log = structlog.get_logger()

_FG_ID = "F60"
try:
    register(_FG_ID, "AlphaAgent Novelty Regularisers", activation_phase=0)
except Exception as _exc:
    log.debug("f60_self_register_deferred", err=str(_exc))


_MIN_DISTANCE = float(os.environ.get("DSL_NOVELTY_MIN_DISTANCE", 0.30))
_COMPLEXITY_CAP = int(os.environ.get("DSL_NOVELTY_COMPLEXITY_CAP", 30))


# ---- Subtree-frequency embedder --------------------------------------------

def _subtree_key(node: Node, depth_limit: int = 2) -> str:
    """Stable, depth-limited shape descriptor of a subtree.

    Depth 0: leaf type only (`T:close`, `C`).
    Depth 1: op_name + immediate child type signatures.
    Depth 2: op_name + child[0]'s depth-1 signature + child[1]'s, ...

    Limiting depth makes the vocabulary tractable: a 30-promoted-factor
    pool yields ~150-300 distinct subtree shapes at depth 2 (sufficient
    for nearest-neighbour novelty detection).
    """
    if isinstance(node, Const): return "C"
    if isinstance(node, Terminal): return f"T:{node.name}"
    if isinstance(node, Op):
        if depth_limit <= 0:
            return f"OP:{node.name}"
        child_keys = ",".join(_subtree_key(a, depth_limit - 1) for a in node.args)
        if node.window is not None:
            return f"{node.name}({child_keys})|w={node.window}"
        return f"{node.name}({child_keys})"
    return "UNK"


def subtree_vector(node: Node) -> dict[str, int]:
    """Multiset of every subtree's depth-2 shape descriptor."""
    bag: Counter = Counter()
    def _walk(n: Node):
        bag[_subtree_key(n, depth_limit=2)] += 1
        if isinstance(n, Op):
            for a in n.args: _walk(a)
    _walk(node)
    return dict(bag)


def cosine_distance(a: dict[str, int], b: dict[str, int]) -> float:
    """1 - cosine similarity of two count vectors."""
    if not a or not b: return 1.0
    keys = set(a.keys()) | set(b.keys())
    dot = sum(a.get(k, 0) * b.get(k, 0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0: return 1.0
    return max(0.0, min(1.0, 1.0 - dot / (na * nb)))


def min_distance_to_pool(node: Node, pool_exprs: Iterable[str]) -> float:
    """Nearest-neighbour cosine distance vs the supplied pool of S-exprs.

    Returns 1.0 when pool is empty (max novelty).
    Invalid expressions in the pool are silently skipped.
    """
    target_vec = subtree_vector(node)
    if not target_vec: return 0.0
    nearest = 1.0
    seen_any = False
    for expr in pool_exprs:
        try:
            other = parse(expr)
        except DSLParseError:
            continue
        d = cosine_distance(target_vec, subtree_vector(other))
        if d < nearest: nearest = d
        seen_any = True
    return nearest if seen_any else 1.0


# ---- Complexity penalty ----------------------------------------------------

def node_count(node: Node) -> int:
    """Total number of nodes in the AST."""
    if isinstance(node, (Const, Terminal)): return 1
    if isinstance(node, Op):
        return 1 + sum(node_count(a) for a in node.args)
    return 1


def window_diversity(node: Node) -> int:
    """Distinct windows used. Higher = more complex (factor is fishing
    across multiple time horizons)."""
    seen: set[int] = set()
    def _walk(n: Node):
        if isinstance(n, Op):
            if n.window is not None: seen.add(n.window)
            for a in n.args: _walk(a)
    _walk(node)
    return len(seen)


def complexity(node: Node) -> int:
    """Combined complexity score. Threshold: _COMPLEXITY_CAP (default 30).

    Formula: nodes + 5 × distinct_windows.
    """
    return node_count(node) + 5 * window_diversity(node)


# ---- Composite novelty score -----------------------------------------------

def score_novelty(node: Node, pool_exprs: Iterable[str]) -> float:
    """Combined [0, 1] novelty score (higher = more promotable).

    score = α × min_distance_to_pool + (1 - α) × (1 - complexity/CAP)
    α = 0.7 — distance dominates. Complexity is the tie-breaker.
    """
    d = min_distance_to_pool(node, pool_exprs)
    c = complexity(node)
    c_norm = max(0.0, 1.0 - c / max(_COMPLEXITY_CAP, 1))
    return 0.7 * d + 0.3 * c_norm


# ---- Gates (called by F54 promoter) ----------------------------------------

def passes_novelty_gate(node: Node, pool_exprs: Iterable[str]
                        ) -> tuple[bool, str]:
    """Return (ok, reason) — ok=True means promote, False means reject."""
    if complexity(node) > _COMPLEXITY_CAP:
        return False, f"complexity_too_high:{complexity(node)}"
    d = min_distance_to_pool(node, pool_exprs)
    if d < _MIN_DISTANCE:
        return False, f"too_similar_to_pool:{d:.3f}"
    return True, ""


def build_decay_re_mining_prompt(decayed_expr: str, hypothesis: str) -> str:
    """Targeted re-mining prompt: ask the LLM to produce a STRUCTURAL variant
    of a decayed factor — same hypothesis, different operators/windows.

    Called by `ml/llm_alpha_dsl.py:check_decay_and_demote` for each
    factor it's about to demote, so the corpus refreshes instead of
    leaking signal value via demotion alone.
    """
    return f"""\
You are revising a quant-trading alpha factor that recently lost predictive
power. Produce a NEW S-expression that:
  1. Targets the SAME economic hypothesis as the original.
  2. Uses STRUCTURALLY different operators or windows from the original.
  3. Is at most {_COMPLEXITY_CAP} nodes total.

Original (now decayed): {decayed_expr}
Original hypothesis: {hypothesis or '(unknown)'}

Output STRICTLY:
<new s-expression>
# hypothesis: <revised one-line statement>
"""
