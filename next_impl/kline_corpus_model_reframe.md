# Next Implementation — Shared Kline Corpus + Reframe ALL Prediction Models (cont. 69e)

Opened 2026-06-02. User decision: stop training prediction models on our own defective
trade log; standardize every data-trained model on a shared, DEEP historical-kline corpus
with forward-return labels. Multi-session.

## WHY (root cause, verified cont. 69d-e)
3 degenerate-model symptoms in one session — predict-all gate 100% short; predicted_entry
offset constant -0.12 bps; conformal widths all ≥0.5. Diagnosis:
- predict-all (xgb) trains on closed trades joined to signals.feature_vector. Only 3010 of
  9433 closed-60d trades (32%) have a feature_vector (missing signals.trade_id backlink) →
  68% zero-filled rows → model collapses to identical output per pair.
- Regime skew: trained when 60d was 71% SHORT; market now 81% LONG → predicts old regime.
- No scheduled predict-all retrain (candlenet/HMM/TFT/PatchTST/GNN/MARL all scheduled;
  predict-all is NOT) → 3-day-stale model (predict_all_xgb.pkl mtime May 30).
- candlenet/forecaster low edge (val_AUC ~0.55-0.65) is INHERENT crypto difficulty, not a
  pipeline bug — but deeper/longer kline history + consistent labels should help calibration.

## KEY INSIGHT
"External trade data" doesn't exist publicly; what we need is abundant MARKET data (Binance
klines = free, years, ~400 pairs). Direction/movement prediction is a MARKET question and
must be trained on klines + forward-return labels — NOT our trades. Candlenet/HMM/forecasters
already do this; predict-all is the offender. Our trade log should ONLY train the
execution-calibrated heads (RR/confidence vs OUR SL/TP) and the selection/timing learners.

## CURRENT DATA SOURCE PER MODEL (verified)
- candlenet 1m/5m/15m/30m/1h : KLINES (build_feature_matrix). Already correct. ✅
- HMM regime               : klines (ml/hmm.py). ✅
- TFT / PatchTST / Mamba    : klines (sequence forecasters). ✅ (verify depth)
- predict-all xgb (9 heads) : OUR TRADES (defective). ✗ — direction/move heads must move to klines.
- direction_model           : check source; conformal width 0.83 → likely thin/low-edge.
- criteria-weights (F10)    : our pair_selections + trades (legit — selection learning).
- entry-timing PPO          : our trades (execution-specific; could use backtest sim).

## TARGET ARCHITECTURE — shared kline corpus
1. INGEST: deep historical klines (target 1-2y) for all active pairs × {1m,5m,15m,30m,1h}
   from Binance get_historical_klines (free; already used celery_app.py:1659). Persist to a
   columnar store (parquet under models/corpus/ or a Postgres `klines` table) + incremental
   top-up task. Backfill once, then keep current.
2. LABELS: standardized forward-return label module `ml/forward_labels.py`:
   direction_y = sign(return over horizon H); mag_y = pct move; (optionally triple-barrier
   for RR). One definition reused by every model so they're comparable/calibratable.
3. FEATURES: reuse prediction/features.FEATURE_COLUMNS + candlenet build_feature_matrix; one
   shared builder so train-time == infer-time features (kills train/serve skew).
4. ADAPTERS: each model trains from the corpus via a thin adapter. predict-all direction/move
   heads → corpus; RR/confidence heads → our CLEANED trades (INNER JOIN real-FV only).
5. SCHEDULE: add predict-all retrain to celery beat (every 6-12h). Standardize cadences.
6. CONFORMAL: recalibrate widths on the corpus; gate auto-activates when a model's width<0.5
   (the cont.69e edge_max filter is already in place). Re-enable conformal:disabled=0 then.

