# F61 — AlphaGen / AlphaSAGE RL Formulaic Alpha Search

**Status:** Tier B — DEFERRED (design only this session, cont. 55).
**Research:** github.com/RL-MLDM/alphagen (KDD 2023), arXiv:2509.25055
AlphaSAGE — GFlowNet-based diversity-aware formulaic alpha search.
Search space 10⁵ → 10¹⁵ vs F25 genetic algorithm.

## Why deferred (HARD)

1. **GPU recommended**: GFlowNet training benefits enormously from GPU.
   CPU-only training is 10-50× slower; F25 GA already exists on CPU and
   covers the same search space at lower quality.
2. **Overlap with F54**: F54 LLM-DSL also expands the search space far
   beyond F25 GA's reach. F54 uses an LLM prior; F61 uses an RL agent.
   Different paths to the same goal. We commit to F54 first; F61 only
   if F54 plateaus.
3. **Code complexity**: ~1500 LoC (GFlowNet trainer + replay buffer +
   reward computation + diversity term).

## When to implement

If, after 60 days of F54 in production, the LLM-DSL miner is exhausting
its productive corner of the search space (declining novelty / declining
average IC per mining run), F61 is the next move. Plug an RL agent into
the same DSL grammar.

## Design sketch

- `ml/alphagen_rl.py` — PPO or GFlowNet agent over the F54 grammar (reuses
  `ml/dsl_grammar.py` + `ml/dsl_evaluator.py`).
- Reward = IC + Sharpe - λ_novelty * AST-distance-to-existing.
- Trained offline; inference is rollout → factor → promoted via the same
  F54 promoter contract.
- Pretrainer step 16: nightly RL fine-tune (60 min on CPU, ~5 min on GPU).

## Open questions

- GFlowNet vs PPO? GFlowNet is the AlphaSAGE choice; gives mode coverage,
  not just mode collapse. Slower to converge.
- GPU access — if VPS adds a GPU, F61 jumps in priority by 2-3 tiers.

## Session handoff

Not implemented yet. Re-open after F54 has been live ≥60 days OR if user
adds a GPU to the VPS.
