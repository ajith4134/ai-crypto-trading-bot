# F59 — RD-Agent(Q) Outer-Loop Strategy Generator

**Status:** Tier A — DEFERRED (design only this session, cont. 55).
**Research:** Microsoft RD-Agent(Q), github.com/microsoft/RD-Agent (12.7k★),
arXiv:2505.15155 — 2× annualised return, 70% fewer factors, <$10/run cost.
Co-optimises factor *and* model selection in a hypothesis-test loop.

## Why deferred

RD-Agent is a meta-system above the F54 LLM-DSL miner. F54 mines factors;
RD-Agent decides which models consume them. Implementing RD-Agent before
F54 has a corpus of mined factors is premature. Order: F54 first (this
session), F59 after ≥30 days of F54 data.

## Why high-value

F36 Strategy Research Engine currently runs a fixed Bayesian-opt over a
hardcoded strategy space. RD-Agent replaces that with an LLM-driven
hypothesis cycle:

1. **Hypothesis**: "factors from F54 family-A perform best in turbulent regime"
2. **Experiment design**: LLM proposes train/test split, baseline, metric
3. **Code synthesis**: LLM writes the eval script
4. **Execution**: bot runs it, returns numbers
5. **Reflection**: LLM updates the hypothesis prior

The +2× headline number is on equities; expect ~30% degradation on crypto
perpetuals (volatility + 24/7 market). Conservative target: +50% annualised
boost from strategy-routing improvements alone.

## Design sketch

- `meta/rd_agent.py` — port of the public RD-Agent loop. Replace the
  OpenAI client with `llm/ollama_client.py` (qwen2.5-coder + deepseek-r1).
- Hypothesis database in PostgreSQL `experiments` table.
- Daily cron: 1 hypothesis cycle per day.
- Outputs: F8 router gets new strategy-variant *weights*, not new code
  variants. F54 is the code generator; F59 is the curator.

## Open questions

- Do we run RD-Agent fully local (Ollama) or allow Cerebras burst? Local
  is slower but matches the F54 decision pattern.
- Hypothesis-cycle frequency — daily is the paper's number, but on a
  weekly-rebalance bot weekly may suffice.

## Session handoff

Not implemented yet. Re-open after F54 has 30+ days of mining data.
