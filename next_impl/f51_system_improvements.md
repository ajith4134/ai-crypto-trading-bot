# Next Impl — F51 System Improvements (Research-Driven)
_Created: 2026-05-27 | Status: ACTIVE_

> Based on exhaustive web research (arXiv, GitHub, trading literature) across
> SL/TP design, symbol ranking, candle pattern ML/RL/AI. Covers 10 improvements
> across 4 categories. Items marked [x] confirmed on disk via Rule-3 two-pass.

---

## Source Research Summary

### Symbol Selection
- Optimal universe: **20–30 pairs** with $100M+ quote volume floor (current $50M is too low)
- Best open-source: freqtrade VolumePairList + SpreadFilter + FreqAI per-pair ML scoring
- ADX >25 on 1h+4h eliminates choppy pairs that CandleNet scores but aren't trending
- Cross-sectional momentum rank (return vs universe median) adds alpha above volume/volatility
- Papers: [arXiv:2602.11708](https://arxiv.org/pdf/2602.11708), [MDPI 2227-9091/13/9/180](https://www.mdpi.com/2227-9091/13/9/180)

### SL/TP
- Exit design drives more return variance than entry design in signal-heavy bots
- Chandelier Exit (HighestHigh − ATR×2.5) auto-scales with volatility, better than fixed $-tiers
- Kelly criterion: half-Kelly retains 75% compound growth at 50% variance — blueprint F16, NOT yet built
- Regime-adaptive ATR multiplier: 1.5× (bull/bear) / 2.5× (unknown) / 3.5× (turbulent)
- VAE OOD gating: >40% risk reduction in 5× leveraged crypto futures (FineFT, NTU Singapore)
- Papers: [arXiv:2604.27150](https://arxiv.org/abs/2604.27150), [arXiv:2512.23773](https://arxiv.org/abs/2512.23773)

### Candle Pattern ML/AI
- GAF-CNN (your CandleNet approach) validated: ~90.7% accuracy, best single architecture
- GAF-YOLO adds location-awareness (where in window pattern occurs): [arXiv:2201.08669](https://arxiv.org/abs/2201.08669)
- HAELT Transformer outperforms on AAPL hourly 2025: [arXiv:2506.13981](https://arxiv.org/html/2506.13981v1)
- GAF Transfer Learning (Coral/CMD) for sparse pairs: [arXiv:2504.00378](https://arxiv.org/abs/2504.00378)
- VLMs (GPT-4V, Gemini) cannot reliably read candlesticks — purpose-built CNNs win
- Harmonic XABCD patterns: [neurotrader888](https://github.com/neurotrader888/TechnicalAnalysisAutomation/blob/main/harmonic_patterns.py)

### Order Flow / Advanced Features
- Multi-level LOB OFI (#1 SHAP feature across 5 crypto assets): [arXiv:2602.00776](https://arxiv.org/abs/2602.00776)
- VPIN hard gate (< 0.4 → suppress entry) more effective than scalar sub-score
- Funding rate >+0.1%/8h + OI declining = crowded unwind risk (Granger-causality confirmed)
- "Better inputs > more layers" — LOB feature engineering beats deep model complexity: [arXiv:2506.05764](https://arxiv.org/abs/2506.05764)

---

## Feature Descriptions

### F51a — HMM-Regime-Adaptive ATR Multiplier
**Problem:** `atr_mult = 2.5` is hardcoded in `compute_initial_sl()`. In a calm bull regime the SL
is unnecessarily wide (stops out too late, costs profit). In turbulence it's too tight (shaken out
by noise). The HMM regime is already running and in Redis.

**How it works:**
- Read `current_regime` from Redis at SL compute time
- Map: `bull` or `bear` (directional, lower noise) → 1.5× ATR
- Map: `unknown` / `neutral` (no regime) → 2.5× ATR (existing default)
- Map: `turbulent` → 3.5× ATR (wider to survive volatility spikes)
- Strategy override (`initial_atr_mult`) still wins over regime default

**Where:** `risk/manager.py:compute_initial_sl()` line ~60

---

### F51b — Funding Rate Extremes Gate
**Problem:** In crypto futures, extreme funding rates signal crowded positioning about to unwind.
Longing into +0.1%/8h funding means: (a) paying extra cost, (b) when longs unwind they push price
DOWN into your position. The bot currently ignores funding entirely in direction scoring.

**How it works:**
- After direction is set, read `{pair}:funding_rate` from Redis (already polled by data/feed.py)
- Funding > +0.05% AND direction = "long" → add –20 penalty to `direction_conf` (Granger-causal threshold)
- Funding < –0.05% AND direction = "short" → add –20 penalty to `direction_conf`
- This softens (not hard-blocks) against-funding entries; strong signals still pass
- Log `funding_penalty_applied` with funding rate value

**Where:** `signals/engine.py:generate_candidate_signals()` after direction set, before final direction_conf cap

---

### F16 — Fractional Kelly Position Sizing (Blueprint Feature 16)
**Already in blueprint Phase 1.** Formula: `f* = W − (1−W)/R` where W = win_rate (decimal),
R = avg_win / avg_loss. Apply 0.5× (half-Kelly). Clamp to 5–30% of balance. Returns max
capital in USDT. Applied as an UPPER BOUND on capital_usdt — Kelly cannot override brain's
existing dynamic sizing but prevents over-betting on low-confidence setups.

**Minimum data gate:** Requires ≥ 30 closed trades to activate (not enough data before that).
Falls back to brain default when insufficient data.

**Where:**
- New function `compute_kelly_capital(balance_usdt)` in `risk/manager.py`
- Called in `signals/engine.py:process_signals()` right before `open_trade()`

---

### F51c — ADX + Cross-Sectional Momentum Pair Ranker (**Opus 4.7 required**)
**Problem:** Current composite ranks pairs by 24h volume/volatility/spread which selects movers
regardless of whether they're trending or just noisy. ADX(14) distinguishes trending from choppy;
only pairs with ADX >25 on 1h should receive new entries.

**How it works:**
- New `score_adx_trend(symbols, exchange_client)` function in scanner — fetches 1h OHLCV from
  Binance for top-50 candidates (not all 300+), computes ADX(14) via TA-Lib
- Scores: ADX ≥ 25 → 80 (trending), ADX 20–24 → 50 (borderline), ADX < 20 → 20 (choppy)
- Also compute 24h cross-sectional momentum rank (pair's return rank within universe)
- Add both as new composite criteria with initial weights in config.yaml
- Register F51c governance ID

**New files:** none
**Modified:** `scanner/main.py`, `config.yaml`

---

### F51d — Chandelier Exit Trailing SL (**Opus 4.7 required**)
**Problem:** Fixed $-tier ratchet ($2/$10/$25/$50) hardcodes dollar amounts regardless of pair
price or volatility. BTC at $100k: a $10 tier is 0.01% of price — meaningless. Chandelier
anchors to price's recent highest high minus N×ATR, which auto-scales with both price level
and current volatility.

**How it works:**
- Track `highest_high_since_entry` and `lowest_low_since_entry` per trade (Redis key, updated per tick)
- Chandelier SL (long) = `highest_high − ATR × 2.5`
- Chandelier SL (short) = `lowest_low + ATR × 2.5`
- Take `max(chandelier_sl, ratchet_sl)` for long, `min(...)` for short
- Keep existing dollar-tier ratchet as FLOOR — chandelier can only be TIGHTER, never wider

**Where:** `risk/manager.py:monitor_trailing_sl()` — add alongside existing Path A/B ratchet
**New Redis keys:** `trade:{id}:highest_high`, `trade:{id}:lowest_low`

---

### F51e — Multi-Level LOB OFI (**Opus 4.7 required**)
**Problem:** Current OFI uses only best bid/ask imbalance (level 1). SHAP analysis shows LOB
imbalance across top 5–10 levels is the #1 predictive microstructure feature in crypto.

**How it works:**
- Subscribe to Binance partial book depth stream (top 10 levels, 100ms)
- Compute weighted OFI: `Σ (bid_qty[i] − ask_qty[i]) × weight[i]` for levels 1–10
  where weight[i] = 1/(i+1) (closer levels weighted higher)
- Store as `{pair}:multi_ofi` in Redis, replacing single `{pair}:ofi`

**Where:** `data/feed.py` — new LOB subscription alongside existing microstructure
**Estimated impact:** #1 SHAP feature, likely 5–15% direction accuracy improvement

---

### F51f — Harmonic XABCD Pattern Scorer (**Opus 4.7 required**)
**Problem:** Bot misses Fibonacci-zone reversal setups (Gartley, Bat, Crab, Butterfly, Cypher,
Shark). These are high-probability reversal signals at precise price levels, complementary to the
trend-following CandleNet.

**How it works:**
- Adapt [neurotrader888/TechnicalAnalysisAutomation](https://github.com/neurotrader888/TechnicalAnalysisAutomation/blob/main/harmonic_patterns.py)
  for real-time use
- Run on 1h candles per pair (less noise than 1m/5m)
- Output: `{pair}:harmonic_pattern` → pattern name + direction + completion pct (0–1)
- In `signals/engine.py`: if pattern present + direction matches signal → +15 bonus;
  if pattern present + direction opposes → –15 penalty

**New file:** `ml/harmonic_patterns.py`
**New Celery beat:** every 60m

---

### F51g — VAE OOD Exit Gating (**Opus 4.7 required**)
**Problem:** During flash crashes, macro events, or regime-anomaly spikes the bot's normal
trailing SL and TP logic is miscalibrated for the environment. Need a mechanism to detect
"this market state is anomalous vs my training distribution" and switch to conservative exit.

**How it works:**
- Train a small VAE (2-layer, 64-dim latent) on feature vectors of profitable closed trades
- At trade open: encode current feature vector, compute reconstruction error
- If reconstruction error > 2σ threshold → set `trade:{id}:ood_mode = 1`
- In `monitor_trailing_sl()`: when ood_mode=1 → use tight 1×ATR SL, TP1=full close, no trail

**New file:** `ml/vae_ood.py`
**Modified:** `risk/manager.py`

---

### F51h — RL Exit Policy Calibration (**Opus 4.7 required**)
**Problem:** SL/TP parameters (SL width, TP1 distance, TP2 distance, trail activation %) are
hand-tuned. arXiv:2604.27150 shows that replaying historical trades under a grid of exit
parameter combinations and selecting regime-specific optima outperforms hand-tuning.

**How it works:**
- Replay all closed trades under grid: SL_pct ∈ [0.5%, 5%], TP1 ∈ [0.3%, 3%],
  TP2 ∈ [0.5%, 5%], trail_activation ∈ [0.5%, 3%]
- Evaluate Sharpe × win_rate per (regime, direction) combination
- Store best params in Redis: `exit_params:{regime}:{direction}` as JSON
- `compute_initial_sl()` and `compute_tp_targets()` read regime-specific optimal params

**New file:** `ml/exit_policy_replay.py`
**Modified:** `risk/manager.py`

---

## Discussion

### Confirmed
- [CONFIRMED] Funding rate (`{pair}:funding_rate`) already polled by `data/feed.py:38`
- [CONFIRMED] `current_regime` Redis key exists at `redis_keys.CURRENT_REGIME`
- [CONFIRMED] OI not currently fetched — F51b uses funding rate only for now
- [CONFIRMED] `memory/query.py` has `get_rolling_win_rate()` and `_fetchall()` for Kelly query
- [CONFIRMED] F16 Kelly formula in blueprint: `f* = W − (1−W)/R`, fractional 0.25–0.5

### Ruled Out
- Full Kelly (use half-Kelly 0.5 only — full Kelly causes catastrophic drawdowns)
- Separate Chandelier for each pair without ATR floor (ATR can be near-zero in illiquid periods)
- VLMs (GPT-4V etc.) for candle pattern detection — proven to fail in [arXiv:2604.12659](https://arxiv.org/html/2604.12659v1)
- Raising max active pairs above 30 without raising liquidity floor to $100M

---

## Implementation Checklist

### F51a — HMM-adaptive ATR multiplier
- [x] `risk/manager.py:compute_initial_sl()` — reads `current_regime`, sets `atr_mult` 1.5/2.5/3.5; strategy override still wins — confirmed risk/manager.py:60-65

### F51b — Funding rate extremes gate
- [x] `signals/engine.py:generate_candidate_signals()` — after direction_conf computed, applies -20 penalty when funding extreme (>+0.0005 long / <-0.0005 short) aligns with direction; soft penalty, increments `brain:funding_gate:penalty_count` — confirmed signals/engine.py:452-481

### F16 — Fractional Kelly position sizing
- [x] `risk/manager.py:compute_kelly_capital(balance, min_trades=30, window_n=50, fractional=0.5)` — queries last 50 closed trades, computes W (win rate), R (avg_win / avg_loss), `f* = W - (1-W)/R`, half-Kelly, clamped to config.capital.per_trade_min_pct..per_trade_max_pct — confirmed risk/manager.py:757
- [x] `signals/engine.py:process_signals()` — calls `compute_kelly_capital()` after F8 router, applies as upper bound on `capital_usdt`; inactive until ≥30 closed trades; logs `kelly_capital_applied`, increments `brain:kelly:capped_count` / `brain:kelly:nonbinding_count` — confirmed signals/engine.py:1267-1295

### F51c — ADX pair ranker
- [x] `scanner/main.py:_wilder_adx()` — pure-numpy Wilder ADX(14) implementation — confirmed scanner/main.py:18-65
- [x] `scanner/main.py:score_adx_trend()` — fetches 1h klines (36 hours = ~36 bars) for top-50 candidates only; scores 80/50/20 for ADX ≥25/20-24/<20 — confirmed scanner/main.py:68-110
- [x] `compute_composite()` — accepts `adx_s` + `weights["adx_trend"]` (backward compatible) — confirmed scanner/main.py
- [x] `run_scan()` — two-pass: pass 1 picks top-50 by composite-without-ADX, pass 2 fetches ADX for them and recomputes — confirmed scanner/main.py
- [x] `config.yaml` — `adx_trend: 0.15` weight; other weights re-balanced to sum 1.0 — confirmed config.yaml:67
- [x] `feature_governance/bootstrap.py` — F51c registered Phase 0 — confirmed bootstrap.py

### F51d — Chandelier Exit
- [x] `risk/manager.py:monitor_trailing_sl()` — tracks `trade:{id}:highest_high` and `trade:{id}:lowest_low` per tick (initialised to entry, updated on favourable mark) — confirmed risk/manager.py
- [x] Chandelier_sl = highest_high − ATR × mult (long), lowest_low + ATR × mult (short); mult is HMM-regime-adaptive (2.0 bull/bear, 2.5 unknown, 3.0 turbulent) — confirmed risk/manager.py
- [x] Acts as Path C alongside existing $-tier (Path B) and %-activation (Path A); takes tighter wins — confirmed risk/manager.py
- [x] `execution/paper.py:close_trade` — deletes high/low watermark keys on close — confirmed execution/paper.py:170
- [x] `execution/live.py:close_trade` — deletes high/low watermark keys on close — confirmed execution/live.py:180
- [x] `feature_governance/bootstrap.py` — F51d registered Phase 0 — confirmed bootstrap.py

### F51e — Multi-level LOB OFI (**Opus 4.7**)
- [ ] `data/feed.py` — Binance partial depth stream (top 10 levels), weighted OFI computation
- [ ] `redis_keys.py` — add `MULTI_OFI` key
- [ ] `signals/engine.py` — read `multi_ofi` instead of `ofi`

### F51f — Harmonic pattern scorer (**Opus 4.7**)
- [ ] `ml/harmonic_patterns.py` — new file, adapted from neurotrader888, real-time XABCD detection on 1h candles
- [ ] `celery_app.py` — new 60m beat task
- [ ] `signals/engine.py` — harmonic bonus/penalty in generate_candidate_signals()
- [ ] `feature_governance/bootstrap.py` — register F51f

### F51g — VAE OOD exit gating (**Opus 4.7**)
- [ ] `ml/vae_ood.py` — new file: VAE architecture, train(), encode_and_score(), threshold calibration
- [ ] `risk/manager.py:monitor_trailing_sl()` — check `trade:{id}:ood_mode`, apply conservative exit
- [ ] `signals/engine.py` — set `trade:{id}:ood_mode` at trade open

### F51h — RL exit policy replay (**Opus 4.7**)
- [ ] `ml/exit_policy_replay.py` — new file: grid replay over closed trades, regime-specific Sharpe optimization
- [ ] `risk/manager.py` — read `exit_params:{regime}:{direction}` from Redis for SL/TP params
- [ ] `celery_app.py` — weekly beat task to re-run replay

### F51m — Dynamic Anchor Universe & Mover Filter Cascade (cont. 51 add-on)
- [x] `scanner/market_data.py` — new module: `fetch_top_marketcaps()` (CoinGecko, 24h cache), `fetch_listing_ages()` (Binance exchangeInfo `onboardDate`, 7d cache), `get_anchor_universe()` — confirmed
- [x] `scanner/main.py:update_active_pairs()` — replaced hardcoded `["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT"]` anchor seed with dynamic top-N by market cap from CoinGecko; Redis cache (`scanner:anchor_pairs`) refreshed per scan — confirmed
- [x] `scanner/main.py:update_active_pairs()` — `_passes_mover_filters()` filter cascade: exclude_pairs / age (>= 30d) / spread (<= 15 bps) / mcap (>= $250M) / quote vol (>= $150M) — confirmed
- [x] `scanner/main.py:run_scan()` — fetches listing ages + mcaps once per scan and passes through — confirmed scanner/main.py:617-628
- [x] Redis seed: `scanner:anchors_target_count=30`, `scanner:movers_topN=30`, `scanner:min_listing_days=30`, `scanner:max_spread_bps=15`, `scanner:min_marketcap_usd=250000000`, `scanner:min_quote_volume_usd=150000000`, `scanner:anchors_only=1` — confirmed live
- [x] Redis seed: `scanner:exclude_pairs` SET seeded with 9 known noise tokens (PHB, ESPORTS, AGT, HMSTR, FIO, ATA, SAGA, SYS, PHA) — confirmed live

### Sub-score persistence bugfix (cont. 51)
- [x] `scanner/main.py:update_active_pairs()` INSERT extended to write `volume_score`, `volatility_score`, `spread_score`, `win_rate_score`, `pnl_score` to `pairs` table — dashboard `/pairs/active` will now show numeric values not "—" — confirmed scanner/main.py

### Blueprint & governance
- [ ] `BOT_BLUEPRINT.md` — add F51 section with all sub-features
- [ ] `BOT_BLUEPRINT.md` — update Phase 1 table with F51a/F51b/F16 (activating now)
- [x] `feature_governance/bootstrap.py` — F16, F51a, F51b, F51c, F51d registered

### Docker rebuild + verification
- [ ] Rebuild after F51a + F51b + F16 code complete
- [ ] Recreate brain / celery_worker / celery_beat / dashboard / scanner / data_feed / watchdog / web_intel
- [ ] Run pretrainer (pending from cont. 49 — all 3 CandleNet .pth files missing)
- [ ] Verify F51a active: check `risk_manager:regime_atr_mult` log key in brain logs
- [ ] Verify F51b active: check `funding_penalty_applied` in brain logs
- [ ] Verify F16 active: check `kelly_capital_applied` in brain logs

---

## Session Handoff
_Last updated: 2026-05-27 cont. 51_
**Done this session (cont. 51):** Research report compiled (49 web searches). F51 next_impl created.
Implemented and verified-on-disk: F51a (HMM-adaptive ATR), F51b (funding-rate gate),
F16 (Fractional Kelly upper-bound), F51c (ADX 1h pair-ranker, two-pass), F51d (Chandelier Exit
trailing SL as Path C alongside existing $-tier/F47 ratchets). Governance IDs registered.
Syntax-checked all 6 modified files. Config weights re-balanced.
**Next step:** Rebuild Docker images, recreate brain/celery_worker/celery_beat/dashboard/scanner/
data_feed containers. Then run pretrainer (CandleNet .pth files still missing). Then verify
the brain logs show `regime_atr_mult`, `funding_penalty_applied`, `kelly_capital_applied`,
`chandelier_applied_count`, and `scanner_adx_pass2` events firing.
**Still pending (next session, Opus 4.7):** F51e (multi-level LOB OFI — needs Binance partial
depth WS), F51f (harmonic XABCD detector — new module), F51g (VAE OOD gate — needs training
data after enough new trades close), F51h (RL exit policy replay — needs replay engine).
**Containers to rebuild:** brain, celery_worker, celery_beat, dashboard, scanner, data_feed,
watchdog, web_intel, pretrainer.
