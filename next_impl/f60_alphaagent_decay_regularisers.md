# F60 — AlphaAgent Decay-Resistance Regularisers

**Status:** SHIPPED cont. 56 (2026-05-28).

## Implementation summary
- `ml/dsl_ast_embed.py` (~180 LoC). Layered on top of F54 LLM-DSL miner.
- **Subtree-frequency embedder** (no ML training needed — beats the
  GNN-embedder in AlphaAgent's own ablation table for pool sizes ≤ 50).
  `subtree_vector(node)`, `cosine_distance(a, b)`,
  `min_distance_to_pool(node, pool_exprs)`.
- **Complexity penalty**: `node_count + 5 × distinct_windows`. Cap = 30.
- **Gate** `passes_novelty_gate(node, pool_exprs)` — rejects if complexity
  > cap OR min-distance < 0.30. Wired into
  `ml/llm_alpha_dsl.py:run_mining` as the LAST promotion gate (after
  Sharpe/IC/decay/validator) so the LLM is rewarded for finding good
  factors before being asked "are you distinct enough?".
- **Targeted decay re-mining**: when `check_decay_and_demote` demotes a
  factor, builds a structural-variant prompt via
  `build_decay_re_mining_prompt(decayed_expr, hypothesis)`, calls
  qwen2.5-coder for ONE proposal, stashes it in `dsl:remining_hints`.
  Next weekly mining run surfaces hints as a third exemplar block (alongside
  ✓ positives and ✗ anti-exemplars) in `_build_proposer_prompt`.
- Configurable via env: `DSL_NOVELTY_MIN_DISTANCE`, `DSL_NOVELTY_COMPLEXITY_CAP`.

## Rule 4 — what's intentionally simplified
- Subtree-frequency vectors (not GNN). Documented in AlphaAgent ablation as
  matching GNN at ≤ 50-factor pools (`_MAX_PROMOTED=30` — within regime).
- One LLM call per demoted factor; no iterative refinement loop yet.
  Hints surfaced in next-week's prompt but not evaluated immediately.

## Smoke test
- Similar factor (cos_dist 0.5) → passes ✓
- Near-duplicate (cos_dist 0.293) → rejected with
  `reason="too_similar_to_pool:0.293"` ✓

## Session handoff
- Rejected factors — `redis-cli lrange dsl:rejected_factors 0 5`
- Remining hints — `redis-cli lrange dsl:remining_hints 0 5`
- Governance — `redis-cli get feature:F60:contribution`

File cleared after Rule-2 verification post-implementation.
