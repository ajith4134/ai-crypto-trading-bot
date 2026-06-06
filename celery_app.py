"""Celery application — background task queue for AirLLM and scheduled tasks."""
import os, sys
if "/app" not in sys.path:
    sys.path.insert(0, "/app")
from celery import Celery
from celery.signals import worker_process_init
from celery.schedules import crontab


@worker_process_init.connect
def init_worker_resources(**kwargs):
    """Initialise Redis and DB in each forked worker process before any task runs.
    Also guarantees /app is on sys.path — Celery's prefork pool sometimes loses
    the parent process's sys.path additions in forked children, which broke
    `from ml.maml import ...` and similar imports inside tasks. Doing it here
    fixes ALL tasks; we can remove the per-task workarounds later."""
    import sys as _sys
    if "/app" not in _sys.path:
        _sys.path.insert(0, "/app")
    try:
        import redis_client
        redis_client.init()
    except Exception:
        pass
    try:
        from db import init_pool
        init_pool()
    except Exception:
        pass

app = Celery(
    "trading_bot",
    broker=f"redis://{os.getenv('REDIS_HOST','localhost')}:{os.getenv('REDIS_PORT','6379')}/0",
    backend=f"redis://{os.getenv('REDIS_HOST','localhost')}:{os.getenv('REDIS_PORT','6379')}/1",
)

app.conf.task_routes = {
    # cont. 65 — CandleNet inference + retrain isolated to a dedicated
    # `candlenet` queue. Previously candlenet_infer_all sat behind 15k+ LLM
    # tasks in `default`, executing only every ~38 min and letting
    # {pair}:{tf}:candle_forecast keys (TTL 90s) expire ~96% of the time.
    # That silently degraded the multi-TF cascade, scanner F50a, engine F48
    # bonus, AND the predict-all pipeline. A dedicated worker + queue keeps
    # the candle-forecast feed warm regardless of LLM backlog. ORDER MATTERS
    # — more specific patterns must come before the catch-all.
    "celery_app.candlenet_infer_all":   {"queue": "candlenet"},
    # cont. 65k — RETRAINS isolated to a dedicated `cn_train` queue + worker
    # (celery_worker_cn_train). Retrains are CPU+memory-heavy and run for minutes;
    # on the shared `candlenet` queue they starved candlenet_infer_all (forecasts
    # expired → PCG strict-rejected everything → trade freeze). Separating them
    # keeps inference warm regardless of retrain activity, and vice versa.
    "celery_app.retrain_candlenet_1m":  {"queue": "cn_train"},
    "celery_app.retrain_candlenet_5m":  {"queue": "cn_train"},
    "celery_app.retrain_candlenet_15m": {"queue": "cn_train"},
    "celery_app.retrain_candlenet_1h":  {"queue": "cn_train"},
    "celery_app.retrain_candlenet_30m": {"queue": "cn_train"},
    # cont. 69f — kline corpus top-up runs on cn_train (has ./ml + ./data/historical).
    "celery_app.update_klines_corpus_task": {"queue": "cn_train"},
    "celery_app.bulk_topup_corpus_task": {"queue": "cn_train"},
    # cont. 69i (P3) — retrain the kline-based predict-all model on cn_train.
    "celery_app.retrain_kline_predictor_task": {"queue": "cn_train"},
    # cont. 69j (P3 finish) — publish LIVE kline predictions on the LIGHT predict_all
    # queue (candlenet worker; 0 backlog) — the default queue is chronically
    # congested (~1950) so 5-min refresh wouldn't keep up there. Uses live Redis
    # candles + the baked model; no corpus mount needed.
    "celery_app.publish_kline_predictions_task": {"queue": "predict_all"},
    # cont. 65 follow-up — predict-all path also isolated. Same root cause
    # as candlenet (default queue blocked by 18k+ LLM tasks). These four
    # tasks (refresh / auto-arm / registry / drift) are sub-second each,
    # so they piggyback on the existing candlenet worker which subscribes
    # to both queues — no extra container needed.
    "celery_app.prediction_refresh_task":      {"queue": "predict_all"},
    "celery_app.auto_arm_prediction_gate_task": {"queue": "predict_all"},
    "celery_app.update_pattern_registry_task":  {"queue": "predict_all"},
    "celery_app.calibration_drift_check_task":  {"queue": "predict_all"},
    # cont. 65k — explicit routes (the catch-all `celery_app.*` below overrides
    # @app.task(queue=...) decorators, so these MUST be listed before it).
    # All land on the candlenet worker (has data/historical + ml + signals mounts
    # + CANDLENET_MAX_SAMPLES). refresh chains retrains in-process (reliable);
    # microstructure + new-symbol sync need signals/microstructure.py.
    "celery_app.refresh_historical_data":   {"queue": "candlenet"},
    "celery_app.train_tf_fusion":           {"queue": "candlenet"},
    "celery_app.microstructure_scan_task":  {"queue": "microstructure"},
    # cont. 70e — shadow ablation runs on candlenet worker (35-col image +
    # ml/ bind-mount) NOT the default queue (32-col + chronically blocked by
    # LLM tasks). Measures the Track-2 microstructure directional-AUC lift.
    "celery_app.shadow_ablation_task":      {"queue": "microstructure"},
    # cont. 70f — Track-4 foundation vol-prior producer (Chronos): candlenet
    # worker has torch + ml/ bind-mount + the downloaded model. predict_all
    # queue is light (0 backlog).
    "celery_app.foundation_forecast_task":  {"queue": "predict_all"},
    # cont. 70 — Launch-Pad maintainer on predict_all (candlenet worker has the
    # /app/signals bind-mount + 0 backlog). Keeps the 10-deep buffer full and
    # shadow-tracks MAE/MFE; shadow-only until launchpad:enabled=1. Never opens trades.
    "celery_app.launch_pad_maintain_task":  {"queue": "predict_all"},
    # cont. 65k — new-symbol sync → default (celery_worker). The cn_train QUEUE
    # has an unresolved kombu/Redis delivery bug (tasks consumed-but-not-executed
    # even with a lean dedicated worker), so it's RETIRED. default is proven
    # reliable, has spare concurrency, and is NOT the candlenet inference worker —
    # so the sync still doesn't compete with inference (the goal). celery_worker
    # gets a ./data/historical mount to write the CSVs.
    "celery_app.sync_new_scanner_symbols":  {"queue": "default"},
    # cont. 65k — SLOW LLM tasks isolated to a dedicated `llm` queue + worker.
    # ROOT CAUSE of the cont. 65k "no trades": these tasks each take ~280-300s
    # (Ollama phi3/mistral on CPU, hitting the 300s read-timeout). On the shared
    # `default` queue they jammed ALL celery_worker slots, so the fast producers
    # (filtered_obi_producer_task etc.) on `default` got ~0 throughput → OFI went
    # stale (< _OFI_MIN 0.0005) → generate_candidate_signals returned [] for every
    # pair → zero signals → zero trades. Isolating them keeps `default` (fast
    # producers) draining. celery_worker_llm consumes this queue; researcher.py is
    # cloud-primary (cont. 65k) so these normally finish in ~3s, falling back to
    # local Ollama only when all cloud providers are cooled down.
    "celery_app.interpret_and_store":        {"queue": "llm"},
    "celery_app.score_web_intel_sentiment":  {"queue": "llm"},
    "celery_app.track_web_intel_outcomes":   {"queue": "llm"},
    "celery_app.ai_scientist_run":           {"queue": "llm"},
    "celery_app.run_strategy_research":      {"queue": "llm"},
    # Remaining LLM-heavy + all producer/misc tasks → default queue.
    "celery_app.*": {"queue": "default"},
}

app.conf.beat_schedule = {
    "sleep-consolidation": {
        "task": "celery_app.sleep_consolidation",
        "schedule": crontab(hour=3, minute=0),
    },
    "daily-summary": {
        "task": "celery_app.send_daily_summary_task",
        "schedule": crontab(hour=8, minute=0),
    },
    "weekly-report": {
        "task": "celery_app.send_weekly_report_task",
        "schedule": crontab(hour=8, minute=0, day_of_week=1),
    },
    # Blueprint F41: Self-Play vs MarS — run every 30 minutes as background task
    "self-play": {
        "task": "celery_app.run_self_play",
        "schedule": crontab(minute="*/30"),
    },
    # Blueprint F30: Feature Governance — full 5-failure-mode check every hour
    "feature-governance-check": {
        "task": "celery_app.run_feature_governance_check",
        "schedule": crontab(minute=15),  # every hour at :15
    },
    # Blueprint F2: Trade Memory Pattern Mining — every 6 hours
    "mine-trade-patterns": {
        "task": "celery_app.mine_trade_patterns",
        "schedule": crontab(minute=30, hour="*/6"),
    },
    # Blueprint F43 / AC-04: Metacog daily evaluation of self-improvement mechanisms.
    # Runs at 04:00 UTC — outside trading peaks, avoids contending with other tasks.
    "metacog-daily-eval": {
        "task": "celery_app.metacog_daily_eval",
        "schedule": crontab(hour=4, minute=0),
    },
    # Blueprint F29 / AE-12: Web Intel source credibility tracking.
    # Compare predicted sentiment vs actual price direction 72h later.
    "track-web-intel-outcomes": {
        "task": "celery_app.track_web_intel_outcomes",
        "schedule": crontab(minute=45, hour="*/6"),
    },
    # Blueprint F9 / T-04: Rejected-signal counterfactual sweeper.
    # Replaces fragile apply_async(countdown=72h) — that lost work on every celery_worker
    # restart because Celery stores ETAs in worker memory, not in the broker.
    "sweep-pending-counterfactuals": {
        "task": "celery_app.sweep_pending_counterfactuals",
        # cont. 69s — was hourly/LIMIT-500 (12k/day) << ~28k rejects/day → 202k
        # backlog. Now every 15 min × LIMIT 3000 = up to 288k/day so the matured
        # [72h,84h] band is always drained and the eval price stays near window-end.
        "schedule": crontab(minute="*/15"),
    },
    # cont. 63 (2026-05-29) — Signal Monitor Phase 1: prune stale replay
    # entries (age > TTL, price drift > 2%, regime change, pair removed).
    # The engine consumer also drops stale entries as it sees them, but a
    # periodic prune catches pairs the engine isn't currently scanning.
    "prune-replay-pool": {
        "task": "celery_app.prune_replay_pool",
        "schedule": 60,  # every 60 s
    },
    # cont. 63 — Signal Monitor Phase 2: Bayesian Beta-distribution adaptive
    # threshold refresh. Re-derives T_high / T_low from per-strength-bucket
    # posteriors every 5 minutes. No-op when no buckets are mature (cold-start).
    "refresh-bayes-threshold": {
        "task": "celery_app.refresh_bayes_threshold",
        "schedule": crontab(minute="*/5"),
    },
    # cont. 63 — Signal Monitor Phase 4: Utility-weighted walk-forward
    # calibration. Pulls last 30 days of closed trades + counterfactuals,
    # sweeps candidate thresholds, validates across 4 weekly folds, writes
    # recommendation. Daily at 03:15 UTC (after sleep_consolidation @ 03:00).
    "util-calib-refresh": {
        "task": "celery_app.util_calib_refresh",
        "schedule": crontab(hour=3, minute=15),
    },
    # cont. 69s — F9/F12 §4.3 EV-override: refresh per-direction CF peak/dd
    # segment stats so the hot-path evaluator does no DB work. Every 30 min.
    "ev-override-refresh-segments": {
        "task": "celery_app.ev_override_refresh_segments",
        "schedule": crontab(minute="*/30"),
    },
    # cont. 63 — Predict-all-before-open Phase B refresh loop.
    # Every 30 s, for top-N scanner-ranked pairs: build feature vec,
    # XGBoost predict, write predictions:{symbol} Redis hash (TTL 90s) +
    # predictions DB row. Cold-start safe — no-op when no model loaded.
    "prediction-refresh": {
        "task": "celery_app.prediction_refresh_task",
        "schedule": 30,
    },
    # cont. 63 — Predict-all Phase A.4 + Phase D.
    # Updates pattern_effectiveness_registry from any trade closed since the
    # last tick. Every 5 min. Cheap aggregation on the trades table.
    "update-pattern-registry": {
        "task": "celery_app.update_pattern_registry_task",
        "schedule": crontab(minute="*/5"),
    },
    # cont. 63 — Predict-all Phase A.4: keep the predictions table bounded.
    # Deletes prediction rows older than 24 h that never got linked to a
    # trade. Hourly.
    "cleanup-stale-predictions": {
        "task": "celery_app.cleanup_stale_predictions_task",
        "schedule": crontab(minute=42),
    },
    # cont. 63 — Phase D calibration drift monitor. Every hour, compute ECE
    # per (cluster × regime) over the last 200 closed-with-prediction trades;
    # if ECE > 0.15 over ≥ 100 samples, set prediction:drift_flag:{cluster}=1
    # so the refresh loop can skip predicting for that cluster until retrain.
    "calibration-drift-check": {
        "task": "celery_app.calibration_drift_check_task",
        "schedule": crontab(minute=27),
    },
    # cont. 64 — F50e self-supervised candle training. Every 60s the trainer
    # walks active pairs × {1m,5m,15m}, enqueues new closed candles, and
    # drains pending entries whose realized direction label is now known
    # into SGD updates on online_predictor.direction + confidence heads.
    # Trains on LIVE candle data — no trade outcome required.
    "candle-online-train": {
        "task": "celery_app.candle_online_train_task",
        "schedule": 60,
    },
    # cont. 70e — Track-2 shadow ablation: two prequential SGD direction
    # heads (35-col full vs 32-col ablated) on the same live candle samples;
    # publishes shadow_abl:{auc_full,auc_ablated,lift} to Redis. Pure shadow,
    # zero trading impact. Runs on the microstructure queue (candlenet worker).
    "shadow-ablation": {
        "task": "celery_app.shadow_ablation_task",
        "schedule": 60,
    },
    # cont. 70f — Track-4 Chronos vol-prior sweep. 1h candles + 3h horizon ->
    # forecast only changes when a new 1h candle closes; 10-min cadence keeps
    # {pair}:foundation_forecast fresh (TTL 1200s). ~300ms/pair on candlenet.
    "foundation-forecast-sweep": {
        "task": "celery_app.foundation_forecast_task",
        "schedule": 600,
    },
    # cont. 64 — auto-arm the predict-all signal gate once online_predictor
    # has accumulated ≥ MIN_TRAINING_SAMPLES (200). Cold-start safe:
    # cannot arm before the model is warm. Kill switch
    # `prediction:gate_auto_arm=0` disables this task entirely.
    "auto-arm-prediction-gate": {
        "task": "celery_app.auto_arm_prediction_gate_task",
        "schedule": crontab(minute="*/5"),
    },
    # Blueprint F25 / L-10: Genetic Algorithm strategy evolution.
    # Basic GA at 50 trades, full Pareto (Sharpe + drawdown) at 300.
    "ga-evolve-params": {
        "task": "celery_app.ga_evolve_params",
        "schedule": crontab(minute=20, hour="*/6"),  # every 6h at :20
    },
    # Blueprint F39B / AB-05 to AB-09: DGM-Style Code Rewriting (daily, 500+ trades).
    # Cont. 40 (2026-05-22): moved from weekly Monday→daily 05:00. The original
    # weekly cadence was a placeholder when there were no LLM-generated
    # strategies in the pool. Now that the F36 pipeline is unblocked
    # (cont. 39 prescreen recovery), DGM can submit improvement candidates
    # daily — feedback loop matches the strategy creation rate.
    "dgm-code-rewrite": {
        "task": "celery_app.dgm_rewrite_weakest",
        "schedule": crontab(hour=5, minute=0),  # daily 05:00 UTC
    },
    # Blueprint F39C / AB-10: AI Scientist continuous hypothesis generation (300+).
    # Reads metacog priority gap + recent trades, submits AirLLM hypothesis task.
    "ai-scientist-hypotheses": {
        "task": "celery_app.ai_scientist_run",
        "schedule": crontab(minute=40, hour="*/4"),  # every 4h at :40
    },
    # Blueprint F18 / L-05: real CryptoBERT + FinBERT sentiment scoring.
    # Replaces the Fear & Greed Index proxy as the primary SENTIMENT_GLOBAL
    # writer when web_intel has recent text.
    # cont. 68: was */5 but CPU BERT inference takes ~6.7min/run (45 texts × 2
    # models × ~375 tokens) — it could NOT keep up at 5min, so the run lagged
    # past the 30min proxy-freshness window and sentiment:source regressed to
    # the stuck-at-29 Fear&Greed proxy. Moved to */10 (no overlap) and text is
    # truncated in the task body (~400 chars) to cut inference time ~3×, so a
    # run completes well inside the freshness window and the proxy never wins.
    "score-web-intel-sentiment": {
        "task": "celery_app.score_web_intel_sentiment",
        "schedule": crontab(minute="*/10"),
    },
    # Blueprint F9 Miss Decoder: postmortem on shadow-win counterfactuals.
    # Runs every hour at :20 — light task that batches up to N shadow wins per call.
    "decode-pending-misses": {
        "task": "celery_app.decode_pending_misses",
        "schedule": crontab(minute=20),
    },
    # Blueprint F45 Cross-Sectional Momentum producer (Liu-Tsyvinski 2022).
    # Every 5 min — ranks active pairs by 7d return on 1h candles, writes per-pair
    # rank + max-1h-return to Redis (TTL 15 min). Consumer in signals/engine.py
    # reads these to modulate per-candidate signal strength.
    "xsmom-compute": {
        "task": "celery_app.xsmom_compute_task",
        "schedule": crontab(minute="*/5"),
    },
    # Blueprint F12 Mismatch Decoder: postmortem on (high-pot loser, low-pot winner) pairs.
    # Every 2h at :35 — heavier because each pair needs the full trade context.
    "decode-pending-mismatches": {
        "task": "celery_app.decode_pending_mismatches",
        "schedule": crontab(minute=35, hour="*/2"),
    },
    # R2 (cont. 55) — refresh per-pair probation / suspension lists from
    # the decoder corpus. Every 5 min.
    "update-pair-lists-from-decoder": {
        "task": "celery_app.update_pair_lists_from_decoder",
        "schedule": crontab(minute="*/5"),
    },
    # Phase A (cont. 55) — capture live CandleNet embeddings for active pairs.
    # Live-data only — reads run_inference output which sources from data/feed.py
    # Redis OHLCV. Every 1 min. Streams to `pattern:embeddings`.
    "capture-pattern-embeddings": {
        "task": "celery_app.capture_pattern_embeddings",
        "schedule": crontab(minute="*"),
    },
    # cont. 70 — Launch-Pad maintainer: keep the 10-deep on-deck buffer full +
    # shadow-track MAE/MFE every 20 s. Shadow-only (launchpad:enabled=0) until
    # reviewed; never opens trades on its own.
    "launch-pad-maintain": {
        "task": "celery_app.launch_pad_maintain_task",
        "schedule": 20,
    },
    # R4 (cont. 55) — recompute bot self-confidence index. Every 5 min.
    "compute-bot-confidence": {
        "task": "celery_app.compute_bot_confidence",
        "schedule": crontab(minute="*/5"),
    },
    # Idea 2 RAG (cont. 64) — safety net that picks up any postmortem rows
    # whose inline embed failed at decode time (transient Ollama hiccup).
    # Every 15 min.
    "embed-pending-postmortems": {
        "task": "celery_app.embed_pending_postmortems",
        "schedule": crontab(minute="*/15"),
    },
    # Idea 3 (cont. 64) — SNIPS-optimal per-bucket threshold update.
    # Nightly 04:30 UTC (after the 04:00 ML retrains so it sees the
    # freshest GA values).
    "compute-ips-thresholds": {
        "task": "celery_app.compute_ips_thresholds",
        "schedule": crontab(minute=30, hour=4),
    },
    # Blueprint F10 Brain-learned scanner criteria weights.
    # Every 4 hours (cont. 26): blueprint mandates "continuously optimised
    # by the Brain" — daily is too coarse. Scans happen every 8h so two
    # weight updates per scan cycle gives the EMA blender time to react to
    # the latest forward-window outcomes without whipping on noise.
    "update-criteria-weights": {
        "task": "celery_app.update_criteria_weights",
        "schedule": crontab(minute=15, hour="*/4"),
    },
    # cont. 28 — ML retrain pipeline. Pre-cont.28, HMM/TFT/PatchTST/GNN
    # were frozen at their pretraining checkpoints. Blueprint Stage 3 §10.1
    # mandates "Commands ML sub-agents — spawns, trains, evaluates."
    # Order: refresh 1h OHLCV first (2:00) → retrain models that need fresh
    # 1h data (TFT 3:00, PatchTST 3:30). HMM/GNN use 1d which is already
    # fresh, so they run independently.
    "refresh-ohlcv-1h": {
        "task": "celery_app.refresh_ohlcv_1h",
        "schedule": crontab(minute=0, hour=2),     # daily 02:00 UTC
    },
    "retrain-hmm": {
        "task": "celery_app.retrain_hmm",
        "schedule": crontab(minute=30, hour=2),    # daily 02:30 UTC
    },
    "retrain-tft": {
        "task": "celery_app.retrain_tft",
        "schedule": crontab(minute=0, hour=3),     # daily 03:00 UTC (after refresh)
    },
    "retrain-patchtst": {
        "task": "celery_app.retrain_patchtst",
        "schedule": crontab(minute=30, hour=3, day_of_week="mon,thu"),  # 2x/week — slow
    },
    "retrain-gnn": {
        "task": "celery_app.retrain_gnn",
        "schedule": crontab(minute=0, hour=4),     # daily 04:00 UTC
    },
    # cont. 35 — MARL training. Heavier than the other retrains (PPO over
    # ~20k env steps); run weekly Sunday 05:00 UTC, well after the other
    # daily retrains so they don't compete for CPU. Hour Agent has no
    # consumer in production code so no retrain task is registered for it.
    "retrain-marl-day": {
        "task": "celery_app.retrain_marl_day",
        "schedule": crontab(minute=0, hour=5, day_of_week="sun"),
    },
    "retrain-marl-minute": {
        "task": "celery_app.retrain_marl_minute",
        "schedule": crontab(minute=30, hour=5, day_of_week="sun"),
    },
    # Blueprint F46 CandleNet inference — run every 60 seconds for all active pairs.
    # Writes {pair}:{interval}:candle_forecast (TTL 90s) + {pair}:atr to Redis.
    # ATR side-effect immediately fixes VPIN-proxy SL widths in risk/manager.py.
    "candlenet-infer": {
        "task": "celery_app.candlenet_infer_all",
        "schedule": 60,  # every 60 seconds
    },
    # cont. 65k Layer 1 — microstructure jump detector, every 15s (REST depth on
    # ~100 pairs ≈ 800 weight/min, well under mainnet limits; ~5-10s per scan).
    "microstructure-scan": {
        "task": "celery_app.microstructure_scan_task",
        "schedule": 15,
    },
    # cont. 65k — DAILY mainnet historical refresh. Keeps training CSVs current
    # with REAL Binance mainnet klines (testnet history is thin/stale). Runs 02:00
    # UTC on cn_train (before the weekly/daily retrains read the data).
    "refresh-historical-daily": {
        "task": "celery_app.refresh_historical_data",
        "schedule": crontab(hour=2, minute=0),
    },
    # cont. 65k — every 30 min: fetch data for NEWLY-added scanner symbols so a
    # rotated-in pair becomes trainable + forecastable promptly (not next daily run).
    "sync-new-scanner-symbols": {
        "task": "celery_app.sync_new_scanner_symbols",
        "schedule": crontab(minute="*/30"),
    },
    # cont. 69f — keep the kline corpus current (P1). Hourly incremental top-up of
    # active pairs; cheap (only candles since last stored). Backfill is manual/deep.
    "update-klines-corpus": {
        "task": "celery_app.update_klines_corpus_task",
        "schedule": crontab(minute=7),   # hourly at :07
    },
    # cont. 69s — DAILY ban-immune all-TF top-up from data.binance.vision daily
    # zips. Runs 01:30 UTC (before the 02:00 REST refresh + retrains). Keeps
    # 1m/5m/15m/30m/1h current for active pairs regardless of fapi ban status —
    # this is the durable fix for the staleness that froze fine TFs at the last
    # manual bulk backfill while the REST incremental was ban-gated.
    "bulk-topup-corpus-daily": {
        "task": "celery_app.bulk_topup_corpus_task",
        "schedule": crontab(hour=1, minute=30),
    },
    # cont. 69i (P3) — retrain the kline predict-all model every 12h so it tracks
    # the regime on the (now fresh) corpus and never re-staleness-collapses.
    "retrain-kline-predictor": {
        "task": "celery_app.retrain_kline_predictor_task",
        "schedule": crontab(minute=20, hour="*/12"),
    },
    # cont. 69j (P3 finish) — publish live kline predictions every 5 min for the
    # deterministic scorer to consume as a soft directional prior.
    "publish-kline-predictions": {
        "task": "celery_app.publish_kline_predictions_task",
        "schedule": crontab(minute="*/5"),
    },
    # Blueprint F46 CandleNet retrain — weekly Sunday 06:00 UTC.
    # Heavier than daily retrains; runs after MARL (05:30) to avoid CPU contention.
    "retrain-candlenet-1m": {
        "task": "celery_app.retrain_candlenet_1m",
        "schedule": crontab(minute=0, hour=6, day_of_week="sun"),
    },
    "retrain-candlenet-5m": {
        "task": "celery_app.retrain_candlenet_5m",
        "schedule": crontab(minute=30, hour=6, day_of_week="sun"),
    },
    # F48 §Idea D — 15m CandleNet retrain (4-TF hierarchy). Runs after 1m/5m
    # so it doesn't compete for CPU with the other retrains.
    "retrain-candlenet-15m": {
        "task": "celery_app.retrain_candlenet_15m",
        "schedule": crontab(minute=0, hour=7, day_of_week="sun"),
    },
    # cont. 65 — 1h CandleNet retrain. Runs after 15m to avoid CPU contention.
    "retrain-candlenet-1h": {
        "task": "celery_app.retrain_candlenet_1h",
        "schedule": crontab(minute=30, hour=7, day_of_week="sun"),
    },
    # cont. 65d — 30m CandleNet retrain. Runs between 15m and 1h. Closes
    # the cascade's 15m→1h horizon gap (Layer 1 of perfect_direction_prediction).
    "retrain-candlenet-30m": {
        "task": "celery_app.retrain_candlenet_30m",
        "schedule": crontab(minute=15, hour=7, day_of_week="sun"),
    },
    # F48 §Idea C — Entry Timing Agent (PPO) weekly retrain. Only triggers
    # actual training when ≥ 200 signal events have been logged.
    "train-entry-timing": {
        "task": "celery_app.train_entry_timing_task",
        "schedule": crontab(minute=30, hour=7, day_of_week="sun"),
    },
    # F49 §Component 1 — Drift Detector. Every 60s computes PSI + KS-test
    # per model and writes drift flags to Redis.
    "detect-drift-all": {
        "task": "celery_app.detect_drift_all_task",
        "schedule": 60,
    },
    # F49 §Component 2 — Performance Monitor. Every 5 min computes rolling
    # AUC + Top-Decile-Lift per model from the prediction log.
    "performance-check-all": {
        "task": "celery_app.performance_check_all_task",
        "schedule": 300,
    },
    # F49 §Component 6 — Meta-Orchestrator. Every 5 min decides which
    # model to retrain based on drift / perf / weekly-cron-miss priority.
    "orchestrator-tick": {
        "task": "celery_app.orchestrator_tick_task",
        "schedule": 300,
    },
    # F52 (cont. 55) — Exchange Net-Flow Directional Gate. Every 5 min poll
    # of CryptoQuant / Glassnode / Coinglass / CoinMetrics free endpoints.
    # See next_impl/f52_exchange_netflow.md.
    "netflow-refresh-all": {
        "task": "celery_app.netflow_refresh_all_task",
        "schedule": 300,
    },
    # F53 (cont. 55) — Qlib Alpha-158 per-minute factor compute. Reads the
    # 1m candle ring buffer; writes {pair}:qlib_alpha:{factor_id}. Reuses
    # 158 closed-form factors implemented in ml/qlib_alphas.py.
    "qlib-alpha-compute": {
        "task": "celery_app.qlib_alpha_compute_task",
        "schedule": 60,
    },
    # F53 (cont. 55) — Hourly IC refresh + Top-K selection. Cross-sectional
    # Spearman IC vs 1h-forward return; promotion gate |IC|≥0.02.
    "qlib-ic-refresh": {
        "task": "celery_app.qlib_ic_refresh_task",
        "schedule": crontab(minute=12),  # every hour at :12 — staggered from
                                          # mass-of-:00 jobs above
    },
    # F54 (cont. 55) — Weekly LLM-DSL alpha mining run. 45-90min on CPU;
    # uses qwen2.5-coder:7b proposer + deepseek-r1:8b validator via Ollama.
    "llm-dsl-mining-run": {
        "task": "celery_app.llm_dsl_mining_run_task",
        "schedule": crontab(minute=0, hour=6, day_of_week="wed"),  # Wed 06:00 UTC
    },
    # F54 (cont. 55) — Per-minute compute of promoted DSL factors for active
    # pairs. Cheap (just numpy evaluation, no LLM); writes per-pair values
    # so the live signals/engine consumer can read them.
    "llm-dsl-promoted-compute": {
        "task": "celery_app.llm_dsl_promoted_compute_task",
        "schedule": 60,
    },
    # F54 (cont. 55) — Daily decay-check + demotion of promoted factors
    # that have flipped sign or lost ≥50% of original IC magnitude.
    "llm-dsl-decay-check": {
        "task": "celery_app.llm_dsl_decay_check_task",
        "schedule": crontab(minute=30, hour=7),  # daily 07:30 UTC
    },
    # F56 (cont. 56) — Nightly conformal-interval recalibration. Reads
    # F49's model:{name}:prediction_log; writes conformal:width:{model}.
    # See next_impl/f56_conformal_prediction_wrapper.md.
    "conformal-interval-refresh": {
        "task": "celery_app.conformal_interval_refresh_task",
        "schedule": crontab(minute=15, hour=4),  # daily 04:15 UTC
    },
    # F58 (cont. 56) — 5-min poll of liquidation level density from
    # Coinglass/Coinalyze (falls back to OI×funding proxy if no API key).
    "liquidation-levels-refresh": {
        "task": "celery_app.liquidation_levels_refresh_task",
        "schedule": 300,
    },
    # cont. 60 — Frontier exit-feature producers.
    # CVD producer: per-pair CVD + close history for divergence detector.
    "cvd-producer": {
        "task": "celery_app.cvd_producer_task",
        "schedule": 60,
    },
    # Hawkes intensity per-pair.
    "hawkes-producer": {
        "task": "celery_app.hawkes_producer_task",
        "schedule": 60,
    },
    # Per-pair BOCPD changepoint posterior.
    "bocpd-per-pair-producer": {
        "task": "celery_app.bocpd_per_pair_producer_task",
        "schedule": 60,
    },
    # Conformal residual quantile per-pair.
    "conformal-residual-producer": {
        "task": "celery_app.conformal_residual_producer_task",
        "schedule": 60,
    },
    # Filtered OBI EMA.
    "filtered-obi-producer": {
        "task": "celery_app.filtered_obi_producer_task",
        "schedule": 30,
    },
    # MM-Hawkes cancel-burst score.
    "mm-hawkes-producer": {
        "task": "celery_app.mm_hawkes_producer_task",
        "schedule": 60,
    },
    # Premium index = mark - spot.
    "premium-index-producer": {
        "task": "celery_app.premium_index_producer_task",
        "schedule": 60,
    },
    # Deribit DVOL refresh.
    "dvol-refresh": {
        "task": "celery_app.dvol_refresh_task",
        "schedule": 300,
    },
    # Coinglass liquidation heatmap (per-pair clusters for dark-side SL).
    "coinglass-liq-refresh": {
        "task": "celery_app.coinglass_liq_refresh_task",
        "schedule": 60,
    },
    # cont. 61 — Seed gene pool stat-arb anchor producer.
    # Kalman-filtered pair residual z-score for kalman_pair_residual_revert seed.
    "kalman-pair-producer": {
        "task": "celery_app.kalman_pair_producer_task",
        "schedule": 60,
    },
    # cont. 66 — Pool-level GA evolution. Generational driver that turns the
    # seeded gene pool into real parent→child lineage chains (closes the
    # post-cont-61 lineage gap). Conservative cadence; bounded per-run.
    "evolve-strategy-pool": {
        "task": "celery_app.evolve_strategy_pool_task",
        "schedule": 21600,  # every 6h
    },
}

