# Next Impl — F49 Autonomous Self-Training Orchestrator
_Created: 2026-05-25 | Status: ACTIVE_

---

## Research & References

- [Autonomous ML Pipelines (2026 outlook)](https://wetranscloud.com/blog/autonomous-ml-pipelines-2026) — Closed-loop systems detect drift, evaluate retraining cost, retrain, validate, deploy. Humans review policies and exceptions, not individual runs.
- [Self-Healing ML Systems — Auto-Retraining (Medium)](https://medium.com/@shahmiahamed0519/building-self-healing-ml-systems-auto-retraining-workflows-and-production-deployment-patterns-3d233a51e722) — Best-practice retrain triggers: scheduled jobs + new labeled data + performance degradation + data distribution shifts. Validation gates: benchmark vs production model, fairness/latency checks.
- [Data Drift Detection Techniques 2026](https://labelyourdata.com/articles/machine-learning/data-drift) — Statistical tests: Kolmogorov-Smirnov, Population Stability Index (PSI), Maximum Mean Discrepancy (MMD), Kullback-Leibler divergence. PSI > 0.25 = "significant shift, retrain candidate." KS p < 0.01 = "high-confidence drift signal."
- [Architect a Self-improving ML System (314e)](https://www.314e.com/engineering-hub/how-to-architect-a-self-improving-ml-system-with-automated-model-retraining/) — Continuous Training (CT) extends CI/CD with automatic retraining loops. Orchestrator detects drift, triggers retraining, validates, deploys atomically.
- [Continual Learning + EWC (arXiv:2507.10485)](https://arxiv.org/pdf/2507.10485) — Overcoming catastrophic forgetting. EWC reduced forgetting from 12.62% to 6.85% on knowledge graph link prediction (NeurIPS 2025 workshop) — a 45.7% reduction over naive sequential training.
- [Zylos — Continual Learning 2026](https://zylos.ai/research/2026-04-09-continual-learning-catastrophic-forgetting-ai-agents) — At weight-level: EWC + orthogonal subspace learning. Parameter-efficient adapters (LoRA) for new capabilities. Replay-based methods (rehearse past experiences) counteract forgetting.
- [AutoML NAS + HPO (arXiv:1908.00709)](https://arxiv.org/pdf/1908.00709) — Survey: AutoKeras uses Bayesian Optimization with neural network kernel; Auto-PyTorch combines Bayesian Optimization + Hyperband for HPO.
- [Meta's Production AutoML (arXiv:2311.07870)](https://arxiv.org/pdf/2311.07870) — Production AutoML must beat human baselines with ~100 model-evaluation trials. Sampling-based search is the practical approach for large models.
- [Active Learning Guide 2025 (Encord)](https://encord.com/blog/active-learning-machine-learning-guide/) — Uncertainty sampling, diversity sampling, query-by-committee. Choose by data nature + learning task.
- [Adaptive Active Learning via RL (arXiv:2603.10435)](https://arxiv.org/pdf/2603.10435) — RL policy decides which samples to label next based on regression model state.
- [Risk-Curiosity-Driven RL Trading (Elsevier 2020+)](https://www.sciencedirect.com/science/article/abs/pii/S0957417420311970) — PPO trading agents use risk-curiosity as intrinsic reward, adjust action choice against state uncertainty.

---

## Feature Description — F49 Autonomous Self-Training Orchestrator

**Goal:** Make every ML / RL / NN model in the bot retrain itself automatically — no manual triggers, no weekly cron-only — driven by drift detection, performance monitoring, and a meta-orchestrator that decides what to train when.

**Existing autonomous infrastructure (already in the bot):**
- F17 Continual Learning EWC (governance ID registered, used in retrain pipelines)
- F22 Meta-RL MAML (Phase 4 — regime adaptation)
- F25 Genetic Algorithm (Phase 1 — strategy params)
- F26 BOCPD (online changepoint detection on price data)
- F34 World Model (learns from each closed trade)
- F38 Curiosity Engine (intrinsic motivation)
- F41 Self-Play vs MarS (adversarial training)
- Celery beat: weekly retrains for HMM, GNN, MARL Day/Minute, CandleNet 1m/5m/15m, Entry Timing Agent
- Direction Model retrains every 50 trades (F13)

**Gaps that F49 fills:**
1. **No drift-triggered retraining** — all retrains are time-based. Regime changes happen between cron runs and models go stale.
2. **No per-model performance monitor** — no automatic detection of "this model's predictive AUC dropped from 0.62 to 0.51."
3. **No hyperparameter auto-tuning** — every retrain uses hardcoded `lr=1e-3, dropout=0.3, batch_size=256`.
4. **No model version rollback** — a bad retrain replaces the .pth file with no way back.
5. **No orchestrator** — multiple models can theoretically retrain simultaneously, thrashing CPU; or nothing trains for days even when drift is severe.
6. **No active learning** — high-uncertainty trades are not prioritized in training data; the bot keeps the same statistics blind-spots.
7. **No continuous online learning for D-Model / World Model** — they retrain in batches, not per-trade.

---

## Component Design

### Component 1 — Drift Detector (`ml/drift_detector.py`)
- **Inputs per model**: training-time feature distributions (saved as JSON next to .pth), live production feature samples (rolling window in Redis)
- **Statistics**: PSI per feature, KS-test p-value per feature, MMD over the joint distribution
- **Trigger**: PSI > 0.25 on any input feature OR KS p < 0.01 on direction-correlated features → write `model:{name}:drift_detected_at = now` to Redis
- **Cadence**: every 60s, runs as a Celery beat task `detect_drift_all`

### Component 2 — Performance Monitor (`ml/performance_monitor.py`)
- **For each model with directional predictions** (CandleNet 1m/5m/15m, Direction Model, TFT, PatchTST):
  - Track rolling 100-trade prediction-vs-actual on closed trades
  - Compute rolling AUC + Top-Decile-Lift
  - Compare to peak (best-ever rolling AUC)
- **Trigger**: rolling_auc < peak_auc × 0.90 → `model:{name}:retrain_needed = "1"`
- **Redis keys**: `model:{name}:rolling_auc`, `model:{name}:peak_auc`, `model:{name}:perf_degraded_at`
- **Cadence**: every 5 min, Celery beat task `performance_check_all`

### Component 3 — Auto HPO Engine (`ml/auto_hpo.py`)
- **Algorithm**: Bayesian Optimization over `{lr ∈ [1e-4, 1e-2], dropout ∈ [0.1, 0.5], batch_size ∈ {64,128,256,512}, weight_decay ∈ [1e-6, 1e-3]}`
- **Budget**: 10 trials per retrain (cheap — total ~3x cost of single train)
- **Persistence**: per-model best params cached in Redis at `model:{name}:hpo_best_params`
- **Library**: use `optuna` (lightweight, pure Python, dropdown-replaceable for production)
- **Integration**: every model's `train()` accepts `hpo_params` kwarg; HPO engine spins up 10 short-budget trial trainers, picks best by val_auc, then does one full-budget train with winning params

### Component 4 — Active Learning Sampler (`ml/active_learning.py`)
- **Strategy**: query-by-prediction-entropy on closed trades
- **Workflow at retrain**:
  1. Compute model's prediction entropy on every closed trade in the last N trades
  2. Top 20% highest-entropy = "uncertain" pool
  3. Over-sample these by 3× in the training set
- **Why**: the bot's biggest knowledge gaps are exactly where its model is most uncertain; biasing training toward these examples accelerates learning

### Component 5 — Model Version Manager (`ml/model_versions.py`)
- **Atomic save**: `train()` writes to `candlenet_1m.v{utc_iso}.pth` then atomically symlinks `candlenet_1m.pth → that file`
- **Retention**: keep last 3 versions; delete older
- **Auto-rollback**: post-train, if validation gates fail OR perf-monitor flags the new model as worse than the previous within 50 trades, automatically symlink back to previous version
- **API**: `rollback(model_name, n_steps_back=1)` available from Celery, dashboard, and brain
- **Redis**: `model:{name}:active_version`, `model:{name}:rollback_count`

### Component 6 — Meta-Orchestrator (`ml/training_orchestrator.py`)
- **Cadence**: every 5 min, Celery beat task `orchestrator_tick`
- **Decision logic** (priority-ordered):
  1. Did any model have its weekly cron miss? → train that one
  2. Any model with `drift_detected_at` AND `last_trained_at < drift_detected_at` (drift since last train)? → train that one (highest priority)
  3. Any model with `retrain_needed = "1"` from perf monitor? → train that one
  4. Otherwise: nothing to do this tick
- **Resource gate**: max 1 model training at a time (CPU-bound); skip if any retrain Celery task is already in flight
- **Audit log**: every decision written to Redis `orchestrator:decisions` (LPUSH + LTRIM to 100)

### Component 7 — Continuous Online Learner (extends F17 EWC)
- **Targets**: Direction Model, World Model (already retrain on every N closed trades; bump to per-trade incremental update)
- **EWC integration**: Fisher Information Matrix computed once after each full retrain; subsequent per-trade updates use EWC quadratic penalty so old knowledge is preserved
- **Replay buffer**: last 500 closed trades cached in Redis (`memrl:replay_buffer`); each incremental update samples 32 trades from buffer + the new closed trade
- **Frequency**: triggered by `CH_TRADE_CLOSED` Redis pub/sub channel — purely event-driven, no polling

### Component 8 — Dashboard Panel "Autonomous Training"
- **Per-model row**: name, last_trained_at, rolling_auc trend (mini-sparkline), drift_score (PSI), retrain_needed flag, active_version timestamp
- **Recent orchestrator decisions**: last 10 entries with timestamp + decision rationale
- **HPO trials**: any trial currently running, the param space explored, best trial so far
- **Manual override**: per-model "force retrain now" button (calls Celery task directly)

---

## Discussion

### Confirmed
- [CONFIRMED] Goal: every ML/RL/NN model self-trains automatically — user stated "built all 8"
- [CONFIRMED] All 8 components approved by user — building in one pass
- [CONFIRMED] Component 1 — Drift Detector with PSI + KS-test
- [CONFIRMED] Component 2 — Performance Monitor (rolling AUC vs peak)
- [CONFIRMED] Component 3 — Auto HPO via Optuna Bayesian Optimisation
- [CONFIRMED] Component 4 — Active Learning by prediction entropy
- [CONFIRMED] Component 5 — Model Version Manager + auto-rollback
- [CONFIRMED] Component 6 — Meta-Orchestrator (Celery beat every 5 min)
- [CONFIRMED] Component 7 — Continuous Online Learner (EWC + replay buffer)
- [CONFIRMED] Component 8 — "Autonomous Training" dashboard panel

### Ruled Out (so far)
- AutoML Neural Architecture Search (NAS) — too compute-heavy for VPS (16 GB RAM, 4 CPU). Stick with HPO over fixed architectures.
- LoRA adapters — overkill for pre-CandleNet sized models; reconsider only if model count and size grow.
- Reinforcement-learning-driven active learning — Component 4 starts simple with entropy-based query; RL active-learning is a follow-up if the simple approach plateaus.

---

## Implementation Checklist

### Foundational + new files
- [x] `ml/model_versions.py` — atomic versioned saves + rollback + retention=3 — confirmed
- [x] `ml/drift_detector.py` — PSI + KS, `detect_drift_all()` — confirmed
- [x] `ml/performance_monitor.py` — rolling AUC, log_outcome, clear_retrain_flag — confirmed
- [x] `ml/auto_hpo.py` — Optuna TPE, 10 trials, cached best params — confirmed
- [x] `ml/active_learning.py` — entropy uncertainty + oversampling — confirmed
- [x] `ml/online_learner.py` — CH_TRADE_CLOSED subscriber + replay buffer — confirmed
- [x] `ml/training_orchestrator.py` — orchestrator_tick + mark_training_done — confirmed

### Wiring
- [x] `ml/candlenet.py` — save_versioned + snapshot_training_distribution + record_live_sample + lazy GAF Dataset (OOM fix) — confirmed
- [x] `signals/engine.py` — log per-TF CandleNet predictions to `trade:{id}:pred_*` — confirmed
- [x] `execution/paper.py` + `execution/live.py` — log_outcome on close + pred Redis cleanup — confirmed
- [x] `celery_app.py` — 3 new beat schedules (detect-drift-all, performance-check-all, orchestrator-tick) + 3 new tasks + `_candlenet_retrain_with_orchestrator()` shared body — confirmed
- [x] `feature_governance/bootstrap.py` — F49 registered — confirmed
- [x] `requirements.txt` — `optuna` added — confirmed
- [x] `main.py` — `start_online_learner()` added to `asyncio.gather` — confirmed
- [x] `dashboard/api.py` — `/autonomous_training` endpoint — confirmed
- [x] `BOT_BLUEPRINT.md` — Feature 49 section with 8 components + changelog entry — confirmed
- [x] `PROGRESS.md` — cont. 47 entry — confirmed

### Pending (intentionally deferred, documented in PROGRESS.md as Watch List)
- [x] Wire HPO into `candlenet.train()` via `hpo_params` kwarg — 2026-05-26 cont. 48. `train()` now accepts `hpo_params: dict | None` and `enable_hpo: bool`. When `enable_hpo=True`, calls `auto_hpo.search_best_params` with a fast 5K-sample × 3-epoch objective (`_hpo_fast_eval`) over (lr, dropout, batch_size, weight_decay). `CandleNetModel(dropout=…)` constructor parameter added so dropout becomes searchable. Celery-triggered retrains check `brain:f49_hpo_enabled` Redis flag (default off so weekly cron stays predictable).
- [x] Auto-rollback trigger when perf monitor flags newly deployed model — 2026-05-26 cont. 48. `auto_rollback_if_perf_degraded()` added to `performance_monitor.py`, called from `compute_rolling_auc` when `rolling_auc < peak_auc * 0.85` and `n ≥ 50`. Gates: active version must be < 48h old, ≥ 2 versions on disk, no rollback in last 24h (cooldown). On rollback: `model_versions.rollback(n_steps=1)`, clear `retrain_needed`, reset `peak_auc` anchor, audit to `orchestrator:decisions`. Counter `model:{name}:auto_rollback_count`.
- [x] `direction_model.online_update()` — 2026-05-26 cont. 48. Sidecar `SGDClassifier(loss="log_loss", learning_rate="constant", eta0=0.01, alpha=1e-4)` at `models/direction_model_online.pkl`. Uses the SAME scaler + feature layout as the canonical model. First call seeds `classes=[0,1]`; subsequent calls `partial_fit` one sample. Thread-safe via `_online_lock`. `reset_online_sidecar()` called from `train_from_closed_trades` success path to drop stale online weights after a fresh batched retrain. EWC λ ignored (sklearn SGD doesn't expose Fisher penalty; L2 alpha is the analogous soft anchor).

### Docker rebuild + retrain
- [ ] Rebuild brain / celery_worker / data_feed / dashboard / pretrainer
- [ ] Recreate containers
- [ ] Re-run pretrainer (now OOM-safe with lazy GAF)
- [ ] Verify Celery beat fires detect-drift-all + performance-check-all + orchestrator-tick

## Session Handoff
_Last updated: 2026-05-26 cont. 48_
**Done this session (cont. 48):** All 3 deferred Watch List items closed — HPO wired into candlenet.train(), auto-rollback wired into performance_monitor, direction_model.online_update() built as SGDClassifier partial_fit sidecar. F49 is now fully implemented end-to-end.
**Next step:** Rebuild + recreate brain / celery_worker / celery_beat / dashboard (rebuild in progress, task bh5b2zkj0). After recreate, verify: `model:direction_model:online_updates` counter increments on the next CH_TRADE_CLOSED message; `model:candlenet_*:hpo_best_params` populates if HPO is enabled via `redis-cli SET brain:f49_hpo_enabled 1`; `model:*:auto_rollback_count` stays at 0 until a real degrade event.
**Blockers:** None.
**Pretrainer:** Currently mid-train on the old image (CandleNet 1m, epoch 10/25, val_loss plateauing at ~17.3 — separate concern, tracked outside this file). cont. 48 changes are backward-compatible — when this run completes the .pth files will load with the new code.
**Containers to rebuild:** brain, celery_worker, celery_beat, dashboard. **Do NOT** rebuild pretrainer mid-run.
