# PROFESSOR AUDIT — Impartial Full-System Evaluation

> Standing rule for this document: **fact vs. inference is always separated.** Anything
> not directly read in code/config or measured in a run is labeled HYPOTHESIS. No fixes
> are made during the audit phase. Goal = a complete, honest map before any judgement.
>
> Engagement framing (agreed with owner, 2026-06-04):
> - Disease = an **unmeasurable, accreted system**; every past fix was a guess because
>   nothing could be falsified.
> - North star is **expectancy / profit factor / Sharpe / max-drawdown / out-of-sample
>   stability** — NOT winrate. High winrate is treated as a red flag until proven.
> - Sequence: Freeze → Build instrument → Inventory(static) → Behavioral audit →
>   Ablation → Patch-vs-rebuild verdict → Reconstruct core → Validate before live.
> - We are in **Phase 2: Inventory & static audit (read-only).**

---

## SYSTEM CENSUS (2026-06-04)

- Python: **58,114 LOC / 212 files**.
- Docker services (18): postgres, redis, ollama, brain, data_feed, micro_ws, kline_ws,
  liq_ws, scanner, web_intel, dashboard, celery_worker, celery_worker_candlenet,
  celery_worker_cn_train, celery_beat, watchdog, pretrainer, nginx.
- Largest code masses:
  - `ml/` 13,431 LOC (47 files) — biggest subsystem = least verifiable machinery.
  - `signals/` 6,779 LOC (17) — entry pipeline.
  - `risk/` 4,825 LOC (14) — sizing + exits + frontier kill switches.
  - `celery_app.py` **4,336 LOC single file** — live loop + task routing; audit red flag.
  - `data/` 3,984 (22), `memory/` 2,734 (9), `dashboard/` 2,498 (2), `prediction/` 2,072 (8).
- Feature flags present in code: F2,F4,F5,F8,F9,F10,F12–F61 (with gaps) — ~50 features.
- Big docs: PROGRESS.md 868 KB, BLUEPRINT (home) 296 KB, BLUEPRINT_COMPLIANCE_AUDIT.md 60 KB.
- Disk: **89% used, 32 G free** (owner memory said 44%/160G — STALE; corpus/models growing).
- Blueprint lives at /home/ajithd747/ai-brain-crypto-bot/BOT_BLUEPRINT.md.

---

## COVERAGE TRACKER  (read = full read, surveyed = skim/grep, skip = artifact)

