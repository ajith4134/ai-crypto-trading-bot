# Next Implementation — Predict-All-Before-Open Architecture

Source: User pasted a ChatGPT plan 2026-05-29. This is my deep-research + on-disk-verified refinement. Bigger scope than Bundle B (which is mid-deploy) and is independent of it — they should ship sequentially, not be merged.

---

## STATUS UPDATE — 2026-05-30 cont. 65

Rule-2 audit (chat session 2026-05-30) found the pipeline shipped but inert. User then mandated
a fix bundle keeping tp1/tp2/SL logic intact. The four root issues + fixes:

| Issue | Root cause | Fix shipped cont. 65 |
|---|---|---|
| `candle_forecast` keys absent ~96% of time | `candlenet_infer_all` on `default` queue behind 15k LLM tasks; TTL 90s | Dedicated `candlenet` queue + `celery_worker_candlenet` container; TTL raised to 300s |
| 1h CandleNet never inferred | Loop hard-coded 1m/5m/15m | Added "1h" to `_MODEL_PATHS`, `_INTERVAL_TO_FG`, `_cache`; loop now iterates 4 TFs; `F48_1h` registered; `retrain_candlenet_1h` task + beat |
| `features.py` lacked candle context | Original blueprint omission | Added 12 features `cn_{1m,5m,15m,1h}_{dir,mag,trend}`; `live_features()` reads `{pair}:{tf}:candle_forecast` JSON |
| Cascade silently fell back to OFI | "all_neutral" rationale conflated neutral vs missing | Counter `cascade:fallback_ofi_cold` + structured warning when all forecasts None |

Verification — closed same session:
- [x] `{pair}:{tf}:candle_forecast` keys continuously present for 1m/5m/15m (88-89 keys each)
- [x] `cascade:fallback_ofi_cold` counter ships; fires for pairs missing forecasts (SPKUSDT)
- [x] `predict_all_xgb.pkl` trained: 6121 samples × 32 features (12 candle features included)
- [x] `predictions:{pair}` keys: 30 written per refresh tick
- [x] `auto_arm_prediction_gate_task` extended to OR-check `xgb_ready | online_ready`; armed once
- [x] tp1/tp2 collapse INTENTIONALLY NOT DONE (user mandate)
- [x] xgb gate DISARMED immediately after arming — degenerate model (all preds identical) →
      would have rejected every signal. Root cause: training rows had `feature_vector IS NULL`
      for all 6121 historical rows.

Option B shipped same session:
- [x] `signals/engine.py:734` now snapshots full 32-key `prediction.features.live_features()`
      output to `signals.feature_vector` JSON. Future xgb retrains will be non-degenerate.
- [x] Verified live: 2 new signals inserted with `cn_5m_dir3, cn_15m_dir3, cn_1m_trend, ofi,
      sentiment` all populated. IOTAUSDT accepted, ALGOUSDT rejected.

Option C shipped same session — Pre-Open Candle Gate (PCG):
- [x] `signals/multi_tf_cascade.py` — `strict` + `required_tfs` params (defense in depth).
- [x] `signals/pre_open_candle_gate.py` — hard-gate with wait-and-retry queue, 4 config knobs,
      10 counters, 2 operator helpers.
- [x] `signals/engine.py` — PCG runs before legacy cascade; wait → return []; drop → return [];
      ready → use PCG direction; disabled/crashed → fall through to legacy cascade.
- [x] Smoke test: 5 pairs READY (1h missing OK because soft), 1 pair WAIT (no forecasts).
- [x] PCG ships OFF (`pcg:enabled` unset). Operator-arm only; see PROGRESS.md cont. 65 for arm sequence.

This file is **CLEARED** — all PCG, predict-all, candle-pipeline, signal-feature-snapshot
work landed. See PROGRESS.md "2026-05-30 cont. 65" for full audit trail.

