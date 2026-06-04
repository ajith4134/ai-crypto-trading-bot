# F54 — Crypto LLM-DSL Alpha Miner (Ollama qwen2.5-coder)

**Status:** Tier S — implementing this session (cont. 55). HIGHEST-ROI item.

## Research basis

- arXiv:2604.26747 "From Hypotheses to Factors" — **the only peer-reviewed
  paper with crypto-specific net-of-cost validation**. LLM-generated DSL
  factors on crypto perpetuals 2024-2026 OOS window:
  - **44.55% annualised return** after 5bps round-trip cost
  - **Sharpe 1.55** net of cost
  - Held up across BTC, ETH, SOL futures
- Microsoft RD-Agent(Q) (arXiv:2505.15155, 12.7k★) — production-grade
  factor+model co-optimisation; 2× annualised return; 70% fewer redundant
  factors; <$10 per mining run.
- AlphaAgent (arXiv:2502.16789) — adds AST-novelty and complexity
  regularisers; +11% excess return, IR 1.5; decay-resistant.

The arXiv:2604.26747 design is the one we implement. RD-Agent and
AlphaAgent ideas (novelty regulariser, factor pruning) are folded in
where they're cheap. RD-Agent as a full outer loop is deferred to F58.

## Why the bot needs this

F25 Genetic Algorithm currently mines numeric parameter tweaks of *existing*
strategies. F36 Strategy Research Engine generates strategy variations but
operates at a coarse level (entry rules, exit rules). Neither generates
*novel formulaic factors*. F54 closes that gap — LLM proposes new DSL
expressions over OHLCV / microstructure / on-chain that have never been
tried before, then the evaluator gates them through Sharpe + IC + decay
filters before promoting to F8 router.

## LLM backend

**Decision: Ollama local with `qwen2.5-coder:7b`** (user confirmed
2026-05-28 — see memory [[local-llm-choice]]). Truly unlimited, no API key,
no ToS issues. Quality vs `mistral:7b`: 4-6× better at code synthesis
(EvalPlus, HumanEval, MBPP benchmarks).

Validator step uses `deepseek-r1:8b` for reasoning-class formula scoring
(loaded via the same `chat_ollama(prompt, model="deepseek-r1:8b")` path).

If user later wants cloud burst: enable `LLM_DSL_CLOUD_BURST=1` env →
falls through to Cerebras free tier (1M tok/day, Llama 3.3 70B). Wrapper
is OpenAI-compatible so the swap is a 5-line change.

## DSL grammar

Implementation of the arXiv:2604.26747 grammar with crypto-specific terminal
extensions.

```
Expr   := Op(Expr, Expr) | Op(Expr, Const) | Op(Expr, Window) | Terminal
Op     := + | - | * | / | rank | ts_mean | ts_std | ts_min | ts_max
        | ts_corr | ts_cov | ts_argmax | ts_argmin | ts_rank | ts_zscore
        | ts_skew | ts_kurt | delta | log | abs | sign | power
        | ts_decay_linear | scale | clip | where_gt | where_lt
Window := 5 | 10 | 20 | 30 | 60 | 120 | 240 | 480 | 720 | 1440
Const  := -2.0 .. 2.0  (sampled at 0.1 grid)
Terminal := open | high | low | close | volume | vwap
          | bid_ask_imbalance | ofi | vpin | kyles_lambda
          | funding_rate | open_interest | exchange_netflow_z
          | turbulence_index
```

15 terminals (vs the paper's 5 — we add crypto-microstructure terminals
because the bot already produces them), 24 ops, 10 window sizes, 41 const
levels. Grammar depth bounded at 6 (paper's recommendation; deeper trees
have collapsing IC due to overfit).

Theoretical search space: ~10^15. The LLM acts as a learned *prior*
sampling sub-space the genetic AlphaForge / pure-RL approaches can't reach.

### Files

- `ml/dsl_grammar.py` — grammar nodes, AST serialise/deserialise,
  pretty-print, validation (no division by zero terminals, window <= history,
  depth <= 6).
- `ml/dsl_evaluator.py` — point-in-time numpy evaluator. Reads
  `{pair}:1m:candles` + microstructure feed; computes the expression along
  the rolling window with no lookahead.