## PHASES
- [x] P0 (DONE cont.69e): conformal edge_max fix (exclude width≥0.5 from abstain). Re-enabled.
- [~] P1 (cont.69f, IN PROGRESS): ml/klines_corpus.py built (mainnet keyless, ban-aware,
      paginated). FINDINGS: existing store data/historical/{pair}/{tf}.csv was SHALLOW
      (~1-2k rows) + STALE (1m/5m/15m/30m ended mid-2024). Trading client is TESTNET (sparse)
      → corpus MUST use mainnet public klines. data/historical bind-mount is on cn_train/
      candlenet/celery_worker (NOT brain/scanner) → run backfill there. Validated BTC: 2y 1h
      fresh, persists to host. DEEP BACKFILL RUNNING (active 200, detached in candlenet worker,
      ban-aware ~0.4s/call). Hourly incremental task `update_klines_corpus_task` (queue cn_train)
      + beat coded — ACTIVATES ON NEXT REBUILD (deferred until backfill done so the worker isn't
      recreated mid-run).
      cont.69h: REST deep backfill kept hitting -1003 IP bans (no progress). SWITCHED to
      data.binance.vision BULK dumps (bulk_backfill_pair / `python -m ml.klines_corpus bulk`):
      monthly+daily kline ZIPs, NO rate limit, ts normalized to ms. Validated BTC 1h = 2.0y
      fresh in 3.5s. RUNNING for active 200 (~3-4s/pair → ~10-15min, detached in candlenet
      worker, log /app/data/historical/_bulk.log). REST incremental stays the cheap hourly top-up.
      TODO after bulk done: rebuild+recreate cn_train (activate hourly top-up); confirm candlenet
      retrain reads fresh corpus (P4). Then P3 unblocked.
- [x] P2 (DONE cont.69g): ml/forward_labels.py — fixed-horizon (fwd_return/direction/magnitude)
      + triple-barrier (win/rr/bars), leak-safe, reads P1 corpus. Validated on synthetic +
      real BTC 1h: balanced up_rate 0.51 (vs our trade log's skewed 28.7% long) — confirms the
      kline-label thesis. Reusable by all kline-trained models.
- [x] P1 (DONE cont.69h): bulk corpus via data.binance.vision; 196/200 active pairs
      deep+fresh (~150 full-depth, rest new listings); dir perms fixed (chown 999); hourly
      incremental top-up `update_klines_corpus_task` LIVE (beat :07).
- [~] P3 (CORE DONE cont.69i): prediction/kline_features.py (OHLCV-only, leak-safe, train==serve)
      + prediction/xgb_kline_trainer.py. Trained 937,281 samples / 181 pairs; up_rate 0.483
      (balanced); val_auc 0.543 (honest low edge); PER-PAIR VARIED output (degeneracy FIXED);
      model models/predict_all_kline.pkl saved+loads. 12h retrain `retrain_kline_predictor_task`
      LIVE (beat :20 */12).
- [x] P3 FINISH (cont.69j): live inference wired. predict_live (live Redis candles) +
      publish_kline_predictions_task → predict:kline:{pair} (queue predict_all, light) +
      debate/fallback.py kline_prior (toggle-gated soft nudge). VERIFIED: 197/197 published,
      scorer uses kline_prior. 5-min beat refresh reliable.
- [ ] P4 (BLOCKED on OOM): candlenet retrain on the deep corpus SIGKILL'd (1.18M samples >
      5.86GB). FIX: set env CANDLENET_MAX_SAMPLES ~150-200k on cn_train, re-run retrains.
      Conformal widths should then narrow <0.5 → P5 (recalibrate + re-enable conformal).
- [ ] P4: point candlenet/HMM/forecasters at the shared corpus (consolidate; deepen history).
- [ ] P5: recalibrate conformal on corpus; re-enable gate when widths<0.5; re-evaluate predict-all
      gate auto-arm ONLY after shadow validation (see [[feedback_predict_gate_disabled]]).

## GUARDRAILS (Rule 4)
- Prediction models are LOW-EDGE by nature (crypto 1-candle AUC ~0.6). Corpus reframe fixes
  degeneracy/staleness, NOT magic accuracy. Keep them as SOFT inputs (deterministic scorer
  prior, limit offset); do NOT re-arm hard gates until shadow-proven.
- Kline backfill for 400 pairs × 5 TFs × 1-2y is large (GB-scale + API weight); throttle via
  the existing _track_weight rate limiter; backfill incrementally off-peak.

## Session handoff
P0 conformal fix done + brain restarted. predict-all gate + conformal + limit-offset all
inert/disabled pending this work. Related: [[feedback_predict_gate_disabled]],
[[feedback_conformal_gate_disabled]]. Start at P1 (kline corpus ingestion).
