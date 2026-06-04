# Next Implementation — Debate Council Advanced Upgrades (cont. 69)

Topic slug: `debate_council_upgrade`
Opened: 2026-06-02 (cont. 69), after fixing the predict-all-gate zero-trades incident.

## Why
User asked: review F37 debate council + add advanced features. User selected ALL four:
1. Ground prompts with real data + RAG
2. Deterministic fallback scorer
3. Single batched LLM call (3 personas, 1 JSON)
4. Confidence calibration (isotonic/Platt)
Plus: "why is ollama always degraded?"

## CONFIRMED findings (Rule 2 — read from source/live)
- **Ollama degradation root cause:** CPU-only VM (no GPU). Container was capped
  `cpus:3.0` with `OLLAMA_NUM_PARALLEL=3` + `OLLAMA_NUM_THREADS=3`. The debate fired
  3 concurrent `decide()` calls → 3 inferences split 3 cores ≈ 1 core each → phi3 blew
  the 60s timeout → "degraded".
- **FIXED (config, applied + live):** docker-compose ollama → `cpus:5.0`,
  `NUM_PARALLEL=2`, `NUM_THREADS=5`. Container recreated.
- **BUT phi3 is throughput-bound:** single WARM 150-token JSON call = ~47s wall
  (~3.4 tok/s) even with 5 threads. So a naive batched call (~3× tokens) would be
  ~120s → unusable. **Design implication: the debate decide() must prefer FAST CLOUD
  (Groq ~0.5s per decision.py comment) and use local phi3 only as last resort.**
- `decide()` (llm/decision.py:86) currently tries **Ollama first, cloud fallback** —
  backwards for the latency-critical debate. Cloud chain = Groq→Cerebras→SambaNova→
  nvidia→mistral; rate-limited (429/cooldown) but mistral/nvidia partially serve.
- Council fail-open (cont. 68b ≥2/3 agents fail → full_allocation) IS baked in the
  running image (verified count match disk).

## Building blocks (verified present)
- `signal["feature_vector"]` = JSON string, 32 cols (prediction/features.py
  FEATURE_COLUMNS): ofi, vpin, bid_ask_imbalance, vol_unit, atr_norm, sentiment,
  regime_bull/bear/turbulent, xsmom_rank, xsmom_return_7d, exchange_netflow_z,
  liq_nearest_above/below_pct, liq_cascade_prob, funding_rate, change_24h,
  signal_strength, trade_potential, pattern_cluster_id, cn_{1m,5m,15m,1h}_* candlenet.
  → GROUNDING source, already on every signal. No new compute.
- `metacognition/postmortem_embed.py`: `build_signal_context(pair,direction,...)`,
  `embed_text()` (nomic-embed 768d), `to_pgvector()`. Need a pgvector similarity
  SELECT against the postmortem table for top-k similar past outcomes (cont. 64 RAG).
- `llm/decision.py: decide(prompt, timeout)`; `llm/providers.py: call_chain(...)`,
  `extract_json_dict(text)`.
- Public council API to PRESERVE: `run_debate(signal, market, capital_pct, balance)`,
  `save_debate_arguments`, `update_beliefs_on_close`, `get_agent_weights`,
  result keys: verdict,size_pct,rounds_used,rounds,weights,bull,bear,risk,llm_available.

## CHOSEN ARCHITECTURE (user picked Option 1, 2026-06-02)
**Deterministic gate (synchronous) + async local LLM debate.**
- HOT PATH: every signal's accept→size verdict comes from the INSTANT deterministic
  scorer (debate/fallback.py, feature_vector-based). The LLM is REMOVED from the
  latency-critical path → it can never again block trades, and phi3's ~3.4 tok/s slowness
  is irrelevant. Matches the user's local-LLM preference (feedback_local_llm_choice).
- BACKGROUND: the batched grounded LLM debate runs ASYNC on LOCAL phi3 (or the fastest
  3B from the benchmark — see below), no timeout pressure. Its job: update agent
  weights/beliefs (verbal reinforcement) + cache a verdict per cluster×regime×dir that
  the deterministic scorer can consult as a prior. Calibration applies to its outputs.
