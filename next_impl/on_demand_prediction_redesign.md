# Next Implementation — On-Demand Two-Stage Prediction + Triple-Barrier Labels + In-Trade Direction Monitoring

Owner idea, cont. 65k (2026-05-31). Status: DRAFT — owner sign-off pending.
Mode: PAPER trading (no real money) — safe to build + validate here.

This doc unifies three owner-driven changes that emerged while fixing the cont. 65k
mono-short freeze, plus the supporting research patterns. It is the production-grade
target architecture for the candle-prediction → entry → in-trade → exit pipeline.

---

## THREE-LAYER ARCHITECTURE (cont. 65k research, owner-approved)

Research finding that reframed the design: **candles (1m–1h) are aggregated/lagging — they
CANNOT catch sudden moves before they happen.** Catching sudden up/downs *before* entry
requires ORDER-BOOK microstructure at the ~3-second horizon. The cross-asset study
(arXiv 2602.00776) shows the top short-horizon predictors are **L1 order-flow imbalance,
bid-ask spread, VWAP-to-mid deviation**, and that these patterns are **stable across assets
& regimes** (the owner's "irrespective of market" requirement). During the 2025-10-10 flash
crash, the OFI signal "reached unprecedented magnitudes and correctly identified the
directional collapse" — i.e. it caught the move as it began. So the system is THREE layers,
each using the tool that wins for its job (it's not NN-vs-math — it's both + RL):

```
LAYER 1 — MICROSTRUCTURE JUMP DETECTOR  (math/formulas; ~1-3s; catches sudden moves)
  real L1 order-flow imbalance + spread + VWAP-to-mid deviation
  + coupled Hawkes process (order-flow clustering → short-term OFI forecast)
  + change-point detection (regime shift ASAP)
  → "is a sudden move starting? direction? is now adverse-selection (bad entry)?"
                 ↓ gates / TIMES the entry
LAYER 2 — MULTI-TF DIRECTION + ENTRY ZONE  (neural net; minutes-hours)
  CandleNet heads + SOFT ATTENTION over TFs (TFT-style), triple-barrier labels,
  computed ON-DEMAND for the shortlisted pair
  → direction + entry price + calibrated confidence
                 ↓ fuse: enter only if Layer1 AND Layer2 agree + confident (conformal)
        direction + entry + size  →  OPEN TRADE
                 ↓
LAYER 3 — DYNAMIC TP / EXIT  (reinforcement learning; continuous)
  PPO/DQN, reward = risk-adjusted (Sharpe, drawdown) → close at the learned peak,
  not a fixed % (supersedes static TP1/TP2 over time)
  + in-trade direction monitor (exit-only on confident debounced flip)
```

**HONEST GAP this fixes:** our current `{pair}:ofi` is a 1-tick price RETURN (noisy,
short-biased — caused the cont.65k mono-short). Layer 1 needs REAL L1 order-book imbalance
from the depth stream (we have WS/REST access but don't use it for this). Layer 1 is the
"catch sudden moves" centerpiece the candle cascade alone can't provide.

Tool-by-job (answers owner "math? NN? I don't know"): Layer 1 = math (OFI/Hawkes/
change-point, fast+interpretable+proven leading indicator); Layer 2 = NN (multi-TF pattern
fusion); Layer 3 = RL (dynamic exit). Triple-barrier = training labels.

Sources: arXiv 2602.00776 (cross-asset microstructure), 2508.02356 (multi-TF HFT),
2509.10542 (Adaptive TFT), 2411.06389 (RL execution), 2409.17591 (Hawkes change-point),
ScienceDirect S1386418126000029 (order flow & crypto returns).

DATA: all training data now pulled from REAL Binance MAINNET public klines (paginated,
no auth) — refreshed AUTOMATICALLY DAILY (celery-beat). Trading stays on testnet.

### Build order (revised cont. 65k)
1. Triple-barrier labeling — DONE. 2. Daily auto data-refresh (mainnet) — cont.65k.
3. LAYER 1 microstructure jump detector — cont.65k (highest value for "catch sudden moves").
4. Layer 2 soft-attention + on-demand trigger. 5. Layer 3 RL exit + in-trade monitor.

---

## Why (the cont. 65k freeze taught us the real problems)

1. **Predict-all is wasteful.** We continuously infer 1m/5m/15m/30m/1h for ~300
   pairs and discard 95%+. That waste clogged the queue, starved training, and was a
   factor in the freeze. (Owner: "instead of predicting all symbols all the time, when
   the bot picks a symbol then in that second get all the charts and predict and wait
   until you get entry price + direction, then place.")
2. **Labels were the root cause.** Forward-filled illiquid candles (O=H=L=C, vol=0)
   were labeled "down" by `1.0 if fut>base else 0.0` → 1.8% up-rate → mono-short
   collapse. The cont. 65k deadband patch is a band-aid; **triple-barrier labeling**
   is the principled fix.
3. **No post-entry direction guard.** (Owner: "predicting direction long or short
   AFTER placing a trade is very important.") VERIFIED: `risk/manager.py` only runs
   trailing-SL on open trades; F13 (`signals/engine.py:173-199`) flips direction
   ONLY at entry. There is no continuous in-trade direction re-prediction → exits are
   purely reactive (wait for SL) not proactive (exit when thesis flips).

---

## Research grounding (web search, cont. 65k)

- **Cascade ranking / two-stage detection** — dominant pattern in large-scale
  selection: cheap funnel → expensive confirm. Validates the on-demand idea.
  (arXiv 2503.09492)
- **Multi-timeframe HFT crypto, 2025 (arXiv 2508.02356)** — almost our exact design:
  cheap trend-net SELECTS which direction-nets to run; multi-head CNN (per-TF) +
  **soft attention** over TFs + orderbook → direction + confidence → confidence-gated
  sizing; 100-300ms cycle; risk controls independent of signal gen. Key lesson:
  *removing the attention layer collapsed confidence scores* → our hard 3-of-4 vote is
  the crude version; learned attention makes confidence meaningful (relevant to the
  calibration problem we hit).
- **Meta-labeling (López de Prado)** — primary model predicts SIDE (high recall),
  secondary model predicts "is primary correct?" → sizing + false-positive suppression.
  Maps onto: cascade = primary, Judge/Meta-RL-Crypto = secondary.
- **Triple-barrier labeling (de Prado)** — label by which of {take-profit, stop-loss,
  time-expiry} barrier is hit first → label encodes direction + risk + timing; flat
  windows naturally become neutral (0). The proper replacement for our deadband.
  (arXiv 2504.02249)
- **Conformal abstention / Selective Conformal Risk Control (arXiv 2512.12844)** —
  trade only when calibrated-confident, else abstain, with distribution-free
  guarantees. Ideal for both the entry gate and the in-trade guard. We already ship
  `ml/conformal_wrapper.py` (F56) but underuse it.

---

## Target architecture

```
STAGE 1 — CHEAP FUNNEL  (continuous, all ~300 pairs, NO heavy ML)
  scanner + OFI + volume surge + volatility + liquidation events
  → rank, shortlist 3-10 candidates  (this is what "picks the symbol")
                         │
STAGE 2 — DEEP CONFIRM  (on-demand, ONLY shortlist, fresh data from local cache)
  per-candidate: pull multi-TF candle history (per-TF lookback, see note),
  run CandleNet heads + SOFT ATTENTION over TFs (replaces hard 3-of-4 vote)
  → direction + entry price + calibrated confidence
  labels trained via TRIPLE-BARRIER (not the deadband)
                         │
STAGE 3 — META-GATE  (the Judge as meta-labeling secondary model)
  "is this signal likely correct?" → size it
  + CONFORMAL ABSTENTION → trade only if calibrated-confident, else skip
                         │
                  open trade (direction + entry + size)
                         │
STAGE 4 — IN-TRADE DIRECTION MONITOR  (NEW — owner's post-entry point)
  every N sec, re-run the Stage-2 cascade on the OPEN pair:
    agrees w/ position → hold (trailing SL rides)
    weakens           → tighten stop / partial exit
    FLIPS (confident) → proactive exit (or reverse)
  confidence-gated + debounced (N consecutive flips OR conf floor) to avoid whipsaw
```

### Per-TF lookback note (correctness)
"24h" is NOT enough for higher TFs. Model needs CONTEXT_LENGTH=60 candles:
1m→1h, 5m→5h, 15m→15h, **30m→30h, 1h→60h**. Lookback must scale per timeframe.
Pull from the local candle store data_feed maintains (NOT a fresh API call) → fast.

---

## Existing vs. to-build (Rule-2 verified on disk, cont. 65k)

| Component | Exists? | File | Gap to close |
|---|---|---|---|
| Cheap scanner/universe | ✅ | `scanner/categories.py` (100-pair categorised) | add OFI/vol/liq RANKING → shortlist, not just universe |
| Multi-TF cascade | ✅ hard-vote | `signals/multi_tf_cascade.py` | replace hard 3-of-4 vote with **soft attention** weighting |
| CandleNet multi-head | ✅ | `ml/candlenet.py` | add attention head; **triple-barrier labels** in train() |
| Pre-open gate (wait for forecast) | ✅ | `signals/pre_open_candle_gate.py` | make forecast **on-demand per candidate** vs precompute-all |
| Entry direction (F13) | ✅ entry-only | `signals/engine.py:173-199`, `ml/direction_model.py` | reuse model in Stage 4 (in-trade) |
| Meta/secondary model | ✅ (Judge) | Meta-RL-Crypto judge | wire as meta-labeling sizing/suppression layer |
| Conformal wrapper | ✅ underused | `ml/conformal_wrapper.py` (F56) | use for entry + in-trade abstention |
| In-trade loop | ⚠️ SL only | `risk/manager.py` `monitor_trailing_sl` | **ADD direction re-prediction step** |
| Triple-barrier labeling | ❌ | — | NEW in `ml/candlenet.py` data prep |
| On-demand inference trigger | ❌ | — | NEW: candidate → JIT multi-TF predict |

---

## Confirmed decisions (owner)
- Cloud LLM primary, Ollama fallback after all cloud cooldown — DONE cont. 65k
  (`llm/researcher.py`, reversible `llm:cloud_primary`).
- Ollama capped to 2 resident models — DONE cont. 65k (docker-compose).
- Build on PAPER, do it right, no rush (no real money at risk).
- On-demand prediction direction is the chosen backbone (vs predict-all).
- In-trade direction monitoring is a first-class component, not an afterthought.
- **(cont. 65k sign-off) TF combine = LIGHTWEIGHT learned weighting FIRST, full soft
  attention later.** Small learned layer (linear/logistic over per-TF dir3 + regime)
  replaces the hard 3-of-4 vote; must beat the vote on paper before graduating to full
  attention. Rationale: fixes crude-confidence without overfitting thin 5-8k data.
- **(cont. 65k sign-off) Triple-barrier width = TIGHT SYMMETRIC ±1 vol_unit**, time
  barrier a few candles per TF. Matches the bot's existing vol_unit stop sizing so
  labels and live exits speak the same language. Replaces the deadband. Tune on paper.
- **(cont. 65k sign-off) In-trade flip action = EXIT-ONLY, confident + debounced.**
  Close on a confident flip requiring N consecutive flipped reads + conf floor (no
  reverse, no partial yet). Graduate to tiered (tighten→partial→full) once proven on
  paper. Reversal explicitly rejected for now (whipsaw risk on a weak model).
- **(cont. 65k sign-off, REVISED) Predict-all = THREE-TIER continuous set:**
  - **BTC/ETH** → continuous, regime detection.
  - **Top 50 scanner pairs (ranked by LIQUIDITY/VOLUME)** → continuous predict-all so
    forecasts are pre-warmed for the real tradeable universe → near-zero latency when
    the funnel picks one of them. Must be the scanner's top-50 LIQUID names (not
    arbitrary) so we don't re-burn cycles on flat/forward-filled pairs (the freeze cause).
  - **Long tail (pairs 51-300)** → ON-DEMAND only, when the cheap funnel flags a spike.
  Rationale (owner cont. 65k): top-50 liquid pairs have real non-flat data AND are the
  ones most likely to trade, so continuous prediction there is pre-warming, not waste —
  and it's cheap now (clean-data GAF cache dropped 3.1GB→250MB). Drops the old 300-pair
  load to ~52 continuous + on-demand tail.
- **(cont. 65k default, owner did not object) Shortlist = 5-8 pairs**, ranked by a
  cheap composite of OFI + volume surge + volatility + liquidation proximity.

## Proposed decisions (remaining, lower-priority — defer to build time)
- [ ] Debounce N (consecutive flipped reads) + conf floor exact values for in-trade exit.
- [ ] In-trade monitor cadence (5-15s candidate range).
- [ ] Exact per-TF time-barrier lengths for triple-barrier.
- [ ] Regime-majors list beyond BTC/ETH (which "few majors").

---

## Build sequence (recommended)
1. **Triple-barrier labeling** in `ml/candlenet.py` (replaces deadband) — fixes the
   data/label root cause; foundation for everything. Retrain 5m/15m, compare AUC.
   (Also do the data-pipeline freshness/row-cap fix here — they overlap.)
   **STATUS cont. 65k: DONE (labeling) + VALIDATED.** Implemented per-window triple-
   barrier: ±1 vol_unit symmetric barriers, time barrier = per-head horizon, single
   forward pass; keep window only if the cascade's primary horizon resolves, else
   skip (non-event — replaces the deadband). Live result on 5m: up_rate **0.5099**
   (was 0.476 deadband, 0.018 raw), 104,208 non-events dropped, 3,942 samples.
   Reverted this session FIRST: PCG strict mode (engine.py) + sentiment_block_short
   default (both were cont. 65k whack-a-mole patches the owner had me undo).
   TODO still in step 1: data-pipeline freshness/row-cap (1000-row 2024 snapshot →
   thin samples, AUC ~0.56); dedicated candlenet worker (inference starves retrains).
2. **Soft attention** over TF heads — calibrated confidence the meta-gate can trust.
3. **On-demand trigger** — candidate shortlist → JIT multi-TF predict from local cache.
4. **In-trade direction monitor** — add re-prediction step to `risk/manager.py` loop,
   conformal-gated + debounced.
5. **Meta-gate** — wire Judge as meta-labeling secondary (size + suppress) + conformal
   abstention on entry and in-trade.

Each step ships + validates on paper before the next. Sequential, not merged.

---

## Session handoff (cont. 65k)
- Freeze ROOT-FIXED: stale-data label collapse → deadband+balance+temp-scale+ECE gate.
  5m (AUC 0.561) + 15m (AUC 0.592) retrained balanced, deployed PAPER. Forecasts
  balanced (5m 18/17/65, 15m 30/21/49). Models WEAK due to thin/stale data — this
  redesign (triple-barrier + data fix) is the quality fix.
- Deploy mechanism: `./ml:/app/ml` bind-mount on celery_worker_candlenet (disk 96%
  full, no rebuild). Revert to baked image once disk healthy.
- NOT yet done: 30m/1h retrain (optional votes), strategy-evolution queue throttle
  (5,783 backlog), decide-LLM cloud-primary (Ollama timeout ~14s/decision).
- Trade flow LIGHT post-fix (deadband neutralizes ~50-65% of illiquid pairs) — milder
  than the freeze; monitor whether paper trades trickle in.

## References
- arXiv 2508.02356, 2503.09492, 2504.02249, 2512.12844; Meta-Labeling (de Prado).
- Code: `signals/{multi_tf_cascade,pre_open_candle_gate,engine}.py`, `risk/manager.py`,
  `ml/{candlenet,direction_model,conformal_wrapper}.py`, `scanner/categories.py`.
- [[project_predict_all_before_open]] [[project_postmortem_rag_shipped]]
  [[feedback_recommend_every_option]] [[feedback_silent_rejection]]
- Supersedes the "predict-all" framing in `next_impl/predict_all_before_open.md`
  (predict-all → predict-ON-DEMAND-before-open).
