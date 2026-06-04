# Next Impl — F50 Candle-First Brain (Multi-TF candle as primary decision factor)
_Created: 2026-05-26 | Status: ACTIVE — F50a + F50b implemented cont. 50 (awaiting rebuild + live verification)_

> Triggered by user 2026-05-26: "is there a way to make the bot include candle chart like 1m/5m/15/1h as one of the main factors which choosing all that... train the bot brain constantly with old and live candle chart data so it gets better and better at next 1m/5m/15/1h movement... ultra advanced ml, rl, deep learning, neural network, ai."

---

## Research & References (2025-2026 SoTA)

- [CryptoMamba (arXiv:2501.01010)](https://arxiv.org/pdf/2501.01010) — First framework to apply Mamba SSM to Bitcoin price prediction. "Effective capture of long-range dependencies" in crypto time series, ~40% lower compute than Transformers on equal sequence length.
- [Hybrid Mamba-Transformer (Australian Science Journal 2026)](https://australiansciencejournals.com/bigdata/article/view/3558) — Mamba+Transformer hybrid on BTC/ETH multi-horizon return prediction. Best of both worlds: SSM for long context, attention for short-range cross-asset correlation.
- [Decision Mamba (arXiv:2403.19925)](https://arxiv.org/pdf/2403.19925) — Decision Transformer with selective SSM backbone. Treats trading as conditioned sequence prediction (given returns-to-go target, predict next action).
- [Decision Mamba self-evolution (arXiv:2406.05427)](https://arxiv.org/html/2406.05427v2) — Self-evolution regularization to bridge offline→online RL safely. Directly answers "train continuously on live + old data without catastrophic forgetting."
- [State-Space Models for HFT (Kinlay 2026)](https://jonathankinlay.com/2026/03/state-space-models-for-market-microstructure-can-mamba-replace-transformers-in-high-frequency-finance/) — Confirms Mamba beats Transformer at market-microstructure scale (sub-minute).
- [N-HiTS (arXiv:2201.12886)](https://arxiv.org/pdf/2201.12886) — Hierarchical interpolation + multi-rate sampling. Best on low-cap crypto per the 918-experiment comparison.
- [Controlled Comparison of DL Architectures (arXiv:2603.16886)](https://arxiv.org/html/2603.16886v1) — 918 experiments across 9 architectures, multiple horizons. N-HiTS wins low-cap, iTransformer wins multi-variate.
- [iTransformer](https://www.datasciencewithmarco.com/blog/itransformer-the-latest-breakthrough-in-time-series-forecasting) — Inverted attention (variates as tokens, attention across variates). Native cross-variable interaction modeling.
- [altFINS pattern scanner](https://altfins.com/knowledge-base/best-automated-crypto-chart-pattern-recognition-platforms/) — 4-TF (15m/1h/4h/1d) AI pattern detection. Inverse H&S 84% success, H&S 82%, Double Bottom 82%. Confirms multi-TF cascade is the production-grade approach.
- [Ensemble DRL "Learn to understand candlesticks" (ScienceDirect)](https://www.sciencedirect.com/science/article/abs/pii/S0957417423018754) — Ensemble DRL trained directly on candlestick patterns outperforms feature-engineered baselines.
- [Phemex AI bot continuous learning 2026](https://phemex.com/news/article/aienhanced-trading-bot-goes-live-with-continuous-learning-capabilities-58155) — Production crypto bot logging every signal as training data; matches the F50e proposal below.
- [TFT + on-chain metrics for multi-crypto (MDPI 2026)](https://www.mdpi.com/2079-8954/13/6/474) — TFT augmented with on-chain (already F19 in this bot) + technicals. Confirms the bot's existing TFT direction is right; F50 builds on top.

---

## Current state diagnosis (Rule 2 — read from code, not docs)

**Scanner (`scanner/main.py`):** picks pairs via 5-criteria composite — `volume`, `volatility`, `spread`, `winrate`, `pnl`. **Zero candle-chart input.** The scanner can promote a pair into the trading universe even when its candle structure is a clean short-only setup that the bot would lose money trading.

**Direction picker (`signals/engine.py:71-133`):** OFI sign is primary (`direction = "long" if ofi > 0 else "short"`). F13 direction_model may flip it (only if `paper_closed ≥ 100` and confidence delta > 10%). CandleNet provides a **score bonus** in the 8-component composite (weight = 0.12) and a trend-ceiling cap (counter-trend capped at 55), but **does NOT drive direction**. The candle is supplementary, not primary.

**Composite scoring (`signals/engine.py:380-403`):**
```
OFI 0.25  | Regime 0.20 | TFT(1h) 0.15 | hist_acc 0.13
CandleNet 0.12 | VPIN 0.10 | PatchTST 0.08 | Sentiment 0.05
```
Candle weight (0.12) ranks **5th of 8**.

**Entry timing (`signals/engine.py:435-448`):** F48 Idea C PPO agent — but gated to `paper_closed ≥ 1000`. Until then, every signal that crosses threshold enters immediately at the next tick.

**Training cadence:**
- CandleNet: weekly cron (Sunday 06:00 UTC) + F49 drift-triggered + F49 perf-degrade-triggered. **Not continuous.**
- F13 direction_model: every 50 closed trades + F49 online sidecar (SGDClassifier partial_fit). **Closest to continuous, but it's a 7-feature LogReg/GBC, not a deep candle model.**
- World Model: online reward learning per close (F34 cont. 5). Latent-space dynamics, not direct candle prediction.

**Gap vs user's ask:**
| User's ask | Current state | Gap |
|---|---|---|
| Candles in scanner pair selection | ❌ none | Add scanner candle score |
| Candles drive direction | Partial (12% weight, no direction flip) | Make candle ensemble the **primary** direction picker |
| Candle-aware entry point | F48 Idea C PPO gated to 1000 trades | Activate sooner OR use Decision-Mamba instead |
| Continuous training on live candles | Weekly + drift-triggered | Per-candle-close online updates |
| Ultra-advanced ML | CNN-GRU-TCN (F48) | Add Mamba SSM (2025-2026 SoTA) |

---

## Feature Description — F50 Candle-First Brain

**Goal:** Make multi-TF candle chart data (1m / 5m / 15m / 1h / 4h) the **primary** factor in pair selection, direction picking, and entry timing — and train the candle models continuously from live closed candles using 2025-2026 SoTA techniques (Mamba SSM, Decision-Mamba, EWC continual learning).

6 components, each independently testable:

### F50a — Scanner candle-setup score
**Where:** new function `scanner/main.py:score_candle_setup(symbols)` + new entry in `compute_composite` weights dict.
**How it works:** For each candidate pair, read `{pair}:15m:candle_forecast` + `{pair}:1h:candle_forecast` from Redis (already published by F48 inference loop). Score = `0.5 * max(dir1, dir3) + 0.3 * (1 - exhaustion_norm) + 0.2 * trend_alignment_15m_vs_1h`. Pairs with no candle forecast yet default to 50 (neutral, doesn't penalize cold-start).
**Composite weight:** target 0.20 of scanner weights (brain-learnable via F10 `criteria_weights.py`). Reduces other weights proportionally: vol 0.30→0.22, volatility 0.20→0.16, spread 0.15→0.12, winrate 0.20→0.16, pnl 0.15→0.14, **candle 0.00→0.20**.
**Effect:** pairs with a clean 15m+1h setup get priority over pairs that are just liquid + volatile.

### F50b — Multi-TF hierarchical direction cascade
**Where:** new module `signals/multi_tf_cascade.py` called from `signals/engine.py` direction-picker block.
**How it works:** Replace OFI-primary picker with a 5-TF cascade (1h → 15m → 5m → 1m + 4h as macro veto):
1. **4h** (new — extend F48 to 4h): macro veto. If 4h trend is strongly opposite to candidate direction → suppress signal entirely.
2. **1h** TFT + 1h CandleNet → macro direction.
3. **15m** CandleNet → setup zone confirmation.
4. **5m** CandleNet → micro-trend agreement.
5. **1m** CandleNet → tactical entry timing (handed off to F50d).
**Decision rule:** require **agreement from ≥3 of {1h, 15m, 5m}**. OFI becomes a tiebreaker, not the primary driver. The trend ceiling (F48 §Idea D) still caps counter-trend signals.
**Why it works:** matches the altFINS / professional-trader pattern — higher TF sets context, lower TF executes.

### F50c — Direction Mamba (deep SSM replaces F13 LogReg/GBC)
**Where:** new module `ml/direction_mamba.py`. Replaces F13's role for `predict_best_direction(pair)`.
**Architecture:**
- Input: last 256 candles from each of {1m, 5m, 15m, 1h} stacked → (256, 4, F) tensor where F = OHLCV + HA + 8 pattern flags + OFI + VPIN + sentiment ≈ 20 features
- Mamba SSM backbone: 4-block selective SSM, hidden=256, state_dim=16 (per CryptoMamba arXiv:2501.01010 config)
- Output heads: P(long_wins), P(short_wins), expected_pnl_normalized (3 heads)
- Trained on the entire closed-trade history augmented with synthetic CFs (same approach as cont. 25 F13 expansion).
**Inference cost:** ~50ms per pair on CPU (Mamba is faster than Transformer at this length).
**Activation:** registered as `F50c_direction_mamba`. Gated to `paper_closed ≥ 500` before going live. Until then, used as a shadow predictor — its picks logged for offline AUC comparison vs current F13.
**Why Mamba over Transformer:** the CryptoMamba paper shows +AUC and -40% compute on identical BTC data. SSM's linear-time recurrence handles 256-token sequences in a single forward pass with no quadratic attention cost.

### F50d — Decision Mamba Entry Agent (replaces F48 Idea C PPO)
**Where:** new module `ml/entry_decision_mamba.py`. Drops F48 §Idea C's `decide_entry` for the same call site.
**How it works:** Per Decision Mamba (arXiv:2403.19925), treats entry as **conditioned sequence prediction**:
- State sequence: last 60 ticks of (microstructure + 4-TF candle forecasts + open trade context)
- Returns-to-go conditioning: "given target +2× ATR reward over next 5 candles, what's the optimal action?"
- Output: P(wait), P(enter_now), P(enter_with_5m_delay), P(skip)
**Why Decision Mamba over PPO:**
1. Offline-RL trained on historical signal events (no live exploration risk)
2. Returns-to-go conditioning lets the same model handle different profit targets (low-risk vs high-conviction trades)
3. Self-evolution regularization (arXiv:2406.05427) makes the offline→online transition safe
4. PPO's policy gradient needs millions of episodes; Decision Mamba needs thousands of historical signals — feasible at our trade volume.
**Activation:** gated to `paper_closed ≥ 1000` (same as F48 PPO). Falls back to `"enter_now"` until then.

### F50e — Continuous live-candle training
**Where:** extend `ml/online_learner.py` to subscribe to a new pub/sub channel `CH_CANDLE_CLOSED` published by `data/feed.py` whenever a 1m/5m/15m closes.
**How it works:**
- Per 1m candle close: 1 SGD step on Direction Mamba's last-window features against the realized 5m/15m direction label (whichever closes first). EWC penalty against the canonical weights (F17 already wires Fisher matrix).
- Per 5m close: same on 15m label.
- Per 15m close: same on 1h label.
- Per 1h close: same on 4h label.
- LoRA adapters (rank=8) for Mamba — each TF gets its own adapter that's continuously updated. Canonical weights are frozen between weekly retrains; adapter-only updates avoid catastrophic forgetting and are 100× cheaper than full backprop.
- F49 replay buffer (already exists) ensures every online step mixes in past windows from underrepresented regimes.
**Throttle:** at most 10 SGD steps/minute across all TFs to keep CPU headroom for live trading. Buffer overflow drops the oldest.

### F50f — Candle-pattern memory (RAG-style retrieval)
**Where:** extend `memory/cognitive/memrl.py` (F35 already exists with clustering).
**How it works:**
- At each closed candle, embed the last 60 candles via Mamba encoder → 256-dim vector
- Vector indexed in a FAISS HNSW index (in-memory + Redis-persisted snapshots every 1h)
- At decision time: encode the current pair's last 60 candles → retrieve top-20 nearest historical windows → look up their realized outcomes → bias the Direction Mamba's posterior by `mean(outcomes)`
**Effect:** "this current setup looks like 18 of the last 20 times saw a +2% move within 5 candles → bias toward long."
**Why it works:** This is the trading-specific instantiation of Retrieval-Augmented Generation. The bot's MemRL already retrieves clusters by feature; F50f extends it to retrieve by RAW CANDLE EMBEDDING — the form humans actually use when "this looks like what happened on Wednesday."

---

## Discussion

### Confirmed by research
- Multi-TF cascade (4h → 1h → 15m → 5m → 1m) IS the production-grade architecture (altFINS, TrendSpider, every pro-trader textbook).
- Mamba SSM beats Transformer on crypto time series with less compute (CryptoMamba, Decision Mamba, multiple 2025-2026 papers).
- Continuous online learning with EWC + replay is a solved problem (F17 already implemented; F49 already wired for direction_model).
- Decision Mamba's returns-to-go conditioning is the right framing for entry timing.

### Ruled out (so far)
- **Pure Transformer for direction prediction.** Mamba's linear recurrence is strictly better at our sequence length (256+) on CPU.
- **End-to-end RL from raw candles.** PPO needs millions of episodes — we have ~3000 closed trades. Offline RL via Decision Mamba is the right scale.
- **Vision Transformer on GAF images alone.** F48 already does GAF+CNN; ViT would cost 5× more without proven gain at our compute budget.
- **Pure online learning (no replay buffer).** Per CoinAPI / 2023-2026 research, online-only catastrophically forgets — replay buffer is mandatory.

### Open design questions for the user
1. **Scope:** all 6 components or a subset? Recommended start: F50a (scanner) + F50b (cascade) → cheap, no new model, can run today. Then F50e (online) on top of CandleNet. Then F50c (Mamba) when CandleNet is stable. F50d + F50f last.
2. **Mamba dependency:** requires `mamba-ssm>=2.2` Python package (~80 MB binary wheel). Acceptable in the Docker image?
3. **Compute budget:** F50c Mamba inference adds ~50ms per pair per decision cycle. With 30 active pairs that's 1.5s/cycle. Current cycle is ~5s. OK?
4. **Activation order:** F50c (Direction Mamba) gated at `paper_closed ≥ 500`. Pretty low bar but the user may want higher. Same for F50d (Entry Mamba) at 1000.

---

## Implementation Checklist (PROPOSED, awaiting confirmation)

### F50a — Scanner candle-setup score (smallest, fastest)
- [x] `scanner/main.py:score_candle_setup(symbols)` — reads `{pair}:{15m,1h}:candle_forecast` + 15m exhaustion. Returns 50 (neutral) when forecasts absent. cont. 50.
- [x] Update `compute_composite` to include candle weight via new `candle_s` kwarg (back-compat: None ⇒ 0 candle contribution). cont. 50.
- [x] Update `config.yaml: scanner.criteria_weights` to add `candle_setup: 0.20`, rebalance others (volume 0.20→0.15, volatility 0.25→0.20, spread 0.15→0.12, win_rate 0.30→0.25, pnl 0.10→0.08). Sums to 1.0. cont. 50.
- [x] Extend F10 `ml/criteria_weights.py`: `_CRITERIA` includes `candle_setup`; `record_pair_selection` writes it; `compute_weights_from_history` selects + correlates it. cont. 50.
- [x] Migration `022_pair_selections_candle.sql` applied — `candle_setup_score NUMERIC(6,2) DEFAULT 50.0`. cont. 50.
- [ ] Dashboard: add candle column to scanner pair table — deferred (not blocking).

### F50b — Multi-TF hierarchical cascade
- [x] `signals/multi_tf_cascade.py` with `pick_direction_cascade(pair, ofi, regime, tft_bias) -> (direction, conf, audit)`. cont. 50.
- [x] Replace direction-picker block at `signals/engine.py` with cascade call. F13 model can still flip after cascade. cont. 50.
- [x] Audit log every cascade decision to Redis list `cascade:decisions` (LPUSH + LTRIM 1000) + per-status counters (`cascade:count:*`, `cascade:rationale:*`). cont. 50.
- [ ] 4h CandleNet — deferred. The cascade's macro-veto path looks for `{pair}:4h:candle_forecast` but gracefully degrades when absent. Adding 4h means extending F48 pretrainer to step_11; not required for cascade to work on {1h, 15m, 5m}.

### F50c — Direction Mamba
- [ ] Add `mamba-ssm` to `requirements.txt` + rebuild Docker
- [ ] `ml/direction_mamba.py` — model class, train_from_closed_trades, predict_best_direction
- [ ] Training data: last N closed trades × 4-TF candle windows + outcomes
- [ ] Validation gates: AUC ≥ 0.58 (higher than F13's 0.55 — Mamba should outperform)
- [ ] Pretrainer step_12
- [ ] Shadow mode: log Mamba picks alongside F13 for 200 trades, compare AUC
- [ ] Live activation: gate to `paper_closed ≥ 500` AND `mamba_shadow_auc > 0.58`

### F50d — Decision Mamba entry agent
- [ ] `ml/entry_decision_mamba.py`
- [ ] Historical signal-event JSONL (already collected by F48 §Idea C `log_signal_event`)
- [ ] Offline training: returns-to-go conditioning per Decision Mamba paper
- [ ] Self-evolution regularizer (arXiv:2406.05427) for safe online updates
- [ ] Replace `decide_entry` call site
- [ ] Same shadow→live activation flow as F50c

### F50e — Continuous live-candle training
- [ ] `data/feed.py` publishes `CH_CANDLE_CLOSED` per closed 1m/5m/15m/1h candle
- [ ] `ml/online_learner.py` adds candle-closed handler with per-TF SGD steps
- [ ] LoRA adapters for Mamba (rank=8) — `ml/lora_adapter.py`
- [ ] EWC + replay (already wired in F17, just bind)
- [ ] Throttle: 10 SGD steps/min cap
- [ ] Dashboard panel: live candle training stats (samples consumed, adapter delta norms)

### F50f — Candle-pattern memory (RAG)
- [ ] Add `faiss-cpu` to requirements
- [ ] Extend `memory/cognitive/memrl.py` with `embed_candle_window` + `retrieve_similar_windows`
- [ ] Per-pair vector index, persisted snapshots every 1h
- [ ] Wire into Direction Mamba's posterior as a bias term
- [ ] Dashboard: "Memory retrievals" panel showing top-K similar past windows for the active signal

---

## Session Handoff
_Last updated: 2026-05-26 cont. 50 design_

**Status:** PROPOSED — awaiting user pick on scope before any code is written. F48 + F49 implementations are complete; pretrainer is currently building CandleNet 1m (cont. 49 fixes live).

**Recommended start:** F50a + F50b together — they're cheap (no new model, no new dependency), provide immediate value (scanner picks candle-aware pairs + direction comes from multi-TF agreement), and validate the design without committing to Mamba binary deps. Once the cascade is logging cleanly we can confirm whether F50c Mamba is worth the complexity.

**Blockers:** Pretrainer must produce candlenet_1m/5m/15m.pth (cont. 49 run, expected ~5h) before F50a/b can score the candle inputs. F50e/f need F50c. F50d is independent and could be parallel to F50c.

**Rule 7 note:** F50c/d (Mamba model code) is non-trivial → Opus 4.7. F50a/b/e (Python wiring around existing primitives) is fine on Sonnet 4.6. If the user picks F50a/b first, switch to Sonnet to save tokens.