Open follow-up (NOT blocking this file):
- Accumulate ≥500 fresh closed trades with rich `signals.feature_vector` snapshots, then
  retrain xgb: `docker compose run --rm brain python -c "from prediction.xgb_predictor
  import train_from_history; print(train_from_history(14, save_path='/app/models/predict_all_xgb.pkl'))"`.
  THEN flip `prediction:gate_enabled=1`. PCG can stay armed in parallel as defense-in-depth.

---

## 0. TL;DR of the proposed change
Today the bot can open a trade with partial predictions (direction known, exits computed mid-flight). Move to: **scan → rank → for top-N symbols predict EVERY trade parameter (entry, direction, SL, TP, hold time, confidence, expected RR) → only execute if all fields exist and pass a gating threshold.** Replace `tp1/tp2` schema with single `tp`. Add a Pattern Effectiveness Registry tracking pattern × regime × outcome. Track predicted vs actual on close.

User's core ask, sharpened:
- Single TP, no TP1/TP2 fan-out
- Symbol decided first, prediction work done second, trade opened third (not 1→3 in one pass)
- Predictions trained on 1m/5m/15m/1h candle context per symbol
- Closed/open trades track which patterns helped — so we know what actually pays

---

## 1. ChatGPT's plan — what's right, what's wrong

### 1.1 Right
- Rank-first → predict-on-candidates (compute efficiency, focuses ML on what matters)
- Multi-timeframe cascade (1m timing, 5m momentum, 15m structure, 1h trend)
- "Don't execute until every prediction field is populated" gate
- Phased ML rollout: XGBoost → Transformer → RL execution (low-risk-first)
- Track derivatives (OI, funding, liquidations) alongside price
- Pattern Effectiveness Registry — track what patterns actually pay