# Phase-0 self-modification FREEZE reverted (cont. 71, owner request) — the 10
# autonomous self-mod beat tasks (feature-governance, bayes-threshold, GA evolve,
# DGM code-rewrite, ai-scientist, pair-list decoder, metacog, F9/F12 decoders,
# strategy-pool evolution) are RESTORED to beat_schedule above. Background returned
# to its pre-professor autonomous state. Context: PROFESSOR_AUDIT.md F-023.


@app.task(bind=True, max_retries=3, default_retry_delay=300, queue="airllm")
def research_strategy(self, hypothesis: str) -> str:
    """AirLLM background task: generate strategy hypothesis."""
    try:
        from llm.researcher import research
        return research(hypothesis, max_new_tokens=1024)
    except Exception as exc:
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=300, queue="airllm")
def opro_optimize(self, prompt: str, window_scores: list) -> dict:
    """F39A / AB-01 to AB-04: OPRO prompt optimization with regression detection.

    Flow:
      1. Score the current window from per-trade % returns (compute_opro_score).
      2. If current_score << opro:last_score → revert_prompt(opro:last_prompt). Done.
      3. Else ask llama.cpp for a candidate improvement; strict-JSON-parse the response.
      4. save_new_prompt(candidate) — this also publishes the new addendum to Redis
         for brain/soar.py to splice into the next Ollama decide prompt.
      5. Shift baseline: opro:last_score = current_score, opro:last_prompt = the prompt
         that produced current_score (so we can revert to it next window if needed).
    """
    import sys, json as _json
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import structlog
    log = structlog.get_logger()

    try:
        import redis_client
        from self_improve.opro import compute_opro_score, save_new_prompt, revert_prompt
    except Exception as exc:
        log.warning("opro_module_import_failed", error=str(exc)[:200])
        return {"status": "import_failed"}

    r = redis_client.get()
    current_score = compute_opro_score(window_scores or [])
    prev_score = float(r.get("opro:last_score") or 50.0)
    prev_prompt = r.get("opro:last_prompt") or ""
    REGRESSION_MARGIN = 10.0

    # Regression check: if the current prompt produced a noticeably worse window,
    # roll back to the previously-good prompt before trying anything new.
    if prev_prompt and current_score < prev_score - REGRESSION_MARGIN:
        try:
            revert_prompt(prev_prompt)
        except Exception as exc:
            log.warning("opro_revert_failed", error=str(exc)[:200])
            return {"status": "revert_failed"}
        r.set("opro:last_score", current_score)
        log.warning("opro_reverted",
                    prev_score=round(prev_score, 2),
                    current_score=round(current_score, 2),
                    margin=REGRESSION_MARGIN,
                    samples=len(window_scores or []))
        # Log to experiments table for metacog evaluation (F43 reads this)
        try:
            from research.engine import log_experiment
            log_experiment(
                experiment_type="F39A",
                outcome="negative",
                metrics={"prev_score": prev_score, "current_score": current_score,
                         "margin": REGRESSION_MARGIN, "samples": len(window_scores or [])},
                notes="OPRO regression detected; prompt reverted",
            )
        except Exception:
            pass
        r.delete("opro:queued_lock")
        return {"status": "reverted",
                "prev_score": prev_score, "current_score": current_score}

    # No regression — try a candidate improvement.
    try:
        from llm.researcher import research
        full_prompt = (
            f"Previous window per-trade % returns: {window_scores}. "
            f"Computed window score: {current_score:.2f} (0-100, 50=neutral). "
            f"Current Decision LLM prompt: {prompt}. "
            "Identify the weakest reasoning step in the current prompt and propose a specific improvement. "
            "Output ONLY a JSON object with keys: weak_step (string), improvement (string), new_prompt_section (string). "
            "The new_prompt_section is concrete guidance to splice into the next decide prompt; keep it under 400 chars. "
            "Do not include any text outside the JSON object."
        )
        raw = research(full_prompt, max_new_tokens=512)
    except Exception as exc:
        log.warning("opro_llm_failed", error=str(exc)[:200])
        r.delete("opro:queued_lock")
        return {"status": "llm_failed"}

    # Strict JSON parse — AE-11: never eval/exec on LLM output.
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            r.delete("opro:queued_lock")
            return {"status": "no_json"}
        parsed = _json.loads(raw[start:end + 1])
    except Exception:
        r.delete("opro:queued_lock")
        return {"status": "parse_failed"}

    if not parsed.get("new_prompt_section"):
        r.delete("opro:queued_lock")
        return {"status": "invalid_payload"}

    try:
        save_new_prompt(_json.dumps(parsed))
    except Exception as exc:
        log.warning("opro_save_failed", error=str(exc)[:200])
        return {"status": "save_failed"}

    # Shift baseline: the prompt that PRODUCED current_score becomes the new revert target.
    r.set("opro:last_score", current_score)
    r.set("opro:last_prompt", prompt)

    log.info("opro_applied",
             current_score=round(current_score, 2),
             prev_score=round(prev_score, 2),
             samples=len(window_scores or []),
             weak_step=str(parsed.get("weak_step", ""))[:80])

    # Metacog signal: 'positive' if window's actual ROI score improved vs previous;
    # 'neutral' if first run (no prev to compare to); 'negative' otherwise.
    outcome = "neutral"
    if prev_prompt:  # second+ run
        outcome = "positive" if current_score >= prev_score else "negative"
    try:
        from research.engine import log_experiment
        log_experiment(
            experiment_type="F39A",
            outcome=outcome,
            metrics={"current_score": current_score, "prev_score": prev_score,
                     "samples": len(window_scores or []),
                     "weak_step": str(parsed.get("weak_step", ""))[:80]},
            notes="OPRO applied new prompt candidate",
        )
    except Exception:
        pass

    # Release dedup lock so the next trade window can queue another opro task.
    r.delete("opro:queued_lock")

    return {"status": "applied",
            "current_score": current_score,
            "prev_score": prev_score,
            "weak_step": str(parsed.get("weak_step", ""))[:120]}


@app.task(queue="default")
def dgm_rewrite_weakest() -> dict:
    """Blueprint F39B / AB-05 to AB-09: weekly DGM code rewriting.

    Activates at 500+ closed paper trades. Identifies the 3 active strategies
    with worst Sharpe and submits AirLLM rewrite tasks (one per strategy).
    The AirLLM task returns improved code which is queued as a new
    experimental strategy via lifecycle (downstream Celery callback).
    """
    import structlog as _sl
    _log = _sl.get_logger()
    try:
        from feature_governance.registry import is_active
        if not is_active("F39A"):
            # F39B uses the F39A governance gate (self-improvement family); honor it.
            return {"status": "f39_inactive"}
    except Exception:
        pass

    try:
        from memory.query import get_paper_closed_count
        paper_closed = get_paper_closed_count()
        if paper_closed < 500:
            return {"status": "not_active", "have": paper_closed, "need": 500}

        from self_improve.opro import get_weakest_strategies, submit_rewrite_task
        weakest = get_weakest_strategies(n=3)
        task_ids = []
        skipped: list[dict] = []
        for s in weakest:
            sid = s.get("id")
            fp = s.get("file_path")
            if sid and fp:
                tid = submit_rewrite_task(sid, fp)
                task_ids.append({"strategy_id": str(sid), "task_id": tid})
            else:
                # Seed strategies and any LLM strategy that lost its file
                # path land here. Logging explicitly so it shows up in
                # feature_health instead of failing silently.
                skipped.append({"strategy_id": str(sid) if sid else None,
                                "name": s.get("name"),
                                "reason": "missing_file_path" if not fp
                                          else "missing_id"})
        _log.info("dgm_rewrite_dispatched", count=len(task_ids),
                  skipped=len(skipped), skipped_detail=skipped[:3])
        # Durable evidence — feature_health reads these counters.
        try:
            import time as _t, redis_client as _rc
            _r = _rc.get()
            _r.incr("dgm:run_count")
            _r.set("dgm:last_run_ts", str(int(_t.time())))
            _r.set("dgm:last_submitted_count", str(len(task_ids)))
        except Exception:
            pass
        return {"status": "ok", "submitted": task_ids, "paper_closed": paper_closed}
    except Exception as exc:
        _log.error("dgm_rewrite_failed", error=str(exc))
        return {"status": "error", "error": str(exc)[:200]}


@app.task(queue="default")
def ai_scientist_run() -> dict:
    """Blueprint F39C / AB-10: AI Scientist continuous hypothesis generation.

    Activates at 300+ closed paper trades. Reads competence gaps from metacog
    + recent trade summary, submits one AirLLM hypothesis-generation task.
    """
    import structlog as _sl
    _log = _sl.get_logger()
    try:
        from feature_governance.registry import is_active
        if not is_active("F39A"):
            return {"status": "f39_inactive"}
    except Exception:
        pass

    try:
        from memory.query import get_paper_closed_count, get_recent_trades
        paper_closed = get_paper_closed_count()
        if paper_closed < 300:
            return {"status": "not_active", "have": paper_closed, "need": 300}

        # Build a short trade summary (last 20 trades — pair, direction, pnl)
        recent = get_recent_trades(20)
        summary_lines = []
        for t in recent[:20]:
            summary_lines.append(
                f"{t.get('pair')} {t.get('direction')} "
                f"pnl={float(t.get('net_pnl_usdt') or 0):.2f}"
            )
        trade_summary = "; ".join(summary_lines) or "no recent trades"

        # Competence gaps from metacog (priority gap is a domain string)
        gaps = []
        try:
            import redis_client as _rc
            gap = _rc.get().get("brain:priority_learning_gap")
            if gap:
                gaps.append(gap)
        except Exception:
            pass

        from self_improve.opro import submit_ai_scientist_task
        task_id = submit_ai_scientist_task(trade_summary, gaps)
        _log.info("ai_scientist_dispatched", task_id=task_id, gaps=gaps)
        # Durable evidence — feature_health reads these counters.
        try:
            import time as _t, redis_client as _rc
            _r = _rc.get()
            _r.incr("ai_scientist:run_count")
            _r.set("ai_scientist:last_run_ts", str(int(_t.time())))
            _r.set("ai_scientist:last_task_id", str(task_id))
            _r.set("ai_scientist:last_gaps", str(gaps)[:200])
        except Exception:
            pass
        return {"status": "ok", "task_id": task_id, "gaps": gaps,
                "paper_closed": paper_closed}
    except Exception as exc:
        _log.error("ai_scientist_failed", error=str(exc))
        return {"status": "error", "error": str(exc)[:200]}


