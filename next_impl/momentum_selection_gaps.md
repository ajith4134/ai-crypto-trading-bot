# Next Implementation — Momentum/AI Selection Gaps (from user's two pasted frameworks)

Created: 2026-06-01 (cont. 68). Topic: audit of two pasted screening frameworks
(multi-stage momentum pipeline + advanced AI frameworks) vs code on disk, and
implementing the genuine gaps.

## Context / symptom
User complaint: "all we are picking are no-movement trades."
Empirical (48h, 2706 closed trades): median price move 0.30%, 49.9% move <0.3%,
BUT avg peak favorable move +$6.52 and only 0.1% never went green → entries DO
move. Root cause of flat *results* was the `mtf_15m_reversal_confirmed` exit
(977 exits / -$4061 / 0.51% avg move / 24min) cutting trades short. That exit's
kill switch (`trail:mtf_15m_reversal_enabled=0`) finally went live at the ~11:27
brain restart — ZERO mtf exits since (verified). Counter `trail:mtf_15m_force_close_count`
frozen at 645. So the no-movement symptom was ~70% an EXIT bug, now fixed.

## Audit result (CONFIRMED, Rule 2 — read on disk)
Already built: volume/spread/OI liquidity filters (scanner/main.py), ROC (xsmom.py),
ADX-14 (score_adx_trend), XGBoost predict-all (prediction/xgb_predictor.py),
HMM regime (ml/hmm.py), funding gate (engine.py:769 F51b), GNN sector contagion
(ml/gnn.py F24 + gnn_multiscale F24M), transformer forecasters (patchtst/tft/mamba/
nhits), on-chain flow (data/onchain_netflow.py), FinBERT+CryptoBERT (ml/sentiment.py),
PPO exit (ppo_policy_exit), BTC-beta proxy (onchain_netflow.py:_btc_beta_proxy).
Rejected correctly: LightGBM (XGBoost dup, cont. 66).

## GAPS (implementable), ranked value-for-movement vs effort
1. [HIGH/trivial]  RSI 60-75 momentum band — NOT in scanner. Add score_rsi() + weight.
2. [HIGH/trivial]  Distance-from-50-EMA score — NOT computed. Add score_ema_distance().
3. [HIGH/medium]   Idiosyncratic-momentum filter — _btc_beta_proxy EXISTS but unused as
                   entry filter. Strip BTC-driven fake altcoin momentum at entry.
4. [MED/medium]    Feed FinBERT live text — model built+wired but starved
                   (sentiment:source=fear_greed_proxy). Needs news/social collector feed.
5. [LOW/large]     PPO capital allocator (entry sizing) — marginal over Kelly (ml/kelly.py).
6. [LOW/large]     TimeGAN synthetic stress — safety/backtest tool, NOT a movement fix.
                   (User initially selected this; flagged as least relevant to symptom.)

## Decisions
- CONFIRMED: build order 1 -> 2 -> 3 -> 4. 5/6 optional/deferred.
- PROPOSED: RSI/EMA scores enter the composite via new criteria_weights keys
  (rsi, ema_distance), summing renormalized to 1.0; gate behind F-flags so the
  F10 weight learner can tune them. Cold-start neutral=50 (same pattern as candle_setup).
- OPEN: exact RSI band reward shape (hard 60-75 window vs smooth) — pending.

## Checklist (per item, Rule 4 production-grade)
- [x] RSI: _wilder_rsi() + _rsi_momentum_score() (SYMMETRIC around 50, penalises
      45-55 flat zone). In score_trend_momentum(). composite term + config weight
      rsi:0.08. Unit-tested (chop RSI 48→score 25; uptrend RSI 77→70). DONE.
- [x] EMA-distance: _ema_distance_score() (symmetric, penalises <0.5% hug + >8%
      overext). composite term + config weight ema_distance:0.06. Unit-tested. DONE.
- [x] Idiosyncratic: F53 gate in signals/engine.py after funding gate. Uses
      _btc_beta_proxy; rejects when move mostly BTC-driven AND direction rides BTC.
      Toggle entry:idiosyncratic_gate_enabled (DEFAULT 0/OFF), entry:idiosyncratic_min_frac
      (0.30), counter signal:reject:btc_beta_fake_momentum. Logic unit-tested. DONE.
