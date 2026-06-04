# Better Entry/Direction Models — Master Roadmap (cont. 70)

**Origin:** user asked (2026-06-03) to find better ML/RL/AI models for multi-TF candle
entry/direction because 5m/15m/30m/1h numbers are weak (val_auc ~0.54). Web research +
on-disk audit done this session.

## CORE DIAGNOSIS (do not re-litigate)
The 0.54 AUC is an **INPUT ceiling, not a model defect**. OHLCV candles at 5m–1h carry
~no directional signal. Confirmed 3 ways:
- OUR data: cont.69y multi-TF candle test lift = -0.004; cont.69v per-trade scores
  non-monotonic vs outcome ("edge is structural, not score").
- Literature: foundation models regress to mean -> low MSE but poor *direction*.
- LOB field headline: "Better Inputs Matter More Than Stacking Another Hidden Layer"
  (arxiv 2506.05764); TLOB paper: simple MLP on good LOB inputs beats SOTA transformers.
=> Swapping candle-model X->Y will NOT help. "Perfect direction" is unachievable; markets
   ~efficient at these horizons. RL's real job is execution/sizing/exits, not direction.

## AUDIT FINDINGS (on disk, this session)
- **micro_ws ALREADY emits live microstructure** to Redis per pair: `:ofi`,
  `:micro:ofi_accel/ofi_prev/ofi_l1`, `:ofi_abs_ewma`, `:cvd_now`, `:cvd_history`.
  Source: data/micro_ws.py, signals/microstructure.py.
- prediction/kline_features.py is **deliberately OHLCV-only** (line 6) because OFI/VPIN
  "can't be reconstructed historically" from the kline corpus. => the offline xgb path
  CANNOT eat OFI. Microstructure must go through an ONLINE train==serve learner
  (ml/candle_online_trainer.py exists, river-based) + forward OFI/CVD logging for a future
  replayable corpus.
- TLOB (f57) / Kronos (f55): blueprints only, NO code yet (greenfield).
- Regime infra LIVE: redis_keys.CURRENT_REGIME, per-pair `bandit:pair_regime:*`,
  signals/engine.py:712 already branches on regime. Structural-edge track is cheapest.
- LIVE WINNERS already prove microstructure > candles (cont.69v): depth_weighted_ofi +1.13,
  exchange_netflow_inflow_fade +0.47 winners; patchtst_direction_confirmer -0.85 LOSER.

## TRACKS (user approved ALL 4 + lean into structural edge)

### Track 1 — Structural edge (CHEAPEST, validated, infra exists)  [START HERE]
From cont.69v (lev5, N=6642): WINNERS = fade/mean-reversion archetypes; LOSERS =
momentum/breakout/continuation + ML confirmers. regime: bull +0.54 / unknown -0.97.
session (UTC h): 12-18h & 03-05h positive; 04/06/09h negative.
ACTIONS: (a) regime!=unknown entry gate; (b) UTC-session multiplier/filter;
(c) archetype capital tilt fade>momentum. CAVEAT: 17-day mostly-bull window -> re-check on
regime shift; gate, don't hard-kill. HIGH-STAKES (live entries) -> show diff + approve before apply.
Plan file: next_impl/structural_edge_filters.md (to create).

### Track 2 — Microstructure online direction model (highest reachable upside)
Wire live OFI/CVD/depth-imbalance into a train==serve online learner (extend
candle_online_trainer.py). Start logging OFI/CVD snapshots to build a replayable corpus.
Use as SOFT prior, not hard gate (cont.69q funding-prior pattern). Plan: next_impl/microstructure_online_model.md.

### Track 3 — TLOB f57 (greenfield; biggest build)
Dual-attention transformer on LOB. Needs LOB depth capture (current micro_ws is L1/OFI, not
full book). Honest: post-cost edge shrinks; F1 decays w/ efficiency. Plan: extend f57_tlob_lob_transformer.md.

### Track 4 — Kronos/FinCast soft prior (bounded greenfield)
Pre-trained foundation model (Kronos f55 OHLCV-native, or open-source FinCast CIKM2025) as
soft magnitude/vol prior in ensemble. NEVER a hard gate (regresses to mean). Plan: extend f55_kronos_foundation_backbone.md.

## SOURCES
- Better-inputs LOB: arxiv 2506.05764 ; TLOB: arxiv 2502.15757
- Order flow / OFI 43% imp, 88% AUC: amberdata blog ; crypto micro patterns arxiv 2602.00776
- FinCast (CIKM2025): arxiv 2508.19609 (open-source: github vincent05r/FinCast-fts)
- Kronos foundation: jonathankinlay.com 2026-02 ; TradeFM: arxiv 2602.23784
- FM regress-to-mean / poor direction: mdpi 2813-0324/11/1/32

## PROGRESS
- Track 1 (regime + session gate): SHIPPED + verified — cont.70b.
- Track 1b (archetype capital tilt fade>momentum): SHIPPED + verified — cont.70d.
- Track 2 (microstructure into online model): SHIPPED cont.70c BUT cont.70e found the 3 features
  NEVER reached the live model — candle_online_train_task (dominant path) runs on the default
  celery_worker which cont.70c never recreated (still 32-col). FIXED cont.70e (recreated
  celery_worker -> 35-col; online bundle re-warmed). Measurement harness ml/shadow_ablation.py LIVE
  (forward prequential ablation, 35 vs 32 col -> shadow_abl:lift). VERDICT cont.70e: lift = -0.0035
  at full window (auc_full 0.5563 vs ablated 0.5598, both ~0.556); oscillates around ~0 = NOISE ->
  NO robust lift -> do NOT gate microstructure; keep as passive features. CONFIRMS input ceiling.
- Track 3 (TLOB f57): NOT STARTED (greenfield; needs LOB depth capture).
- Track 4 (foundation vol/magnitude prior): SHIPPED cont.70f as a CHRONOS REVIVE (not greenfield
  Kronos). Audit found ml/foundation_forecast.py + model already present but dormant+miswired+
  version-bugged. Fixed (1h candles, q10/q90, spread_frac); producer beat task live (sweep_ok=60);
  wired SOFT into SIZING (engine.py) + SL FLOOR (risk/manager.py), both firing. TP blend deferred.
  Kronos (greenfield) DEFERRED to P5, gated on P4 measurement. Plan:
  next_impl/track4_vol_sizing_prior.md.

## SESSION HANDOFF
Tracks 1, 1b done. Track 2 deployed+fixed (cont.70e); its AUC-lift measurement is LIVE but warming
— read `redis-cli get shadow_abl:lift` (+ auc_full/auc_ablated/n/status) after ~15-30 min for the
stabilized value. P6 gate-promotion of microstructure waits on that number. If lift is in the noise
band (~0), that CONFIRMS the input-ceiling diagnosis (don't promote; microstructure stays a passive
feature). Next NEW build: Track 3 (TLOB) or Track 4 (Kronos/FinCast). Both greenfield + need an
image rebuild (prediction/ not bind-mounted). Measure Track 2's directional-AUC lift once the online
model warms before promoting any feature to a gate (P6). Model note (Rule 7): Tracks 3/4 are
non-trivial code creation -> Opus 4.7+ (currently Opus 4.8, OK).