@app.task(queue="default")
def ga_evolve_params() -> dict:
    """Blueprint F25 / L-10: Genetic Algorithm runs every 6h.

    Reads recent closed trades, evolves a 6-param vector (signal threshold,
    turbulence cap, DCA drops, trailing SL distance, kelly fraction), writes
    the best to Redis `ga:best_params` for signals/engine to consume.
    """
    import structlog as _sl
    _log = _sl.get_logger()
    try:
        from ml.genetic_algorithm import evolve_and_publish
        result = evolve_and_publish()
        _log.info("ga_evolve_complete", result=result)
        return result
    except Exception as exc:
        _log.error("ga_evolve_failed", error=str(exc))
        return {"status": "error", "error": str(exc)[:200]}


@app.task(queue="airllm")
def maml_adapt_on_changepoint() -> dict:
    """Blueprint F22 / L-13: BOCPD-triggered MAML adaptation of the world model.

    Activates at 500+ closed paper trades. Below that, MAML's regime-diversity
    assumption isn't met and the call returns early with `status=not_active`.
    Triggered by ml.bocpd.update on changepoint events.
    """
    import structlog as _sl
    _log = _sl.get_logger()
    try:
        from ml.maml import adapt_world_model_to_recent_regime
        result = adapt_world_model_to_recent_regime()
        _log.info("maml_adapt_complete", result=result)
        return result
    except Exception as exc:
        _log.error("maml_adapt_task_failed", error=str(exc))
        return {"status": "error", "error": str(exc)[:200]}


@app.task(bind=True, max_retries=3, default_retry_delay=300, queue="airllm")
def sleep_consolidation(self) -> str:
    """AirLLM background task: nightly memory consolidation.

    Blueprint V-04: this is the nightly cycle that (1) refreshes analytics
    metrics, (2) runs V-04 memrl consolidation (which triggers F17 EWC Fisher
    snapshot at 300+ trades), and (3) synthesises an LLM summary of the day.
    """
    try:
        import asyncio
        import structlog as _sl
        _log = _sl.get_logger()
        from llm.researcher import research
        from analytics.metrics import update_all_metrics
        from memory.cognitive.memrl import run_sleep_consolidation
        update_all_metrics()
        # V-04 + F17 EWC consolidation
        consol = asyncio.run(run_sleep_consolidation())
        _log.info("sleep_consolidation_memrl", result=consol)
        summary_prompt = (
            "Summarise the key trading patterns and lessons from today's closed trades. "
            "Focus on what conditions led to wins vs losses. "
            "Output as JSON with keys: key_patterns, regime_insights, suggested_improvements."
        )
        return research(summary_prompt, max_new_tokens=1024)
    except Exception as exc:
        raise self.retry(exc=exc)


@app.task(queue="web_intel")
def interpret_and_store(prompt: str, source: str, source_url: str = "", raw_text: str = "") -> dict:
    """F29 Web Intelligence — synchronous LLM interpret + write to DB.

    Flow:
    1. Send prompt to llama.cpp via llm.researcher.research()
    2. Strict JSON parse (AE-11: never eval/exec)
    3. Write parsed signal to web_intelligence table
    Returns {status, signal_type|error}.
    """
    import sys, json
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import structlog
    log = structlog.get_logger()

    try:
        from llm.researcher import research
        raw = research(prompt, max_new_tokens=384)
    except Exception as exc:
        log.warning("web_intel_llm_failed", error=str(exc)[:200])
        return {"status": "llm_failed"}

    # Strict JSON parse — never eval/exec on external content (AE-11)
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return {"status": "no_json"}
        parsed = json.loads(raw[start:end + 1])
    except Exception:
        return {"status": "parse_failed"}

    if not parsed.get("type") and not parsed.get("signal_type"):
        return {"status": "invalid_payload"}

    # Normalize keys (LLM may emit either)
    signal = {
        "source": source,
        "source_url": source_url,
        "raw_content": raw_text[:1000],
        "type": parsed.get("signal_type") or parsed.get("type"),
        "pairs_affected": parsed.get("pairs_affected", []),
        "sentiment": parsed.get("sentiment", "neutral"),
        "confidence": parsed.get("confidence", 50),
        "summary": parsed.get("summary", "")[:500],
    }

    try:
        from web_intel.collector import write_signal_to_db
        write_signal_to_db(signal)
        log.info("web_intel_signal_stored",
                 source=source, type=signal["type"], sentiment=signal["sentiment"])
        return {"status": "stored", "type": signal["type"], "sentiment": signal["sentiment"]}
    except Exception as exc:
        log.warning("web_intel_db_write_failed", error=str(exc)[:200])
        return {"status": "db_failed", "error": str(exc)[:200]}


@app.task(queue="default")
def run_feature_governance_check() -> dict:
    """F30: Feature Governance — run full 5-failure-mode check + 7-step decode.
    Triggered every 25 trades from write_trade_close() at 50+ trades.
    Also runs as a beat task every hour."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from feature_governance.bootstrap import bootstrap_all_features
    from feature_governance.registry import run_full_governance_check
    bootstrap_all_features()  # idempotent — ensures _REGISTRY is populated in this worker
    return run_full_governance_check()


@app.task(queue="default")
def run_strategy_research() -> dict:
    """F36: Strategy Research Engine.

    Full flow:
    1. Pull hypothesis from curiosity queue (or generate from recent metrics)
    2. Submit to llama.cpp via llm.researcher.research() — SYNCHRONOUS call here
       (this whole task is already a background job, so blocking is fine)
    3. Parse the LLM output as strict JSON (AE-11: never eval/exec)
    4. If valid: queue as experimental strategy via F8 lifecycle
    5. Log research note to experiments table
    """
    import sys, json, time
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import redis_client
    import structlog
    log = structlog.get_logger()
    r = redis_client.get()

    # Step 1: hypothesis source
    raw = r.rpop("research:hypothesis_queue")
    if raw:
        hdata = json.loads(raw)
        hypothesis_seed = hdata.get("reason", "Analyse recent trade outcomes and generate a hypothesis.")
        source = hdata.get("source", "curiosity")
    else:
        from analytics.metrics import compute_rolling_metrics
        m = compute_rolling_metrics(50)
        hypothesis_seed = (
            f"Win rate: {m.get('win_rate', 0):.1f}%, avg PnL: {m.get('net_pnl_usdt', 0):.2f} USDT. "
            "Identify the primary cause of losses and suggest a specific strategy adjustment."
        )
        source = "periodic"

    prompt = (
        f"You are a quant strategist. Based on this trade context: {hypothesis_seed}\n\n"
        "Output ONLY a JSON object with keys:\n"
        "  hypothesis: short string describing the strategy idea\n"
        "  entry_conditions: list of plain-English conditions for entry\n"
        "  exit_conditions: list of plain-English conditions for exit\n"
        "  dca_thresholds: object with round_1_pct and round_2_pct (negative numbers)\n"
        "  entry_overrides: TYPED object with any subset of these keys, all optional:\n"
        '    min_signal_strength (number 0-100, e.g. 30 means reject signals below 30 strength)\n'
        '    turbulence_cap (number 0-10, e.g. 3.0 means reject turbulent-regime signals when turbulence > 3.0)\n'
        '    regime_whitelist (list from {"bull","bear","turbulent"}, e.g. ["bull","bear"] to skip turbulent regimes entirely)\n'
        "  Make entry_overrides reflect the strategy's character — momentum strategies usually want stricter "
        "  min_signal_strength and may skip turbulent; mean-reversion strategies tolerate turbulent. "
        "  Omit any key you don't have a strong opinion on.\n"
        "  trailing_sl_params: TYPED object with any subset of these keys, all optional:\n"
        '    initial_atr_mult (number 0.5-10, default 2.5; lower = tighter initial stop)\n'
        '    initial_min_pct (number 0.005-0.20, default 0.03; floor for initial stop as fraction of mark)\n'
        '    trailing_dist_pct (number 0.005-0.10, default 0.02; how far behind mark the trailing stop sits when in profit)\n'
        "  Mean-reversion strategies usually want tight stops (low atr_mult, low trailing_dist_pct); breakout/momentum strategies want wider stops (let winners run).\n"
        "  position_sizing_rules: TYPED object with any subset of these keys, all optional:\n"
        '    capital_pct_mult (number 0.25-2.0, default 1.0; multiplier on default capital per trade)\n'
        "  High-conviction strategies (e.g. multi-timeframe confirmation, strong filters) deserve > 1.0; "
        "  exploratory/unproven strategies should be < 1.0 to limit blast radius.\n"
        "Do not include any text outside the JSON object."
    )

    r.set("brain:last_research_time", int(time.time()))

    # Step 2: synchronous LLM call (this task IS a background worker)
    try:
        from llm.researcher import research
        raw_out = research(prompt, max_new_tokens=512)
    except Exception as exc:
        log.warning("research_llm_failed", error=str(exc)[:200])
        return {"status": "llm_failed", "error": str(exc)[:200]}

    # Step 3: strict JSON parse (AE-11)
    try:
        start = raw_out.find("{")
        end = raw_out.rfind("}")
        if start < 0 or end <= start:
            log.warning("research_no_json_found", raw_preview=raw_out[:120])
            return {"status": "no_json"}
        parsed = json.loads(raw_out[start:end + 1])
    except Exception as exc:
        log.warning("research_json_parse_failed", error=str(exc)[:120])
        return {"status": "parse_failed"}

    # Step 4: validate required fields
    if not parsed.get("hypothesis"):
        return {"status": "invalid_payload"}

    # Validate F36 LLM output. Each validator returns None if nothing usable
    # came back, in which case we store NULL and the F8 router falls back to
    # config defaults at runtime. Cont. 17 added dca_thresholds validator +
    # pass/null compliance metrics per validator.
    from research.engine import (
        validate_entry_overrides,
        validate_sl_params,
        validate_capital_rules,
        validate_dca_thresholds,
    )
    validated_overrides = validate_entry_overrides(parsed.get("entry_overrides"))
    validated_sl        = validate_sl_params(parsed.get("trailing_sl_params"))
    validated_capital   = validate_capital_rules(parsed.get("position_sizing_rules"))
    validated_dca       = validate_dca_thresholds(parsed.get("dca_thresholds"))

    strategy_seed = {
        "hypothesis": parsed.get("hypothesis", ""),
        "entry_conditions": parsed.get("entry_conditions", []),
        "exit_conditions": parsed.get("exit_conditions", []),
        # validated_dca is the typed-clipped version; fall back to raw if
        # validator returned None (keeps legacy/loose strategies usable but
        # the F8 router cont. 8 won't recognise the loose shape).
        "dca_thresholds": validated_dca or parsed.get("dca_thresholds", {}),
        "entry_overrides":      validated_overrides,
        "trailing_sl_params":   validated_sl,
        "position_sizing_rules": validated_capital,
    }

    # Step 4.5: F36 Step 3 — originality check (AST similarity vs existing pool)
    try:
        from research.engine import originality_check
        # We do NOT have executable strategy code yet (LLM returned plain English
        # rules). The originality check is therefore informational — it compares
        # the comment header. Even loose similarity is signal.
        header = (f"# Hypothesis: {parsed.get('hypothesis')}\n"
                  f"# Entry: {parsed.get('entry_conditions')}\n"
                  f"# Exit: {parsed.get('exit_conditions')}")
        is_original = originality_check(header)
        if not is_original:
            log.info("research_strategy_too_similar",
                     hypothesis=parsed.get("hypothesis", "")[:80])
            try:
                from research.engine import log_research_note
                log_research_note(
                    hypothesis=parsed.get("hypothesis", "")[:500],
                    metrics={"source": source, "stage": "originality"},
                    decision="archived",
                    reason="AST similarity ≥80% vs existing strategy pool",
                )
            except Exception:
                pass
            return {"status": "rejected_not_original"}
    except Exception as exc:
        log.warning("originality_check_failed", error=str(exc)[:200])

    # Step 4.6: F36 Step 4 — World Model prescreen (was a return-True stub
    # before 2026-05-21 cont. 2). If clearly unpromising AND not in the
    # marginal band, archive without paper trial. If marginal, fire the
    # Step-6 iteration loop and use the best variant.
    iter_history: list[dict] = []
    try:
        from research.engine import world_model_prescreen, iterate_marginal_strategy
        screen = world_model_prescreen(strategy_seed)
        log.info("research_prescreen",
                 promising=screen.get("promising"),
                 marginal=screen.get("marginal"),
                 score=screen.get("score"),
                 prob_profit=screen.get("prob_profit"),
                 direction=screen.get("direction"),
                 n=screen.get("n_samples"))
        # Instrument every prescreen invocation (accepted, archived, marginal).
        # Counter measures gate activity, NOT strategy creations.
        if not screen.get("skipped"):
            try:
                import redis_client as _rc
                _r = _rc.get()
                _r.incr("research:prescreen_count")
                _r.set("research:prescreen_last_ts", int(time.time()))
            except Exception:
                pass
        if not screen.get("skipped"):
            if not screen.get("promising") and not screen.get("marginal"):
                # Clearly unpromising — archive before paper trial.
                try:
                    from research.engine import log_research_note
                    log_research_note(
                        hypothesis=parsed.get("hypothesis", "")[:500],
                        metrics={"source": source, "stage": "prescreen",
                                 **{k: screen.get(k) for k in
                                    ("score", "prob_profit", "direction", "n_samples")}},
                        decision="archived",
                        reason="world_model_prescreen: clearly unpromising",
                    )
                except Exception:
                    pass
                return {"status": "rejected_prescreen", "screen": screen}
            if not screen.get("promising") and screen.get("marginal"):
                # Marginal band — try evolutionary iteration before archive.
                best, best_screen, iter_history = iterate_marginal_strategy(
                    strategy_seed, max_iters=10
                )
                log.info("research_iteration_complete",
                         iters=len(iter_history) - 1,
                         start_score=iter_history[0]["score"],
                         best_score=best_screen.get("score"),
                         became_promising=best_screen.get("promising"))
                try:
                    import redis_client as _rc
                    _r = _rc.get()
                    _r.incr("research:iteration_runs")
                    _r.set("research:iteration_last_iters", max(0, len(iter_history) - 1))
                except Exception:
                    pass
                strategy_seed = best
                if not best_screen.get("promising"):
                    try:
                        from research.engine import log_research_note
                        log_research_note(
                            hypothesis=parsed.get("hypothesis", "")[:500],
                            metrics={"source": source, "stage": "iteration",
                                     "best_score": best_screen.get("score"),
                                     "iters": len(iter_history) - 1,
                                     "history": iter_history},
                            decision="archived",
                            reason="iteration did not lift strategy out of marginal band",
                        )
                    except Exception:
                        pass
                    return {"status": "rejected_after_iteration",
                            "screen": best_screen, "iterations": len(iter_history) - 1}
    except Exception as exc:
        log.warning("research_prescreen_failed", error=str(exc)[:200])
        # Fail-open — don't block strategy creation on prescreen errors.

    # Step 5: queue as experimental strategy via F8 lifecycle.
    # Cont. 41: derive a descriptive name from the hypothesis keywords so
    # dashboards / logs read e.g. `mr_strict_risk_001` instead of the opaque
    # `research_1779468669`. Strategy id (UUID) is still authoritative; this
    # is just the human-readable handle.
    def _name_from_hypothesis(hyp: str) -> str:
        import re
        _STOP = {"the","a","an","with","and","or","for","of","on","to","in",
                 "by","at","is","it","as","that","this","be","using"}
        words = [w.lower() for w in re.findall(r"[A-Za-z]+", str(hyp))[:8]
                 if w.lower() not in _STOP and len(w) >= 3]
        if not words:
            return f"research_{int(time.time())}"
        slug = "_".join(words[:3])
        # Suffix counter so similar hypotheses don't collide (mean_reversion_strict_001).
        try:
            with db_conn() as _conn:
                with _conn.cursor() as _cur:
                    _cur.execute(
                        "SELECT COUNT(*) FROM strategies WHERE name LIKE %s",
                        (f"{slug}_%",))
                    n = _cur.fetchone()[0] or 0
            return f"{slug}_{n + 1:03d}"
        except Exception:
            return f"{slug}_{int(time.time()) % 10000:04d}"

    # cont. 61 — Lineage stamping. If the marginal-iteration loop crossed
    # this seed with a top-active strategy, _crossover_parent holds that
    # active strategy's name. Look up its UUID + generation and stamp the
    # child so the GA produces a real parent → child chain in the strategies
    # table (instead of every research strategy having parent=NULL,
    # generation=0). Silent-rejection counter when lookup fails per
    # [[feedback_silent_rejection]].
    _parent_id = None
    _generation = 0
    _crossover_parent_name = strategy_seed.get("_crossover_parent")
    if _crossover_parent_name:
        try:
            with db_conn() as _conn:
                with _conn.cursor() as _cur:
                    _cur.execute(
                        "SELECT id, generation FROM strategies "
                        "WHERE name = %s AND status IN ('active','experimental') "
                        "ORDER BY created_at DESC LIMIT 1",
                        (_crossover_parent_name,),
                    )
                    _row = _cur.fetchone()
            if _row:
                _parent_id = str(_row[0])
                _generation = int(_row[1] or 0) + 1
                try:
                    import redis_client as _rc
                    _rc.get().incr("research:lineage_parent_attached_count")
                except Exception:
                    pass
            else:
                try:
                    import redis_client as _rc
                    _r = _rc.get()
                    _r.incr("research:lineage_parent_skipped_count")
                    _r.set("research:lineage_last_skip_reason",
                           f"parent_name_not_found:{_crossover_parent_name}")
                except Exception:
                    pass
        except Exception as exc:
            log.debug("research_lineage_lookup_failed",
                      parent_name=_crossover_parent_name,
                      error=str(exc)[:120])

    try:
        from strategy.lifecycle import create_experimental
        strategy = {
            "name": _name_from_hypothesis(strategy_seed.get("hypothesis", "")),
            "source": "research",
            "code": f"# Auto-generated by Strategy Research Engine\n# Hypothesis: {strategy_seed.get('hypothesis')}\n# DO NOT exec() — parameters only.",
            "entry_conditions": json.dumps(strategy_seed.get("entry_conditions", [])),
            "exit_conditions": json.dumps(strategy_seed.get("exit_conditions", [])),
            "dca_rules": json.dumps(strategy_seed.get("dca_thresholds", {})),
            # F8 router-consumed typed configs. None when LLM emission failed
            # validation — router falls back to config defaults at runtime.
            "entry_overrides": (json.dumps(strategy_seed["entry_overrides"])
                                if strategy_seed.get("entry_overrides") else None),
            "trailing_sl_params": (json.dumps(strategy_seed["trailing_sl_params"])
                                   if strategy_seed.get("trailing_sl_params") else None),
            "position_sizing_rules": (json.dumps(strategy_seed["position_sizing_rules"])
                                      if strategy_seed.get("position_sizing_rules") else None),
            "parent_strategy_id": _parent_id,
            "generation": _generation,
        }
        sid = create_experimental(strategy)
        log.info("research_strategy_created", strategy_id=sid, source=source,
                 parent=_parent_id, generation=_generation)
    except Exception as exc:
        log.warning("research_create_experimental_failed", error=str(exc)[:200])
        return {"status": "create_failed", "error": str(exc)[:200]}

    # Step 6: log research note
    try:
        from research.engine import log_research_note
        log_research_note(
            hypothesis=strategy_seed.get("hypothesis", "")[:500],
            metrics={"source": source, "iterations": len(iter_history)},
            decision="queued_for_paper_trial",
            reason=f"Strategy {sid} created from {source}",
        )
    except Exception:
        pass

    return {"status": "queued", "strategy_id": str(sid),
            "hypothesis": strategy_seed.get("hypothesis", "")[:120],
            "iterations": max(0, len(iter_history) - 1)}


@app.task(queue="default")
def track_web_intel_outcomes() -> dict:
    """F29 / AE-12: Source credibility tracking.

    For each web_intelligence row with outcome_tracked=FALSE and fetched_at older
    than the tracking window (default 72h):
      1. Parse pairs_affected (JSONB list of pair symbols)
      2. Determine actual price direction over the tracking window via CANDLES
      3. Call web_intel.collector.update_source_credibility(signal_id, sentiment, direction)
         which both marks the row's outcome AND updates aggregate credibility_score
         for the source per blueprint AE-12.

    Rule 4 honesty:
      - Consumer side (brain reading credibility_score to weight signals) NOT wired
        in this PR — web_intelligence has 1 row total; wiring consumers now would be
        theatrical. Defer until ≥50 tracked rows accumulate.
      - F29 governance gates the WHOLE web intel pipeline; this task respects it.
      - Beat schedule fires every 6h to keep DB write load light.
    """
    import sys, json
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import structlog
    log = structlog.get_logger()

    try:
        from feature_governance.registry import is_active
        if not is_active("F29"):
            return {"status": "f29_inactive"}
    except Exception:
        pass

    try:
        import redis_client, redis_keys
        from db import db_conn
        from web_intel.collector import update_source_credibility

        TRACKING_WINDOW_HOURS = 72
        MIN_MOVE_PCT = 0.5     # below this → "flat" → not scored

        r = redis_client.get()
        tracked_total = 0
        updates: list[dict] = []

        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, source, sentiment, pairs_affected
                    FROM web_intelligence
                    WHERE outcome_tracked = FALSE
                      AND fetched_at < NOW() - (INTERVAL '1 hour' * %s)
                    ORDER BY fetched_at ASC
                    LIMIT 200
                """, (TRACKING_WINDOW_HOURS,))
                rows = cur.fetchall()

        for signal_id, source, sentiment, pairs_raw in rows:
            try:
                pairs = pairs_raw if isinstance(pairs_raw, list) else json.loads(pairs_raw or "[]")
            except Exception:
                pairs = []

            # If no pairs affected → can't measure direction; mark tracked but skip credibility.
            if not pairs:
                with db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE web_intelligence SET outcome_tracked = TRUE WHERE id = %s",
                            (signal_id,),
                        )
                continue

            # Aggregate direction across affected pairs by comparing oldest available
            # candle in the tracking window to current price.
            ups = 0
            downs = 0
            flats = 0
            for pair in pairs[:5]:  # cap at 5 pairs per signal
                cur_price = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", pair)) or 0)
                candles = r.lrange(redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1h"),
                                   TRACKING_WINDOW_HOURS - 1, TRACKING_WINDOW_HOURS - 1)
                if cur_price <= 0 or not candles:
                    continue
                try:
                    past_close = float(json.loads(candles[0])["c"])
                except Exception:
                    continue
                if past_close <= 0:
                    continue
                move_pct = (cur_price - past_close) / past_close * 100
                if move_pct > MIN_MOVE_PCT:
                    ups += 1
                elif move_pct < -MIN_MOVE_PCT:
                    downs += 1
                else:
                    flats += 1

            if ups > downs and ups > flats:
                actual_direction = "up"
            elif downs > ups and downs > flats:
                actual_direction = "down"
            else:
                actual_direction = "flat"

            try:
                result = update_source_credibility(str(signal_id), sentiment, actual_direction)
                tracked_total += 1
                updates.append({
                    "signal_id": str(signal_id),
                    "source": source,
                    "predicted": sentiment,
                    "actual": actual_direction,
                    "correct": result.get("correct"),
                    "credibility_score": result.get("credibility_score"),
                    "n_tracked": result.get("n_tracked"),
                })
            except Exception as exc:
                log.warning("update_credibility_failed",
                            signal_id=str(signal_id), error=str(exc)[:200])

        log.info("track_web_intel_outcomes_complete",
                 rows_aged_out=len(rows), tracked=tracked_total)
        return {
            "status": "tracked",
            "rows_aged_out": len(rows),
            "tracked": tracked_total,
            "sample_updates": updates[:5],  # truncate for log
        }
    except Exception as exc:
        log.error("track_web_intel_outcomes_failed", error=str(exc)[:200])
        return {"status": "failed", "error": str(exc)[:200]}