- [x] FinBERT feed (item 4): CORRECTION — model was NOT starved. The free-RSS→
      web_intelligence→FinBERT chain (celery_app.score_web_intel_sentiment, */5, F18)
      is LIVE and writes sentiment:source=cryptobert+finbert. Earlier proxy snapshot
      was a transient stale-window. REAL issue: CPU BERT took 401s/run (>5min cycle)
      so it lagged past the 30min proxy-freshness window → regressed to stuck F&G=29.
      FIXES (celery_app.py, mounted, worker restarted): (a) schedule */5→*/10 (no
      overlap); (b) truncate text 1500→400 chars (~3× faster inference); (c) per-pair
      normalize BTC/"BTC/USD"→BTCUSDT + drop forex noise (USD/IRR/USD/VND/USD) — this
      ALSO fixes a latent bug where update_pair_sentiment wrote "BTC:sentiment" but
      consumers read "BTCUSDT:sentiment" (per-pair sentiment was dead). DONE.
- [ ] PPO capital allocator (item 5): LARGE. entry position-sizing via PPO over
      Kelly baseline (ml/kelly.py). Off-symptom; low priority.
- [ ] TimeGAN (item 6): LARGE. synthetic vol-scenario stress sim. Off-symptom.
- [ ] FINAL: rebuild image so config.yaml (10 weights) is baked; today it's activated
      via Redis brain:feature_weights seed (10 keys, sum 1.0). Update PROGRESS.md.

## Activation state (cont. 68)
- scanner/main.py + signals/engine.py LIVE via bind-mount (./scanner, ./signals).
- config.yaml NOT mounted → new weights activated via Redis brain:feature_weights
  10-key seed {volume.08,volatility.14,spread.06,winrate.17,pnl.06,candle_setup.13,
  adx_trend.12,open_interest.10,rsi.08,ema_distance.06}. Durable on next rebuild.
- Item 3 gate is OFF by default — enable + paper-validate:
  `redis-cli set entry:idiosyncratic_gate_enabled 1`, then watch
  signal:reject:btc_beta_fake_momentum and flat-trade %.

## VALIDATION (cont. 68, user: "validate 1-4 first")
- Item 1+2 RSI/EMA: ✅ PROVEN LIVE after scanner restart (process hot-reload — bind-mount
  alone didn't reload the running proc). Log: scanner_adx_pass2 n_rsi_momentum=19
  n_rsi_flat=18 n_ema_trending=21 n_ema_flat=8. 18/50 top candidates flat→penalised 25.
- Item 4 sentiment: ✅ real cryptobert+finbert live (n=44, normalized 0.4615). Manual
  run 362.8s (was 401s; most texts already short). */10 + worker restart deployed.
  Added active-pairs validation filter (drops LLM noise like BLOCKCHAINUSDT/IRRUSDT) —
  worker restarted to load.
- Item 3 idiosyncratic gate: ⚠️ built + plumbing validated, but was INERT. ROOT CAUSE
  (Rule 2): data/feed.py polls short candles limit=65, so 1m:candles never exceeds ~65,
  but _btc_beta_proxy required ≥100 → beta=0 for ALL pairs → gate never fires (also killed
  F52). FIX applied: onchain_netflow.py floor 100→60, m 60→40. Verified real betas compute
  (ETH .12 SOL .26). **NOT yet live in brain** — brain mounts ./signals,./ml,./risk,./memory
  but NOT ./data, so onchain_netflow.py needs an IMAGE REBUILD. DEFERRED until 1h training
  finishes (rebuild would kill it). Gate toggle is ON but fail-open/harmless until then.
- 1h candle training: IN PROGRESS, NOT complete. 73 min/epoch CPU (epoch 0 done 12:42,
  val_loss 0.4962). No candlenet_1h.pth yet. ~4-7h more (early-stop patience 2).

## DEPLOY-PENDING (next rebuild, AFTER 1h training completes)
- config.yaml 10 weights (currently Redis-seeded brain:feature_weights).
- data/onchain_netflow.py beta floor (activates item 3 + revives F52).
- Use selective `docker compose up -d brain scanner celery_worker` to avoid recreating
  celery_worker_cn_train (preserves training).

## Session handoff
Items 1,2,4 LIVE+validated. Item 3 built+fixed, activates on next brain rebuild. Items
5/6 (PPO/TimeGAN) NOT started — large, off-symptom, user chose validate-first. Model: Opus.