- engine.py:2217-2239 debate block → replaced: call deterministic scorer synchronously;
  enqueue async debate (fire-and-forget celery task) keyed by signal_id.
- Async model: BENCHMARK DONE — phi3:mini=4.2 tok/s, qwen2.5:3b=1.1, llama3.2:3b=0.8.
  phi3 is the FASTEST local model; 3B alts are worse on this CPU. → keep phi3 for async
  debate. Confirms there is no faster-local win → deterministic hot-path gate is correct.

## DESIGN (Rule 5 — blueprint deviation, justified)
Blueprint F37 = 3 agents × up to 3 rounds (≤9 LLM calls) SYNCHRONOUS. On a CPU-only box
this is the direct cause of timeouts AND it gated real-time entries on a slow LLM. Redesign
(user-approved Option 1): deterministic synchronous gate + async local batched debate.

1. **Batched grounded single call** (`debate:batched_mode=1`, default on; off → old path):
   ONE `decide()` prompt that embeds (a) a compact feature_vector digest, (b) regime/
   turbulence, (c) top-k RAG postmortems, and asks for one JSON:
   `{bull:{argue_for,confidence,arguments}, bear:{argue_against,risk_score,arguments},
     risk:{risk_acceptable,recommended_size_pct,concerns}}`.
   Call with `prefer_cloud=True` (new decide arg) so Groq/cloud serves it fast; phi3 last.
2. **Deterministic fallback scorer** (`debate/fallback.py`): pure-Python verdict from the
   feature_vector when the LLM call fails/times out — replaces blind full_allocation with a
   GRADED verdict. Inputs: signal_strength, regime alignment vs direction, vol_unit,
   liq_cascade_prob, funding, candlenet dir agreement. Output verdict ∈
   {full_allocation, reduced_allocation, exploratory, skip_risk} + size_pct. Keeps risk
   gating alive during LLM outages (today's gap).
3. **Confidence calibration** (`debate/calibration.py` + periodic celery fit):
   bucket each agent's stated confidence vs realized was_correct from debate_arguments +
   trade outcomes → reliability map in Redis (`debate:calib:{role}`). `_synthesise` maps raw
   confidence→calibrated before weighting. Phase 1 = simple frequency buckets (no sklearn
   dep); upgrade to isotonic later if sklearn present.
4. **Grounding/RAG helper** (`debate/context.py`): `build_grounding(signal)` →
   feature digest string; `retrieve_postmortems(pair,direction,k)` → top-k via pgvector.

## decide() change
Add `prefer_cloud: bool = False` to `llm/decision.py:decide`. When True, try cloud chain
FIRST, Ollama last. Council batched call passes prefer_cloud=True. All other callers
unchanged (default False).

## Checklist
- [ ] docker-compose ollama cpus/threads/parallel — DONE (applied + recreated)
- [ ] llm/decision.py: add prefer_cloud arg
- [ ] debate/context.py: grounding digest + RAG retrieval
- [ ] debate/fallback.py: deterministic scorer
- [ ] debate/calibration.py: read-side map + apply in _synthesise
- [ ] debate/council.py: batched_mode path in run_debate; keep old path behind flag off
- [ ] celery_app.py: periodic calibration-fit task (predict_all/llm queue)
- [ ] rebuild brain (Rule 3) + recreate; verify a real debate runs <5s via cloud,
      verdict distribution sane, fallback fires on forced LLM-down
- [ ] Rule-2 verify each, then delete this file ("File cleared.")

## Session handoff
Live incident RESOLVED (predict-all gate disarmed, 9 trades open). Ollama config fix
APPLIED + verified (but phi3 still ~3.4 tok/s → cloud-first is mandatory for the debate).
Next: implement items above. Gate must stay `prediction:gate_auto_arm=0` until predictor
retrained (see memory feedback_predict_gate_disabled).