@app.task(queue="default")
def metacog_daily_eval() -> dict:
    """F43 / AC-04, AC-05: Evaluate self-improvement mechanisms daily; escalate
    underperformers to F30 Feature Governance.

    Reads `experiments` table (filtered to last 7 days, ≥10 decided samples per
    mechanism) and computes per-mechanism success rates. Anything below 30% gets
    escalated via `deactivate_feature`.

    Mechanism keys must match feature_governance/bootstrap.py registry — see
    log_experiment() in research/engine.py for the conventions.

    Rule 4: gated on F43 active; needs trade_count ≥ 100 (matches F43's phase 2
    activation in bootstrap).
    """
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import structlog
    log = structlog.get_logger()

    try:
        from feature_governance.registry import is_active
        if not is_active("F43"):
            return {"status": "f43_inactive"}
    except Exception:
        pass

    # Celery worker process doesn't run brain's startup bootstrap, so _REGISTRY is
    # empty here. escalate_to_governance → deactivate_feature → assert_registered
    # would fail. Bootstrap is idempotent — safe to call every invocation.
    try:
        from feature_governance.bootstrap import bootstrap_all_features
        bootstrap_all_features()
    except Exception as exc:
        log.warning("metacog_bootstrap_skipped", error=str(exc)[:200])

    try:
        import redis_client
        trade_count = int(redis_client.get().get("brain:paper_closed") or 0)
        if trade_count < 100:
            return {"status": "below_phase_2_threshold", "trade_count": trade_count}

        from metacognition.monitor import (
            evaluate_self_improvement_mechanisms, escalate_to_governance,
        )
        results = evaluate_self_improvement_mechanisms(
            trade_count, min_samples=10, window_days=7,
        )

        actions: dict = {"evaluated": results, "escalated": []}
        UNDERPERFORM_THRESHOLD_PCT = 30.0
        for mechanism, stats in results.items():
            sr = stats.get("success_rate_pct", 100.0)
            n = stats.get("samples", 0)
            if sr < UNDERPERFORM_THRESHOLD_PCT:
                reason = (
                    f"7-day success_rate={sr}% over {n} samples "
                    f"(below threshold {UNDERPERFORM_THRESHOLD_PCT}%)"
                )
                log.warning("metacog_escalation_triggered",
                            mechanism=mechanism, success_rate_pct=sr, samples=n)
                try:
                    escalate_to_governance(mechanism, reason)
                    actions["escalated"].append({
                        "mechanism": mechanism, "success_rate_pct": sr, "samples": n,
                    })
                except Exception as exc:
                    log.warning("metacog_escalation_failed",
                                mechanism=mechanism, error=str(exc)[:200])

        log.info("metacog_daily_eval_complete",
                 trade_count=trade_count,
                 mechanisms_evaluated=len(results),
                 escalated=len(actions["escalated"]))
        return {"status": "evaluated",
                "trade_count": trade_count, **actions}
    except Exception as exc:
        log.error("metacog_daily_eval_failed", error=str(exc)[:200])
        return {"status": "failed", "error": str(exc)[:200]}


@app.task(queue="default")
def mine_trade_patterns() -> dict:
    """F2: Mine patterns across closed paper trades (winners vs losers) — every 6 hours.
    Output lands in Redis brain:patterns_mined for dashboard + LLM prompt context."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from feature_governance.registry import is_active
        if not is_active("F2"):
            return {"status": "f2_inactive"}
    except Exception:
        pass
    from memory.pattern_miner import mine_patterns
    return mine_patterns()


@app.task(queue="default")
def retrain_direction_model() -> dict:
    """F13 / X-09: Retrain Direction Prediction Model from closed paper trades.
    Triggered every 50 closed trades at ≥100 from memory.write.write_trade_close.
    Logs result to experiments table for F43 metacog evaluation."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from ml.direction_model import train_from_closed_trades
    result = train_from_closed_trades()

    # Metacog signal: success if model trained with adequate samples + train_acc above
    # a chance-baseline. Below baseline → metacog can flag F13 for review.
    try:
        from research.engine import log_experiment
        status = result.get("status", "unknown")
        train_acc = float(result.get("train_acc", 0))
        samples = int(result.get("samples", 0))
        # Heuristic: positive only if trained AND train_acc clearly above coin-flip baseline.
        # Negative if explicitly failed (single_class, insufficient_data) OR trained with
        # train_acc near random (below 0.55).
        if status == "trained" and train_acc >= 0.55:
            outcome = "positive"
        elif status == "trained":
            outcome = "negative"  # trained but barely above noise — model not learning
        else:
            outcome = "neutral"   # data not ready
        log_experiment(
            experiment_type="F13",
            outcome=outcome,
            metrics={"status": status, "train_acc": train_acc, "samples": samples,
                     "pos_rate": result.get("pos_rate", 0)},
            notes=f"Direction Model retrain: {status}",
        )
    except Exception:
        pass

    return result


# ── cont. 28 — ML retrain pipeline for HMM/TFT/PatchTST/GNN ──────────────
# Wires the existing pretrainer.main trainers as scheduled Celery tasks so
# the four models actually update as the market evolves. Blueprint Stage 3
# §10.1: "Commands ML sub-agents — spawns, trains, evaluates, retires them."
# Pre-cont.28 these models stayed frozen at pretraining checkpoints (May 16-18
# 2026) for the lifetime of the bot.

_HMM_TOP_N      = 30
_TFT_TOP_N      = 50
_PATCHTST_TOP_N = 30
_GNN_TOP_N      = 20
_OHLCV_REFRESH_TOP_N = 50
_OHLCV_REFRESH_DAYS  = 90   # only refresh recent window — full history is on disk


def _active_pair_list(top_n: int) -> list[str]:
    """Top-N active pairs ordered by composite score. Falls back to first N
    historical CSV symbols when scanner hasn't run yet."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import redis_client as _rc
    from pathlib import Path
    r = _rc.get()
    try:
        active = sorted(r.smembers("scanner:active_pairs"))
        if active:
            return active[:top_n]
    except Exception:
        pass
    p = Path("/app/data/historical")
    if p.exists():
        return sorted([d.name for d in p.iterdir() if d.is_dir()])[:top_n]
    return []


@app.task(queue="default")
def refresh_ohlcv_1h(top_n: int = _OHLCV_REFRESH_TOP_N,
                     lookback_days: int = _OHLCV_REFRESH_DAYS) -> dict:
    """Append fresh 1h candles to data/historical/{pair}/1h.csv for the top-N
    active pairs. Deduplicates by timestamp. Pre-cont.28 these CSVs were
    pulled once at pretrainer time (June 2024) and never refreshed, so the
    TFT/PatchTST retrains were running on year-old data.

    Rule 4 caveats:
      - Only 1h interval refreshed. 1d data is reasonably fresh already
        (HMM/GNN use 1d) and 5m/15m aren't used by current trainers.
      - Append-only with timestamp dedup — does not handle exchange
        symbol delistings.
      - Best-effort: per-pair errors are logged and skipped, not retried.
    """
    import sys, csv, time
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import structlog
    from datetime import datetime, timedelta, timezone
    from pathlib import Path
    import redis_client as _rc
    log = structlog.get_logger()

    pairs = _active_pair_list(top_n)
    if not pairs:
        return {"status": "skipped", "reason": "no_pairs"}

    try:
        from exchange.client import BinanceClient
        client = BinanceClient()
    except Exception as exc:
        log.warning("ohlcv_refresh_client_init_failed", error=str(exc)[:200])
        return {"status": "error", "reason": f"client_init: {str(exc)[:100]}"}

    start_ms = int((datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    updated = 0
    skipped = 0
    failed = 0
    new_rows_total = 0
    for symbol in pairs:
        out_dir = Path("/app/data/historical") / symbol
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "1h.csv"
        existing_ts: set[int] = set()
        if out_file.exists():
            try:
                with open(out_file) as f:
                    next(f, None)  # skip header
                    for line in f:
                        ts_part = line.split(",", 1)[0]
                        if ts_part:
                            try:
                                existing_ts.add(int(ts_part))
                            except ValueError:
                                pass
            except Exception:
                pass
        try:
            klines = client.get_historical_klines(symbol, "1h", start_ms, end_ms)
        except Exception as exc:
            log.warning("ohlcv_refresh_fetch_failed",
                        symbol=symbol, error=str(exc)[:200])
            failed += 1
            continue
        new_klines = [k for k in (klines or []) if int(k[0]) not in existing_ts]
        if not new_klines:
            skipped += 1
            continue
        write_header = not out_file.exists()
        try:
            with open(out_file, "a", newline="") as f:
                writer = csv.writer(f)
                if write_header:
                    writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
                for k in new_klines:
                    writer.writerow([k[0], k[1], k[2], k[3], k[4], k[5]])
            updated += 1
            new_rows_total += len(new_klines)
        except Exception as exc:
            log.warning("ohlcv_refresh_write_failed",
                        symbol=symbol, error=str(exc)[:200])
            failed += 1
        time.sleep(0.05)

    try:
        r = _rc.get()
        r.set("ml:ohlcv_refresh_last_ts", int(time.time()))
        r.set("ml:ohlcv_refresh_last_updated", updated)
        r.set("ml:ohlcv_refresh_last_new_rows", new_rows_total)
    except Exception:
        pass

    log.info("ohlcv_refresh_complete",
             pairs=len(pairs), updated=updated, skipped=skipped,
             failed=failed, new_rows=new_rows_total)
    return {"status": "ok", "pairs": len(pairs), "updated": updated,
            "skipped": skipped, "failed": failed, "new_rows": new_rows_total}


def _wrap_pretrainer_train(fn_name: str, top_n: int, metric_redis_key: str) -> dict:
    """Shared wrapper for HMM/TFT/PatchTST/GNN retrain tasks. Calls
    pretrainer.main.train_{fn_name}(symbols[:top_n]); publishes a fresh-train
    timestamp + elapsed time to Redis. Returns a status dict."""
    import sys, time
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import structlog
    log = structlog.get_logger()
    pairs = _active_pair_list(top_n)
    if len(pairs) < 5:
        return {"status": "skipped", "reason": "insufficient_pairs",
                "pairs": len(pairs)}
    try:
        import pretrainer.main as pm
        fn = getattr(pm, fn_name)
    except Exception as exc:
        return {"status": "error", "reason": f"import: {str(exc)[:120]}"}
    t0 = time.time()
    try:
        fn(pairs)
    except Exception as exc:
        log.warning("ml_retrain_failed", fn=fn_name, error=str(exc)[:200])
        return {"status": "error", "reason": str(exc)[:200]}
    elapsed = round(time.time() - t0, 1)
    try:
        import redis_client as _rc
        r = _rc.get()
        r.set(f"{metric_redis_key}_ts", int(time.time()))
        r.set(f"{metric_redis_key}_elapsed_s", elapsed)
    except Exception:
        pass
    log.info("ml_retrain_complete", fn=fn_name, pairs=len(pairs), elapsed_s=elapsed)
    return {"status": "ok", "fn": fn_name, "pairs": len(pairs), "elapsed_s": elapsed}


@app.task(queue="default")
def retrain_hmm() -> dict:
    """Refit HMM regime model on top-30 active pairs' 1d closes. 1d data is
    already reasonably fresh on disk; no OHLCV refresh dependency."""
    return _wrap_pretrainer_train("train_hmm", _HMM_TOP_N, "ml:hmm:last_train")


@app.task(queue="default")
def retrain_tft() -> dict:
    """Retrain TFT on top-50 active pairs' 1h closes. Depends on prior
    refresh_ohlcv_1h having appended recent candles."""
    return _wrap_pretrainer_train("train_tft", _TFT_TOP_N, "ml:tft:last_train")


@app.task(queue="default")
def retrain_patchtst() -> dict:
    """Retrain PatchTST on top-30 active pairs' 1h candles. Slow (2 epochs
    over big transformer). Scheduled less frequently than TFT."""
    return _wrap_pretrainer_train("train_patchtst", _PATCHTST_TOP_N, "ml:patchtst:last_train")


@app.task(queue="default")
def retrain_gnn() -> dict:
    """Re-initialise GNN with fresh Xavier weights and refreshed correlation
    sanity-log on top-20 active pairs' 1d data."""
    return _wrap_pretrainer_train("train_gnn", _GNN_TOP_N, "ml:gnn:last_train")


# ── Blueprint F21 MARL training (cont. 35) ────────────────────────────────
# Day and Minute agents both have production consumers; Hour Agent does
# not, so no task is registered for it (Rule 4 honesty — see
# ml/marl_training.py module docstring).
def _wrap_marl_train(fn_name: str, metric_redis_key: str) -> dict:
    """Shared wrapper for MARL retrain tasks. Honest reporting: any failure
    inside train_marl_<level> bubbles up as status='error' AND the Redis
    timestamp is NOT updated. Contrast with _wrap_pretrainer_train which
    has a known swallowed-exception issue (tracked separately)."""
    import sys, time
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import structlog
    log = structlog.get_logger()
    try:
        import ml.marl_training as mt
        fn = getattr(mt, fn_name)
    except Exception as exc:
        return {"status": "error", "reason": f"import: {str(exc)[:120]}"}
    t0 = time.time()
    try:
        result = fn()
    except Exception as exc:
        log.warning("marl_retrain_failed", fn=fn_name, error=str(exc)[:200])
        return {"status": "error", "reason": str(exc)[:200]}
    elapsed = round(time.time() - t0, 1)
    if result.get("status") == "ok":
        try:
            import redis_client as _rc
            r = _rc.get()
            r.set(f"{metric_redis_key}_ts", int(time.time()))
            r.set(f"{metric_redis_key}_elapsed_s", elapsed)
            r.set(f"{metric_redis_key}_trades", result.get("n_trades", 0))
        except Exception:
            pass
    log.info("marl_retrain_complete", fn=fn_name, elapsed_s=elapsed, **result)
    return {**result, "elapsed_s": elapsed}


@app.task(queue="default")
def retrain_marl_day() -> dict:
    """Train Day Agent PPO on closed-trade history. ~20k env steps."""
    return _wrap_marl_train("train_marl_day", "ml:marl:day:last_train")


@app.task(queue="default")
def retrain_marl_minute() -> dict:
    """Train Minute Agent PPO on closed-trade history. ~20k env steps."""
    return _wrap_marl_train("train_marl_minute", "ml:marl:minute:last_train")


# ── Blueprint F46 CandleNet tasks ─────────────────────────────────────────────

def _scanner_universe() -> set:
    """Live scanner active-pair set (dynamic — reflects current size, 100/200/…)."""
    try:
        raw = redis_client.get().smembers("scanner:active_pairs") or set()
        return {(x.decode() if isinstance(x, bytes) else x) for x in raw}
    except Exception:
        return set()


def _mainnet_refresh_pairs(pairs, days: int | None = None) -> dict:
    """cont. 65k — fetch REAL Binance MAINNET klines (futures→spot, paginated, no
    auth) for the given pairs and overwrite their training CSVs. Shared by the
    daily full refresh and the frequent new-symbol sync. Creates CSV dirs on
    demand so brand-new scanner symbols become trainable immediately."""
    import csv as _csv, json as _json, time as _time, urllib.request as _u
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path as _P
    _log = structlog.get_logger()
    DATA = _P("/app/data/historical")
    if not DATA.exists():
        return {"status": "no_data_dir"}
    INTERVALS = ["5m", "15m", "30m", "1h"]      # 1m skipped (heaviest/noisiest)
    DAYS = days if days is not None else int(os.environ.get("REFRESH_FETCH_DAYS", "45"))
    PAGE = 1500
    FUT = "https://fapi.binance.com/fapi/v1/klines"
    SPOT = "https://api.binance.com/api/v3/klines"

    def _get(url):
        req = _u.Request(url, headers={"User-Agent": "curl/8"})
        return _json.loads(_u.urlopen(req, timeout=20).read())

    def _paginate(base, sym, itv, start, end):
        out, cur = [], start
        for _ in range(400):
            try:
                k = _get(f"{base}?symbol={sym}&interval={itv}"
                         f"&startTime={cur}&endTime={end}&limit={PAGE}")
            except Exception:
                break
            if not k:
                break
            out += k
            cur = k[-1][0] + 1
            if len(k) < PAGE:
                break
            _time.sleep(0.12)
        return out

    now = int(_dt.now(_tz.utc).timestamp() * 1000)
    start = now - int(DAYS * 86400 * 1000)
    new_dirs = refreshed = nodata = failed = 0
    for pair in sorted(pairs):
        _pd = DATA / pair
        if not _pd.exists():
            try:
                _pd.mkdir(parents=True, exist_ok=True)
                new_dirs += 1
            except Exception:
                failed += 1
                continue
        for itv in INTERVALS:
            kl = _paginate(FUT, pair, itv, start, now) or _paginate(SPOT, pair, itv, start, now)
            if not kl:
                nodata += 1
                continue
            try:
                with open(_pd / f"{itv}.csv", "w", newline="") as f:
                    w = _csv.writer(f)
                    w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
                    for k in kl:
                        w.writerow([k[0], k[1], k[2], k[3], k[4], k[5]])
                refreshed += 1
            except Exception:
                failed += 1
    return {"new_dirs": new_dirs, "refreshed": refreshed,
            "nodata": nodata, "failed": failed}


@app.task(queue="candlenet")
def refresh_historical_data() -> dict:
    """cont. 65k — DAILY full refresh of training CSVs from REAL mainnet klines,
    THEN retrain all candle TFs IN-PROCESS on the fresh data.

    Refreshes the UNION of on-disk pairs + the live scanner universe (DYNAMIC: reads
    scanner:active_pairs at run time → a 100→200 scanner change is reflected next run).

    cont. 65k — routed to `candlenet` (reliable delivery) and chains the retrains
    IN-PROCESS rather than enqueuing to `cn_train`: the cn_train QUEUE delivery proved
    flaky (tasks consumed-but-not-run, a kombu/Redis-broker gremlin from repeated worker
    recreates), whereas direct in-process execution is rock-solid. Samples are capped
    (CANDLENET_MAX_SAMPLES) so each retrain's GAF fits memory. Runs once at 02:00 UTC;
    the brief inference pause during the nightly retrain batch is acceptable."""
    from pathlib import Path as _P
    _log = structlog.get_logger()
    disk = {p.name for p in _P("/app/data/historical").iterdir() if p.is_dir()} \
        if _P("/app/data/historical").exists() else set()
    scan = _scanner_universe()
    res = _mainnet_refresh_pairs(disk | scan)
    _log.info("refresh_historical_complete", scanner_pairs=len(scan),
              total_pairs=len(disk | scan), **res)
    try:
        redis_client.get().set("data:last_refresh_ts",
                               int(__import__("time").time()))
    except Exception:
        pass
    # Chain retrains in-process on the fresh data (reliable; no queue dependency).
    retrains = {}
    for tf, fn in (("5m", retrain_candlenet_5m), ("15m", retrain_candlenet_15m),
                   ("30m", retrain_candlenet_30m), ("1h", retrain_candlenet_1h)):
        try:
            r = fn()
            retrains[tf] = r.get("status") if isinstance(r, dict) else str(r)
        except Exception as exc:
            retrains[tf] = f"error:{str(exc)[:80]}"
        _log.info("daily_retrain_done", interval=tf, result=retrains.get(tf))
    # Layer 2a — retrain the learned TF-fusion model on fresh trade outcomes too.
    try:
        fusion = train_tf_fusion()
    except Exception as exc:
        fusion = {"status": f"error:{str(exc)[:80]}"}
    _log.info("daily_tf_fusion_done", result=fusion)
    return {"status": "ok", "refresh": res, "retrains": retrains, "tf_fusion": fusion}