| Module / file | LOC | Status | Notes |
|---|---|---|---|
| brain/soar.py | 606 | READ | orchestrator / SOAR loop / _act gates — see pipeline map + F-001..F-006 |
| signals/engine.py | 3191 | PARTIAL | generate_candidate_signals + accept_or_reject READ; process_signals (1729-3165) pending |
| signals/ (rest) | 3588 | pending | bayes_threshold, ev_override, launch_pad, microstructure, etc. |
| risk/manager.py | 2409 | PARTIAL | compute_initial_sl READ; monitor_trailing_sl (~1500-line monolith) STRUCT — F-018/F-019 |
| risk/ (rest) | 2416 | pending | frontier exits, hedge, trail_params |
| feature_governance/registry.py | 529 | READ | autonomous on/off controller — see F-015/F-016 (CRITICAL) |
| signals/engine.py process_signals | 1436 | STRUCT | Layer-3 mapped via grep; ~15+ MORE gates/overrides — see F-017 |
| prediction/ | 2072 | pending | predict-all + conformal gates |
| ml/hmm.py | 261 | READ | regime classifier — cont.70 redesign is SANE (rule-based); see F-012 |
| ml/ (rest) | 13170 | pending | 46 files of models/trainers — contribution to gate UNVERIFIED (F-014) |
| debate/fallback.py | 271 | READ | deterministic_verdict — GOOD design; but re-applies regime (F-021) |
| debate/council.py | 728 | STRUCT | async LLM debate (learning-only, off hot path) |
| execution/ | 1403 | STRUCT | live/paper open/close/modify_sl/dca/partial mapped |
| risk/frontier/ | 1300 | STRUCT | composable exit evaluators — GOOD pattern; see F-020 |
| celery_app.py | 4336 | pending | live loop + task routing |
| redis_keys.py | 257 | pending | shared-state contract (coupling map) |
| config.yaml | 213 | READ | risk knobs — 20x fixed leverage (F-024); stage gates; DCA off |
| celery_app.py | 4336 | STRUCT | 88 tasks + beat schedule catalogued — F-022/F-023 |
| config.py / main.py / db.py / redis_keys.py | — | READ/STRUCT | wiring + key contract |
| ml/ (46 model files) | 13170 | WIRING-MAPPED | consumer-count map done; nhits=DEAD; contribution is an ABLATION question, not a reading one |
| speculative layers (world_model, metacognition, self_play, self_improve, curiosity, pattern, memory/cognitive, research, web_intel) | ~7k | WIRING-MAPPED | see consumer map; most weakly integrated / advisory-only |
| data/ scanner/ strategy/ llm/ prediction/ dashboard/ tools/ pretrainer/ notifications/ account_risk/ watchdog/ analytics/ | — | SURVEYED | structure + wiring mapped; infra/producers, not decision logic |
| data/ | 3984 | pending | feeds, liq, kline |
| account_risk/ scanner/ execution/ | — | pending | |
| metacognition/ world_model/ self_play/ self_improve/ curiosity/ pattern/ research/ web_intel/ | — | pending | speculative-AI layers — verify if live |
| memory/ strategy/ strategies/ feature_governance/ | — | pending | |
| frontend/build, hf_cache/*, __pycache__, .git | — | skip | generated/binary artifacts |

---

## DECISION PIPELINE MAP  (where a trade is BORN and every place it can be KILLED)

_(to be filled — this is the single most important artifact for the "trades won't open /
mono-long / mono-short" problem)_

### Trade-birth path (brain level — brain/soar.py)
SOAR loop every **5s**: OBSERVE → DECIDE → ACT.
- OBSERVE: reads `regime`, `turbulence`, `global_sentiment`, `active_pairs`, `brain_stage` from Redis.
- DECIDE: builds LLM prompt → calls ollama `decide`/`classify` (6–20s timeout) → produces
  `{trade, reason}`. Also runs L2 world-model uncertainty, L3 MemRL base-rate, L9 metacog
  confidence, F38 curiosity — **all written to Redis, none gate anything** (advisory).
- ACT: the real entry path → ends in `process_signals(active_pairs, brain_state, engine)`.

### Veto / kill points at BRAIN level (in ACT order)
1. `brain_stage>=2` + LLM `trade=False` → **LLM macro-veto** — DEMOTED to advisory, default OFF
   (`brain:llm_macro_veto_enabled`). Was a 2.5h halt cause (FACT, per code comment + memory).
2. `check_turbulence_circuit_breaker()` → return (in risk/manager.py — read pending).
3. `bot:max_open_trades` / `bot:max_position_usdt` unset → return (`brain_waiting_for_settings`).
4. `balance<=0` → return.
5. `bot:running != "1"` → return (Stop button).
6. `len(open_trades) >= max_open` → **at capacity** → return (skips ALL signal gen).
7. capital sizing chain → `capital_starved` return if `<5 USDT` or can't cover reserve.
8. F21 MARL day-agent caps capital; F16 Kelly caps capital (50+ trades).
9. F8 strategy selector (UCB1 bandit) picks strategy UUID.
10. → `process_signals(...)` = **per-pair gauntlet** (signals/engine.py — the real gate stack).

Stage transitions (`_check_stage_transition`) are **winrate+sharpe gated**
(stage_2→3 winrate, stage_3→4 winrate+sharpe). NOTE: gating evolution on winrate inherits the
flawed-north-star problem.

### THE PER-PAIR VETO GAUNTLET (signals/engine.py) — the heart of the disease
A signal must survive **THREE sequential layers**, each a chain of independent veto gates that
were added in different `cont.XX` sessions, each with its own Redis toggle + threshold, all
**ANDed together** (any one returns `[]`/`False` → trade dies).

**LAYER 1 — `generate_candidate_signals` (L17-1024): ~13 HARD kills (`return []`)**
- L45/75/111/113 empty/no-forecast; L133 low_volatility; L174 pre-open-candle-gate;
  L177/211 multi-TF cascade veto (+ macro 4h veto) ***[directional]***;
  L275 micro order-book jump veto; L489 low_predicted_move (HARD, cont.66);
  **L519 short-side guard — blocks SHORT when HTF dir3>0.55 ***[directional]***;**
  L891 idiosyncratic/BTC-beta gate (default off); L919 RL entry-timing "skip" (F48);
  L980 candle-close-confirm (P4, has deadlock detector). Plus F52 net-flow directional gate.

**LAYER 2 — `accept_or_reject` (L1184-1726): ~13 more `return False`**
- regime_not_in_strategy_whitelist (+deadlock breaker); prediction gate ×4 (not_ready/
  low_conf/low_rr/**dir_mismatch** ***[directional]***); structural regime_unknown gate
  (+deadlock breaker); **signal_too_weak**; turbulence_too_high;
  **btc_lead: dump→blocks_long / pump→blocks_short ***[directional, regime-scaled]***;**
  **sentiment gate: regime-keyed, blocks contra-regime side ***[directional]***;**
  liquidity_below_floor; **F56 conformal_uncertain**.

**LAYER 3 — `process_signals` (L1729-3165): capacity / slot-selection / sizing / execute** (pending read).

### >>> CENTRAL MECHANISM (FACT) — this single finding explains MOST of your symptoms <<<
1. **~26+ veto gates ANDed in series.** Survival is multiplicative. Even at 90%/gate,
   0.9^26 ≈ 6% of legitimate signals survive → "trades won't open." Tightening any one gate in a
   session silently drops total throughput non-linearly → "fixed X, broke trading."
2. **A single unreliable `regime` label fans out into ~4 directional suppressors:**
   (a) regime composite scoring docks against-regime signals ~20 strength (code comment: bull-regime
   avg SHORT strength 19.8 vs LONG 33.6); (b) short-side guard; (c) regime-keyed sentiment gate;
   (d) regime-scaled btc-lead gate. So a WRONG "bull" label → shorts crushed on all four → **mono-long**;
   wrong "bear" → **mono-short**. The regime classifier is a **single point of failure** whose error
   cascades into systemic directional bias. THIS is your "saves bull when it's bear → mono" bug.
3. **`min_signal_strength` is mutated through 6-7 override layers** (default 30 → GA[15,28] →
   +F9 bucket-delta re-clip[15,35] → F8 per-strategy override → Bayes full-override → `risk:min_signal_strength`
   CEILING clamp(18) → +structural session penalty). **No human or log can state the runtime value.**
   This is the literal definition of an unmeasurable decision.
4. **4+ "deadlock detectors" exist** (strategy-router regime, structural regime, P4 candle-confirm, MARL)
   that auto-disable a gate when it rejects >80%. The *existence of a whole genre of self-defense code*
   is proof the gauntlet routinely starves the bot to zero. They treated the symptom (auto-bypass) instead
   of the cause (too many serial gates).

---

## PER-FEATURE INVENTORY (F2–F61)

| Feature | Claimed purpose | Wired? | Fires? | Off-switch | On/Off now | Coupling / state | Verdict |
|---|---|---|---|---|---|---|---|
| _TBD_ | | | | | | | |

---

## FINDINGS LOG  (numbered; each tagged FACT or HYPOTHESIS)

- **F-001 (HYPOTHESIS, high value):** The entire DECIDE LLM call (every 5s, 6–20s ollama/cloud)
  now feeds only the macro-veto, which is **default-OFF**. So the per-cycle LLM verdict is
  effectively **decoration with a real CPU/latency cost**. Confirm no other consumer reads
  `verdict`. If so → DECIDE LLM is a deletion candidate (or move to low-frequency).
- **F-002 (HYPOTHESIS, high value):** L2 world-model uncertainty, L3 MemRL base-rate, L9 metacog
  confidence, F38 curiosity are computed every cycle and written to Redis but **gate nothing in
  soar.py**. Need to grep all consumers. If nothing reads them to change a decision → pure
  decoration burning CPU. This is the literal "features that don't contribute" you described.
- **F-003 (FACT):** Real entry gating is NOT in the brain — it's downstream in
  `signals/engine.py::process_signals` (3,191 LOC). That is where "trades won't open /
  mono-long / mono-short / signal-strength skip" must be diagnosed. Priority read.
- **F-004 (FACT):** Features are gated by `feature_governance.registry.is_active("Fxx")` — a
  governance layer can silently deactivate features. Must read registry.py to learn what is ON
  vs OFF *right now* and by what rule (auto vs manual). Central to "we disabled things to fix".
- **F-005 (FACT):** "At capacity" (gate 6) skips ALL signal generation when open==max_open. If
  max_open is small and exits are slow, the bot looks dead even with good signals. Interacts with
  the SL/exit logic — a slow-exit bug would masquerade as a "won't open" bug.
- **F-006 (FACT):** Brain evolution + several sizing features (Kelly F16, stage gates) are
  **winrate-driven**. Flawed north star is wired into the control flow, not just reporting.

- **F-007 (FACT, ROOT CAUSE):** ~26+ serial ANDed veto gates across 3 layers → multiplicative
  trade-starvation; per-session gate additions interact non-linearly. This is the master cause of
  "trades won't open" and "fixed one thing, broke another." See CENTRAL MECHANISM #1.
- **F-008 (FACT, ROOT CAUSE):** The `regime` label fans out into ~4 directional gates → a single
  classifier error produces mono-long / mono-short. Regime is a single point of failure with
  amplified blast radius. See CENTRAL MECHANISM #2. (Read ml/hmm.py + regime composite next.)
- **F-009 (FACT, ROOT CAUSE):** `min_signal_strength` resolved via 6-7 mutating override layers
  from different sessions; runtime value is unknowable. Unmeasurable by construction. #3.
- **F-010 (FACT):** 4+ deadlock detectors = institutionalized evidence the gauntlet starves the bot.
  Band-aids on over-gating, not fixes. #4.
- **F-012 (FACT, important + partly REASSURING):** `ml/hmm.py` was REDESIGNED in cont.70 and is
  now **sound**: a transparent rule — regime = sign of `composite = 0.35*BTC_24h + 0.65*alt_median_24h`
  vs ±1.5%, with alt-breadth tie-break and a dispersion/vol "turbulent" overlay; HMM off by default;
  never defaults to "bull"; "unknown" keeps prior. The OLD bugs (cross-section-as-timeseries,
  positional labels, unmapped 4th state, bull default) are FIXED. BUT two live risks remain:
  (a) it reads `BTCUSDT:change_24h` + per-pair `change_24h`; if those keys go **stale/missing**,
  composite→None→"unknown"→**keeps prior regime forever** (sticky-wrong). (b) It runs only every
  **60 SOAR cycles (~5 min)** and is gated behind `is_active("F14")`. Verify freshness + F14 ON in
  behavioral phase. The classifier is NOT the bug anymore — its **blast radius** is (see F-008).
- **F-013 (FACT, confirms mono mechanism):** `signal_strength = trade_potential` = weighted avg of
  8 components: ofi 0.25, **regime 0.20**, tft 0.15, hist_acc 0.13, candlenet 0.12, vpin 0.10,
  patchtst 0.08, sentiment 0.05. The `regime` component = **100 aligned / 50 unknown-or-turbulent /
  0 AGAINST regime**. So a contra-regime trade is docked **0.20×100 = 20 strength points** purely for
  fighting the (possibly wrong) regime label — matching the code's own "short 19.8 vs long 33.6 in bull"
  note. With min_strength ~18-28, that 20-pt regime penalty ALONE can sink contra-regime trades below
  the gate → **mono-directional**. This is the precise arithmetic of your mono-bull/mono-short bug.
- **F-014 (HYPOTHESIS, very high value — needs consumer trace):** The gated value is `trade_potential`
  (8 components). The heavy ML — Mamba(P1), Chronos/Foundation(F50g), GNN-multiscale(F24M),
  netflow(F52), Qlib-158(F53), LLM-DSL(F54), liq-cascade(F58) — feeds ONLY `direction_conf`, a
  SEPARATE sum of ±5/±8/±18 nudges. `direction_conf` appears to be consumed only by the F56 conformal
  abstain gate + storage. IF so, **most of the 13k-line ml/ subsystem barely influences whether a
  trade is taken** — the literal "we added ML and never checked if it contributes" problem, quantified.
  MUST trace every `direction_conf` consumer before declaring FACT.
- **F-011 (HYPOTHESIS):** Many gates are "default off" or "soft" now (idiosyncratic, predict-all,
  conformal re-enabled, llm macro-veto) — the live ON/OFF set is scattered across dozens of Redis
  keys with no single source of truth. Need a live `redis` dump of all gate toggles to know the
  CURRENT effective gauntlet. (Action: snapshot Redis keys in behavioral-audit phase.)
- **F-014 (NOW FACT, with nuance):** `direction_conf` consumer trace done. TFT(F19), CandleNet(F46),
  PatchTST(F20) DO reach the entry gate (they're components of `trade_potential`, weights .15/.12/.08).
  But **Mamba(P1), Chronos/Foundation(F50g), GNN-multiscale(F24M), Qlib-158(F53), LLM-DSL(F54),
  liq-cascade(F58), netflow(F52) feed ONLY `direction_conf`**, which is consumed by just: the F56
  conformal soft-abstain gate + stored as a trade column for post-hoc learning/analytics. So
  **thousands of lines of ML (qlib_alphas 613, llm_alpha_dsl 746 + dsl_* ~782, gnn_multiscale 350,
  mamba 468, foundation 327, …) exert near-zero influence on whether a trade opens.** Quantified
  confirmation of "added ML, never checked contribution." Ablation will measure each precisely.
- **F-015 (FACT, CRITICAL — autonomous instability source):** `feature_governance` is an autonomous
  controller that turns features OFF/ON based on a per-feature "contribution" score. **That score is
  FAKE attribution:** `update_contribution` writes the SAME bot-wide +1/-1 (trade won/lost) onto
  EVERY active feature — it does NOT isolate a feature's effect. The code itself admits this. So the
  controller literally **cannot tell which feature helped or hurt**, yet it deactivates/reactivates/
  probations them. It deactivated **36/36 features in 32 seconds TWICE** (D-02, D-07) during losing
  streaks; the fix was bolt-on peer-snapshot guards, not real attribution. This autonomous on/off
  churn is a direct cause of "we disabled things to fix and new problems arose" — the SYSTEM is
  toggling features on a metric that measures the market, not the feature.
- **F-016 (FACT):** Feature ON/OFF truth lives in ONE Redis key `brain:active_feature_flags`
  (default True except baked-off F13/F35). A **Redis wipe resurrects features**: it already caused
  F13 to silently flip trade direction **714×** after the flags key was cleared. No durable,
  auditable source of truth for "what is on right now." Confirms F-004/F-011.
- **F-017 (FACT):** `process_signals` (Layer 3) adds **~15+ MORE veto/override points** on top of
  the two earlier gauntlets: launchpad funnel, replay pool, slot-bandit, dup_pair, **dir_balance
  gate (ANOTHER directional controller)**, xsmom modulator, F35 MemRL reject, cluster context,
  GNN leader/sympathy boosts, MPP, Q-modulate, L2/L9 inline-decide rejects, confidence hard_skip,
  R4/R2 block, postmortem-RAG override, F37 debate (skip/skip_risk/size_mult), F21 MARL-minute
  (skip/hold). **Total entry gauntlet ≈ 40+ decision points across 3 layers**, with at least
  TWO independent directional controllers (regime-suppression vs dir_balance) that can fight.
  Revises F-007 upward: not 26, ~40+.
- **F-018 (FACT):** `compute_initial_sl` stacks **7 layers** to make one SL distance: regime atr_mult
  {bull/bear 1.5, turbulent 3.5, else 2.5} → min_pct floor → VPIN vol_unit (legacy ATR empty) →
  F48 CandleNet mag floor → cont.70f Chronos vol-band floor → cont.60 DVOL scaling (0.7–1.5×) →
  cont.60 liquidation dark-side snap. All are `max()`/scale (widen-only) so less dangerous than the
  override-tower in min_strength, but still opaque/unpredictable per trade.
- **F-019 (FACT, SL ROOT CAUSE):** The trailing-exit brain `monitor_trailing_sl` is a
  **single ~1,500-line function** (risk/manager.py ~513–2047) accreted across cont.47/53/57/60/62/
  64/65/67. It contains Paths A–F, TP1/TP2 checkpoint SL-locks, peak-crossed Redis latches, wickless
  TP debounce, 15m-reversal force-close veto, the Capital Ladder 50/75/85, breakeven shield,
  chandelier time-decay — all interleaved with ~16 return points and many nested closures. This is
  an **untestable monolith** and is the structural reason "SL logic" keeps breaking: no one can
  change one path without unknowable side effects on the others. Runs as a Celery task.
- **F-020 (FACT):** There are **TWO parallel exit systems**: (1) the 1,500-line monolith
  `monitor_trailing_sl`, and (2) `risk/frontier/evaluate_all_exits` — a clean, composable chain of
  independent evaluators (CVD divergence, filtered-OBI, funding-premium, Hawkes, MM-Hawkes spoof,
  BOCPD killswitch, GNN-contagion kill, liq-cascade kill, LLM exit council, PPO exit, deep-hedging)
  folded via `ExitDecision.fold`. The frontier design is GOOD and salvageable; the monolith is not.
  Two exit brains on the same trade risk conflicting decisions — interaction must be traced.
- **F-021 (FACT, refines F-008 — regime over-counting):** The `regime` signal is applied as a
  penalty/gate **4–6 independent times**: (a) `trade_potential` regime_score (~20 effective pts off
  contra-regime), (b) entry gates (short-guard + regime-keyed sentiment + regime-scaled btc-lead),
  (c) `debate/fallback.deterministic_verdict` (+8 aligned / **−12 opposed** / −6 turbulent),
  (d) initial-SL `atr_mult` keyed by regime. Same input double/triple/quad-counted → regime massively
  over-weighted → contra-regime trades crushed at multiple independent stages → self-reinforcing
  mono-directional bias + broken probability calibration.
- **POSITIVE-001 (FACT):** `debate/fallback.py` (interpretable, fail-open, reasons attached) and
  `risk/frontier/*` (composable evaluators + fold) are **examples of the RIGHT design** already in the
  codebase. The rebuild should generalize these patterns, not invent new ones.
- **F-022 (FACT):** **88 Celery tasks**; beat schedule runs heavy background compute on a 6-CPU box —
  candle_online_train every 60s, shadow_ablation every 60s, prediction_refresh 30s, foundation_forecast
  600s, xsmom 5min, bayes_threshold 5min, GA evolve 6h, self_play 30min, pattern mining 6h, conformal
  daily, web_intel, etc. Most feed machinery that barely changes trades (F-014). This is the
  "CPU allotted to features" pain — large, continuous cost for near-zero decision impact.
- **F-023 (FACT, CRITICAL — co-root-cause with the gauntlet):** The system runs **MULTIPLE autonomous
  self-modification loops simultaneously**: `dgm_rewrite_weakest` (Darwin-Gödel-Machine — the bot
  **rewrites its own code** daily 05:00), `ai_scientist_run` (4h), `ga_evolve_params` (param evolution
  6h), OPRO (prompt evolution per 5 trades), `feature_governance` (auto feature on/off hourly on FAKE
  attribution, F-015), `update_pair_lists_from_decoder`, `refresh_bayes_threshold` (adaptive entry gate
  5min). **The running system continuously mutates its own code, parameters, prompts, thresholds,
  feature-flags, and pair-lists.** Consequences: (1) the system you debug in a session is literally
  NOT the system running hours later → "one session's change broke another"; (2) behavior is
  non-reproducible → "we don't know how it decides"; (3) **measurement/ablation is impossible while
  the system rewrites itself underneath the experiment.** This must be FROZEN before anything can be
  measured or fixed.
- **F-024 (FACT, ACUTE SURVIVAL RISK):** Leverage is **fixed at 20x** (leverage_min=max=20), with up
  to **80% of capital deployed** and `full_deploy_mode` mandated. At 20x, a **2.5% adverse price move =
  50% capital loss**. Combine with the mono-directional bias (F-008/F-021): the bot tends to hold many
  simultaneous SAME-direction 20x positions, so a SINGLE wrong regime call can produce catastrophic
  correlated drawdown. The celebrated high winrate makes this MORE dangerous, not less (classic
  "pennies in front of a steamroller": many small wins, rare correlated 20x wipeout). **This is the
  most acute threat to account survival and must be de-risked before live capital — independent of the
  rest of the rebuild.**

## ────────────────────────────────────────────────────────────────────────────
## VERDICT  (after full static audit, 2026-06-04)
## ────────────────────────────────────────────────────────────────────────────

**The plumbing is fine. The brain is the problem. Rebuild the decision core; keep the infrastructure.**

KEEP (healthy / valuable — do NOT rewrite):
- Data layer: WS feeds, kline corpus + bulk backfill, scanner, exchange client, execution
  (paper/live open/close/modify_sl), Docker/Postgres/Redis topology.
- Two in-repo patterns that are CORRECT: `risk/frontier` composable evaluators+fold, and
  `debate/fallback.deterministic_verdict` (interpretable, fail-open, reasons attached).
- The regime classifier MATH (ml/hmm.py post-cont.70) — sound; the problem is how it's USED.

REBUILD / STRIP (the disease lives here):
- ~40-gate 3-layer entry gauntlet (F-007/F-017) → ONE transparent scored decision.
- 6–7-layer `min_signal_strength` override tower (F-009) → ONE threshold.
- 4–6× regime over-counting (F-008/F-021) → count regime ONCE.
- 1,500-line `monitor_trailing_sl` monolith (F-019) → composable exit (reuse frontier pattern).
- `feature_governance` autonomous toggling on FAKE attribution (F-015/F-016) → DELETE; replace with
  offline ablation-driven keep/cut decided by humans on measured data.
- Autonomous self-modification loops (F-023: DGM, AI-scientist, GA, OPRO, bayes-gate, pair-decoder)
  → FREEZE. They make the system unmeasurable and non-reproducible.
- 20x fixed leverage + 80% deployment + mono bias (F-024) → de-risk immediately.
- Dead/near-dead ML (F-014; nhits=0 consumers; direction_conf-only forecasters) → quarantine; re-admit
  only if it earns its place in ablation.

WHY NOT a full from-scratch rewrite: the infrastructure is the expensive, working part and is NOT the
source of any symptom. The surgical target is the decision core (signals/engine.py entry path +
risk/manager.py exit monolith + the autonomous loops), where 100% of the diagnosed root causes live.

## ────────────────────────────────────────────────────────────────────────────
## INSTRUCTION MANUAL  (sequenced; each phase gates the next)
## ────────────────────────────────────────────────────────────────────────────

### PHASE 0 — FREEZE & DE-RISK (do first; reversible)
0.1 Disable ALL self-modification beat tasks (dgm_rewrite_weakest, ai_scientist_run, ga_evolve_params,
    OPRO writeback, update_pair_lists_from_decoder, refresh_bayes_threshold, run_feature_governance_check)
    → system becomes STATIC.
0.2 Pin feature flags: one auditable `brain:active_feature_flags` on disk+Redis; governance can't change it.
0.3 Cut leverage to 3–5x and total deployment ≤40% until validated. Survival before optimization.
0.4 Git tag `pre-rebuild-baseline`; snapshot full Redis (all gate toggles) = the true current gauntlet.

### PHASE 1 — BUILD THE INSTRUMENT (measurement first)
1.1 Decision ledger: one row/candidate with FINAL score, regime, and the SINGLE take/kill reason.
1.2 Backtest/replay over kline corpus with strict train/val/test + walk-forward, no lookahead
    (extend pretrainer/walk_forward.py).
1.3 Real metrics: expectancy, profit factor, Sharpe/Sortino, max DD, OOS stability. Winrate descriptive
    only; remove it from stage-gates (F-006).
GATE: any change's OOS effect on expectancy/DD is stateable + reproducible, else STOP.

### PHASE 2 — COLLAPSE THE DECISION CORE (central fix)
2.1 Replace the 3-layer gauntlet with ONE scoring function (generalize debate/fallback):
    score = base + Σ(components) − Σ(risk penalties); decision = threshold; every term logged.
2.2 Count regime ONCE (single signed term). Delete duplicate regime penalties in scoring/sentiment/
    btc-lead/debate/SL.
2.3 ONE threshold, one logged source. Delete the GA/bucket/bayes/ceiling tower.
2.4 Replace SL monolith with composable exit rules (reuse frontier ExitDecision.fold).
2.5 Delete deadlock detectors — unneeded at ~5 gates; the ledger shows any starvation cause.

### PHASE 3 — ABLATION (keep/cut on EVIDENCE)
3.1 Baseline = collapsed core with only OFI + regime.
3.2 Add ONE component at a time; keep only positive marginal OOS contribution (purged CV / deflated
    Sharpe to avoid overfit false-positives).
3.3 Everything failing ablation → delete. Expect most of the 50 features + most of ml/ to be cut. That
    is success.

### PHASE 4 — VALIDATE BEFORE LIVE
4.1 Pre-register success bar (e.g. PF>1.3, maxDD<X%) over N weeks paper-forward on UNSEEN data.
4.2 Only on pass do leverage/deployment rise + live engage. Re-admit any self-mod loop only after it
    passes its own ablation, one at a time.

## ────────────────────────────────────────────────────────────────────────────
## OPEN QUESTIONS FOR OWNER
## ────────────────────────────────────────────────────────────────────────────
- Q1: Approve Phase 0 freeze + leverage cut now (reversible, high-safety)?
- Q2: Rebuild in-place on a branch, or build the collapsed core beside the old one and A/B in paper?
- Q3: Any feature you have direct evidence is profitable that must be protected from ablation?
  (I'll still demand the evidence — but I want your priors on record.)
