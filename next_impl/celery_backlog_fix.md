# Celery Backlog — find & fix all the bottleneck-blockers (cont. 74)

Topic slug: `celery_backlog_fix`
Opened: 2026-06-07 (cont. 74). Follows the Priority-2 finding that scheduled
producers are starved by a systemic queue backlog (netflow ~29h stale).

## Diagnosis (Rule 2/9/13 — measured live, not assumed)
Queue depths: default=10931, predict_all=7428, microstructure=6436, candlenet=610;
cn_train/airllm/llm/celery/web_intel=0.

default histogram (3000 sample): ~15 idempotent producer tasks each ~159 copies
(filtered_obi 318=2×/min, capture_pattern_embeddings, qlib, kalman, hawkes,
mm_hawkes, bocpd, coinglass, cvd, detect_drift, llm_dsl, premium_index,
conformal_residual, candle_online_train) + interpret_and_store ×100.
predict_all: launch_pad_maintain 789 + prediction_refresh 526.
microstructure: microstructure_scan 1193 + shadow_ablation 299.

ROOT CAUSES (all confirmed):
1. **Arrival > drain.** ~15 producers fire every 30–60s into default's 4 slots;
   candlenet worker has only **2** slots for **3** queues holding 14k backlog.
2. **No `expires`.** Idempotent "refresh-latest" producers accumulate one stale
   copy per missed cycle → unbounded pileup. Worker is alive (active: coinglass×3,
   capture, launch_pad×2) — it's OUTRUN, not dead.
3. **Abusive cadence.** coinglass_liq_refresh = 146 openInterestHist API calls
   EVERY 60s (heavy; eats slots). ~15 producers all at 60s.
4. **Orphaned `llm` queue.** No worker consumes `llm` (workers cover default,airllm,
   celery,web_intel / candlenet,predict_all,microstructure / cn_train). Minor now
   (llm depth=0; interpret_and_store lands in default instead), noted not load-bearing.

## Fixes (durable + one-time reset)
1. **Global `expires` auto-injection** after `app.conf.beat_schedule` (celery_app.py):
   every periodic entry gets `options.expires = clamp(2.5×interval, 45s, 3600s)`.
   A 60s producer expires at 150s → an over-subscribed worker drops stale copies
   instead of hoarding. Self-healing; caps EVERY queue regardless of load. Daily/6h
   jobs clamp to 3600s (still consumed, never pile 100×). THE structural fix.
2. **coinglass_liq_refresh cadence 60s → 300s** (146 API calls every 5 min, not 60s).
3. **Consumer concurrency bump (10 vCPU headroom):** default worker 4→6,
   candlenet worker 2→4. Mostly I/O-bound (API/redis) so processes overlap on waits.
4. **One-time purge** of default / predict_all / microstructure (idempotent refresh
   producers — next beat re-enqueues fresh; stale copies are pure waste). Destructive
   but safe in paper mode; accepts loss of stale interpret_and_store/shadow copies.

## Ollama (user ask — "work freely on 32GB")
No hard mem_limit (kept — a cgroup ceiling OOM-kills mid-inference, per existing note).
- `OLLAMA_MAX_LOADED_MODELS` 3→4 (more models resident → less LRU eviction/reload).
- `OLLAMA_KEEP_ALIVE` 5m→30m (kills the ~18s cold-start reload thrash between
  decide()/embed/research calls).
Model rec: LLM heavy-lift is already cloud-primary (Groq/Cerebras/SambaNova Llama-3.3
70B); local Ollama is fallback + embeddings + fast decide(). CPU-only (7 cores) →
bigger local models = slower. Optional quality bump for non-latency tasks:
`qwen2.5:14b-instruct` (~9GB). NOT recommending a 32B+ local model on CPU.

## Verify (Rule 4/12)
- Post-deploy: default/predict_all/microstructure depths fall and STAY low (<~500).
- netflow:updated_at refreshes (was 29h stale) once default drains.
- No important low-frequency task dropped (expires clamped ≥ its own cadence).

## Deploy
celery_app.py + docker-compose.yml bind-mounted → recreate celery_worker,
celery_worker_candlenet, ollama + restart celery_beat. No image rebuild. Paper mode.

## STATUS — DONE + VERIFIED (cont. 74)
RESOLVED: all queues 11k/7k/6k/610 → 0, FLAT over 90s. The decisive fix was
launch_pad_maintain (measured 464-622s runtime, scheduled every 20s → recreated at
300s) on top of the global `expires` (13 stale tasks dropped, confirmed) + purge +
concurrency bump (4→6, 2→4) + coinglass 60→300. Forecast/cvd/ofi keys now fresh.
DEPLOY GOTCHA: single-file mounts need `up -d --force-recreate` (atomic-rename → new
inode; `restart` keeps the stale one). Correction: netflow staleness = disabled
(Priority 1), NOT backlog. FOUND (separate): update_pattern_registry_task NameError
('_log' undefined) — pattern registry never updates; fix next.