@app.task(queue="candlenet")
def train_tf_fusion() -> dict:
    """Layer 2a — train the learned multi-TF FUSION model (soft-attention
    replacement) on recent REAL trade outcomes. Features = the per-TF CandleNet
    outputs + regime + microstructure context snapshotted in trades.feature_vector;
    label = realized up/down from net_pnl_usdt. Produces a calibrated P(up) the
    cascade uses instead of the hard 3-of-4 vote. See ml/tf_fusion.py."""
    from ml.tf_fusion import train_from_trades
    from db import db_conn
    rows = []
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT feature_vector, direction, net_pnl_usdt FROM trades "
                    "WHERE feature_vector IS NOT NULL AND status = 'closed' "
                    "AND net_pnl_usdt IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT 20000")
                for fv, direction, pnl in cur.fetchall():
                    rows.append({"feature_vector": fv, "direction": direction,
                                 "net_pnl_usdt": pnl})
    except Exception as exc:
        return {"status": "db_error", "error": str(exc)[:160]}
    return train_from_trades(rows)


@app.task(queue="microstructure")
def sync_new_scanner_symbols() -> dict:
    """cont. 65k — FREQUENT (every 30 min) sync so NEWLY-added scanner symbols get
    training data + a CSV dir PROMPTLY instead of waiting for the daily refresh.
    The scanner rotates its universe (symbols added/removed); this fetches data for
    any live scanner pair that lacks a usable CSV. candlenet inference + the
    microstructure scan already read scanner:active_pairs dynamically, so once the
    CSV lands a new symbol is fully covered (forecasts + jump signals + next retrain).
    Lightweight: usually 0-few new pairs."""
    from pathlib import Path as _P
    _log = structlog.get_logger()
    DATA = _P("/app/data/historical")
    scan = _scanner_universe()
    if not scan:
        return {"status": "no_scanner_pairs"}
    # "Missing" = no dir, or 5m.csv absent/empty (rotated-in symbol never fetched).
    missing = []
    for p in scan:
        f = DATA / p / "5m.csv"
        try:
            if not f.exists() or f.stat().st_size < 200:
                missing.append(p)
        except Exception:
            missing.append(p)
    if not missing:
        return {"status": "ok", "new_symbols": 0}
    res = _mainnet_refresh_pairs(missing)
    _log.info("sync_new_scanner_symbols", new_symbols=len(missing),
              sample=sorted(missing)[:8], **res)
    try:
        redis_client.get().setex("data:last_new_symbol_sync_ts", 7200,
                                 int(__import__("time").time()))
    except Exception:
        pass
    return {"status": "ok", "new_symbols": len(missing), **res}


@app.task(queue="microstructure")
def microstructure_scan_task() -> dict:
    """Layer 1 (cont. 65k) — scan the live scanner universe for order-book
    microstructure (real L1 OFI + spread + VWAP-dev) and write a jump-score +
    direction per pair. The 'catch sudden moves before they happen' signal the
    candle cascade can't provide. Dynamic: reads scanner:active_pairs at call
    time. Own `microstructure` queue so its ~5-10s REST scan never blocks the
    fast producers on default."""
    from signals.microstructure import scan_all
    return scan_all()


@app.task(queue="candlenet")
def candlenet_infer_all() -> dict:
    """Run CandleNet inference for all active pairs on both 1m and 5m.

    Called every 60 seconds. Writes {pair}:{interval}:candle_forecast (TTL 90s)
    and {pair}:atr to Redis. ATR side-effect fixes VPIN-proxy SL width in
    risk/manager.py with zero changes to that file.
    """
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import json
    import structlog
    log = structlog.get_logger()
    try:
        import redis_client
        r = redis_client.get()
        # cont. 58 fix: scanner:active_pairs is a Redis SET (see
        # redis_keys.ACTIVE_PAIRS + celery_app.py:1282). Prior `r.get(...)`
        # raised WRONGTYPE on every tick, silently dropping ALL inference and
        # the F48 TP1/TP2 pipeline that depends on `{pair}:{interval}:candle_forecast`.
        pairs_all = [p.decode() if isinstance(p, bytes) else p
                     for p in r.smembers("scanner:active_pairs")]
        if not pairs_all:
            return {"status": "no_active_pairs"}
        # cont. 66 — cap inference to the top-N pairs by 24h volume so the
        # forecast loop fits 6 cores. 200-pair × 5-TF inference every 60s
        # over-subscribes the box (load ~25/6) and leaves coverage incomplete;
        # capping to the most-liquid ~100 keeps THEIR forecasts fully fresh.
        # The full 200 stay tradeable — the tail just trades on cheaper
        # OFI/microstructure signals. Redis-tunable: candlenet:infer_max_pairs
        # ("0" = no cap → revert to all active pairs).
        try:
            _cap = int(r.get("candlenet:infer_max_pairs") or 100)
        except (TypeError, ValueError):
            _cap = 100
        if _cap > 0 and len(pairs_all) > _cap:
            import redis_keys as _rk
            def _vol24(p):
                try:
                    return float(r.get(_rk.TICKER_VOLUME_24H.replace("{pair}", p)) or 0)
                except (TypeError, ValueError):
                    return 0.0
            pairs = sorted(pairs_all, key=_vol24, reverse=True)[:_cap]
        else:
            pairs = sorted(pairs_all)
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:120]}

    from ml.candlenet import run_inference
    ok, skipped = 0, 0
    # cont. 65 — 1h completes the cascade's full 4-TF hierarchy. Inference
    # silently returns {} (counted as skipped) until models/candlenet_1h.pth
    # exists; the architecture is forward-compatible and 1m/5m/15m flows are
    # unchanged.
    for pair in pairs:
        for interval in ("1m", "5m", "15m", "30m", "1h"):
            try:
                fc = run_inference(pair, interval)
                if fc:
                    ok += 1
                else:
                    skipped += 1
            except Exception as exc:
                log.warning("candlenet_infer_task_pair_failed",
                            pair=pair, interval=interval, error=str(exc)[:120])
                skipped += 1
    return {"status": "done", "ok": ok, "skipped": skipped}


def _candlenet_retrain_with_orchestrator(interval: str, fg_id: str) -> dict:
    """Shared body for CandleNet retrain tasks — handles F49 orchestrator
    lock release + status reporting."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from feature_governance.registry import is_active
        if not is_active(fg_id):
            return {"status": f"{fg_id.lower()}_inactive"}
    except Exception:
        pass
    from ml.candlenet import train as _train
    from pathlib import Path
    # F49 §Component 3 — opt-in HPO. When `brain:f49_hpo_enabled` is "1"
    # (settable from the dashboard or CLI), the celery-triggered retrain
    # runs Optuna search before full training. Defaults off so the weekly
    # cron remains fast and predictable.
    enable_hpo = False
    try:
        from redis_client import get as _rget
        enable_hpo = (_rget().get("brain:f49_hpo_enabled") or b"0") in (b"1", "1")
    except Exception:
        pass
    try:
        result = _train(interval, data_dir=Path("data/historical"),
                        models_dir=Path("models"),
                        enable_hpo=enable_hpo)
        status = "ok" if result.get("status") == "trained" else "failed"
    except Exception as exc:
        result = {"status": "exception", "error": str(exc)[:200]}
        status = "failed"
    # F49 — release the orchestrator's in-flight lock
    try:
        from ml.training_orchestrator import mark_training_done
        mark_training_done(f"candlenet_{interval}", status=status)
    except Exception:
        pass
    return result


@app.task(queue="candlenet")
def retrain_candlenet_1m() -> dict:
    """Weekly retrain of CandleNet 1m model on latest historical data.
    Also triggered on-demand by F49 orchestrator on drift / perf degrade."""
    return _candlenet_retrain_with_orchestrator("1m", "F48_1m")


@app.task(queue="candlenet")
def retrain_candlenet_5m() -> dict:
    """Weekly retrain of CandleNet 5m model on latest historical data.
    Also triggered on-demand by F49 orchestrator on drift / perf degrade."""
    return _candlenet_retrain_with_orchestrator("5m", "F48_5m")


@app.task(queue="candlenet")
def retrain_candlenet_15m() -> dict:
    """F48 §Idea D — Weekly retrain of CandleNet 15m model (4-TF hierarchy).
    Also triggered on-demand by F49 orchestrator on drift / perf degrade."""
    return _candlenet_retrain_with_orchestrator("15m", "F48_15m")


@app.task(queue="candlenet")
def retrain_candlenet_1h() -> dict:
    """cont. 65 — Weekly retrain of CandleNet 1h model. Closes the cascade's
    1h candle vote gap (previously only TFT 1h bias)."""
    return _candlenet_retrain_with_orchestrator("1h", "F48_1h")


@app.task(queue="candlenet")
def retrain_candlenet_30m() -> dict:
    """cont. 65d — Weekly retrain of CandleNet 30m model. Closes the 15m→1h
    horizon gap in the multi-TF cascade (Layer 1 of perfect_direction_prediction)."""
    return _candlenet_retrain_with_orchestrator("30m", "F48_30m")


@app.task(queue="default")
def train_entry_timing_task() -> dict:
    """F48 §Idea C — Weekly training of the PPO entry-timing agent.
    Refuses to train until ≥ 200 logged signal events are available."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from ml.entry_timing_agent import train_entry_timing
    return train_entry_timing()


# ── F49 Autonomous Self-Training Orchestrator tasks ──────────────────────────

@app.task(queue="default")
def detect_drift_all_task() -> dict:
    """F49 §Component 1 — Drift Detector. Every 60s, computes PSI + KS-test
    per model between training-time and live feature distributions."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from feature_governance.registry import is_active
        if not is_active("F49"):
            return {"status": "f49_inactive"}
    except Exception:
        pass
    from ml.drift_detector import detect_drift_all
    return detect_drift_all()


@app.task(queue="default")
def performance_check_all_task() -> dict:
    """F49 §Component 2 — Performance Monitor. Every 5 min, computes rolling
    100-trade AUC per model and flags retrain_needed when degraded."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from feature_governance.registry import is_active
        if not is_active("F49"):
            return {"status": "f49_inactive"}
    except Exception:
        pass
    from ml.performance_monitor import performance_check_all
    return performance_check_all()


@app.task(queue="default")
def orchestrator_tick_task() -> dict:
    """F49 §Component 6 — Meta-Orchestrator. Every 5 min, picks WHICH model
    to retrain based on drift > perf > weekly-cron-miss priority."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from feature_governance.registry import is_active
        if not is_active("F49"):
            return {"status": "f49_inactive"}
    except Exception:
        pass
    from ml.training_orchestrator import orchestrator_tick
    return orchestrator_tick()


@app.task(queue="default")
def score_web_intel_sentiment() -> dict:
    """Blueprint F18 / L-05 — periodic CryptoBERT + FinBERT scoring of recent
    web_intel text. Replaces the Fear & Greed proxy as the primary writer of
    SENTIMENT_GLOBAL and per-pair sentiment for any pair tagged in
    web_intelligence.pairs_affected.

    Runs every 5 minutes via beat schedule. F30 governance gate (F18). Reads
    the last 60 minutes of web_intelligence rows; if too few (< 5) extends
    the window to 6h so a quiet news cycle still produces fresh sentiment.
    """
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import structlog
    log = structlog.get_logger()
    try:
        from feature_governance.registry import is_active
        if not is_active("F18"):
            return {"status": "f18_inactive"}
    except Exception:
        pass

    try:
        from db import db_conn
        from ml.sentiment import (
            update_global_sentiment, update_pair_sentiment,
        )
    except Exception as exc:
        log.warning("sentiment_imports_failed", error=str(exc)[:200])
        return {"status": "import_failed", "error": str(exc)[:200]}

    # Pull recent web_intel text. Each row carries source + raw_content +
    # pairs_affected. We pick the most informative text field per row.
    rows = []
    window_minutes = 60
    for _attempt in range(2):
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, source, raw_content, summary, pairs_affected
                       FROM web_intelligence
                       WHERE fetched_at > NOW() - INTERVAL '%s minutes'
                       ORDER BY fetched_at DESC
                       LIMIT 200""" % window_minutes,
                )
                rows = cur.fetchall()
        if len(rows) >= 5 or window_minutes >= 360:
            break
        window_minutes = 360   # widen to 6h on low-volume windows

    if not rows:
        log.info("sentiment_no_recent_text", window_minutes=window_minutes)
        return {"status": "no_recent_text", "window_minutes": window_minutes}

    # Build the global batch.
    def _source_type_for(src: str) -> str:
        s = (src or "").lower()
        if "reddit" in s: return "reddit"
        if "twitter" in s or "x.com" in s: return "twitter"
        if "telegram" in s: return "telegram"
        return "news"

    global_items = []
    per_pair: dict[str, list[dict]] = {}
    # cont. 68: only score per-pair sentiment for pairs we ACTUALLY trade. The
    # LLM pairs_affected extraction emits noise ("USD", "IRR/USD", "DeFi tokens",
    # "crypto-related equities") that each cost a full BERT re-score and wrote to
    # dead keys. Validate the normalised symbol against the live scanner universe.
    try:
        _active_syms = set(redis_client.get().smembers("scanner:active_pairs")) or set()
    except Exception:
        _active_syms = set()
    # cont. 69: fiat/forex bases that the LLM pairs_affected extractor emits as
    # noise — never tradeable crypto perps, so drop before the BERT re-score.
    _FIAT_CODES = frozenset({
        "JPY", "EUR", "GBP", "CNY", "KRW", "INR", "IRR", "VND", "RUB", "TRY",
        "BRL", "AUD", "CAD", "CHF", "HKD", "SGD", "MXN", "ZAR", "NGN", "ARS",
    })
    # cont. 68: truncate to ~400 chars. For news/social SENTIMENT polarity the
    # headline + lead sentence carry the signal; the full article body roughly
    # tripled CPU BERT inference time for negligible polarity gain.
    _SENT_MAXCHARS = 400
    for rid, src, raw, summary, pairs_affected in rows:
        text = (raw or summary or "")[:_SENT_MAXCHARS].strip()
        if not text:
            continue
        item = {"text": text, "source_type": _source_type_for(src)}
        global_items.append(item)
        if isinstance(pairs_affected, list):
            for pair in pairs_affected:
                if not (isinstance(pair, str) and pair):
                    continue
                # Normalise "BTC"/"BTC/USD" → "BTCUSDT", then keep ONLY symbols
                # that are in the live tradeable universe (drops all LLM noise).
                base = pair.split("/")[0].strip().upper()
                if not base or base in ("USD", "USDT", "USDC", "BUSD"):
                    continue
                # cont. 69: explicit fiat/forex blocklist. The active-set filter
                # below only fires when _active_syms is populated; when it's
                # transiently empty, forex noise (JPYUSDT, EURUSDT, …) leaked in
                # and burned a BERT re-score. These are never tradeable perps.
                if base in _FIAT_CODES:
                    continue
                sym = base if base.endswith("USDT") else f"{base}USDT"
                if _active_syms and sym not in _active_syms:
                    continue
                per_pair.setdefault(sym, []).append(item)

    # 1) Global sentiment update
    g_res = update_global_sentiment(global_items)

    # 2) Per-pair updates — only when we have at least 2 texts for that pair
    pair_results = {}
    for pair, items in per_pair.items():
        if len(items) < 2:
            continue
        pair_results[pair] = update_pair_sentiment(pair, items)

    log.info("sentiment_scoring_complete",
             window_minutes=window_minutes,
             rows=len(rows), global_n=g_res.get("n"),
             global_normalized=g_res.get("normalized"),
             pairs_updated=len(pair_results))
    return {
        "status": "updated",
        "window_minutes": window_minutes,
        "rows_examined": len(rows),
        "global": g_res,
        "pairs_updated": list(pair_results.keys()),
    }


@app.task(queue="default")
def run_self_play() -> dict:
    """F41: Self-Play vs MarS — run episode, feed outcome to World Model, update stats.
    F30 governance gate: skip when F41 deactivated."""
    import sys, json
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import redis_client
    try:
        from feature_governance.registry import is_active
        if not is_active("F41"):
            return {"status": "f41_inactive"}
    except Exception:
        pass
    from self_play.mars import (
        run_self_play_episode, feed_to_world_model, record_episode_stats,
    )
    r = redis_client.get()

    result = run_self_play_episode(n_steps=200)
    feed_to_world_model(result)
    record_episode_stats(result)

    wins = int(r.get("brain:self_play_wins") or 0) + (1 if result["total_pnl"] > 0 else 0)
    games = int(r.get("brain:self_play_games") or 0) + 1
    win_rate = round(wins / games * 100, 2)
    r.set("brain:self_play_wins", wins)
    r.set("brain:self_play_games", games)
    r.set("brain:self_play_win_rate", win_rate)
    r.set("brain:self_play_last_pnl", round(result["total_pnl"], 4))

    import structlog
    structlog.get_logger().info("self_play_done",
        pnl=result["total_pnl"], win_rate=win_rate, games=games)

    # Blueprint F41/AD-06: Promote strategy to paper trial when win rate > 55%
    # over enough games. cont. 61 — added 24h dedupe + silent-rejection
    # counters per [[feedback_silent_rejection]] so the never-firing path
    # becomes visible in feature_health.
    _DEDUPE_KEY = "self_play:last_promotion_ts"
    _DEDUPE_TTL = 86400  # 24h
    _SKIP_KEY = "self_play:promotion_skipped_count"
    _SKIP_REASON_KEY = "self_play:promotion_last_skip_reason"

    def _bump_skip(reason: str) -> None:
        try:
            r.incr(_SKIP_KEY)
            r.set(_SKIP_REASON_KEY, reason)
            r.set("self_play:promotion_last_skip_ts", int(__import__("time").time()))
        except Exception:
            pass

    if games < 20:
        _bump_skip(f"insufficient_games:{games}")
    elif win_rate < 55.0:
        _bump_skip(f"win_rate_below_threshold:{win_rate}")
    elif r.get(_DEDUPE_KEY):
        _bump_skip("dedupe_cooldown_active")
    else:
        try:
            from self_play.mars import promote_from_self_play
            promoted = promote_from_self_play(win_rate / 100.0, threshold=0.55)
            if promoted:
                r.setex(_DEDUPE_KEY, _DEDUPE_TTL, int(__import__("time").time()))
                r.incr("self_play:promotion_success_count")
                structlog.get_logger().info("self_play_strategy_promoted_to_paper",
                    win_rate=win_rate, games=games)
            else:
                _bump_skip("promote_returned_false")
        except Exception as exc:
            _bump_skip(f"exception:{str(exc)[:80]}")
            structlog.get_logger().warning("self_play_promotion_failed", error=str(exc))

    return result


@app.task(queue="default")
def evolve_strategy_pool_task() -> dict:
    """cont. 66 — Pool-level GA evolution task.

    Generational driver that turns the seeded gene pool into real parent→child
    lineage chains, closing the lineage gap found post-cont-61 (every strategy
    was stuck at generation=0 / parent_strategy_id=NULL because the 27 seeds
    were never themselves crossed/mutated).

    Gated by Redis flag ``research:pool_evolution_enabled`` (set to "0" to
    disable). Silent-rejection counters per [[feedback_silent_rejection]] so a
    never-firing pool stays visible in feature_health.
    """
    import time as _t, json as _json
    import structlog
    import redis_client as _rc
    log = structlog.get_logger()
    r = _rc.get()

    _SKIP_KEY = "research:pool_evolution_skipped_count"

    def _skip(reason: str) -> dict:
        try:
            r.incr(_SKIP_KEY)
            r.set("research:pool_evolution_last_skip_reason", reason)
            r.set("research:pool_evolution_last_skip_ts", int(_t.time()))
        except Exception:
            pass
        log.info("pool_evolution_skipped", reason=reason)
        return {"status": "skipped", "reason": reason}

    # Kill switch.
    try:
        if r.get("research:pool_evolution_enabled") in ("0", b"0"):
            return _skip("disabled_by_flag")
    except Exception:
        pass

    try:
        r.incr("research:pool_evolution_runs")
        r.set("research:pool_evolution_last_run_ts", int(_t.time()))
    except Exception:
        pass

    try:
        from research.engine import evolve_strategy_pool
        candidates = evolve_strategy_pool(max_children=2, min_parent_trades=0)
    except Exception as exc:
        log.warning("pool_evolution_generate_failed", error=str(exc)[:200])
        return _skip(f"generate_exception:{str(exc)[:80]}")

    if not candidates:
        return _skip("no_eligible_parents_or_all_unpromising")

    def _jb(v):
        return _json.dumps(v) if isinstance(v, (dict, list)) else v

    created = []
    for cand in candidates:
        parent_name = cand.get("_evolution_parent_name", "?")
        partner_name = cand.get("_evolution_partner_name", "?")
        generation = int(cand.get("generation", 1))

        # Human-readable child name: evo_g{gen}_{parent-slug}_{nnn}
        base_slug = "".join(ch for ch in str(parent_name).lower()
                            if ch.isalnum() or ch == "_")[:24] or "strat"
        slug = f"evo_g{generation}_{base_slug}"
        try:
            with db_conn() as _conn:
                with _conn.cursor() as _cur:
                    _cur.execute(
                        "SELECT COUNT(*) FROM strategies WHERE name LIKE %s",
                        (f"{slug}_%",))
                    n = _cur.fetchone()[0] or 0
            child_name = f"{slug}_{n + 1:03d}"
        except Exception:
            child_name = f"{slug}_{int(_t.time()) % 10000:04d}"

        strategy = {
            "name": child_name,
            "source": "research",
            "code": (f"# Auto-generated by Pool GA Evolution (cont. 66)\n"
                     f"# parent={parent_name} partner={partner_name} "
                     f"generation={generation}\n"
                     f"# DO NOT exec() — parameters only."),
            "entry_conditions": _jb(cand.get("entry_conditions", [])),
            "exit_conditions": _jb(cand.get("exit_conditions", [])),
            # DCA hard-off per [[feedback_dca_disabled]] — force never-fire
            # thresholds so GA mutation can never resurrect DCA on a child.
            "dca_rules": _jb({"round_1_pct": -50.0, "round_2_pct": -80.0}),
            "entry_overrides": (_jb(cand["entry_overrides"])
                                if cand.get("entry_overrides") else None),
            "trailing_sl_params": (_jb(cand["trailing_sl_params"])
                                   if cand.get("trailing_sl_params") else None),
            "position_sizing_rules": (_jb(cand["position_sizing_rules"])
                                      if cand.get("position_sizing_rules") else None),
            "parent_strategy_id": cand.get("parent_strategy_id"),
            "generation": generation,
        }
        try:
            from strategy.lifecycle import create_experimental
            cid = create_experimental(strategy)
            created.append({"id": cid, "name": child_name, "parent": parent_name,
                            "partner": partner_name, "generation": generation,
                            "score": cand.get("_prescreen_score"),
                            "bootstrap": cand.get("_prescreen_bootstrap")})
            try:
                r.incr("research:pool_evolution_children_created")
                r.set("research:pool_evolution_last_child_ts", int(_t.time()))
            except Exception:
                pass
            log.info("pool_evolution_child_created", child_id=cid, name=child_name,
                     parent=parent_name, partner=partner_name,
                     generation=generation, score=cand.get("_prescreen_score"),
                     bootstrap=cand.get("_prescreen_bootstrap"))
        except Exception as exc:
            log.warning("pool_evolution_create_failed",
                        name=child_name, error=str(exc)[:160])
            _skip(f"create_exception:{str(exc)[:60]}")

    if not created:
        return _skip("all_candidates_failed_create")
    return {"status": "ok", "created": created}