### 1.2 Wrong / needs refinement
- **Single TP is partially wrong** — research shows partial TPs (sell 50% at TP1, trail rest) typically beat single TP on heavy-tailed crypto returns. Right answer: keep ONE predicted TP exit number (user's instinct) but let the existing profit-lock ratchet (≥80% memory) act as the runner. Effectively: predicted TP = "guaranteed take here," the ratchet handles the upside extension if it goes beyond. No second prediction needed.
- **"Confidence" must be calibrated, not raw model probability.** A model that outputs 0.84 may actually be right 64% of the time. Use `ml/conformal_wrapper.py` (already in repo) — it gives statistically valid coverage guarantees. Raw softmax/sigmoid is misleading.
- **Expected RR should be a distribution**, not a point estimate. Output p25/p50/p75. Trade-outcome distributions are heavy-tailed; a single number hides the variance.
- **Hand-labeled SMC/ICT patterns (FVG, OB) are brittle.** Different traders define them differently; labels drift. Better: let the model learn pattern CLUSTERS from candle embeddings (e.g., k-means/HDBSCAN on `ml/candlenet.py` outputs). Then label clusters humanly post-hoc if useful.
- **Predict-all-before-execute has latency cost.** If prediction takes 500ms, signals on fast-moving pairs get stale. Right design: continuously refresh predictions for the top-N ranked symbols at 1m cadence on a background worker, so when a signal fires the prediction is ALREADY warm in Redis.

### 1.3 Missing entirely from ChatGPT's plan
- **Calibration drift detection** — models lose calibration over time. Need a continual monitor (Brier score, ECE) that triggers retraining.
- **Per-regime conditioning** — patterns work differently in bull/bear/range. Pattern registry must be per-regime, not global.
- **Rejected-setup tracking (survivor bias)** — the Pattern Registry must record patterns we DIDN'T take and what they would have done, not just executed trades. Otherwise we only learn "patterns we took and won," not "patterns we should be taking." This is the same problem F9 already addresses for whole signals; extend it to pattern level.
- **Prediction store as a relational table**, not just columns on `trades`. One predictions row per (symbol × scan_cycle × tf_set) with an FK to trades when one materializes. This lets us count predictions made vs trades opened — a hit rate the system needs for self-evaluation.
- **Forward-walk backtest harness** before any prediction model goes live in paper. Don't train-on-history then deploy — leak-prone. Walk-forward with embargoed test windows.
- **Decision-boundary calibration curves** per pattern_cluster per regime, persisted. Not just "model says 0.84" — "0.84 means 71% historical accuracy in bull regime for cluster C7."

---

## 2. What already exists in the bot (Rule 2 verified)

### 2.1 Symbol selection / ranking
- `scanner/main.py` — Section R-01 to R-11. Scans all USDT-M futures, scores on 5 criteria (Wilder ADX, OI, volume, etc.), maintains active pair list. **This IS the symbol ranker.**

### 2.2 Multi-timeframe
- `signals/multi_tf_cascade.py` — **Multi-TF cascade already exists.** Needs reading to confirm whether it covers all 4 TFs (1m/5m/15m/1h) and how it composes.

### 2.3 ML stack (under `ml/`)
- `candlenet.py` + `candlenet_mae.py` — candle representation learning (CandleNet + Masked-Autoencoder variant)
- `direction_model.py` — direction prediction
- `entry_timing_agent.py` — PPO RL agent that decides {wait, enter, skip} at signal time (this IS ChatGPT's Phase 3, already shipped)
- `conformal_wrapper.py` — calibrated confidence intervals (kelly_multiplier exists)
- `continual_learning.py` — online learning loop (F49 §C7)
- `foundation_forecast.py` — foundation model for forecasts
- `gnn.py` + `gnn_multiscale.py` — graph neural nets for cross-asset
- `auto_hpo.py` — hyperparameter search
- `bocpd.py` — Bayesian online change point detection (regime breaks)
- `drift_detector.py` — model drift monitoring
- `dsl_evaluator.py` + `dsl_grammar.py` + `llm_alpha_dsl.py` — LLM-generated strategy DSL

### 2.4 Schema (trades table)
- `tp1, tp1_fired, tp1_target, tp2, tp2_target` — current multi-TP system
- `trailing_sl_level, hold_time_seconds, direction_confidence` — exit/timing/confidence already present
- **48 references to `tp1`/`tp2` in `risk/manager.py` alone** — collapsing them is invasive

### 2.5 Data feeds
- OI, funding, liquidations already integrated across `signals/engine.py`, `scanner/main.py`, `ml/llm_alpha_dsl.py`, `risk/frontier/exit_signals.py`
- `data/external/binance_liq_estimator.py`, `coinalyze_liq.py`, `coinglass_liq.py`, `deribit_dvol.py`, `moondev_liq.py` — multi-source liquidation data
- `data/hawkes_producer.py`, `data/mm_hawkes_producer.py` — Hawkes process for order-flow modeling
- `data/onchain_netflow.py` — on-chain netflow

### 2.6 Pattern detection
- No file matches `order_block`, `fair_value_gap`, `FVG`, `liquidity_sweep`, `break_of_structure` directly
- BUT candle pattern intelligence is embedded in `ml/candlenet.py` (learned representation) and the multi-TF cascade
- **Gap**: explicit SMC/ICT pattern extraction does not exist in code; either build it OR (better, per §1.2) skip it and use learned candle clusters

### 2.7 Pattern Effectiveness Registry
- Does NOT exist (`grep pattern_effectiveness` returns nothing)
- **This is a real gap and the biggest novel piece of the ChatGPT plan**

---

## 3. Reframed architecture

```
                    ┌─────────────────────────────────────┐
                    │ EXISTING (preserve)                 │
                    ├─────────────────────────────────────┤
SCANNER  ─────────► │ scanner/main.py — R-01..R-11        │
(all USDT-M pairs)  │ 5-criteria score → active pair list │
                    └─────────┬───────────────────────────┘
                              │ top-N ranked
                              ▼
                    ┌─────────────────────────────────────┐
                    │ NEW: PREDICTION REFRESH LOOP        │
                    │ celery beat task every 1m           │
                    │ For each top-N symbol:              │
                    │   - pull 1m/5m/15m/1h candles       │
                    │   - candlenet_mae → embedding       │
                    │   - cluster ID (HDBSCAN)            │
                    │   - XGBoost(emb + OI + funding +    │
                    │       liqs + regime) → predict      │
                    │       all 7 fields                  │
                    │   - conformal_wrapper → calibrated  │
                    │       confidence band               │
                    │   - write to predictions:{symbol}   │
                    │     Redis hash (TTL 90s)            │
                    └─────────┬───────────────────────────┘
                              │ predictions cached, warm
                              ▼
                    ┌─────────────────────────────────────┐
                    │ EXISTING: signals/engine.py         │
                    │ When a signal fires:                │
                    │   - check predictions:{symbol}      │
                    │   - if missing → reject             │
                    │     (silent-rejection counter)      │
                    │   - if confidence < gate → reject   │
                    │   - if predicted RR < 1.5 → reject  │
                    │   - else: use predicted             │
                    │     entry/SL/TP/hold as the trade   │
                    │     spec (no recompute)             │
                    └─────────┬───────────────────────────┘
                              │
                              ▼
                    ┌─────────────────────────────────────┐
                    │ EXISTING: entry_timing_agent.py PPO │
                    │ Decides {wait, enter, skip}.        │
                    │ Already in place — keep as-is.      │
                    └─────────┬───────────────────────────┘
                              │
                              ▼
                    ┌─────────────────────────────────────┐
                    │ EXISTING: execution path            │
                    │ Trade opens with the predicted      │
                    │ entry/SL/TP/hold (single TP).       │
                    │ Profit-lock ratchet (≥80% memory)   │
                    │ acts as runner above predicted TP.  │
                    └─────────┬───────────────────────────┘
                              │ trade closes
                              ▼
                    ┌─────────────────────────────────────┐
                    │ NEW: OUTCOME LOGGER + REGISTRY      │
                    │ - write predicted_vs_actual to      │
                    │   predictions table                 │
                    │ - update pattern_effectiveness for  │
                    │   (cluster × regime × direction)    │
                    │ - calibration-drift monitor reads   │
                    │   this; triggers retrain when ECE   │
                    │   > 0.15 for 100+ samples           │
                    └─────────────────────────────────────┘
```

The key change is the **NEW prediction refresh loop** that pre-warms predictions for the top-N ranked symbols. Signals consume them; nobody waits for a predict-on-demand call. Latency stays sub-10ms at signal time.

---

## 4. Concrete implementation plan (phased)

### Phase A — Schema collapse + prediction store + Registry (2-3 days)
1. Migration 027:
   - `ALTER TABLE trades ADD COLUMN tp numeric(18,8), tp_fired bool DEFAULT FALSE, tp_target numeric(18,8);`
   - Backfill: `UPDATE trades SET tp = tp1, tp_fired = tp1_fired, tp_target = tp1_target WHERE tp1 IS NOT NULL;`
   - Keep tp1/tp2 columns for 1 release — DROP in Migration 028 after code is fully migrated
   - `CREATE TABLE predictions (id uuid PK, symbol text, scan_cycle_id uuid, tf_set text, predicted_direction text, predicted_entry numeric, predicted_sl numeric, predicted_tp numeric, predicted_hold_seconds int, conformal_confidence numeric, predicted_rr_p25 numeric, predicted_rr_p50 numeric, predicted_rr_p75 numeric, pattern_cluster_id int, market_regime text, model_version text, created_at timestamptz, trade_id uuid FK NULL);`
   - `CREATE TABLE pattern_effectiveness_registry (pattern_cluster_id int, market_regime text, direction text, n_trades int, n_wins int, avg_rr numeric, avg_hold_seconds int, net_pnl_usdt numeric, calibration_ece numeric, last_updated timestamptz, PRIMARY KEY (pattern_cluster_id, market_regime, direction));`
   - Add to `trades`: `prediction_id uuid FK`, `pattern_cluster_id int`, `predicted_hold_seconds int`, `actual_hold_seconds int` (= existing hold_time_seconds), `predicted_rr numeric`, `actual_rr numeric`, `prediction_success bool`

2. Code changes (~150 LOC):
   - `risk/manager.py`: replace all 48 `tp1`/`tp2` references with `tp`. Profit-lock ratchet stays as-is (handles upside beyond TP). Tip: do this via sed-then-review, not by hand.
   - New file `pattern/registry.py`: read/write the registry, expose `update_on_close(trade_id)` and `lookup_effectiveness(cluster, regime, direction)`.

### Phase B — Prediction Refresh Loop (3-4 days)
1. New file `prediction/refresh_loop.py`: pulls scanner top-N from Redis, fetches multi-TF candles via existing exchange client, runs XGBoost prediction, writes to `predictions:{symbol}` Redis hash with 90s TTL.
2. New file `prediction/xgb_predictor.py`: wraps an XGBoost model trained on (CandleNet embedding | OI | funding | liquidations | regime | recent pattern history) → 7 outputs (direction, entry_offset_bps, sl_offset_bps, tp_offset_bps, hold_seconds, confidence_raw, rr_p50). Then `conformal_wrapper.py` calibrates the confidence raw → coverage-guaranteed band.
3. Beat schedule: `prediction-refresh: every 30s` (top-N is small; full refresh well under cadence).
4. `pretrainer/main.py` extension: produce the training dataset from closed `trades` + `counterfactuals`. Forward-walk validation with 2-week embargoed test windows.

### Phase C — Signal-side enforcement (1-2 days)
- `signals/engine.py`: at signal-firing, look up `predictions:{symbol}` Redis hash. If missing, reject with `reason=prediction_not_ready` + counter. If `conformal_confidence < gate`, reject. If `predicted_rr_p50 < 1.5`, reject. If all pass, use the predicted entry/SL/TP/hold as the trade spec (skip the current SL/TP derivation block).

### Phase D — Pattern Effectiveness consumer + drift monitor (2 days)
- `celery beat` task `update_pattern_registry` every 5 min: scan trades closed in last interval, join to predictions on prediction_id, write the actual vs predicted comparisons, update the registry per (cluster, regime, direction).
- `celery beat` task `calibration_drift_check` every 1 hr: compute Expected Calibration Error per cluster. If ECE > 0.15 on 100+ samples → flag for retrain, write `prediction:drift_flag:{cluster}=1`.

### Phase E — Optional Phase 2 ML (later, 1-2 weeks)
- Transformer candle model (per ChatGPT's Phase 2). Same interface as XGBoost predictor (drop-in replacement), but trained on raw 500-candle sequences instead of CandleNet embeddings. Compare against XGBoost in shadow mode for 2+ weeks before swap.

### Phase F — Backtest harness (1 week, parallel)
- `pretrainer/walk_forward.py`: implement 2-week embargoed walk-forward. Required before any new predictor goes live in paper. Output: per-week predicted_vs_actual report and calibration curves.

**Total: ~3-4 weeks for A-D (production-grade), +2 weeks for E if greenlit.** Significantly more invasive than Bundle B (which is days). User should know.

---

## 5. Interaction with Bundle B (currently mid-deploy)
- Bundle B's R4 bot self-confidence index is **reusable as a top-level "should we trust the predictor at all today" gate.** When `bot_confidence < 0.30`, the predict-all gate hard-skips regardless of per-symbol prediction.
- Bundle B's R1 per-(regime × strength-band) overrides become **less relevant** under the new architecture — the predictor outputs absolute SL/TP/hold per signal, no need to layer min_strength deltas. But Bundle B's data (which patterns get rejected by signal_too_weak) still feeds the predictor training set.
- The Meta-RL-Crypto judge in Phase 4 of the prior plan becomes **the meta-model that decides which OF the predicted signals to take**, sitting on top of the predictor. So the locked judge plan still ships; its inputs just become richer.

Conclusion: **don't pause Bundle B.** Let it finish. Then start Phase A here.

---

## 6. Open questions for user (blocking, must answer before scoping)
1. **Backfill or hard-cut on tp1/tp2?** Backfill is safer (existing trades' tp1 → tp); hard-cut is cleaner but loses history.
2. **Top-N for prediction refresh?** ChatGPT didn't pick a number. I'd say N=20-30 (matches scanner active list size) with 30s refresh — sustainable on a single celery worker.
3. **Pattern cluster count?** HDBSCAN auto-decides but bounded — 30 clusters? 100? Tradeoff: more clusters = finer per-pattern stats, but slower convergence per cluster.
4. **Confidence gate threshold?** Conformal coverage 0.7 (high), 0.8 (very high), or per-regime tuned?
5. **Single TP vs predicted-TP-plus-trailing-runner?** User said single TP. I'd recommend: predicted TP = take 100% target; existing profit-lock ratchet handles "what if price keeps going" — preserves user's "no TP1/TP2" while not throwing away the upside. Confirm.
6. **Hand-labeled SMC/ICT patterns alongside learned clusters?** Or only learned clusters? (My pick: only learned, but expose cluster centroids for human inspection.)

---

## 7. Code-agent command (the one-paragraph task)

> Replace TP1/TP2 with a single TP across the trades schema and the codebase. Add `predictions` and `pattern_effectiveness_registry` tables. Build a celery-beat prediction refresh loop that pre-warms entry/direction/SL/TP/hold_time/conformal_confidence/predicted_RR for the top-N scanner-ranked symbols every 30s using an XGBoost model trained on CandleNet embeddings + OI + funding + liquidations + regime + (1m/5m/15m/1h) candle context. At signal-firing time, reject any signal whose predictions are missing, whose conformal confidence is below the configured gate, or whose predicted_RR_p50 is below 1.5; otherwise use the cached predicted entry/SL/TP/hold as the trade spec (do not recompute). After each trade closes, update `pattern_effectiveness_registry` with the cluster × regime × direction outcome, and run a per-cluster Expected Calibration Error check; flag clusters with ECE > 0.15 over 100+ samples for retraining. Wire Bundle B's bot-confidence index as a top-level hard-skip when below 0.30. Add forward-walk backtest validation in pretrainer/walk_forward.py with 2-week embargoed test windows; gate any new predictor model deploy on it. Sequence: Phase A (schema + registry + tp1/tp2 collapse, ~150 LOC + migration), Phase B (refresh loop + XGBoost + conformal wrap, ~400 LOC + new files), Phase C (signal-side enforcement, ~80 LOC in signals/engine.py), Phase D (registry consumer + drift monitor, ~200 LOC + 2 beat tasks). Single TP; existing profit-lock ratchet preserves upside extension beyond TP. Paper-only first; live promotion gated on 4+ weeks of shadow-vs-live agreement.

---

## 8. Sources

- [LSTM + XGBoost crypto price prediction (arXiv 2506.22055)](https://arxiv.org/pdf/2506.22055)
- [Attention Transformer + GRU crypto prediction (arXiv 2504.17079)](https://arxiv.org/pdf/2504.17079)
- [Multi-Timeframe Feature Engineering preprint](https://www.preprints.org/manuscript/202603.0994/v1/download)
- [SMC + ICT Trading Guide 2026 — backtest data showing 50-65% win rates (not 70-80%)](https://forextradelab.com/blog/smart-money-concepts-ict-trading-guide/)
- [Smart Money Concepts Python package (joshyattridge/smart-money-concepts) — reference for SMC pattern definitions](https://github.com/joshyattridge/smart-money-concepts)
- [Risk/Reward & SL Methods comparison (mql5 blog, Feb 2026)](https://www.mql5.com/en/blogs/post/767486)
- [ML approaches to crypto trading optimization — Springer Discover AI](https://link.springer.com/article/10.1007/s44163-025-00519-y)
