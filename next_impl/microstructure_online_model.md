# Track 2 — Microstructure features into the online direction model (cont. 70)

Part of next_impl/better_direction_models_roadmap.md. GROUNDED by on-disk audit this session.

## KEY FINDING (Rule 10 paid off): OFI is ALREADY half-wired
`prediction/features.py` FEATURE_COLUMNS (32-col live vector) ALREADY includes microstructure:
`ofi` (line 31), `vpin` (32), `bid_ask_imbalance` (33), plus sentiment, regime one-hots,
xsmom, exchange_netflow_z, liquidations, funding, and 12 CandleNet multi-TF cols. live_features()
(features.py:103) reads them from redis_keys.{OFI,VPIN,BID_ASK_IMBALANCE} each tick. This vector
feeds the river online learner (candle_online_trainer.py -> online_predictor) AND the XGB
predict-all. => "wire OFI into the model" is DONE for the online path. Don't rebuild it.

## THE REAL GAP vs cont.69v winners
micro_ws (data/micro_ws.py) emits richer live keys the model does NOT see:
- `{pair}:cvd_now`, `{pair}:cvd_history`  (CVD — winner-adjacent; NOT in FEATURE_COLUMNS)
- `{pair}:micro:ofi_accel`, `:ofi_prev`, `:ofi_l1`  (OFI dynamics)
- `{pair}:ofi_abs_ewma`  (OFI magnitude baseline)
- depth_weighted_ofi (cont.69v WINNER +1.13) — verify exact key; plain `ofi` != depth-weighted.

## CRITICAL: CVD is RAW + per-pair scale varies ~200x (UNUSABLE as-is)
Live sample: BTCUSDT:cvd_now=-608.6 vs ETHUSDT:cvd_now=-122908.9. MUST normalize before feeding:
- cvd_z = cvd_now / (abs_ewma or rolling std of cvd)  OR  tanh(cvd_now / scale).
- ofi already appears pre-scaled (BTC ofi_abs_ewma=-1.83) — confirm ofi is normalized too.
- ofi_accel empty for some pairs (BTC nil) -> 0-fill is honest signal-of-absence (existing pattern).

## RISK: LOW to live trading
predict-all gate is DISABLED (prediction:gate_enabled=0, gate_auto_arm=0; see
feedback_predict_gate_disabled). So a FEATURE_COLUMNS shape change CANNOT block live entries — it
only affects shadow / soft-prior. Safe window to change the shape.

## SHAPE-CHANGE = 4 PARITY SURFACES + a missing count-guard (must do ALL — Rule 4)
Adding columns to the frozen FEATURE_COLUMNS tuple requires:
1. prediction/features.py — append cols to FEATURE_COLUMNS + populate in live_features() (with
   CVD normalization).
2. prediction/xgb_predictor.py:235 predict path uses LIVE FEATURE_COLUMNS but the loaded model
   expects bundle["feature_columns"]/feature_count. ADD a count-guard: if
   len(FEATURE_COLUMNS) != bundle["feature_count"] -> return None (dormant) until retrain.
   (Mirror the cont.69q xgb_kline_trainer `_bundle_current` dormancy pattern — it does NOT exist
   here yet; only a key-presence check at lines 73-82.)
3. ml/direction_model.py:413 `_build_live_features` — SEPARATE live feature builder; keep parity
   or it silently diverges.
4. walk_forward / pretrainer historical-snapshot wrapper (features.py:110 docstring) — historical
   reconstruction of the new micro cols is impossible (same reason kline_features went OHLCV-only)
   -> the OFFLINE xgb predict-all can't learn them; only the ONLINE river path can. So either
   (a) keep new micro cols ONLINE-only (river learns live, train==serve), and 0-fill them in the
   offline XGB vector, OR (b) start LOGGING micro snapshots now to build a replayable corpus for a
   future offline retrain. Recommend BOTH: online-now + start logging.
5. Retrain: trigger retrain_kline_predictor / online model so the new shape is adopted; predict-all
   stays dormant (already disabled) until then.

## PLAN (ordered)
P1. Confirm exact micro_ws key names + which are pre-normalized (read data/micro_ws.py writers).
P2. Add cols: `cvd_z`, `ofi_accel`, `ofi_abs_ewma` (start with 3 highest-signal). Normalize CVD.
P3. Add the feature_count dormancy guard to xgb_predictor (so old model dormants cleanly).
P4. Mirror in ml/direction_model._build_live_features.
P5. Start a micro-snapshot log (Redis stream or corpus csv) for future offline replay.
P6. Retrain online + xgb; verify in shadow (predict-all stays gated OFF per
   feedback_predict_gate_disabled) before any re-arm.

## DEPLOY
features.py / xgb_predictor.py / direction_model.py: brain bind-mounts prediction? VERIFY in
reference_service_bind_mount_map — refresh loop runs on the prediction/refresh worker; the online
trainer on celery_worker_candlenet/cn_train. Likely a celery_worker REBUILD (prediction/ not a
brain bind-mount). CHECK before claiming deploy (Docker COPY cache gotcha).

## STATUS: CORE SHIPPED + VERIFIED (cont.70c).
DONE: cvd_z/ofi_l1/ofi_accel added to FEATURE_COLUMNS (32->35), live_features populated (real
values verified), xgb_predictor dormancy guard + online_predictor n_features self-heal guard (both
verified firing), image rebuilt x2, brain+candlenet+cn_train recreated. Online river model relearning
at 35-col live.
REMAINING (P5/P6): (a) start a micro-snapshot LOG so the OFFLINE predict-all can eventually replay
these cols (currently 0-fill historical); (b) optional manual retrain of legacy predict_all_xgb at
35-col (train_from_history, CLI-only) OR formally retire it in favor of predict_all_kline; (c) after
the online model warms, measure whether the 3 micro cols improve directional AUC vs the 32-col
baseline before promoting to any gate. Track 1 gate shipped cont.70b.

## CORRECTION cont.70e (2026-06-03): cont.70c's deploy was INCOMPLETE.
The candle-online training path (candle_online_train_task, the DOMINANT ~189k-update stream) runs
on the DEFAULT `celery_worker`, which cont.70c never recreated -> it stayed 32-col, so the 3
features never reached the live online_predictor (on-disk bundle was 32-col, n_updates=189150). The
DEPLOY-section caution above ("Likely a celery_worker REBUILD ... CHECK before claiming deploy") was
RIGHT. FIXED: `docker compose up -d --no-deps celery_worker` (no rebuild — image a1607d2 already
35-col). Bundle auto-reset 32->35, re-warmed. brain + celery_worker now both 35-col (ended
shared-pickle width thrashing).
MEASUREMENT (the (c) item): ml/shadow_ablation.py — forward prequential ablation (two SGD direction
heads, 35-col full vs 32-col ablated, same live candle samples), beat task on the microstructure
queue (candlenet worker, no rebuild). Publishes shadow_abl:{auc_full,auc_ablated,lift,n,status}.
Warm-gate _WARM_BEFORE_EVAL=1000 + reload/persist-per-tick (fixes concurrency=2 fork divergence).
EARLY (post-warm-gate): lift ~+0.01 to +0.013 (auc_full ~0.605 vs ablated ~0.593), consistently
positive -> the 3 micro cols add a small REAL directional edge. Confirm with the full-window
stabilized value before P6 promotion. P5 micro-snapshot log still pending.