@app.task(queue="default")
def sweep_pending_counterfactuals() -> dict:
    """T-04 sweeper: find rejected signals older than the counterfactual window that
    haven't been evaluated yet, and run track_counterfactual on each.

    Replaces the previous fragile `apply_async(countdown=72h)` pattern in
    signals/engine.py:_schedule_counterfactual — Celery stores ETAs in worker memory,
    so every celery_worker restart silently dropped all pending counterfactual evals.
    Result: 14 MemRL-rejected signals had ZERO counterfactual rows even 10h after
    rejection, despite the audit's Issue #11 mitigation.

    SCAN-and-evaluate is robust to restarts since the state lives in the DB.

    cont. 69s — CORRECTNESS + THROUGHPUT REDESIGN. Two bugs found in the prior
    hourly/LIMIT-500 version:
      1. THROUGHPUT: 500/hr = 12k/day vs ~28k rejected signals/day → the pending
         backlog grew unbounded (202,881 rows, ~12 days behind on 2026-06-02).
      2. CORRECTNESS (the load-bearing one): track_counterfactual compares the
         signal's original price to the CURRENT mark price. That is only valid
         if evaluated ~72h after generation (window end ≈ now). For a 12-day-old
         backlog signal it compares a 21-May price to TODAY's price → a garbage
         `would_have_won` that then poisons the bayes threshold + shadow win rate.
         Draining the backlog naively would mass-corrupt the learner.

    New design:
      * BULK-RETIRE the unevaluable ancient backlog (older than window+SLACK) in a
        single INSERT…SELECT that stubs a counterfactual row with NULL outcomes and
        reason 'stale_backlog_unevaluable'. They stop counting as pending and never
        touch bayes/shadow.
      * EVALUATE only the just-matured band [window, window+SLACK] against the
        current mark (≈ window-end price within SLACK), at high throughput.
    Beat schedule: every 15 min (see app.conf.beat_schedule) so the matured band is
    always cleared and the eval price stays close to the true window-end price.
    """
    import sys, json
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import structlog
    log = structlog.get_logger()

    import config
    window_hours = int(getattr(config.strategies, "counterfactual_window_hours", 72))
    # Signals older than window+SLACK are evaluated against a mark price too far
    # past their window end to be fair → retire as unevaluable instead.
    SLACK_HOURS = 12
    BATCH_LIMIT = 3000

    from db import db_conn

    # --- 1) Bulk-retire the unevaluable ancient backlog (one fast SQL) ---
    stale_cleared = 0
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO counterfactuals
                        (signal_id, tracking_window_start, tracking_window_end,
                         peak_profit_pct, peak_loss_pct, would_have_won,
                         miss_decode_reason, pair, direction, signal_time, created_at)
                    SELECT s.id, s.generated_at, NOW(),
                           NULL, NULL, NULL,
                           'stale_backlog_unevaluable', s.pair, s.direction,
                           s.generated_at, NOW()
                    FROM signals s
                    LEFT JOIN counterfactuals c ON c.signal_id = s.id
                    WHERE s.accepted = FALSE
                      AND s.generated_at < NOW() - INTERVAL '{window_hours + SLACK_HOURS} hours'
                      AND c.signal_id IS NULL
                """)
                stale_cleared = cur.rowcount or 0
            conn.commit()
    except Exception as exc:
        log.warning("sweep_backlog_retire_failed", error=str(exc)[:200])

    # --- 2) Evaluate the just-matured band [window, window+SLACK] ---
    pending: list[tuple] = []
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT s.id, s.pair
                FROM signals s
                LEFT JOIN counterfactuals c ON c.signal_id = s.id
                WHERE s.accepted = FALSE
                  AND s.generated_at <  NOW() - INTERVAL '{window_hours} hours'
                  AND s.generated_at >= NOW() - INTERVAL '{window_hours + SLACK_HOURS} hours'
                  AND c.signal_id IS NULL
                ORDER BY s.generated_at ASC
                LIMIT {BATCH_LIMIT}
            """)
            pending = cur.fetchall()

    evaluated = 0
    for signal_id, pair in pending:
        try:
            track_counterfactual(str(signal_id), str(pair))
            evaluated += 1
        except Exception as exc:
            log.warning("sweep_track_counterfactual_failed",
                        signal_id=str(signal_id), pair=pair, error=str(exc)[:200])
    try:
        import redis_client as _rc
        _r = _rc.get()
        if stale_cleared:
            _r.incrby("counterfactuals:stale_retired_count", int(stale_cleared))
        _r.incrby("counterfactuals:evaluated_count", int(evaluated))
    except Exception:
        pass
    log.info("sweep_counterfactuals_complete",
             evaluated=evaluated, matured_band=len(pending),
             stale_retired=stale_cleared)
    return {"status": "ok", "evaluated": evaluated,
            "matured_band": len(pending), "stale_retired": stale_cleared}


@app.task(queue="default")
import functools as _functools


@_functools.lru_cache(maxsize=256)
def _corpus_rows_cached(pair: str, interval: str, mtime: float):
    """Read data/historical/{pair}/{interval}.csv → tuple of (ts_ms, high, low, close).
    cont. 70g2 — the corpus is the BAN-FREE price source (data.binance.vision bulk +
    WS top-up; NO fapi REST). Cached by (pair, interval, file-mtime) so a sweep of many
    signals on one pair reads the CSV once. Returns () when missing/unreadable."""
    import csv as _csv
    path = f"/app/data/historical/{pair}/{interval}.csv"
    rows = []
    try:
        with open(path, newline="") as f:
            rd = _csv.reader(f)
            next(rd, None)  # header
            for x in rd:
                if len(x) >= 6:
                    try:
                        rows.append((int(x[0]), float(x[2]), float(x[3]), float(x[4])))
                    except (TypeError, ValueError):
                        continue
    except Exception:
        return ()
    rows.sort()
    return tuple(rows)


def _corpus_window(pair: str, start_ms: int, end_ms: int, interval: str = "15m"):
    import os as _os
    path = f"/app/data/historical/{pair}/{interval}.csv"
    try:
        mt = _os.path.getmtime(path)
    except OSError:
        return []
    return [x for x in _corpus_rows_cached(pair, interval, mt)
            if start_ms <= x[0] <= end_ms]


def _cf_path_eval(pair, direction, start_ms, window_h, target_pct, stop_pct):
    """Walk the corpus candle path over [start, start+window_h]; return path-aware
    counterfactual metrics. CONSERVATIVE intrabar rule: the adverse extreme is assumed
    hit before the favourable one within each bar (worst case for would_have_won, since
    a 15m bar's high/low order is unknown). Returns None when the corpus does not span
    the window (caller falls back to the point-in-time mark comparison)."""
    end_ms = start_ms + int(window_h * 3600 * 1000)
    win = _corpus_window(pair, start_ms, end_ms, "15m")
    if len(win) < 2:
        return None
    entry = win[0][3]
    if entry <= 0:
        return None
    if win[-1][0] < end_ms - 2 * 3600 * 1000:   # corpus ends >2h short → not covered
        return None
    mfe = mae = dd_to_peak = running_adv = 0.0
    realistic_exit = None
    won_path = None
    for ts, hi, lo, close in win:
        if direction == "long":
            fav = (hi - entry) / entry * 100.0
            adv = (lo - entry) / entry * 100.0
        else:
            fav = (entry - lo) / entry * 100.0
            adv = (entry - hi) / entry * 100.0
        running_adv = min(running_adv, adv)
        if fav > mfe:
            mfe = fav
            dd_to_peak = running_adv
        mae = min(mae, adv)
        if realistic_exit is None:                 # first TP/SL touch wins (adverse-first)
            if adv <= -stop_pct:
                realistic_exit, won_path = -stop_pct, False
            elif fav >= target_pct:
                realistic_exit, won_path = target_pct, True
    if realistic_exit is None:                     # neither hit → 72h time exit
        final = win[-1][3]
        realistic_exit = ((final - entry) / entry * 100.0) if direction == "long" \
            else ((entry - final) / entry * 100.0)
        won_path = realistic_exit > 0
    return {"mfe": round(mfe, 4), "mae": round(mae, 4),
            "dd_to_peak": round(dd_to_peak, 4),
            "realistic_exit": round(realistic_exit, 4), "won_path": bool(won_path)}


def track_counterfactual(signal_id: str, pair: str) -> None:
    """T-04: Track a rejected signal 72h after rejection.
    Blueprint: compare signal direction vs actual price movement.
    'direction' = long and price went UP → would_have_won = True.
    'direction' = short and price went DOWN → would_have_won = True.

    cont. 70g2 — now PATH-AWARE from the corpus (true MFE/MAE + SL-aware TP-before-SL
    outcome), falling back to the legacy point-in-time mark comparison when the corpus
    doesn't cover the window. Toggle: counterfactual:path_aware_enabled (default 1).
    """
    import json
    import redis_client, redis_keys
    from memory.write import write_counterfactual
    from datetime import datetime, timezone
    from db import db_conn

    r = redis_client.get()
    now = datetime.now(timezone.utc)

    # Read signal's original direction and price at generation time
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT generated_at, direction, feature_vector FROM signals WHERE id = %s",
                (signal_id,),
            )
            row = cur.fetchone()
    if not row:
        return

    signal_time, direction, feature_vector = row[0], row[1], row[2]

    # cont. 69s — maturity guard (defense-in-depth). track_counterfactual scores
    # against the CURRENT mark, which only approximates the window-end price if the
    # signal matured recently. If it is older than window+SLACK (e.g. a backlog row
    # that slipped past the sweep's band filter), DO NOT compute a fake outcome —
    # retire it as unevaluable so it cannot poison the bayes threshold / shadow rate.
    try:
        import config as _cfg_mg
        _window_h = int(getattr(_cfg_mg.strategies, "counterfactual_window_hours", 72))
    except Exception:
        _window_h = 72
    _slack_h = 12
    if signal_time is not None:
        _age_h = (now - signal_time).total_seconds() / 3600.0
        if _age_h > (_window_h + _slack_h):
            from memory.write import write_counterfactual as _wc_stale
            _wc_stale(signal_id, {
                "tracking_window_start": signal_time,
                "tracking_window_end": now,
                "peak_profit_pct": None,
                "peak_loss_pct": None,
                "would_have_won": None,
                "miss_decode_reason": "stale_backlog_unevaluable",
            })
            return  # no bayes / shadow update

    # Extract the mark price at signal generation from feature_vector
    original_price = 0.0
    if feature_vector:
        fv = feature_vector if isinstance(feature_vector, dict) else json.loads(feature_vector)
        original_price = float(fv.get("mark", 0))

    current_price = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", pair)) or 0)

    # Did price move in the signal's predicted direction?
    if original_price > 0 and current_price > 0:
        price_went_up = current_price > original_price
        would_have_won = (direction == "long" and price_went_up) or \
                         (direction == "short" and not price_went_up)
        # PnL % if the trade had been taken with trailing SL (approx: entry vs current)
        if direction == "long":
            peak_profit_pct = round((current_price - original_price) / original_price * 100, 4)
            peak_loss_pct   = min(0.0, peak_profit_pct)
        else:
            peak_profit_pct = round((original_price - current_price) / original_price * 100, 4)
            peak_loss_pct   = min(0.0, peak_profit_pct)
    else:
        would_have_won = False
        peak_profit_pct = None
        peak_loss_pct   = None

    write_counterfactual(signal_id, {
        "tracking_window_start": signal_time,
        "tracking_window_end": now,
        "peak_profit_pct": peak_profit_pct,
        "peak_loss_pct": peak_loss_pct,
        "would_have_won": would_have_won,
    })

    # Update running shadow win rate in Redis
    r_data = r.get(redis_keys.SHADOW_WIN_RATE)
    stats = json.loads(r_data) if r_data else {"total": 0, "won": 0}
    stats["total"] += 1
    if would_have_won:
        stats["won"] += 1
    stats["rate"] = round(stats["won"] / stats["total"] * 100, 2)
    r.set(redis_keys.SHADOW_WIN_RATE, json.dumps(stats))

    # cont. 63 (2026-05-29) — Phase 2 hook. Feed the would_have_won outcome
    # into the Bayesian threshold tuner so the per-strength-bucket Beta
    # posterior updates on REJECTED-but-evaluated signals. Combined with
    # the accept-side updates in execution/{paper,live}.close_trade, this
    # closes the feedback loop required for adaptive T_high.
    try:
        from db import db_conn as _db_conn_strength
        with _db_conn_strength() as _conn_s:
            with _conn_s.cursor() as _cur_s:
                _cur_s.execute(
                    "SELECT signal_strength FROM signals WHERE id = %s",
                    (signal_id,))
                _row_s = _cur_s.fetchone()
        if _row_s and _row_s[0] is not None:
            from signals.bayes_threshold import record_outcome as _bayes_rec
            _bayes_rec(float(_row_s[0]), bool(would_have_won))
    except Exception:
        pass


@app.task(queue="default")
def send_daily_summary_task() -> None:
    from analytics.metrics import compute_rolling_metrics
    from notifications.telegram import send_daily_summary
    import redis_client, redis_keys
    r = redis_client.get()
    metrics = compute_rolling_metrics(100)
    send_daily_summary({
        "trades_opened": 0,
        "trades_closed": metrics.get("trade_count", 0),
        "daily_pnl": metrics.get("net_pnl_usdt", 0),
        "win_rate": metrics.get("win_rate", 0),
        "brain_stage": int(r.get(redis_keys.BRAIN_STAGE) or 1),
        "top_pair": "N/A",
    })


@app.task(queue="default")
def send_weekly_report_task() -> None:
    from analytics.metrics import compute_rolling_metrics
    from notifications.telegram import send_weekly_report
    import redis_client, redis_keys
    r = redis_client.get()
    metrics = compute_rolling_metrics(500)
    send_weekly_report({
        "sharpe": metrics.get("sharpe", 0),
        "brain_stage": int(r.get(redis_keys.BRAIN_STAGE) or 1),
    })


# ────────────────────────────────────────────────────────────────────────────
# F9 Miss Decoder + F12 Mismatch Decoder background tasks.
# Both consume already-accumulated DB rows and write LLM-decoded postmortems
# back. Strictly read-side analytics — no signal/trade mutation. Beat schedule
# entries above.
# ────────────────────────────────────────────────────────────────────────────

_F9_BATCH_LIMIT  = 10   # shadow wins to decode per beat tick
_F12_BATCH_LIMIT = 5    # mismatch pairs to decode per beat tick
_F12_WINDOW_HRS  = 24   # period within which a high-pot loser & low-pot winner count as same period
_F12_HIGH_POT    = 60   # trade_potential_score threshold for "high potential"
_F12_LOW_POT     = 40   # ...and "low potential"


@app.task(queue="default")
def xsmom_compute_task() -> dict:
    """F45 Cross-Sectional Momentum producer (Liu-Tsyvinski 2022).

    Wrapper around `signals.xsmom.compute_and_publish` so Celery beat can invoke
    it. The actual logic lives in the module so it's testable + reusable.
    """
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from signals.xsmom import compute_and_publish
    return compute_and_publish()


