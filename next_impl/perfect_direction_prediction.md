# Perfect-Direction Prediction Across Bull + Bear Regimes — Deep Research & Plan

Topic slug: `perfect_direction_prediction`
Created: 2026-05-30 (cont. 65d)
Trigger: Owner — "do a we deep thinking and advaced research to makeit possible … long short in both bull and bear regime … add 30m if it helps"

---

## A. What the live data actually shows (Rule 2 — measured, not assumed)

Snapshot at 2026-05-30 15:09 UTC, 88 open + 1980 closed-in-last-24h trades:

| Symptom | Value |
|---|---|
| Open trades: short | 98 / 99 (99%) |
| Open trades: long | 1 / 99 (1%) |
| Open trades: market_regime at entry = bear | 99 / 99 (100%) |
| Closed-24h: short trades | 1741 |
| Closed-24h: long trades | 239 |
| Closed-24h shorts: avg net_pnl | **-$1.44** |
| Closed-24h longs: avg net_pnl | -$0.54 |
| Closed-24h shorts: total | **-$2,515** |
| Closed-24h longs: total | -$129 |
| Closed-24h: peak_loss < -$1 (price moved against entry) | 1288 / 1978 (65%) |
| Closed-24h: avg peak_LOSS reached | -$12.68 |
| Closed-24h: avg peak_PROFIT reached | +$10.89 |
| Closed-24h: reached >$0.5 profit then closed negative ("give-back") | 495 / 1978 (25%) |
| **Direction prediction edge** (CandleNet 1m dir1 aligned with trade direction): | **51.3% win rate = coinflip** |
| Same for 5m: | 51.3% |
| Same for 15m: | 51.1% |
| Strong-aligned (\|dir−0.5\|>0.15): 5m | 52.2% (marginally better) |
| 1h CandleNet present in feature_vector | 0 / 1980 (producer down) |
| Entry-price == mark-at-entry | 87 / 87 (no slippage) |

**Two conclusions stand out:**

1. **The direction predictor adds no usable edge.** 51.3% win rate when trade direction matches prediction is statistical coinflip. The bot is opening trades in the direction CandleNet says — but that direction is wrong half the time.

2. **The bot is severely short-biased because the regime is permanently "bear".** The Redis `current_regime` key has held `bear` continuously; 100% of open trades opened in bear regime; the upstream entry pipeline only fires shorts in bear regime. Every counter-rally squeezes a wave of shorts.

The "perfect entry, bad direction" framing the owner stated is **correct** — execution is clean (`entry_price == mark`, no slippage, 0 stuck-from-bug), the loss source is the directional decision itself plus regime mono-culture.

---

## B. Root-cause decomposition

| # | Cause | Evidence |
|---|---|---|
| B1 | **Direction-model has no edge** | 51.3% accuracy when trade-direction matches model output |
| B2 | **Regime stuck "bear"** | Redis `current_regime=bear` for ≥24h+; HMM model is `hmm_regime.pkl` last trained 2026-05-29 07:24 — reasonably fresh but state hasn't switched |
| B3 | **Short-only when bear** | 99% of opens are shorts in bear regime; the entry pipeline filters out longs in bear (signals/engine.py routing) |
| B4 | **1h CandleNet completely down** | 0 / 127 pairs have `:1h:candle_forecast`; the cascade vote is missing its longest-horizon signal |
| B5 | **No 30m intermediate TF** | Stack is 1m → 5m → 15m → 1h (gap is huge: 15m to 1h skips 4× horizon, exactly where intraday trend reversals live) |
| B6 | **Single-model dependence** | CandleNet provides dir/mag for every TF, but is the ONE model deciding direction. No diversifying ensemble. |
| B7 | **No conformal-confidence filter** | `conformal_confidence` column is NULL on all trades — F56 conformal wrapper not gating entries (would tell us "this prediction is in a high-uncertainty band, don't trade") |
| B8 | **No order-flow / microstructure direction signal** | feature_vector has `ofi`, `bid_ask_imbalance`, `funding_rate` recorded but the entry decision uses only CandleNet dir + magnitude. OFI/CVD/VPIN have known **linear** relationship with short-term price (literature §F) but aren't gating direction |

B1 + B6 + B8 together are the smoking gun: **one model with no edge is making all directional calls** while several other independent signals sit unused in the feature vector.

---

## C. Why CandleNet has no edge today (hypothesis tree)

Three plausible failure modes, ranked by likelihood:

1. **Training-distribution mismatch.** CandleNet was last retrained 2026-05-29 (yesterday). If May 28-29 was choppy / regime-edge and the model trained on a window that doesn't generalise to today's micro-structure (ETF outflow Friday + weekend thinness), the dir1/dir3 outputs are essentially noise. Hit rate ~50% is consistent with that.
2. **Label leakage / overfit.** dir1/dir3 are probability scores around 0.5 (we saw 0.248, 0.2347 — i.e. weak shorts). When the model is uncertain (dir near 0.5), the predictor IS noise but the consumer treats it as a directional vote.
3. **Wrong target.** dir3 = "probability next 3 candles close above current" is a directional CLASSIFIER, but the trade's outcome over 5-30 minutes depends on PATH (drawdown before close), not endpoint. Hit-rate metrics hide path-dependent losses.

All three argue for the same answer: **don't depend on one direction model. Combine it with orthogonal signals and only fire when several agree.**

---

## D. SOTA research summary (2026 papers, web-sourced)

- **TFT vs Kronos vs PatchTST**: zero-shot foundation models (TimeGPT, Chronos / Kronos) tens-of-times faster than custom TFT and on par for crypto hourly. TFT wins when fine-tuned with extra features. (Sources at bottom.)
- **Stacking ensembles** for crypto 30m direction reach **~82% accuracy, 88% AUC-ROC** when combining LSTM + GRU with sentiment + technicals. That is *meaningfully above the 51% we have now*. (Bitcoin LSTM-GRU stacker, Mathematics 10(8), MDPI.)
- **OFI / CVD / VPIN** have documented **linear** relationship with near-term price; Hawkes-process OFI estimation captures lagged bid/ask dependence. VPIN > 0.7 is a reliable warning of an imminent directional move. (Dean Markwick OFI guide; Buildix VPIN guide.)
- **HMM regime detection** literature: 3-state outperforms 2-state. **Non-Homogeneous HMMs** (time-varying transition matrices) + **ensemble-HMM voting** beat classic HMM. Recent work also recommends Wasserstein-clustering regime detection over single HMM. (Preprints 202603.0831.)
- **30m timeframe specifically**: the LSTM+GRU stacker that reaches 82% directional accuracy targets exactly 30 min ahead. Confirms the gap in our 1m → 5m → 15m → 1h stack.
- **Adaptive Temporal Fusion Transformer (arXiv:2509.10542)**: adds regime-aware gating to TFT for crypto. Better than vanilla TFT on volatility-spike windows — directly relevant to our "stuck bear, getting squeezed on rallies" problem.

Take-aways for our architecture:

- Add a **30m TF** to close the 15m→1h gap (low cost: extend the CandleNet worker loop).
- Add a **direction ensemble**: CandleNet + OFI/CVD microstructure + a fast 30m sequence model. Trade only when ≥ 2 of 3 agree.
- Add a **conformal-band filter** (F56 already exists in scaffold per next_impl/f56_conformal_prediction_wrapper.md): if conformal interval crosses zero, prediction confidence is too low → skip.
- Improve regime detection: 3-state HMM, **dwell-time hysteresis** (don't flip on a single-bar transition), and an **NHHMM** variant. Plus expose `current_regime_confidence` so the entry pipeline can de-bias when confidence is low.
- Unblock the **1h CandleNet producer** so the cascade has its long-horizon vote.

---

## E. Proposed architecture (the "perfect direction" pipeline)

Three layers, each independently testable, and each can ship in isolation.

### Layer 1 — Add 30m + unblock 1h CandleNet (1 day, low risk)

1. Extend `celery_app.compute_candle_forecast` to include `30m` alongside `1m, 5m, 15m, 1h`. Output `{pair}:30m:candle_forecast` with the standard `{dir1, dir3, dir5, mag1, mag3, mag5, trend}` schema.
2. Diagnose the 1h producer. Suspect: `retrain_candlenet_1h` weekly task hasn't produced the model file or the loader fails. Restore.
3. Extend `feature_vector` keys: `cn_30m_dir3, cn_30m_mag3, cn_30m_trend` join the existing per-TF keys.
4. Cascade vote (`signals/multi_tf_cascade.py`) considers all 5 TFs. Voting rule: simple majority of (1m, 5m, 15m, 30m, 1h) with weighted confidence — longer TFs weight more for direction, shorter for entry timing.

**Why first**: closes the producer gap and uses only existing infra. Even without the bigger ensemble work, this should lift direction accuracy a few points by adding one more independent vote.

### Layer 2 — Orthogonal direction ensemble (1 week, medium risk)

A **vote-of-three** ensemble at signal-engine entry time:

| Voter | Source | Strength when |
|---|---|---|
| **CandleNet cascade** | existing 1m/5m/15m/30m/1h CandleNet dir3 majority | Trend continuation |
| **Microstructure signal** | OFI(L5) + CVD slope + VPIN-gated sign of order flow imbalance — Hawkes-process estimator from `risk/frontier/exit_signals.py:evaluate_hawkes` already implements the math; promote to ENTRY-side use too | Imminent directional move (next 1-5 min) |
| **30m sequence model** | new lightweight LSTM/GRU stacker on (price returns, OFI, funding, on-chain netflow) targeting 30-min ahead direction — train daily on F9 corpus | Intraday mean-reversion / reversal |

**Decision rule** (replaces current direction routing in `signals/engine.py`):
- If **all 3 agree** AND conformal interval doesn't cross zero (F56 wrapper) → take trade in that direction.
- If **2 of 3 agree** AND conformal-band confidence > threshold → take trade with size_mult = 0.5 (half-size, half-Kelly bet).
- If **disagreement** → **skip the signal**. No trade is better than a coinflip trade at -$1.44 EV.

This explicitly enables **longs in bear regime** when ensemble agrees on long — eliminates the regime mono-culture (B3).

### Layer 3 — Regime detection upgrade (2-3 days, low risk to retire old HMM)

1. **3-state HMM (bull / bear / chop)** retrained nightly on rolling 30 days, with Bayesian dwell-time prior so a single noisy bar can't flip the regime — eliminates the "stuck bear" symptom.
2. **Expose `current_regime_confidence`** (posterior probability of the most-likely state). Entry pipeline gates: if confidence < 0.6, fall back to *regime-agnostic* mode (Layer 2 ensemble decides direction without regime filter).
3. **Volatility regime overlay** (vol_unit z-score): in HIGH vol, halve the size_mult AND require Layer-2 unanimous agreement (not 2/3). In LOW vol, allow either 2/3 or unanimous.

---

## F. Detailed implementation checklist

```
Layer 1 — Producer + 30m
[ ] celery_app.py: add 30m to compute_candle_forecast TF list
[ ] celery_app.py: diagnose why retrain_candlenet_1h not producing forecasts.
    Most likely candidates: missing model file at /app/models/candlenet_1h.pkl
    OR pipe to {pair}:1h:candle_forecast failing silently.
[ ] signals/multi_tf_cascade.py: add 30m to TF list + weights
[ ] memory/write.py write_trade_create: persist cn_30m_dir3, cn_30m_mag3,
    cn_30m_trend into feature_vector
[ ] feature_governance/bootstrap.py: register F48_30m feature

Layer 2 — Direction ensemble
[ ] signals/direction_ensemble.py (new):
    - vote_candlenet(pair) → (dir, conf) from cascade
    - vote_microstructure(pair, r) → (dir, conf) from OFI/CVD/VPIN
    - vote_seq_30m(pair) → (dir, conf) from new 30m LSTM-GRU stacker
    - aggregate_votes(...) → (final_dir, size_mult, reason)
[ ] ml/seq_30m.py (new): train + predict the 30m LSTM-GRU
    - Source: Mathematics 10(8) (2022) MDPI stacker design
    - Inputs: (returns_1m, returns_5m, ofi_z, funding_rate, change_24h,
      vpin, candle_pattern_cluster) over a 60-step window
    - Output: P(next-30m-close > current)
    - Train daily via celery_beat from F9 corpus
[ ] risk/frontier/exit_signals.py:evaluate_hawkes — REFACTOR to also expose
    an entry-side `direction_vote_hawkes(pair, r)` (most logic already there)
[ ] signals/engine.py:process_signals — REPLACE the existing single-model
    direction call with `direction_ensemble.aggregate_votes(...)`. Skip
    trade if disagreement; halve size if 2/3.
[ ] memory/write.py: persist {ensemble_vote_candlenet, ensemble_vote_micro,
    ensemble_vote_seq30, ensemble_size_mult, ensemble_skip_reason} into the
    new `signals_at_entry` JSONB column (currently 0/88 populated — wire it).
[ ] F56 conformal: integrate as final pre-trade veto

Layer 3 — Regime upgrade
[ ] ml/hmm.py: 2-state → 3-state HMM. Add dwell-time hyperprior.
[ ] ml/hmm.py: expose `get_regime_confidence()` returning posterior.
[ ] redis_keys: add CURRENT_REGIME_CONFIDENCE.
[ ] signals/engine.py: if regime confidence < 0.6, set regime to "unknown"
    for the entry pipeline (regime filter relaxes; ensemble decides solo).
[ ] pretrainer/main.py train_hmm: retrain nightly on rolling 30d data.
[ ] Bonus: NHHMM with funding-rate / VIX-equivalent as exogenous covariate
    (2026 preprints 202603.0831 recipe).

Verification scaffold (Layer 4 — non-functional but required for honesty)
[ ] Per-trade ensemble vote breakdown logged in `signals_at_entry`.
[ ] New counters: signals:ensemble_unanimous_count,
    signals:ensemble_2of3_count, signals:ensemble_skipped_count
[ ] Decoded F9 corpus: for the next 1k trades after deploy, compare ensemble
    win rate vs current 51.3% baseline. Need 95% CI lift ≥ 5 pp before
    declaring success.
[ ] Per-regime accuracy report (bull/bear/chop) — confirm long trades in bear
    regime are profitable when ensemble agrees.
```

---

## G. Owner decision points

| # | Decision | Default I'd pick | Why |
|---|---|---|---|
| G1 | Ship Layers in order 1 → 2 → 3, or in parallel? | **Sequential** | Layer 1 is a 1-day producer fix that may already lift accuracy; measure before investing in Layer 2 |
| G2 | New 30m model: LSTM-GRU stacker (per the 82%-accuracy paper) vs PatchTST vs Kronos | **LSTM-GRU stacker** | Lowest infra cost, paper shows 82% on 30-min Bitcoin direction; we have the training corpus (F9) |
| G3 | Entry-time ensemble rule: 3/3, 2/3, or weighted-score? | **3/3 full-size, 2/3 half-size, ≤1 skip** | Aligns to half-Kelly; matches conformal-band confidence tiers |
| G4 | Regime upgrade: 3-state HMM only, or 3-state HMM + NHHMM? | **3-state HMM first**, NHHMM is a follow-up | NHHMM is research-grade complexity; 3-state already moves us off "permanent bear" |
| G5 | Paper-mode trial duration before deciding on Layer 2 deploy | **2 weeks** | The 51% baseline is on 1980 trades / 24h; need a comparable sample at the new architecture |
| G6 | If Layer 2 ensemble disagreement rate is very high (say 70%+), should we relax to 2/3 only? | Tentatively **no** — disagreement means low edge, skipping is correct | But re-evaluate after 1k trades |

---

## H. Risk register

- **Refusal to trade**: ensemble could disagree often → trade volume drops. That's GOOD for capital preservation (current edge is negative) but reduces sample size for learning.
- **Look-ahead bias** in the 30m stacker: ensure the 60-step window is strict-past. Add a unit test.
- **Stale models**: nightly retrain only catches yesterday's regime. For sudden ETF/news shocks the model will lag. Mitigate with the VPIN > 0.7 kill-switch already in `evaluate_hawkes` (force-flat).
- **Compute load**: stacker inference per pair per signal is ~5-15ms on CPU. With 127 pairs and ~1Hz signal rate = ~2s/sec CPU. Run on dedicated celery worker.

---

## I. References (web research, 2026)

- [Forecasting High Frequency OFI](https://www.researchgate.net/publication/382944327) and [Hawkes-OFI paper arXiv:2408.03594](https://arxiv.org/html/2408.03594v1)
- [OFI guide — Dean Markwick](https://dm13450.github.io/2022/02/02/Order-Flow-Imbalance.html)
- [VPIN flow toxicity — Buildix](https://www.buildix.trade/blog/what-is-vpin-flow-toxicity-crypto-trading)
- [Stacking LSTM-GRU for BTC 30-min direction — Mathematics 10(8), MDPI](https://www.mdpi.com/2227-7390/10/8/1307)
- [Adaptive TFT for Crypto — arXiv:2509.10542](https://arxiv.org/abs/2509.10542)
- [HMM regime detection 2024-2026 — Preprints 202603.0831](https://www.preprints.org/manuscript/202603.0831)
- [Markwick / QuantStart HMM regime](https://www.quantstart.com/articles/hidden-markov-models-for-regime-detection-using-r/)
- [Volatility regime detection — Volatility Box](https://volatilitybox.com/research/volatility-regime-detection/)
- [TFT-based crypto multi-asset strategy — MDPI Systems 13(6)](https://www.mdpi.com/2079-8954/13/6/474)
- [Comparative ML for crypto trading — Springer 2025](https://link.springer.com/article/10.1007/s44163-025-00519-y)
- [BTC direction on-chain feature selection — Sciencedirect 2025](https://www.sciencedirect.com/science/article/pii/S266682702500057X)

---

## J. Session handoff

### Cont. 65e (initial Layer 1 attempt, 2026-05-30)
- Wrote Layer 1 code (ml/candlenet.py, signals/multi_tf_cascade.py, celery_app.py,
  feature_governance/bootstrap.py). PROGRESS.md cont. 65e claimed deploy. **NEVER ACTUALLY DEPLOYED.**

### Cont. 65f (Rule-2 audit + real deploy, 2026-05-30)
**Verification revealed:**
- Containers were never rebuilt → 0 occurrences of `30m` in running brain/candlenet images.
- 30m + 1h `.pth` files never created → 0 forecasts in Redis.
- Trade flow unchanged: 110 opens / 30 min, 100 % shorts, 0 longs.
- `trades.signals_at_entry` column never written by any code path (only
  reference is a sub-key inside brain_actions JSONB at memory/write.py:742) →
  cascade audit measurement is impossible until wiring is added.

**Out-of-band finds:**
- `update_pattern_registry_task` raised `NameError: log` every 5 min since cont. 63.
- Pretrainer `_csv_dir.glob("*.csv")` is non-recursive but CSVs live at
  `data/historical/<PAIR>/<INT>.csv` → sentinel always reads 0 → step_1 always
  re-runs. Saved by per-file `if out_file.exists(): continue` inside
  `download_ohlcv`. Fixed to `.rglob` in this session.

**Actions taken:**
1. Bug fixes: `_log` typos in celery_app.py:3396,3406,3408; `.rglob` in
   pretrainer/main.py:654.
2. F49 trackers extended: `candlenet_30m`, `candlenet_1h` added to
   `ml/drift_detector.py`, `ml/performance_monitor.py`, `ml/conformal_wrapper.py`.
3. 30m source per owner decision (G-30m): `pretrainer/main.py:39 INTERVALS`
   gains `"30m"`. Pretrainer fetches real exchange 30m candles, not resampled.
4. Pair coverage bumped: `DOWNLOAD_PAIRS 300 → 350`.
5. Memory cap per owner decision (G-mem): `celery_worker_candlenet` deploy
   memory `1500M → 3000M` to fit 1h training (92K samples OOMed at 1.5 GB).
6. Hardcoded `× 6` references in pretrainer replaced with `× len(INTERVALS)`
   so they never go stale again.
7. Containers rebuilt + recreated: `brain`, `celery_worker`,
   `celery_worker_candlenet`, `celery_beat`.

**Blocked work remaining for Layer 1 fidelity:**
- [ ] Build + run pretrainer to fetch 30m CSVs (350 pairs × 30m = ~20 MB).
      Expected new files: 300 × 30m + 50 new pairs × 7 TFs ≈ 650 downloads.
- [ ] After pretrainer completes, re-trigger `retrain_candlenet_30m.delay()`
      and `retrain_candlenet_1h.delay()` on the 3 GB worker.
- [ ] Verify both `.pth` files exist + next `candlenet_infer_all` cycle
      reports `skipped ≤ 100` (down from 204).
- [ ] Verify F49 drift snapshot populates for both new models.

### Cont. 65g+ next-session prerequisites for Layer 1b vs Layer 2 decision

1. **Wire `trades.signals_at_entry` write path** in
   `memory/write.py:write_trade_create` to persist cascade_audit + per-TF
   votes at entry. Small change (~50 lines). Without this we cannot measure
   per-TF alignment vs outcome and the Layer 1b vs Layer 2 decision is
   blind.
2. After 24 h of populated audit dicts, compute per-TF alignment vs outcome.
3. Decision per next_impl §G1: lift ≥ 4 pp → Layer 1b (extend FEATURE_COLUMNS
   + retrain XGB direction model); no lift → Layer 2 (OFI/CVD ensemble + 30m
   LSTM-GRU stacker).

Delete this file after Layer 1 fidelity is reached AND Layer 1b/2 decision
is executed.

Linked memories: [[feedback-profit-lock]], [[feedback-verify-before-fix]],
[[feedback-silent-rejection]], [[feedback-blueprint-grade-check]],
[[feedback-bind-mount-recurrence]] (cont. 65e's claimed-but-undeployed
fits this pattern exactly).
