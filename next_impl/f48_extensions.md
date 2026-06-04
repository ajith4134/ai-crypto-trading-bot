# Next Impl — F48 CandleNet Extensions
_Created: 2026-05-25 | Status: ACTIVE_

> **Status update (cont. 49):** code is fully in containers. The cont. 47 pretrainer run (with lazy-GAF Dataset) ran 21h and stalled at CandleNet 1m epoch 10/25 with stagnating val_loss (17.44 → 17.35). Root cause was NOT memory — it was (a) MSE on raw % `mag` labels dominating gradient by ~20× vs BCE(dir), and (b) lazy GAF recomputing the same image per sample per epoch on only 2 PyTorch threads. cont. 49 fixes both without any tradeoff — see "Speed + convergence redesign" below. This topic stays ACTIVE until all three `candlenet_*.pth` files exist and validation gates pass.

---

## Speed + convergence redesign (cont. 49)

Per Rule 5 — when blueprint fidelity blocks an unfixable issue, redesign first. The cont. 47 lazy-GAF Dataset solved memory but introduced a new bottleneck: per-sample GAF compute × 25 epochs × 92K samples × single-threaded data loader = ~40 min/epoch. The cont. 49 rewrite keeps all 7 ideas, all 25-epoch ceiling, all 92K samples, all validation gates — only the training mechanics change:

1. **`torch.set_num_threads(4)` + `set_num_interop_threads(2)`** in `pretrainer/main.py` (was implicit 2 threads). 2× speedup on Conv/Linear ops.
2. **Precomputed GAF cache** — `np.empty((N, 4, 60, 60), dtype=np.float16)` allocated once before the epoch loop, populated in ~2 minutes, then `_CachedGAFDataset.__getitem__` becomes an O(1) slice. fp16 keeps it ≤2.7 GB (vs 5.3 GB fp32, vs 5GB+ Python list overhead in cont. 46 OOM). Eliminates 25× redundant GAF compute.
3. **`SmoothL1Loss(z_score(mag))`** replaces `MSE(raw % mag)`. mag head's gradient now lives on the same scale as BCE(dir), so the direction head can actually learn. mag_mean / mag_std persisted in checkpoint so inference un-scales predictions back to % move (`run_inference` + `_get_mag_scaler`).
4. **Early stopping with patience=5** on val_loss. Auto-finishes when converged; exits cleanly if stuck. 25-epoch ceiling preserved.
5. **Per-epoch logging** (was every 5). Visibility into per-epoch tr_loss / val_loss / patience_left / epoch_secs.

**Expected:** ~5 min/epoch × ~15 epochs (typical convergence) = ~1.5h per interval × 3 = ~5h total for all CandleNets. Entry-timing PPO is separate (no candle compute). All blueprint-grade work preserved.

---

## Approaches that did NOT work (cont. 47)

- **Eager GAF pre-computation during candlenet.train() as Python list of tensors.** First run produced ~92,700 training windows × 57.6 KB GAF tensor each, but in a Python list with per-tensor refcount + dict overhead ≈ ~5 GB peak RAM + Python heap bloat. Pretrainer exited 137 (OOM-kill) at step_7. **Note:** cont. 49's precomputed-GAF cache is a SINGLE contiguous fp16 numpy allocation (2.7 GB, no per-tensor overhead) — this is the correct pattern; cont. 47's mistake was the Python-list shape, not eager precompute per se.

---

## Research & References