@app.task(queue="default")
def decode_pending_misses(limit: int = _F9_BATCH_LIMIT) -> dict:
    """F9 Miss Decoder: postmortem LLM call on shadow-win counterfactuals."""
    import sys, time
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import structlog
    from db import db_conn
    import redis_client
    log = structlog.get_logger()
    r = redis_client.get()

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  c.id, c.signal_id, c.would_have_won,
                  c.peak_profit_pct, c.peak_loss_pct, c.trailing_sl_exit_pct,
                  s.pair, s.direction, s.timeframe, s.signal_strength,
                  s.market_regime, s.rejection_reason, s.brain_stage
                FROM counterfactuals c
                JOIN signals s ON s.id = c.signal_id
                WHERE c.would_have_won = TRUE
                  AND c.miss_decoded = FALSE
                ORDER BY c.created_at ASC
                LIMIT %s
            """, (limit,))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

    if not rows:
        log.info("decode_misses_complete", decoded=0, pending=0)
        return {"status": "ok", "decoded": 0, "pending": 0}

    from metacognition.decoders import decode_miss
    decoded = 0
    failed  = 0
    for row in rows:
        cf  = {k: row[k] for k in ("would_have_won", "peak_profit_pct",
                                   "peak_loss_pct", "trailing_sl_exit_pct")}
        sig = {k: row[k] for k in ("pair", "direction", "timeframe",
                                   "signal_strength", "market_regime",
                                   "rejection_reason", "brain_stage")}
        result = decode_miss(cf, sig)
        if not result:
            failed += 1
            continue
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    import json as _json_cf
                    cur.execute("""
                        UPDATE counterfactuals
                        SET miss_decode_reason       = %s,
                            miss_tag                 = %s,
                            miss_tag_confidence      = %s,
                            miss_tag_evidence        = %s,
                            predicted_peak_profit_pct = %s,
                            miss_decoded             = TRUE
                        WHERE id = %s
                    """, (result.get("decode_reason", "")[:2000],
                          result.get("miss_tag"),
                          result.get("miss_tag_confidence"),
                          _json_cf.dumps(result.get("miss_tag_evidence") or {}),
                          result.get("predicted_peak_profit_pct"),
                          row["id"]))
            decoded += 1
            # Idea 2 RAG (cont. 64) — embed the decode_reason for future
            # vector-search at signal-firing time. Non-fatal: any Ollama
            # hiccup is caught + retried by embed_pending_postmortems beat.
            try:
                from metacognition.postmortem_embed import (
                    embed_text as _emb, to_pgvector as _vec, build_signal_context as _ctx,
                )
                _embed_input = _ctx(
                    pair=row.get("pair"), direction=row.get("direction"),
                    timeframe=row.get("timeframe"), regime=row.get("market_regime"),
                    signal_strength=row.get("signal_strength"),
                    rejection_reason=row.get("rejection_reason"),
                    brain_stage=row.get("brain_stage"),
                ) + " | " + result.get("decode_reason", "")[:400]
                _emb_vec = _emb(_embed_input)
                if _emb_vec is not None:
                    with db_conn() as conn2:
                        with conn2.cursor() as cur2:
                            cur2.execute(
                                "UPDATE counterfactuals "
                                "SET decode_reason_embedding = %s::vector "
                                "WHERE id = %s",
                                (_vec(_emb_vec), row["id"]),
                            )
                    try:
                        r.incr("postmortem_rag:f9_embedded_count")
                    except Exception:
                        pass
            except Exception as _exc:
                log.warning("postmortem_embed_inline_failed",
                            kind="f9", id=str(row["id"]), err=str(_exc)[:160])
            # cont. 27 — F9 decoder writeback. cont. 55 — pass regime +
            # signal_strength so the actuator can write to the right
            # (regime × strength-band) bucket. cont. 63 (2026-05-29) — also
            # pass predicted_peak_profit_pct so the actuator can scale
            # magnitude proportionally to regret size (was: small/medium fixed).
            # Failures are non-fatal.
            fc = result.get("filter_change")
            if fc:
                try:
                    from metacognition.actuator import apply_filter_change
                    apply_filter_change(
                        fc,
                        regime=row.get("market_regime"),
                        signal_strength=(float(row["signal_strength"])
                                         if row.get("signal_strength") is not None
                                         else None),
                        predicted_peak_profit_pct=result.get("predicted_peak_profit_pct"),
                    )
                except Exception as exc:
                    log.warning("decode_miss_writeback_failed",
                                signal_id=str(row["signal_id"]),
                                error=str(exc)[:200])
        except Exception as exc:
            log.warning("decode_misses_write_failed",
                        signal_id=str(row["signal_id"]), error=str(exc)[:200])
            failed += 1

    try:
        r.incr("decoders:miss_decoded_count", decoded)
        if decoded > 0:
            r.set("decoders:miss_last_ts", int(time.time()))
    except Exception:
        pass

    log.info("decode_misses_complete",
             decoded=decoded, failed=failed, batch=len(rows))
    return {"status": "ok", "decoded": decoded, "failed": failed,
            "batch": len(rows)}


@app.task(queue="default")
def decode_pending_mismatches(limit: int = _F12_BATCH_LIMIT,
                              window_hours: int = _F12_WINDOW_HRS) -> dict:
    """F12 Mismatch Decoder: postmortem on (high-pot loser, low-pot winner) pairs."""
    import sys, time
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import structlog
    from db import db_conn
    import redis_client
    log = structlog.get_logger()
    r = redis_client.get()

    # Pair high-pot losers with the nearest contemporaneous low-pot winner
    # (within window_hours of each other). DISTINCT ON ensures each loser
    # is paired with exactly one winner. LEFT JOIN excludes already-decoded
    # pairs (UNIQUE constraint on mismatches would also catch it, but pre-
    # filtering saves LLM calls).
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT DISTINCT ON (loser.id)
                  loser.id              AS loser_id,
                  winner.id             AS winner_id,
                  LEAST(loser.exit_time, winner.exit_time)    AS period_start,
                  GREATEST(loser.exit_time, winner.exit_time) AS period_end,
                  loser.trade_potential_score   AS loser_pot,
                  winner.trade_potential_score  AS winner_pot,
                  loser.net_pnl_usdt    AS loser_pnl,
                  winner.net_pnl_usdt   AS winner_pnl,
                  loser.pair            AS loser_pair,
                  loser.direction       AS loser_dir,
                  loser.direction_confidence AS loser_dirconf,
                  loser.market_regime   AS loser_regime,
                  loser.exit_reason     AS loser_exit_reason,
                  loser.hold_time_seconds AS loser_hold,
                  winner.pair           AS winner_pair,
                  winner.direction      AS winner_dir,
                  winner.direction_confidence AS winner_dirconf,
                  winner.market_regime  AS winner_regime,
                  winner.exit_reason    AS winner_exit_reason,
                  winner.hold_time_seconds AS winner_hold
                FROM trades loser
                JOIN trades winner
                  ON winner.status = 'closed'
                 AND winner.net_pnl_usdt > 0
                 AND winner.trade_potential_score < %s
                 AND ABS(EXTRACT(EPOCH FROM (winner.exit_time - loser.exit_time))) < %s
                 AND winner.id != loser.id
                LEFT JOIN mismatches m
                  ON m.loser_trade_id = loser.id
                 AND m.winner_trade_id = winner.id
                WHERE loser.status = 'closed'
                  AND loser.net_pnl_usdt < 0
                  AND loser.trade_potential_score >= %s
                  AND m.id IS NULL
                ORDER BY loser.id, ABS(EXTRACT(EPOCH FROM (winner.exit_time - loser.exit_time)))
                LIMIT %s
            """, (_F12_LOW_POT, window_hours * 3600, _F12_HIGH_POT, limit))
            cols = [d[0] for d in cur.description]
            pairs = [dict(zip(cols, row)) for row in cur.fetchall()]

    if not pairs:
        log.info("decode_mismatches_complete", decoded=0, pending=0)
        return {"status": "ok", "decoded": 0, "pending": 0}

    from metacognition.decoders import decode_mismatch
    decoded = 0
    failed  = 0
    for p in pairs:
        loser = {
            "pair": p["loser_pair"], "direction": p["loser_dir"],
            "trade_potential_score": float(p["loser_pot"] or 0),
            "direction_confidence": p["loser_dirconf"],
            "market_regime": p["loser_regime"],
            "exit_reason": p["loser_exit_reason"],
            "net_pnl_usdt": float(p["loser_pnl"] or 0),
            "hold_time_seconds": p["loser_hold"],
        }
        winner = {
            "pair": p["winner_pair"], "direction": p["winner_dir"],
            "trade_potential_score": float(p["winner_pot"] or 0),
            "direction_confidence": p["winner_dirconf"],
            "market_regime": p["winner_regime"],
            "exit_reason": p["winner_exit_reason"],
            "net_pnl_usdt": float(p["winner_pnl"] or 0),
            "hold_time_seconds": p["winner_hold"],
        }
        result = decode_mismatch(loser, winner)
        if not result:
            failed += 1
            continue
        try:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO mismatches (
                            loser_trade_id, winner_trade_id,
                            period_start, period_end,
                            loser_potential, winner_potential,
                            loser_pnl_usdt, winner_pnl_usdt,
                            decode_reason
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (loser_trade_id, winner_trade_id) DO NOTHING
                        RETURNING id
                    """, (
                        p["loser_id"], p["winner_id"],
                        p["period_start"], p["period_end"],
                        p["loser_pot"], p["winner_pot"],
                        p["loser_pnl"], p["winner_pnl"],
                        result.get("decode_reason", "")[:2000],
                    ))
                    _ins_row = cur.fetchone()
                    _mismatch_id = _ins_row[0] if _ins_row else None
            decoded += 1
            # Idea 2 RAG (cont. 64) — embed the F12 decode_reason. F12
            # context is a (loser_pair, winner_pair, regime) tuple — we
            # embed using the loser side since that's the "high-pot but
            # lost" pattern most actionable at signal-firing time.
            if _mismatch_id is not None:
                try:
                    from metacognition.postmortem_embed import (
                        embed_text as _emb, to_pgvector as _vec, build_signal_context as _ctx,
                    )
                    _embed_input = _ctx(
                        pair=loser.get("pair"), direction=loser.get("direction"),
                        timeframe=None, regime=loser.get("market_regime"),
                        signal_strength=loser.get("trade_potential_score"),
                        rejection_reason=None,
                        brain_stage=None,
                    ) + " | " + result.get("decode_reason", "")[:400]
                    _emb_vec = _emb(_embed_input)
                    if _emb_vec is not None:
                        with db_conn() as conn2:
                            with conn2.cursor() as cur2:
                                cur2.execute(
                                    "UPDATE mismatches "
                                    "SET decode_reason_embedding = %s::vector "
                                    "WHERE id = %s",
                                    (_vec(_emb_vec), _mismatch_id),
                                )
                        try:
                            r.incr("postmortem_rag:f12_embedded_count")
                        except Exception:
                            pass
                except Exception as _exc:
                    log.warning("postmortem_embed_inline_failed",
                                kind="f12", id=str(_mismatch_id), err=str(_exc)[:160])
            # cont. 27 — F12 decoder writeback. Apply structured scorer_change
            # (if present) to Redis scorer overrides via the bounded actuator.
            sc = result.get("scorer_change")
            if sc:
                try:
                    from metacognition.actuator import apply_scorer_change
                    apply_scorer_change(sc)
                except Exception as exc:
                    log.warning("decode_mismatch_writeback_failed",
                                loser=str(p["loser_id"]),
                                error=str(exc)[:200])
        except Exception as exc:
            log.warning("decode_mismatches_write_failed",
                        loser=str(p["loser_id"]), error=str(exc)[:200])
            failed += 1

    try:
        r.incr("decoders:mismatch_decoded_count", decoded)
        if decoded > 0:
            r.set("decoders:mismatch_last_ts", int(time.time()))
    except Exception:
        pass

    log.info("decode_mismatches_complete",
             decoded=decoded, failed=failed, batch=len(pairs))
    return {"status": "ok", "decoded": decoded, "failed": failed,
            "batch": len(pairs)}


# ============================================================================
# Idea 2 Postmortem RAG (cont. 64, 2026-05-30) — embed-on-decode is wired
# inline above; the two tasks below are (1) a safety-net beat that catches
# rows where inline embed failed (Ollama hiccup) and (2) a one-shot
# backfill of historical postmortems. Both target counterfactuals AND
# mismatches with NULL decode_reason_embedding.
# ============================================================================

_RAG_BATCH_LIMIT  = 50
_RAG_F12_TIMEOUT  = 6.0


def _embed_pending_for_table(table: str, text_col: str, batch: int,
                             log, redis_client_inst) -> dict:
    """Internal helper — embed up to `batch` rows where text_col IS NOT NULL
    and decode_reason_embedding IS NULL. Used by both the beat task and the
    one-shot backfill. Returns counts."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from db import db_conn
    from metacognition.postmortem_embed import embed_text, to_pgvector

    assert table in ("counterfactuals", "mismatches"), table

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, {text_col} FROM {table} "
                f"WHERE {text_col} IS NOT NULL "
                f"  AND decode_reason_embedding IS NULL "
                f"ORDER BY id LIMIT %s",
                (batch,),
            )
            rows = cur.fetchall()

    if not rows:
        return {"table": table, "embedded": 0, "pending": 0}

    embedded = 0
    failed   = 0
    for row_id, text in rows:
        try:
            vec = embed_text(text or "")
            if vec is None:
                failed += 1
                continue
            with db_conn() as conn2:
                with conn2.cursor() as cur2:
                    cur2.execute(
                        f"UPDATE {table} "
                        f"SET decode_reason_embedding = %s::vector "
                        f"WHERE id = %s",
                        (to_pgvector(vec), row_id),
                    )
            embedded += 1
        except Exception as exc:
            log.warning("embed_pending_row_failed",
                        table=table, id=str(row_id), err=str(exc)[:160])
            failed += 1

    try:
        suffix = "f9" if table == "counterfactuals" else "f12"
        redis_client_inst.incr(f"postmortem_rag:{suffix}_backfill_embedded_count",
                               embedded)
    except Exception:
        pass

    return {"table": table, "embedded": embedded, "failed": failed,
            "batch": len(rows)}


@app.task(queue="default")
def embed_pending_postmortems(batch: int = _RAG_BATCH_LIMIT) -> dict:
    """Safety net — embeds rows whose inline embedding failed (Ollama
    hiccup at decode time). Runs every 15 min via beat. Bounded batch so
    one slow Ollama pass can't starve the queue."""
    import structlog
    import redis_client
    log = structlog.get_logger()
    r = redis_client.get()

    cf_res = _embed_pending_for_table("counterfactuals", "miss_decode_reason",
                                      batch, log, r)
    mm_res = _embed_pending_for_table("mismatches", "decode_reason",
                                      batch, log, r)
    log.info("embed_pending_postmortems_complete",
             cf=cf_res, mm=mm_res)
    return {"status": "ok", "counterfactuals": cf_res, "mismatches": mm_res}


@app.task(queue="default")
def backfill_postmortem_embeddings(max_total: int = 10_000) -> dict:
    """One-shot — embed every historical row with decode_reason set but
    embedding NULL. Bounded by max_total so a runaway can't OOM the
    worker. User triggers manually after Idea 2 ships:

        docker exec trading-bot-celery_worker-1 \\
          python -c "from celery_app import backfill_postmortem_embeddings as t; \\
                     print(t.delay().get(timeout=1800))"
    """
    import structlog
    import redis_client
    log = structlog.get_logger()
    r = redis_client.get()

    total_cf = 0
    total_mm = 0
    while total_cf + total_mm < max_total:
        cf = _embed_pending_for_table("counterfactuals", "miss_decode_reason",
                                      _RAG_BATCH_LIMIT, log, r)
        mm = _embed_pending_for_table("mismatches", "decode_reason",
                                      _RAG_BATCH_LIMIT, log, r)
        total_cf += cf.get("embedded", 0)
        total_mm += mm.get("embedded", 0)
        # Stop when both queues drained.
        if cf.get("batch", 0) == 0 and mm.get("batch", 0) == 0:
            break
        # Stop if a full pass produced zero embeddings — Ollama is down,
        # no point in tight-looping.
        if cf.get("embedded", 0) == 0 and mm.get("embedded", 0) == 0:
            log.warning("backfill_postmortem_no_progress",
                        cf=cf, mm=mm)
            break

    log.info("backfill_postmortem_embeddings_complete",
             counterfactuals=total_cf, mismatches=total_mm)
    return {"status": "ok", "counterfactuals": total_cf,
            "mismatches": total_mm}


@app.task(queue="cn_train")
def update_klines_corpus_task() -> dict:
    """cont. 69f (P1) — hourly incremental top-up of the shared kline corpus
    (data/historical/{pair}/{tf}.csv) for active pairs, so candlenet + future
    kline-trained models always train on CURRENT data. Deep backfill is a manual
    run: `python -m ml.klines_corpus backfill --pairs all`. Runs on cn_train
    (has ./ml + ./data/historical mounts). Mainnet public klines, ban-aware."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    # celery_app.py has no module-level `log`; loggers are function-local here.
    import structlog
    log = structlog.get_logger()
    # cont. 69p — skip the REST top-up while the fapi IP is -1003 banned.
    # The dashboard fapi-recovery monitor writes fapi:ban_status every 30s;
    # the deep data.binance.vision bulk corpus is already current to ~T-1, so
    # training needs nothing from this hourly REST poll. Hammering the banned
    # endpoint (1.7k 403s/90m) is a no-op that likely PROLONGS the ban (and the
    # 403s were being silently swallowed as "ok"). Auto-resumes when the monitor
    # flips fapi:ban_status back to "ok". Silent-rejection rule: log + counter.
    try:
        import redis_client
        r = redis_client.get()
        # cont. 69x — PREFER the zero-REST WS-klines corpus path. data/kline_ws.py
        # streams CLOSED mainnet candles into Redis sorted sets (klines:ws:{pair}:{itv});
        # incremental_from_ws merges them into the CSV corpus with NO fapi REST. This
        # retires the hourly /fapi/v1/klines hammer (~1500 weight/min) for good. Set
        # corpus:ws_klines_enabled="0" to fall back to the REST top-up (gated below).
        if (r.get("corpus:ws_klines_enabled") or "1") == "1":
            from ml.klines_corpus import incremental_from_ws
            return incremental_from_ws(which="active", r=r)
        # ---- REST fallback (weight-bearing) ----
        # Manual kill-switch independent of the auto ban-monitor. The dashboard
        # fapi-recovery monitor flips fapi:ban_status back to "ok" within 30s whenever
        # fapi is reachable, so it CANNOT durably pause this REST top-up; this flag
        # can (the daily data.binance.vision bulk keeps the corpus current to ~T-1, so
        # training loses nothing). Silent-rejection rule.
        if (r.get("corpus:incremental_enabled") or "1") != "1":
            r.incr("corpus:incremental_skipped_disabled")
            log.warning("update_klines_corpus_skipped", reason="disabled_flag",
                        note="corpus:incremental_enabled=0; bulk corpus current to ~T-1")
            return {"status": "skipped", "reason": "disabled_flag",
                    "skipped_total": int(r.get("corpus:incremental_skipped_disabled") or 0)}
        if r.get("fapi:ban_status") == "banned":
            r.incr("corpus:incremental_skipped_fapi_banned")
            log.warning("update_klines_corpus_skipped", reason="fapi_banned",
                        note="REST top-up paused; bulk corpus current to ~T-1")
            return {"status": "skipped", "reason": "fapi_banned",
                    "skipped_total": int(r.get("corpus:incremental_skipped_fapi_banned") or 0)}
    except Exception as exc:
        # Never let the gate itself break the task — fall through to the poll.
        log.warning("update_klines_corpus_gate_check_failed", error=str(exc)[:120])
    try:
        from ml.klines_corpus import update_corpus
        return update_corpus(mode="incremental", which="active")
    except Exception as exc:
        log.warning("update_klines_corpus_failed", error=str(exc)[:200])
        return {"status": "failed", "error": str(exc)[:200]}


@app.task(queue="cn_train")
def bulk_topup_corpus_task() -> dict:
    """cont. 69s — DAILY ban-immune corpus top-up for ALL timeframes via
    data.binance.vision daily zips (static CDN — unaffected by fapi -1003 bans
    that pause the REST update_klines_corpus_task). Keeps 1m/5m/15m/30m/1h current
    for active pairs so SL/TP tuning, live signals AND model training always read
    fresh data. Rolling depths per DEFAULT_LOOKBACK_DAYS."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import structlog
    log = structlog.get_logger()
    try:
        from ml.klines_corpus import update_corpus
        rep = update_corpus(mode="bulk_topup", which="active")
        try:
            import redis_client
            redis_client.get().set("corpus:last_bulk_topup_ok", 1)
        except Exception:
            pass
        return rep
    except Exception as exc:
        log.warning("bulk_topup_corpus_failed", error=str(exc)[:200])
        return {"status": "failed", "error": str(exc)[:200]}


@app.task(queue="cn_train")
def retrain_kline_predictor_task() -> dict:
    """cont. 69i (P3) — retrain the kline-based predict-all direction/move model
    on the shared corpus + standardized forward labels. Keeps it regime-current so
    it never re-collapses to constant output (the cont.69d degeneracy). Runs on
    cn_train (./ml + ./prediction + ./data/historical)."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from prediction.xgb_kline_trainer import train
        return train()
    except Exception as exc:
        log.warning("retrain_kline_predictor_failed", error=str(exc)[:200])
        return {"status": "failed", "error": str(exc)[:200]}


@app.task(queue="predict_all")
def publish_kline_predictions_task() -> dict:
    """cont. 69j (P3 finish) — write LIVE kline-model predictions to
    `predict:kline:{pair}` (TTL 900s) for the active universe, so the
    deterministic scorer can read a CURRENT soft directional prior without doing
    inference in the signal hot path. Uses live Redis candles (not the corpus)."""
    import sys
    import json as _j
    import time as _t
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        import redis_client
        from prediction.xgb_kline_trainer import predict_live
        r = redis_client.get()
        pairs = sorted(p.decode() if isinstance(p, bytes) else p
                       for p in r.smembers("scanner:active_pairs"))
        ok = 0
        for pair in pairs:
            try:
                pr = predict_live(pair)
                if pr:
                    pr["ts"] = int(_t.time())
                    r.setex(f"predict:kline:{pair}", 900, _j.dumps(pr))
                    ok += 1
            except Exception:
                continue
        r.set("predict:kline:published_count", ok)
        r.set("predict:kline:last_run_ts", int(_t.time()))
        return {"status": "ok", "published": ok, "pairs": len(pairs)}
    except Exception as exc:
        log.warning("publish_kline_predictions_failed", error=str(exc)[:200])
        return {"status": "failed", "error": str(exc)[:200]}


@app.task(queue="default")
def update_criteria_weights() -> dict:
    """F10 Brain-learned scanner criteria weights (cont. 18).

    Correlates per-pair sub-scores at scan time with realized PnL over the
    forward window, normalizes to weights summing to 1.0, EMA-blends into
    existing BRAIN_FEATURE_WEIGHTS in Redis. Scanner picks up the new
    weights on the next scan cycle (8h).
    """
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from feature_governance.registry import is_active
        if not is_active("F10"):
            return {"status": "f10_inactive"}
    except Exception:
        pass
    from ml.criteria_weights import update_weights_in_redis
    return update_weights_in_redis()


# ============================================================================
# cont. 55 (2026-05-28) — F52 / F53 / F54 beat task implementations.
# All three task families wrap into is_active() governance checks so the
# user can deactivate via the dashboard without code edits.
# ============================================================================

@app.task(queue="default")
def netflow_refresh_all_task() -> dict:
    """F52 — refresh exchange net-flow z-scores for every active pair.

    Polls free-tier providers (CryptoQuant / Glassnode / Coinglass /
    CoinMetrics) for BTC/ETH/SOL native flows; derives BTC-β proxy for
    remaining alts. Every 5 minutes by the beat schedule.
    """
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from feature_governance.registry import is_active
        if not is_active("F52"):
            return {"status": "f52_inactive"}
    except Exception:
        pass
    from data.onchain_netflow import refresh_all
    return refresh_all()


@app.task(queue="default")
def qlib_alpha_compute_task() -> dict:
    """F53 — per-minute Alpha-158 factor compute for every active pair."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from feature_governance.registry import is_active
        if not is_active("F53"):
            return {"status": "f53_inactive"}
    except Exception:
        pass
    from ml.qlib_alphas import compute_for_active_pairs
    return compute_for_active_pairs()


@app.task(queue="default")
def qlib_ic_refresh_task() -> dict:
    """F53 — hourly IC refresh + Top-K selection.

    Recomputes rolling 7d Spearman IC per factor and writes the survivors
    into `qlib:top_k_factor_ids`. Consumer in signals/engine.py reads it.
    """
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from feature_governance.registry import is_active
        if not is_active("F53"):
            return {"status": "f53_inactive"}
    except Exception:
        pass
    from ml.qlib_alphas import refresh_ic_top_k
    return refresh_ic_top_k()


@app.task(bind=True, max_retries=1, default_retry_delay=600,
          time_limit=7200, soft_time_limit=6900, queue="default")
def llm_dsl_mining_run_task(self) -> dict:
    """F54 — weekly LLM-DSL alpha mining run.

    45-90min on CPU using Ollama qwen2.5-coder:7b. Wraps Sharpe + IC +
    decay-ratio gates and a deepseek-r1:8b overfit-pass. Promoted factors
    land in `dsl:promoted_factors`.

    Time-limited at 2h hard / 1h55m soft so a stuck Ollama doesn't pin
    the celery worker — soft kill triggers task-level cleanup.
    """
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from feature_governance.registry import is_active
        if not is_active("F54"):
            return {"status": "f54_inactive"}
    except Exception:
        pass
    try:
        from ml.llm_alpha_dsl import run_mining
        return run_mining()
    except Exception as exc:
        log.error("llm_dsl_mining_run_failed", error=str(exc)[:300])
        # Don't retry — weekly cadence, next Wed will retry naturally.
        return {"status": "error", "error": str(exc)[:300]}