- `ml/llm_alpha_dsl.py` — main miner. Prompts qwen2.5-coder with the
  grammar + current top-10 factors as exemplars + the *anti-exemplar* list
  (factors we've already mined and rejected) + the F54 history; expects
  JSON with `{expr, hypothesis, expected_horizon_min}`.
- `ml/dsl_promoter.py` — Sharpe + IC + decay filter; promotes survivors
  to F8 router as a new strategy variant.

### Miner loop (weekly)

```
1. Load 100 most-recent active pairs' 30d 1m candles into memory.
2. Build the LLM prompt:
   - grammar BNF
   - 10 strongest current factors (from F53 + F54 already-promoted)
   - 20 most-recently-rejected proposals (anti-exemplars to discourage)
   - performance feedback: "factor X had IC=0.04 first week, 0.005 last week — decayed"
3. Sample 50 candidate formulas from qwen2.5-coder (temp=0.9 for diversity).
4. Parse each into DSL AST. Reject malformed (~10-20% expected).
5. For each valid candidate:
   - Evaluate point-in-time over 30d on 100 pairs.
   - Compute IC vs 1h-forward return.
   - Compute Sharpe of a long-top-decile / short-bottom-decile portfolio (5bps cost).
   - Compute Decay-Ratio: IC over first 10d / IC over last 10d.
6. Validator pass (deepseek-r1:8b): for each survivor with Sharpe>1.0,
   ask "is this likely overfit to BTC alone, or robust?" — reject obvious
   overfits (rare; ~5% of survivors).
7. Survivors with Sharpe>1.0, IC>0.03, Decay-Ratio in [0.5, 2.0] → promote.
8. Promote = (a) register in feature_governance as F54_<hash>;
              (b) push to F8 strategy router as a new variant;
              (c) write to `dsl:promoted_factors` Redis list.
9. Audit log of every step → DB `experiments` table.
```

### Promotion contract with F8 router

F8 router has a fixed schema for strategy variants. New F54 factors are
wrapped as `LongShortDecileStrategy(factor_id=F54_<hash>)` — a simple
long-top-decile, short-bottom-decile sleeve sized in proportion to the
factor's Sharpe. F8 then schedules the sleeve into the live ensemble.

### Redis keys

```
DSL_PROMOTED_FACTORS    = "dsl:promoted_factors"      # JSON list[dict]
DSL_REJECTED_FACTORS    = "dsl:rejected_factors"      # JSON list[dict] (capped 1000)
DSL_LAST_MINING_RUN     = "dsl:last_mining_run"       # str (epoch)
DSL_MINING_HEALTH       = "dsl:mining_health"         # JSON: {accepted, rejected, errors}
DSL_DISABLED            = "dsl:disabled"              # str ("1" = kill switch)
{pair}:dsl_alpha:{hash} = "{pair}:dsl_alpha:<hash>"   # str (float) factor value
```

### Celery beat

```python
"llm_dsl_alpha_mining": {
    "task": "ml.llm_alpha_dsl.run_mining",
    "schedule": 604800.0,   # weekly (7 days × 86400)
},
"llm_dsl_alpha_compute": {
    "task": "ml.llm_alpha_dsl.compute_promoted_for_active_pairs",
    "schedule": 60.0,
},
"llm_dsl_alpha_decay_check": {
    "task": "ml.dsl_promoter.check_decay_and_demote",
    "schedule": 86400.0,    # daily
},
```

### F30 governance

```python
register("F54", "LLM-DSL Crypto Alpha Miner", activation_phase=0)
# Each promoted factor gets its own sub-id: F54_<8-char-hash>
```

### Config

```yaml
llm_dsl_alpha:
  enabled: true
  ollama_proposer_model: "qwen2.5-coder:7b"
  ollama_validator_model: "deepseek-r1:8b"
  candidates_per_run: 50
  mining_interval_s: 604800
  promote_thresholds:
    min_sharpe: 1.0
    min_ic: 0.03
    decay_ratio_lo: 0.5
    decay_ratio_hi: 2.0
  max_promoted_factors: 30
  history_lookback_pairs: 100
  history_lookback_days: 30
  cloud_burst_enabled: false  # set true + env var if user later wants Cerebras
```

## Implementation checklist

- [ ] `ml/dsl_grammar.py` — grammar + AST + validation
- [ ] `ml/dsl_evaluator.py` — point-in-time numpy evaluator
- [ ] `ml/llm_alpha_dsl.py` — miner loop + Ollama prompts
- [ ] `ml/dsl_promoter.py` — Sharpe/IC/decay gates + F8 wiring
- [ ] `redis_keys.py` — dsl: namespace
- [ ] `signals/engine.py` — promoted-factors composite contribution (dsl_bonus ±15)
- [ ] `feature_governance/bootstrap.py` — F54 entry
- [ ] `celery_app.py` — three beat tasks
- [ ] `config.yaml` — llm_dsl_alpha block
- [ ] `Dockerfile` — `ollama pull qwen2.5-coder:7b` and `ollama pull deepseek-r1:8b` in entrypoint
- [ ] DB migration — `experiments` table column for `dsl_factor_hash`

## Cold-start behaviour

First mining run takes ~45-90 min on the VPS (50 candidates × ~30s each
on CPU). Until first promote, `dsl_bonus = 0` everywhere. After first
mining run, promoted factors compute every minute via the celery beat
task; signal contribution emerges immediately.

If qwen2.5-coder model isn't pulled yet at first run → the miner logs
`ollama_model_not_found` and short-circuits. No regression. Pretrainer
step is responsible for ensuring both models are pulled before bot starts.

## Honest scope (Rule 4)

- Mining runs **weekly**, not nightly. Nightly would generate too many
  redundant proposals for the same week's market state.
- AlphaAgent's full AST-novelty regulariser (arXiv:2502.16789) — only the
  cheap part is implemented: anti-exemplar list in the LLM prompt. Full
  AST-distance pruning across ALL prior factors deferred to a sub-task
  (would benefit from a dedicated F60 entry).
- RD-Agent(Q) full hypothesis-test-loop (arXiv:2505.15155) deferred to F58.
- Cloud burst path (Cerebras / OpenRouter fallback) is **scaffolded but
  not enabled**. Set `LLM_DSL_CLOUD_BURST=1` env var to flip on later.
- Multi-asset cross-section IC computation is implemented (long-top-decile
  / short-bottom-decile) BUT it doesn't yet account for funding-rate cost
  for short legs. Approximation: assume avg funding 0.01%/8h = ~0.1% per
  10d holding period. Real funding-aware backtest deferred.

## Session handoff

- Last mining run — `redis-cli get dsl:last_mining_run`
- Promoted count — `redis-cli get dsl:promoted_factors | jq length`
- Mining health — `redis-cli get dsl:mining_health`
- Per-pair sample — `redis-cli keys "BTCUSDT:dsl_alpha:*"`
- Ollama models pulled — `docker exec ollama ollama list | grep -E "qwen2.5-coder|deepseek-r1"`
- Governance contribution — `redis-cli get feature:F54:contribution`

File cleared after Rule-2 verification post-implementation.