- [arXiv:2508.02356](https://arxiv.org/abs/2508.02356): Neural Network-Based Algorithmic Trading — Multi-timeframe direction prediction networks combining 1m/5m/1h with multi-head CNN + orderbook statistics. Achieves positive risk-adjusted returns. Key finding: **higher TF sets context, lower TF executes**. Full hierarchy is the production-grade approach.

- [arXiv:2603.19136](https://arxiv.org/pdf/2603.19136): Adaptive Regime-Aware Stock Prediction — Autoencoder detects regime anomalies, **dual Transformer heads** (one for stable, one for event-driven conditions), SAC RL controller blends them. Achieves 0.59% MAPE on S&P 500. Key finding: routing inference through different heads per regime dramatically outperforms a single model.

- [arXiv:2306.17178](https://arxiv.org/pdf/2306.17178): Optimal Execution Using RL — PPO/SAC agent for entry timing. State: 512-dim vector updated every 100ms. Action: wait / enter / skip. Multi-agent RL earned +4.7% while market fell -11%. Key finding: **entry timing as a separate RL problem** from direction prediction is a well-validated production architecture.

- [PMC:11935771](https://pmc.ncbi.nlm.nih.gov/articles/PMC11935771/): CNN on candlestick patterns — GAF (Gramian Angular Field) image encoding of OHLC + CNN achieves 90–93% pattern classification accuracy. Key finding: **GAF is the best-suited way to encode candlestick sequences visually**, preserving temporal dependency in the image structure.

- [arXiv:1901.05237](https://ar5iv.labs.arxiv.org/html/1901.05237): Encoding Candlesticks as Images — GAF encoding window=10, shape (10,10,4), CNN similar to LeNet. CNN-LSTM outperforms CNN alone (82.7% accuracy). Key finding: combining spatial (GAF-CNN) with sequential (GRU) is better than either alone.

- [ResearchGate TCN vs GRU](https://www.researchgate.net/publication/395804879): TCN slightly better MAE/MAPE, GRU better MSE. TCN converges in <100 epochs vs ~1000 for CNN-LSTM. TCN = **10× faster training, fully parallelizable, better on long sequences**. GRU marginally better on large deviations. Verdict: TCN-GRU hybrid beats both.

- [markettaker.com exhaustion](https://markettaker.com/2025/02/how-to-identify-trend-exhaustion/): Exhaustion = consecutive same-direction candles 2–3× historical average + decreasing body size + declining volume. "When streak exceeds average by 2–3×, look for counter-trend". Key finding: this is a statistical signal, not a gut call — **computable and model-able**.

- [MDPI:2079-8954](https://www.mdpi.com/2079-8954/14/1/111): RL for crypto trading with PPO. Volatility-scaled SL: tighten to 1.5×σ during high-vol, 1.0×σ in low-vol. Drawdown reduced from 25.6% to 12.3%. Key finding: **magnitude prediction → dynamic TP/SL sizing** is validated and production-grade.

- [arXiv:2507.18983 KASPER](https://arxiv.org/pdf/2507.18983): KAN (Kolmogorov-Arnold Networks) for regime detection + explainability. Not a direct implementation target but confirms **regime-conditioned routing** is active research in 2025-2026.

---

## Feature Descriptions

### Idea A — Exhaustion Scorer (Counter-Trend in Bull/Bear)

**Problem it solves:** In a bull market, the bot avoids all shorts (F48 trend ceiling). But after 7+ consecutive green candles with shrinking bodies and declining volume, a pullback is highly probable and profitable. The bot currently misses these entirely.

**How it works:**
- Computes a rolling `exhaustion_score` from CandleNet's feature matrix (already computed for inference):
  1. `streak` = count of consecutive same-direction HA candles
  2. `body_trend` = slope of HA body sizes over streak (shrinking = exhaustion)
  3. `vol_trend` = slope of volume over streak (declining = exhaustion)
  4. `pattern_flag` = shooting_star or doji flag on final candle (both already in F48 features)
- `exhaustion_score = streak_z_score × (-body_slope) × (-vol_slope) × (1 + pattern_flag)`
  where `streak_z_score = (streak - historical_mean) / historical_std` per pair per timeframe
- Score > 2.0 = **exhaustion detected** → counter-trend entry window opens for 3 candles
- When in exhaustion window + signal is counter-trend (short in bull): override trend ceiling (allow up to 70% confidence instead of 55% hard cap)
- When in exhaustion window + signal confirms exhaustion direction: add +20 to candlenet_bonus

**Architecture:** Pure statistical layer on top of existing F48 features — no new model needed. Computed inside `run_inference()` as an additional output field.

**Redis output:** `{pair}:{interval}:exhaustion_score` (float, TTL 90s)

**Integration in signals/engine.py:**
- Read exhaustion_score alongside candle_forecast
- If exhaustion_score > 2.0 AND signal direction contradicts F48 trend: loosen trend ceiling to 70%
- If exhaustion_score > 2.0 AND signal direction confirms exhaustion: add +20 to candlenet_bonus

---

### Idea B — Magnitude-Driven Dynamic TP/SL

**Problem it solves:** Current TP and SL are ATR-multiple based — static per trade. CandleNet already predicts `mag1`, `mag3`, `mag5` (% expected move at 1, 3, 5 candles ahead). These are not used for TP/SL sizing — a wasted signal.

**How it works:**
- `mag1` = expected % move in 1 candle = natural TP target
- `mag3` = expected % move in 3 candles = outer TP (scale-out level)
- `mag5` used as SL validation: if `mag5` contradicts `mag1` direction, widen SL (market not committing)
- `tp1 = entry_price × (1 + mag1 × direction_sign)` — first TP (take partial)
- `tp2 = entry_price × (1 + mag3 × direction_sign)` — second TP (remainder)
- SL width = max(ATR × multiplier, mag1 × 0.5) — never smaller than half the expected move
- If `mag1 < 0.3%` on both TFs: skip trade (low-value setup) — already implemented as -5 bonus

**Architecture:** No new model. Change in `risk/manager.py` to read `candle_forecast` and compute TP from `mag1`/`mag3`.

**Where to implement:** `risk/manager.py` — `compute_initial_sl_tp()` function.

---

### Idea C — RL Entry Timing Agent (PPO)

**Problem it solves:** The bot enters trades the moment the signal score crosses the threshold — which might be mid-candle, during a spread spike, or just before a micro-pullback. An RL agent decides: enter now vs wait one candle vs skip.

**How it works:**
- **State vector** (per-step, updated at 1m tick):
  - CandleNet 1m forecasts: dir1, dir3, dir5, mag1, mag3, mag5, trend (7)
  - CandleNet 5m forecasts: same (7)
  - exhaustion_score_1m, exhaustion_score_5m (2)
  - current_spread_pct, bid_ask_imbalance (2)
  - time_in_current_bar_pct (0–1) (1)
  - recent_5m_volatility (ATR-normalized) (1)
  - signal_score_normalized (1)
  - time_since_signal_fired (in candles) (1)
  - Total: ~22 features
- **Actions:** 0=wait, 1=enter_now, 2=skip_signal
- **Reward:** (trade_pnl_if_entered_now - trade_pnl_at_signal_open) / ATR
- **Algorithm:** PPO (Stable-Baselines3), MlpPolicy, 256×256 hidden, lr=3e-4
- **Training:** Replay of historical signal events — for each signal that fired, simulate what would have happened entering at t=0 vs t+1, t+2 (up to 5 candles later)
- **Activation threshold:** 1000 live trades before RL agent activates (falls back to immediate entry)

**Architecture:** New module `ml/entry_timing_agent.py`. New Celery task to call it at each signal event. Saved as `models/entry_timing_agent.zip`.

---

### Idea D — 4-Timeframe Hierarchy (add 15m)

**Problem it solves:** 1m captures noise, 5m is better, but without a 15m layer the bot misses whether the 5m move is a continuation or a counter-trend blip within a bigger 15m structure.

**How it works:**
- Add `CandleNet 15m` (F48_15m) — identical architecture, trained on 15m candles
- Feed 15m candles via `_poll_short_candles(r, "15m")` every 15 minutes
- **Hierarchy logic in signals/engine.py:**
  - 1h TFT provides the macro bias (already exists)
  - 15m CandleNet sets the meso trend (new)
  - 5m CandleNet confirms setup zone (existing)
  - 1m CandleNet times the entry candle (existing)
- **Scoring:**
  - All 3 TFs (1m/5m/15m) agree: candlenet_bonus = +25 (up from +15)
  - 2 of 3 agree: +10 (up from +5)
  - Conflict: -15 (up from -10)
  - 15m trend ceiling: if 15m says bull trend → hard cap at 50% for shorts (tighter than 55%)

**Architecture:** Third CandleNet model. 3 new governance IDs: F48_15m. New pretrainer step.

---

### Idea E — Regime-Conditioned CandleNet Heads

**Problem it solves:** Bull-trend candle behavior and mean-reverting sideways behavior are fundamentally different. A single model trained on all regimes is a compromise — it's mediocre in all of them.

**How it works:**
- HMM (F14) already runs and outputs regime: 0=bull, 1=bear, 2=turbulent
- **Option 1 — Regime embedding:** concat 3-dim one-hot regime vector into CandleNetModel's dense layer (192 → 195 → 128). Same model, regime-aware.
- **Option 2 — Separate models:** train 3 CandleNet variants on regime-filtered data, route inference by HMM output at inference time. More robust but 3× storage.
- **Recommended:** Option 1 for 1m/5m (simpler), Option 2 for 15m (if added in Idea D)
- **How regime embedding works in training:** label each training window with HMM regime at that timestamp; concat regime one-hot to the hidden state before output heads
- **Expected gain:** regime-specific directional accuracy improves ~5–10% per literature; most critical for turbulent regime (currently lowest accuracy)

**Architecture:** Modify `CandleNetModel` in `ml/candlenet.py` — add `regime_embed` input, change input dim from 192 to 195 in first dense layer.

---

### Idea F — GAF Image Stream (Visual Candle Pattern CNN)

**Problem it solves:** CandleNet's CNN branch operates on raw feature vectors. Human traders recognize visual patterns (flags, wedges, head-and-shoulders) that are hard to encode as features but obvious as images. GAF encodes this visually.

**How it works:**
- Gramian Angular Field: convert last 60 close prices to polar coords, compute Gram matrix → 60×60 image
- Do this for 4 channels: O, H, L, C → shape (60, 60, 4)
- Feed into small ResNet-18 style CNN → 128-dim embedding
- **Fusion:** concat GAF embedding to CandleNet's 192-dim concat layer → 320-dim → 128 dense
- No separate training needed — trained jointly with CandleNet

**Architecture:** New `ml/gaf_encoder.py` module. `CandleNetModel` updated to accept optional GAF input.

**Literature result:** 90–93% candlestick pattern classification accuracy on 3h/5h windows. CNN-LSTM on GAF = 82.7% trade direction accuracy.

---

### Idea G — TCN Replacement of GRU Branch

**Problem it solves:** GRU is sequential (O(T) time) — each timestep depends on the previous. TCN is fully parallel (O(log T) with dilated convolutions). For 60-candle context: TCN is ~10× faster at inference, converges in <100 epochs vs ~1000 for CNN-LSTM.

**How it works:**
- Replace `nn.GRU(17, 128, 2, dropout=0.2)` with a TCN block:
  - 4 dilated causal Conv1D layers: dilations = [1, 2, 4, 8], kernel=3, channels=128
  - Each layer: Conv1D → BatchNorm → ReLU → Dropout(0.2)
  - Output: take final timestep → [B, 128] (same as GRU)
- Keep CNN branch unchanged
- Concat [B, 64+128=192] — same as before
- **TCN-GRU hybrid (better):** keep both GRU AND TCN branches → concat [B, 64+128+128=320] → Dense(320→128)

**Trade-off:** GRU slightly better on MSE (large deviation handling), TCN better on MAE/MAPE (average behavior). Hybrid captures both.

**Architecture:** New `class TCNBlock` in `ml/candlenet.py`. `CandleNetModel` updated to add TCN branch alongside existing GRU branch.

---

## Discussion

### Confirmed
- [CONFIRMED] F48 is built — CNN-GRU, 1m and 5m, 17 features, 7 outputs
- [CONFIRMED] Idea A — Exhaustion Scorer: counter-trend entry window in bull/bear markets
- [CONFIRMED] Idea B — Magnitude TP/SL: dynamic TP1/TP2 from mag1/mag3 in risk/manager.py
- [CONFIRMED] Idea C — RL Entry Timing Agent: PPO (Stable-Baselines3) wait/enter/skip
- [CONFIRMED] Idea D — 4-TF Hierarchy: add 15m CandleNet (F48_15m)
- [CONFIRMED] Idea E — Regime-Conditioned Heads: 3-dim HMM embedding in CandleNetModel dense layer
- [CONFIRMED] Idea F — GAF Image Stream: Gramian Angular Field encoder + ResNet → fused into CandleNet
- [CONFIRMED] Idea G — TCN-GRU Hybrid: add TCN branch alongside existing GRU branch

### Ruled Out
- Pure GRU replacement with TCN — GRU better on MSE/volatility spikes, keep both (hybrid only)
- Separate CandleNet model per regime for 1m/5m — embedding approach preferred (less storage)
- Tick-level OFI real-time feed — WebSocket at 100ms not in infrastructure yet

## Implementation Checklist

### Architecture changes (ml/candlenet.py)
- [x] Add `class _TCNBlock` + `_CausalConv1d` — 4 dilated causal Conv1D layers (dilations 1/2/4/8, kernel=3, ch=128, BN+ReLU+Dropout) — confirmed ml/candlenet.py:73,87
- [x] Add GAF input branch to `CandleNetModel` — accepts optional (B,4,60,60) tensor → GAFResNet → 128-dim — confirmed ml/candlenet.py:161,199
- [x] Add regime embedding to `CandleNetModel` — 3-dim one-hot concat at fusion layer — confirmed ml/candlenet.py:131,209
- [x] Update `run_inference()` — pass HMM regime from Redis, compute exhaustion_score, write `{pair}:{interval}:exhaustion_score` — confirmed ml/candlenet.py:317,337
- [x] Update `train()` — accept regime labels per window, include GAF, train TCN branch — confirmed ml/candlenet.py:373

### ml/ha_features.py additions
- [x] Add `compute_exhaustion_features()` — streak / body_slope / vol_slope / pattern_flag — confirmed ml/ha_features.py:167

### New file: ml/gaf_encoder.py
- [x] `def compute_gaf(opens, highs, lows, closes)` — polar encoding → GASF matrix → (60,60,4) tensor — confirmed ml/gaf_encoder.py:44
- [x] `class GAFResNet` — 2× Conv2D + 2 ResBlocks → AdaptiveAvgPool → Linear(→128) — confirmed ml/gaf_encoder.py:83

### New file: ml/entry_timing_agent.py
- [x] `class EntryTimingEnv` — 22-dim state, 3 actions (wait/enter/skip), reward = PnL delta vs signal-open — confirmed ml/entry_timing_agent.py:221
- [x] `def train_entry_timing(...)` — PPO, MlpPolicy 256×256, 1M steps, save to models/entry_timing_agent.zip — confirmed ml/entry_timing_agent.py:322
- [x] `def decide_entry(pair, signal_score, signal_age_candles)` → returns "wait" | "enter" | "skip" (falls back to "enter" if model missing or <1000 trades) — confirmed ml/entry_timing_agent.py:169

### risk/manager.py
- [x] Read `{pair}:1m/5m/15m:candle_forecast` via `_candlenet_avg_mag_pct()` — confirmed risk/manager.py:126
- [x] `compute_tp_targets()` returns tp1 = entry × (1 + mag1 × sign), tp2 = entry × (1 + mag3 × sign) — confirmed risk/manager.py:147
- [x] SL floor = max(ATR × mult, mag1 × 0.5) — never narrower than half expected move — confirmed risk/manager.py:113-116

### data/feed.py
- [x] Add 15m polling at tick_counter % 180 (every 15 min) — confirmed data/feed.py:421
- [x] `_poll_short_candles` updated to gate on F48_15m governance ID for 15m calls — confirmed data/feed.py:241

### feature_governance/bootstrap.py
- [x] Added `("F48_15m", "CandleNet 15min Model", 0)` — confirmed feature_governance/bootstrap.py:91

### celery_app.py
- [x] `"retrain-candlenet-15m"` beat — Sun 07:00 UTC — confirmed celery_app.py:208
- [x] `retrain_candlenet_15m()` task — confirmed celery_app.py:1520
- [x] `"train-entry-timing"` beat — Sun 07:30 UTC — confirmed celery_app.py:214
- [x] `train_entry_timing_task()` task — confirmed celery_app.py:1538
- [x] `candlenet_infer_all()` extended loop to include "15m" — confirmed celery_app.py:1469

### pretrainer/main.py
- [x] `train_candlenet_15m(symbols)` — step_9 — confirmed pretrainer/main.py:444,536
- [x] `train_entry_timing_agent_step()` — step_10 — confirmed pretrainer/main.py:453,539
- [x] `verify_all_checkpoints()` — added candlenet_15m.pth — confirmed pretrainer/main.py:479

### signals/engine.py
- [x] Read exhaustion_score per TF — confirmed signals/engine.py:195
- [x] Read 15m forecast — confirmed signals/engine.py:183
- [x] Bonus scoring: 3 TFs +25 / 2 TFs +10 / disagree -15 — confirmed signals/engine.py:213-220
- [x] 15m counter-trend ceiling 50, 1m/5m 55 — confirmed signals/engine.py:239-241
- [x] Exhaustion override loosens ceiling to 70 + +20 bonus — confirmed signals/engine.py:266-267
- [x] Call `decide_entry()` after threshold; "skip" suppresses signal — confirmed signals/engine.py:438,442
- [x] F48_15m governance check — confirmed signals/engine.py:183

### dashboard/api.py
- [x] CandleNet 15m row — confirmed dashboard/api.py:1346-1368
- [x] Entry Timing Agent row (file: entry_timing_agent.zip) — confirmed dashboard/api.py:1370+

### BOT_BLUEPRINT.md
- [x] Production Extensions subsection added — confirmed BOT_BLUEPRINT.md:1656
- [x] F48_15m mention in Phase 0 activation table — confirmed BOT_BLUEPRINT.md:382 ("F48 CandleNet 1m/5m/15m")
- [x] Changelog entry — confirmed BOT_BLUEPRINT.md:4216

### TP/SL wiring (additional, Idea B end-to-end)
- [x] `signals/engine.py`: after `engine.open_trade()`, compute TPs via `compute_tp_targets(pair, direction, mark)` and write `trade:{id}:tp1` / `tp2` / `mag1_pct` / `mag3_pct` to Redis — confirmed signals/engine.py:1247-1262
- [x] `risk/manager.py:monitor_trailing_sl`: read `trade:{id}:tp1` each tick; if mark crosses tp1, `engine.close_trade(reason="candlenet_tp1")` then delete TP keys — confirmed risk/manager.py:226-264
- [x] `execution/paper.py:close_trade`: delete TP Redis keys on close (any reason) — confirmed execution/paper.py
- [x] `execution/live.py:close_trade`: delete TP Redis keys on close (any reason) — confirmed execution/live.py

### Docker rebuild + retrain
- [x] cont.46 rebuild — initial code in containers
- [x] cont.47 rebuild — lazy-GAF OOM fix
- [x] cont.48 rebuild — F49 deferred items (HPO / auto-rollback / online_update)
- [x] cont.49 rebuild — speed + convergence rewrite (task b50bha3yk, in flight)
- [ ] Recreate brain / celery_worker / celery_beat / dashboard with new image
- [ ] Run fresh pretrainer (`docker compose run --rm pretrainer python pretrainer/main.py`) — expected ~5h total
- [ ] Verify all three `candlenet_*.pth` land + validation gates pass + dashboard switches CandleNet rows from "missing" to "active"

## Session Handoff
_Last updated: 2026-05-26 cont. 49_
**Done this session (cont. 49):** Diagnosed root cause of pretrainer stall (loss imbalance + single-threaded GAF, not memory). Killed the 21h stalled run. Rewrote `candlenet.train()` with precomputed fp16 GAF cache, SmoothL1 on z-scored mag, early stopping (patience=5), per-epoch logging, and `torch.set_num_threads(4)` in pretrainer entry. Persisted mag_scaler in checkpoint; `_load`/`run_inference` un-scale at inference. cont. 48 changes (HPO / auto-rollback / online_update) carried forward.
**Next step:** Recreate containers, run pretrainer, watch first 1-2 epochs of CandleNet 1m to confirm new code path. Expected per-epoch time should drop from ~40min to ~5min; val_loss should actually move (the cont. 47 plateau at 17.3 was the gradient-imbalance bug, not a model-capacity ceiling).
**Blockers:** Rebuild in flight (task b50bha3yk).
**Containers to rebuild:** brain, celery_worker, celery_beat, dashboard, pretrainer.