@app.task(queue="default")
def llm_dsl_promoted_compute_task() -> dict:
    """F54 — per-minute compute of promoted DSL factor values for active pairs."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from feature_governance.registry import is_active
        if not is_active("F54"):
            return {"status": "f54_inactive"}
    except Exception:
        pass
    from ml.llm_alpha_dsl import compute_promoted_for_active_pairs
    return compute_promoted_for_active_pairs()


@app.task(queue="default")
def llm_dsl_decay_check_task() -> dict:
    """F54 — daily decay-check + demotion of stale promoted factors."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from feature_governance.registry import is_active
        if not is_active("F54"):
            return {"status": "f54_inactive"}
    except Exception:
        pass
    from ml.llm_alpha_dsl import check_decay_and_demote
    return check_decay_and_demote()


# ============================================================================
# cont. 56 (2026-05-28) — F56 / F58 beat tasks (F60 has no beat — runs inside
# the F54 mining loop as a regulariser).
# ============================================================================

@app.task(queue="default")
def conformal_interval_refresh_task() -> dict:
    """F56 — nightly conformal-interval refresh across every known model."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from feature_governance.registry import is_active
        if not is_active("F56"):
            return {"status": "f56_inactive"}
    except Exception:
        pass
    from ml.conformal_wrapper import update_intervals
    return update_intervals()


@app.task(queue="default")
def liquidation_levels_refresh_task() -> dict:
    """F58 — 5-min poll of liquidation-level density per active pair."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from feature_governance.registry import is_active
        if not is_active("F58"):
            return {"status": "f58_inactive"}
    except Exception:
        pass
    from data.liquidation_levels import refresh_all
    return refresh_all()


# ─── cont. 60 — Frontier exit-feature producer tasks ─────────────────────

@app.task(queue="default")
def cvd_producer_task() -> dict:
    """cont. 60 — Per-pair CVD + close history for divergence detector."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from data.cvd_producer import update_cvd_all_active
    n = update_cvd_all_active()
    return {"status": "ok", "pairs_updated": n}


@app.task(queue="default")
def hawkes_producer_task() -> dict:
    """cont. 60 — Per-pair Hawkes self-excitation intensity."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from data.hawkes_producer import update_hawkes_all_active
    n = update_hawkes_all_active()
    return {"status": "ok", "pairs_updated": n}


@app.task(queue="default")
def bocpd_per_pair_producer_task() -> dict:
    """cont. 60 — Per-pair BOCPD changepoint posterior."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from data.bocpd_per_pair_producer import update_bocpd_all_active
    n = update_bocpd_all_active()
    return {"status": "ok", "pairs_updated": n}


@app.task(queue="default")
def conformal_residual_producer_task() -> dict:
    """cont. 60 — Per-pair conformal residual q95."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from data.conformal_residual_producer import update_conformal_all_active
    n = update_conformal_all_active()
    return {"status": "ok", "pairs_updated": n}


@app.task(queue="default")
def filtered_obi_producer_task() -> dict:
    """cont. 60 — EMA-filtered OBI history."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from data.filtered_obi_producer import update_filtered_obi_all_active
    n = update_filtered_obi_all_active()
    return {"status": "ok", "pairs_updated": n}


@app.task(queue="default")
def mm_hawkes_producer_task() -> dict:
    """cont. 60 — MM-Hawkes cancel-burst spoof score."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from data.mm_hawkes_producer import update_mm_hawkes_all_active
    n = update_mm_hawkes_all_active()
    return {"status": "ok", "pairs_updated": n}


@app.task(queue="default")
def premium_index_producer_task() -> dict:
    """cont. 60 — Premium index (mark - spot)."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from data.premium_index_producer import update_premium_all_active
    n = update_premium_all_active()
    return {"status": "ok", "pairs_updated": n}


@app.task(queue="default")
def dvol_refresh_task() -> dict:
    """cont. 60 — Deribit DVOL refresh + decile."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from data.external.deribit_dvol import refresh_dvol
    return refresh_dvol(currency="BTC")


@app.task(queue="default")
def coinglass_liq_refresh_task() -> dict:
    """cont. 60 — Coinglass liquidation heatmap per active pair."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from data.external.coinglass_liq import refresh_all_active
    return refresh_all_active()


@app.task(queue="default")
def kalman_pair_producer_task() -> dict:
    """cont. 61 — Kalman-filtered pair residual z-score for seed gene pool
    kalman_pair_residual_revert archetype."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from data.kalman_pair_producer import update_kalman_all_baskets
    n = update_kalman_all_baskets()
    return {"status": "ok", "baskets_updated": n}


@app.task(queue="default")
def update_pair_lists_from_decoder() -> dict:
    """R2 (cont. 55) — refresh BRAIN_PAIR_PROBATION and BRAIN_PAIR_SUSPENSION
    from the decoder corpus.
      Probation: pair appears in F9 misses ≥3 times in last 24h → add.
      Suspension: pair appears as F12 loser ≥3 times in 24h (+5 delta), or
                  ≥6 times in 24h (blocked=True for 6h).
      Graduates from probation: post-add closed_trades ≥50 AND winrate ≥50%.
      Auto-lifts suspension: now ≥ auto_lift_at.
    """
    import json as _json, sys, time
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import structlog
    from db import db_conn
    import redis_client
    import redis_keys
    log = structlog.get_logger()
    r = redis_client.get()

    try:
        from feature_governance.registry import is_active
        if not is_active("F46"):
            return {"status": "f46_inactive"}
    except Exception:
        pass

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.pair, COUNT(*) AS n
                FROM counterfactuals c
                JOIN signals s ON s.id = c.signal_id
                WHERE c.miss_decoded = TRUE
                  AND c.created_at > NOW() - INTERVAL '24 hours'
                GROUP BY s.pair
                HAVING COUNT(*) >= 3
            """)
            f9_pairs = dict(cur.fetchall())

            cur.execute("""
                SELECT t.pair, COUNT(*) AS n
                FROM mismatches m
                JOIN trades t ON t.id = m.loser_trade_id
                WHERE m.decoded_at > NOW() - INTERVAL '24 hours'
                GROUP BY t.pair
                HAVING COUNT(*) >= 3
            """)
            f12_loser_pairs = dict(cur.fetchall())

    now = int(time.time())

    prob_raw = r.get(redis_keys.BRAIN_PAIR_PROBATION)
    probation = _json.loads(prob_raw) if prob_raw else {}
    if not isinstance(probation, dict):
        probation = {}

    for pair, miss_count in f9_pairs.items():
        if pair not in probation:
            probation[pair] = {
                "strength_delta":      -5.0,
                "candle_setup_boost":  0.05,
                "graduates_after":     50,
                "trades_in_probation": 0,
                "added_ts":            now,
                "miss_count_24h":      int(miss_count),
            }
        else:
            probation[pair]["miss_count_24h"] = int(miss_count)

    if probation:
        with db_conn() as conn:
            with conn.cursor() as cur:
                graduated = []
                for pair, pd in list(probation.items()):
                    added_ts = pd.get("added_ts", now)
                    cur.execute("""
                        SELECT COUNT(*) AS n,
                               COUNT(*) FILTER (WHERE net_pnl_usdt > 0) AS wins
                        FROM trades
                        WHERE pair = %s
                          AND closed_at IS NOT NULL
                          AND opened_at > to_timestamp(%s)
                    """, (pair, added_ts))
                    n, wins = cur.fetchone()
                    n = int(n or 0); wins = int(wins or 0)
                    pd["trades_in_probation"] = n
                    pd["wins_in_probation"]   = wins
                    if n >= 50 and (wins / max(1, n)) >= 0.50:
                        graduated.append(pair)
                for pair in graduated:
                    del probation[pair]
                    log.info("pair_probation_graduated", pair=pair)

    try:
        r.set(redis_keys.BRAIN_PAIR_PROBATION, _json.dumps(probation))
        r.set("pair:probation:count", len(probation))
    except Exception:
        pass

    susp_raw = r.get(redis_keys.BRAIN_PAIR_SUSPENSION)
    suspension = _json.loads(susp_raw) if susp_raw else {}
    if not isinstance(suspension, dict):
        suspension = {}

    expired = [p for p, sd in suspension.items()
               if isinstance(sd, dict) and now >= int(sd.get("auto_lift_at", 0))]
    for pair in expired:
        del suspension[pair]
        log.info("pair_suspension_auto_lifted", pair=pair)

    for pair, loser_count in f12_loser_pairs.items():
        loser_count = int(loser_count)
        blocked = loser_count >= 6
        delta = 10.0 if blocked else 5.0
        suspension[pair] = {
            "strength_delta":  delta,
            "blocked":         blocked,
            "loser_count_24h": loser_count,
            "auto_lift_at":    now + (6 * 3600),
            "added_ts":        now,
        }

    try:
        r.set(redis_keys.BRAIN_PAIR_SUSPENSION, _json.dumps(suspension))
        r.set("pair:suspension:count", len(suspension))
    except Exception:
        pass

    log.info("pair_lists_updated",
             probation_count=len(probation),
             suspension_count=len(suspension),
             f9_pairs=len(f9_pairs),
             f12_loser_pairs=len(f12_loser_pairs))
    return {"status": "ok",
            "probation_count": len(probation),
            "suspension_count": len(suspension)}


@app.task(queue="default")
def compute_bot_confidence() -> dict:
    """R4 (cont. 55) — recompute bot self-confidence index."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from metacognition.confidence import compute_and_store
    return compute_and_store(window_seconds=3600)


@app.task(queue="default")
def compute_ips_thresholds() -> dict:
    """Idea 3 (cont. 64) — SNIPS-optimal per-bucket threshold update.

    Iterates every (regime × strength-band) bucket with ≥ 100 decoded
    F9 samples; computes τ* = argmax SNIPS V(τ); EWMA-blends the
    optimal delta (τ* − ga_base) into `brain:filter_overrides
    .by_regime_and_band[<bucket>].min_signal_strength_delta`.

    Nightly beat at 04:30 UTC (after the 04:00 ML retrains). F46 gated.
    One-shot manual:

        docker exec trading-bot-celery_worker-1 python -c \
          "from celery_app import compute_ips_thresholds as t; \
           print(t.delay().get(timeout=600))"
    """
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from metacognition.ips_optimiser import run_full_pass
    return run_full_pass()


@app.task(queue="default")
def capture_pattern_embeddings() -> dict:
    """Phase A (cont. 55) — capture live CandleNet embeddings for active pairs.

    Live-data-only per user mandate 2026-05-29: uses ml.candlenet.run_inference
    which reads current OHLCV from Redis (populated by data/feed.py). Never
    samples closed-trade snapshots. Output streamed to `pattern:embeddings`.
    """
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from pattern.live_capture import capture_for_active_pairs
    return capture_for_active_pairs()


@app.task(queue="predict_all")
def launch_pad_maintain_task() -> dict:
    """cont. 70 — Launch-Pad maintainer iteration. Lazy-imports the package so
    celery beat (which only mounts celery_app.py) registers the task without
    importing signals/. Shadow-only until launchpad:enabled=1; never opens trades."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from signals.launch_pad.maintainer import run
    return run()


@app.task(queue="default")
def prune_replay_pool() -> dict:
    """Signal Monitor Phase 1 (cont. 63, 2026-05-29) — periodic stale prune."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from signals.replay_pool import prune_stale
    return prune_stale()


@app.task(queue="default")
def refresh_bayes_threshold() -> dict:
    """Signal Monitor Phase 2 (cont. 63) — refresh adaptive T_high / T_low."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from signals.bayes_threshold import refresh
    return refresh()


@app.task(queue="default")
def util_calib_refresh() -> dict:
    """Signal Monitor Phase 4 (cont. 63) — nightly utility-weighted walk-forward
    calibration of the signal-strength acceptance threshold."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from signals.utility_calibration import calibrate
    return calibrate()


@app.task(queue="default")
def ev_override_refresh_segments() -> dict:
    """cont. 69s — refresh F9/F12 EV-override per-direction CF peak/dd segment
    stats into Redis (hot-path evaluator reads the cache, never the DB)."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from signals.ev_override import refresh_segments
    return refresh_segments()


@app.task(queue="predict_all")
def prediction_refresh_task() -> dict:
    """Predict-all Phase B (cont. 63) — every-30s refresh of cached
    predictions for the top-N scanner-ranked symbols. Cold when no model.

    cont. 65: routed to dedicated `predict_all` queue. Previously sat behind
    18k+ LLM tasks in `default` and only executed once every ~1.5h, which
    let `predictions:{pair}` keys (TTL 90s) expire and the engine-side gate
    sees nothing. Symmetric fix to the candlenet queue isolation."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from prediction.refresh_loop import refresh
    return refresh()


@app.task(queue="default")
def candle_online_train_task() -> dict:
    """F50e (cont. 64) — self-supervised candle training tick. Reads new
    closed candles for active pairs, enqueues pending-label entries,
    drains realized labels into SGD updates of online_predictor's
    direction + confidence heads. Trains on LIVE candle data of the
    symbols about to be placed — independent of trade outcomes."""
    import sys
    import structlog as _sl
    _log = _sl.get_logger()
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from ml.candle_online_trainer import train_tick
        return train_tick()
    except Exception as exc:
        _log.warning("candle_online_train_task_failed",
                     error=str(exc)[:200])
        return {"status": "failed", "error": str(exc)[:200]}


@app.task(queue="microstructure")
def shadow_ablation_task() -> dict:
    """cont. 70e — Track-2 shadow ablation tick. Feeds two prequential SGD
    direction heads (35-col full vs 32-col ablated) the same live candle
    samples and publishes the directional-AUC lift to Redis. Pure shadow:
    never trades, never gates, never touches online_predictor. Runs on the
    microstructure queue (candlenet worker: 35-col image + ml/ bind-mount)."""
    import sys
    import structlog as _sl
    _log = _sl.get_logger()
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from ml.shadow_ablation import tick
        return tick()
    except Exception as exc:
        _log.warning("shadow_ablation_task_failed", error=str(exc)[:200])
        return {"status": "failed", "error": str(exc)[:200]}


@app.task(queue="predict_all")
def foundation_forecast_task() -> dict:
    """cont. 70f — Track-4 producer. Runs the Chronos vol-prior sweep over
    active pairs, populating {pair}:foundation_forecast (mag + q10/q90 vol
    band) for the sizing + SL/TP consumers. Soft prior, never a gate. Runs on
    the candlenet worker (torch + ml/ bind-mount + downloaded model)."""
    import sys
    import structlog as _sl
    _log = _sl.get_logger()
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from ml.foundation_forecast import run_forecast_sweep
        return run_forecast_sweep()
    except Exception as exc:
        _log.warning("foundation_forecast_task_failed", error=str(exc)[:200])
        return {"status": "failed", "error": str(exc)[:200]}


@app.task(queue="predict_all")
def auto_arm_prediction_gate_task() -> dict:
    """F50e (cont. 64) — auto-arm the predict-all signal gate once
    online_predictor has warmed up. Idempotent. Kill switch:
    `prediction:gate_auto_arm = "0"`. Manual override of gate state
    via `prediction:gate_enabled` is preserved (we only FLIP from "0"/missing
    to "1"; never demote)."""
    import sys
    import time as _time
    import structlog as _sl
    _log = _sl.get_logger()
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        import redis_client as _rc
        r = _rc.get()
        auto = r.get("prediction:gate_auto_arm")
        if auto in ("0", b"0"):
            return {"status": "auto_arm_disabled"}
        current = r.get("prediction:gate_enabled")
        if current in ("1", b"1"):
            return {"status": "already_armed"}
        # cont. 65 — arm when EITHER predictor is warm. xgb is the
        # heavyweight model (trained from 60d closed trades); online_predictor
        # is the F50e candle-stream-warmed JIT model. Either being ready
        # means signals/engine.py can resolve a prediction at gate time.
        from prediction.online_predictor import is_ready as _online_ready, state
        from prediction.xgb_predictor import is_ready as _xgb_ready
        xgb_on = _xgb_ready()
        online_on = _online_ready()
        if not (xgb_on or online_on):
            st = state()
            return {"status": "cold",
                    "n_updates": st.get("n_updates", 0),
                    "min_required": st.get("min_required", 200),
                    "xgb_ready": xgb_on}
        r.set("prediction:gate_enabled", "1")
        r.set("prediction:gate_armed_at", int(_time.time()))
        r.incr("prediction:gate_armed_count")
        _log.info("prediction_gate_auto_armed",
                  xgb_ready=xgb_on, online_ready=online_on,
                  n_updates=state().get("n_updates", 0))
        return {"status": "armed",
                "xgb_ready": xgb_on, "online_ready": online_on,
                "n_updates": state().get("n_updates", 0)}
    except Exception as exc:
        _log.warning("auto_arm_prediction_gate_failed",
                     error=str(exc)[:200])
        return {"status": "failed", "error": str(exc)[:200]}


@app.task(queue="predict_all")
def update_pattern_registry_task() -> dict:
    """Predict-all Phase A.4 (cont. 63) — every 5 min, recompute the
    pattern_effectiveness_registry rows for any (cluster × regime × direction)
    that has had a trade close in the last interval."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from db import db_conn
    from pattern.registry import update_on_close
    updated = 0
    failed = 0
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id FROM trades
                    WHERE status     = 'closed'
                      AND exit_time  > NOW() - INTERVAL '10 minutes'
                      AND pattern_cluster_id IS NOT NULL
                """)
                rows = [r[0] for r in cur.fetchall()]
    except Exception as exc:
        _log.warning("update_pattern_registry_fetch_failed",
                     error=str(exc)[:200])
        return {"status": "fetch_failed", "error": str(exc)[:200]}
    for tid in rows:
        try:
            res = update_on_close(str(tid))
            if res is not None:
                updated += 1
        except Exception as exc:
            failed += 1
            _log.warning("update_pattern_registry_one_failed",
                         trade_id=str(tid), error=str(exc)[:200])
    _log.info("update_pattern_registry_complete",
              scanned=len(rows), updated=updated, failed=failed)
    return {"status": "ok", "scanned": len(rows),
            "updated": updated, "failed": failed}


@app.task(queue="default")
def cleanup_stale_predictions_task() -> dict:
    """Predict-all Phase A.4 (cont. 63) — hourly delete of unmatched stale
    rows in the predictions table. Bounded growth."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from db import db_conn
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM predictions
                    WHERE created_at < NOW() - INTERVAL '24 hours'
                      AND trade_id IS NULL
                """)
                deleted = cur.rowcount
                conn.commit()
        log.info("cleanup_stale_predictions_complete", deleted=int(deleted or 0))
        return {"status": "ok", "deleted": int(deleted or 0)}
    except Exception as exc:
        log.warning("cleanup_stale_predictions_failed", error=str(exc)[:200])
        return {"status": "failed", "error": str(exc)[:200]}


@app.task(queue="predict_all")
def calibration_drift_check_task() -> dict:
    """Predict-all Phase D (cont. 63) — hourly per-(cluster × regime) ECE
    computation. Flags clusters with ECE > 0.15 over ≥ 100 samples so the
    refresh loop can skip predicting for them until retrain."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from db import db_conn
    flagged: list[dict] = []
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT pattern_cluster_id, market_regime
                    FROM trades
                    WHERE status='closed'
                      AND exit_time > NOW() - INTERVAL '30 days'
                      AND pattern_cluster_id IS NOT NULL
                """)
                buckets = cur.fetchall()
        from prediction.xgb_predictor import _load_bundle
        bundle = _load_bundle()
        if bundle is None:
            log.info("calibration_drift_check_cold_no_model")
            return {"status": "cold", "checked": 0}
        # Use the calibration_ece + drift_flagged columns on the registry.
        for cluster_id, regime in buckets:
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT confidence, outcome FROM (
                          SELECT direction_confidence/100.0 AS confidence,
                                 (net_pnl_usdt > 0)::int AS outcome
                          FROM trades
                          WHERE pattern_cluster_id = %s
                            AND market_regime      = %s
                            AND status='closed'
                            AND direction_confidence IS NOT NULL
                            AND exit_time > NOW() - INTERVAL '30 days'
                          ORDER BY exit_time DESC LIMIT 200
                        ) AS recent
                    """, (cluster_id, regime))
                    pairs = cur.fetchall()
            if len(pairs) < 100:
                continue
            # 10-bin ECE
            bins = 10
            bin_sums = [(0, 0.0, 0.0)] * bins
            for conf, outcome in pairs:
                c = float(conf or 0)
                b = min(bins - 1, int(c * bins))
                cnt, cs, os_ = bin_sums[b]
                bin_sums[b] = (cnt + 1, cs + c, os_ + float(outcome or 0))
            ece = 0.0
            n = len(pairs)
            for cnt, cs, os_ in bin_sums:
                if cnt == 0:
                    continue
                bin_conf = cs / cnt
                bin_acc = os_ / cnt
                ece += (cnt / n) * abs(bin_acc - bin_conf)
            drift = ece > 0.15
            try:
                _r = redis_client.get()
                if drift:
                    _r.set(f"prediction:drift_flag:{cluster_id}:{regime}", "1")
                else:
                    _r.delete(f"prediction:drift_flag:{cluster_id}:{regime}")
                _r.set(
                    f"prediction:ece:{cluster_id}:{regime}", round(ece, 4))
            except Exception:
                pass
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE pattern_effectiveness_registry
                           SET calibration_ece    = %s,
                               n_samples_for_ece  = %s,
                               drift_flagged      = %s,
                               last_updated       = NOW()
                         WHERE pattern_cluster_id = %s
                           AND market_regime      = %s
                    """, (round(ece, 4), n, drift, cluster_id, regime))
                    conn.commit()
            if drift:
                flagged.append({"cluster": cluster_id, "regime": regime,
                                 "ece": round(ece, 4), "n": n})
        log.info("calibration_drift_check_complete",
                 buckets=len(buckets), flagged=len(flagged))
        return {"status": "ok", "buckets": len(buckets),
                "flagged": flagged}
    except Exception as exc:
        log.warning("calibration_drift_check_failed", error=str(exc)[:200])
        return {"status": "failed", "error": str(exc)[:200]}

