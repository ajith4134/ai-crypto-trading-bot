# Signal Monitor Replay Pool + Adaptive Threshold + Bandit Slot Selector + Utility Calibration

**Status:** APPROVED — implementation in progress (cont. 63)
**User decision:** 2026-05-29 — implement Phases 1+2+3+4 (skip RPE-PER & FDR-Control bandit)
**Research date:** 2026-05-29 (16 web searches; 17 sources cited at bottom)

---

## ✅ IMPLEMENTED — 2026-06-02 (cont. 69s) — see PROGRESS.md cont. 69s

- **CF pipeline**: throughput+correctness redesign. Backlog **202,881 → 3**; bulk-retires
  unevaluable stale rows; evaluates only the matured [72h,84h] band every 15 min ×3000;
  maturity guard stops late rows poisoning bayes. (celery_app.py, memory/write.py)
- **P4 util_calib**: FIXED the dead `trades.signal_id` join (→ `signals.trade_id=trades.id`).
  0 → 60,309 rows; now emits a recommendation (T=55, advisory 50/50 blend). (utility_calibration.py)
- **btc_dump gate**: now strength-bypass (≥60) + regime-widened — no longer kills str-89
  bull longs on BTC noise. (engine.py) `debate_skip_risk` made replay-recoverable.
- **P1 replay**: still consumer-starved under full_deploy (unchanged) — needs the slot-carve
  decision. P3 bandit still off-by-default.

---

## ✅ RE-VERIFIED — 2026-06-05 (Rule-2 + Rule-9 live audit)

Supersedes the CF-pipeline row in the 2026-06-02 table below (that table was written
just BEFORE this file's own 2026-06-02 fix and never updated — it is now stale).

- **CF pipeline: HEALTHY.** Real mature backlog = **0** (`would_have_won IS NULL AND
  created_at < now()-84h` = 0). The 201,138 rows that look "unevaluated" are all inside
  the 72–84h maturity window (oldest NULL = 62h old) and will evaluate on schedule.
  Fresh flow is fully caught up: 28,196 created in last 24h = 28,196 evaluated. The
  June-2 redesign (bulk-retire stale + [72h,84h] matured-band sweep) is working.
  *Rule-9 note: an interim read of "201k backlog never drains" was disconfirmed by the
  age-split query — it was immature-by-design, not a backlog.*
- **F9 fed with fresh CF data** → `brain:filter_overrides` actively written (bull bands).
- **P4 util_calib**: now emits a recommendation (per the IMPLEMENTED block above, T=55).
- **P1 replay pool**: ✅ RESOLVED 2026-06-05 (cont. 70d) — see below.

---

## ✅ P1 REPLAY RESOLVED — 2026-06-05 (cont. 70d) — replay→launch-pad integration

**Real root cause (reconciled, Rule 9):** `consume_count=0` was NOT full-deploy slot
starvation. With `launchpad:enabled=1` the engine HARD-BYPASSES the legacy replay
consumer (`engine.py:1783` `[] if _lp_mode`) — the launch-pad funnel is the sole
opener (D1). The producer kept writing (`produce_count` 5446→10330) into a pool nothing
read.

**Fix (owner design, 2026-06-05):** the launch-pad maintainer now stages recoverable
replay-pool signals as **ADDITIVE extra slots** (id ≥ `store.REPLAY_SLOT_BASE`=1001) on
top of the base depth — "the table grows to 11, 12, …". They go through the SAME qualify
gate and the SAME engine funnel (`gate.funnel_pairs` reads every mirror slot), so the
funnel-only-opens invariant (D1) holds. Tagged `source='replay'` + `replay_reason` +
`replay_strength` **permanently** (survives into `launch_pad_history` + `trade_id`→trades
for the reliability study). On fire → `replay_pool.mark_consumed()` (consume_count finally
moves) + history row; on TTL → DELETE (never left empty, never refilled by the base flow).

Files: `store.py` (create_replay_slot/delete_slot/source cols/clear delete-branch),
`maintainer.py` (`_sync_replay_slots`), `gate.py` (replay consume), `redis_keys.py`
(LAUNCHPAD_REPLAY_* — bypassed at runtime via local consts since redis_keys is baked in
the image). DB: `source`/`replay_reason`/`replay_strength` on launch_pad + _history.

Kill switch: `launchpad:replay_slots_enabled` (default 0), cap `launchpad:replay_max_slots`
(default 5). Enabled on paper 2026-06-05. **Verified end-to-end:** controlled inject of 6
active non-buffer pairs → 5 staged into slots 1001-1005 (cap respected), one qualified
green + visible to funnel, no symbol dup vs base buffer, base maintainer unaffected,
history insert with new cols OK. Synthetic test slots cleaned afterward.

**Honest limitation (Rule 4):** under launchpad-only mode the pool is fed almost entirely
by pairs ALREADY in the base buffer (only buffer pairs reach the reject path), which are
dedup-skipped — so ORGANIC staging is modest, happening mainly when a recoverable pair is
displaced/expired out of its base slot and gets a second chance within the 15-min TTL.
Higher yield when launchpad is OFF (legacy flow feeds the pool from the full scan; the
engine's own consumer then handles it — my guard prevents double-consume).

---

## ⚑ VERIFIED STATUS — 2026-06-02 (cont. 69s, Rule 2 audit)  *(CF row superseded — see 2026-06-05 above)*

Live-state audit (Redis + Postgres + engine.py/celery_app.py read). What actually
contributes to the open-trade accept/reject decision TODAY:

| Part | Built | Wired into accept path | Live verdict |
|------|-------|------------------------|--------------|
| **P2 Bayes adaptive threshold** | ✅ | ✅ `engine.py:1273 get_adaptive_min_strength` | **WORKING.** `t_high=25.0` (clip floor), refreshed 4 min ago (5-min beat runs). Today nothing above str **24.9** is rejected `signal_too_weak` (29 rejects, max_str 24.9). This is the ONLY phase contributing. |
| **P1 Replay pool** | producer ✅ / consumer ✅ wired (`engine.py:1588`) | ❌ effectively **inert** | `produce_count=5446`, **`consume_count=0`**, pool size 10. `mark_consumed()` only fires when a replay is re-opened as a trade; under `bot:full_deploy_mode=1` all slots are always full → consumer loop never has room → entries age out (900s TTL) before consumption. Design assumes free slots; full-deploy removes them. |
| **P3 Bandit slot selector** | module ✅ | ❌ | `bandit:enabled` unset → off-by-default. Never engaged. |
| **P4 Util calibration** | module ✅, beat scheduled 03:15 | ❌ | **All `util_calib:*` keys EMPTY** — never produced a `recommended_t_high`. Either erroring or starved of fresh CF data. NEEDS diagnosis. |
| **CF evaluation pipeline** (feeds P2 + shadow + F9) | ✅ runs | degraded | **202,881 rejected signals pending, CF stream ~12 days behind** (newest CF-evaluated signal generated 21 May). Root cause: `sweep_pending_counterfactuals` = hourly × `LIMIT 500` = **12k/day**, but ~28k rejected signals/day. Backlog grows ~16k/day. Adaptive threshold + shadow win-rate train on 12-day-stale data. |

**Rule-9 reframe:** the dashboard "Recent Missed Opportunities" panel (str~41 `signal_too_weak`
longs, peaks +5..+19%) is **STALE — dated 24 May**, BEFORE bayes `t_high` collapsed to 25. That
exact miss is already cured. The LIVE high-strength over-rejection has MOVED to other gates
(today, 6h): `debate_skip_risk` n=88 max_str 46.6, `btc_dump_*_blocks_long` n=43 max_str 89.4.
Those — not `signal_too_weak` — are where current regret lives.

**Fix priority (verified, this session):**
1. **CF throughput** (data spine; unblocks P2/P4/F9 quality): raise sweep `LIMIT`→~3000 + run
   every 10-15 min so drain rate > inflow. Low risk (beat-schedule/LIMIT only). Watch LLM/DB load.
2. **P4 util_calib**: diagnose empty output (check celery log for `util_calib_refresh` error).
3. **P1 replay under full-deploy**: decide — carve 1 reserved slot for replays, or accept inert.
4. Current regret is on `debate_skip_risk` / `btc_dump` gates, not `signal_too_weak` — retarget.

---

## Problem (verbatim from dashboard signal monitor 2026-05-29)

| Pair    | Dir  | Counterfactual peak | Decoded reason |
|---------|------|---------------------|----------------|
| HOMEUSDT| LONG | +4.87 %             | signal_too_weak @ strength 42.74 (just below threshold 50) |
| LUMIAUSDT| LONG| +4.78 %             | weak signal strength |
| NEARUSDT| LONG | +46.82 %            | weak signal strength |
| ONDOUSDT| LONG | +4.96 %             | low strength |
| UBUSDT  | LONG | +14.45 %            | strength 49.15 — overly-rigid cutoff at 50, regime=bull, peak_loss=0 |

**Pattern**: bot rejects near-threshold strong-evidence longs in bull regime; miss-decoder produces a "would-have-won" report but no actuation. We have the data; we need to *use* it.

---

## Phase 1 — Hysteresis Replay Pool

**Module:** new `signals/replay_pool.py` + producer/consumer hooks in `signals/engine.py`.

**Producer** (in `process_signals` after rejection):
- If `signal_strength ≥ T_low` AND `rejection_reason ∈ {signal_too_weak, memrl_low_winrate, marl_minute_hold, marl_minute_skip, conformal_uncertain, btc_recent_*, sentiment_*}` (recoverable reasons) push JSON to `signals:replay_pool` sorted-set, score=push_ts.
- Hard-rejection reasons (`pair_suspended_blocked`, `bot_confidence_below_floor`, `turbulence_too_high`, `regime_not_in_strategy_whitelist`, `hmm_regime_gate_overlay`) are **not** replayable.
- ZADD followed by ZREMRANGEBYRANK 0 -101 to LRU-cap at 100 entries.

**Consumer** (at top of `process_signals` loop, before fresh-signal generation):
- Loop until `len(get_open_trades()) >= max_open` or replay pool empty:
  1. ZRANGEBYSCORE of fresh-enough entries (`now - 900s`)
  2. For each candidate, validate: `|mark_now - mark_then| / mark_then ≤ 0.02`, regime unchanged, pair still in active scanner pool
  3. Re-score: re-run cascade direction check + read current strength after F45 / cluster modulation
  4. If re-scored strength ≥ `T_high` (adaptive from Phase 2) → hand to standard accept path, then open if approved
  5. ZREM the consumed/stale entry, increment counter

**Stale rules** (production-validated, Florinelchis 2026):
- Age > 900 s → drop, `signals:replay:stale_age_count`++
- Price drift > 2 % → drop, `signals:replay:stale_drift_count`++
- Regime changed since push → drop, `signals:replay:stale_regime_count`++
- Pair removed from scanner → drop, `signals:replay:stale_pair_count`++

**Kill switch:** `signals:replay_pool:enabled` ("1" default; "0" disables both producer and consumer).

---

## Phase 2 — Bayesian Beta-Distribution Adaptive Threshold

**Module:** new `signals/bayes_threshold.py`.

**Buckets**: strength bands `[15,25), [25,35), [35,45), [45,55), [55,65), [65,75), [75,100]`.

**Update path** (called from `track_counterfactual` at end of counterfactual evaluation):
- Find bucket b containing the rejected signal's `signal_strength`.
- `bayes_threshold:bucket:{b}:alpha` += 1 if `would_have_won`, else `bayes_threshold:bucket:{b}:beta` += 1.

**Update path** (called from `engine.close_trade` for accepted trades):
- Same bucket assignment, alpha/beta from realised PnL > 0.

**Refresh task** (Celery beat, every 5 min, `bayes_threshold_refresh`):
- For each bucket, compute posterior mean `α/(α+β)` and lower 5%-credible-bound via `scipy.stats.beta.ppf(0.05, α, β)`.
- `T_high` = lowest bucket-lower-bound such that posterior P(win) ≥ max(0.55, current accepted bucket's P(win) - 0.05). Clipped to `[25.0, 60.0]`.
- `T_low` = `T_high - 10.0`, clipped to `[15.0, T_high - 5]`. This is the floor for Phase 1's replay pool admission.
- Write `bayes_threshold:t_high`, `bayes_threshold:t_low`, `bayes_threshold:last_refresh_ts`.
- Anchor prior toward `util_calib:recommended_t_high` (Phase 4) when present: blend 0.5/0.5 with Bayesian posterior.

**Consumer** in `accept_or_reject` (`signals/engine.py:809-871`):
- AFTER existing GA + overrides resolve `min_strength`:
- If `bayes_threshold:enabled == "1"` and `bayes_threshold:t_high` present (and refresh < 2 h old), override `min_strength = bayes_T_high`. Bayesian wins over GA when fresh; GA used as cold-start.
- Log `bayes_threshold_applied` once per signal.

**Kill switch:** `bayes_threshold:enabled` ("1" default).
**Cold-start floor:** if any bucket has `α+β < 30` total trades, do NOT update T_high from that bucket — wait for sample.

---

## Phase 3 — Position-Based Multi-Play Thompson Sampling Slot Selector

**Module:** new `signals/slot_selector.py`.

**Posterior storage**: Beta per `(pair, regime)`:
- `bandit:pair_regime:{pair}:{regime}:alpha`, `:beta` updated at trade close from PnL > 0.

**Selection** (replaces FCFS pop in Phase 1 consumer):
- Build candidate set = (current cycle's accepted live signals) ∪ (replay-pool entries that pass stale checks AND re-scored strength ≥ T_high).
- For each, sample `θ_i ~ Beta(α, β)` for `(pair, regime)`. Expected R-multiple proxy = `θ_i` (win-rate posterior — kept simple since payoff ratio is bot-wide via Kelly).
- Sort by θ DESC, take top-`(max_open - n_open)` for this cycle.
- Tie-break by signal_strength.

**Cold-start:** `(pair, regime)` with `α+β < 10` defaults to `θ = signal_strength / 100`.

**Kill switch:** `bandit:enabled` ("1" default). When off, falls back to FCFS (current behaviour after Phase 1).

---

## Phase 4 — Utility-Weighted Walk-Forward Calibration

**Module:** new `signals/utility_calibration.py` + Celery beat task.

**Schedule:** nightly 03:15 UTC (`util_calib_refresh`).

**Algorithm**:
1. Pull last 30 days of: accepted closed trades (`trades` table — net_pnl_usdt, fees_usdt, signal_strength), rejected signals with counterfactuals (`signals` + `counterfactuals` — peak_profit_pct, peak_loss_pct, would_have_won, signal_strength).
2. For each candidate threshold `T ∈ {25, 28, 30, 32, 35, 38, 40, 42, 45, 48, 50, 55}`:
   - Simulated decision: accept if `strength ≥ T`.
   - Estimated PnL contribution:
     - Originally-accepted trade kept: realised net_pnl_usdt.
     - Originally-accepted trade dropped: 0 (no opportunity cost — capital redeployed).
     - Originally-rejected signal admitted: `0.5 × peak_profit_pct × estimated_capital × leverage / 100 - fees_estimate` (0.5 = trailing SL capture fraction, empirical).
   - Compute total utility `U(T) = Σ contribution × confidence_adjustment`.
3. Recommended `T*` = argmax U(T). Walk-forward validate: split 30d into 4 weekly folds, T* must be optimal in ≥ 3/4 folds OR within 10% utility of fold-optimal.
4. Write `util_calib:recommended_t_high = T*`, `util_calib:last_run_ts`, `util_calib:utility_delta_vs_current`.

**Phase 2 consumes** `util_calib:recommended_t_high` as a Bayesian-prior anchor (blend 0.5/0.5 with posterior).

**Kill switch:** `util_calib:enabled` ("1" default).

---

## Redis keys added (additions to `redis_keys.py`)

```python
# Phase 1 — Replay Pool (cont. 63)
REPLAY_POOL                = "signals:replay_pool"
REPLAY_POOL_ENABLED        = "signals:replay_pool:enabled"
REPLAY_POOL_TTL_SECONDS    = "signals:replay:ttl_seconds"
REPLAY_POOL_DRIFT_PCT_MAX  = "signals:replay:drift_pct_max"
REPLAY_PRODUCE_COUNT       = "signals:replay:produce_count"
REPLAY_CONSUME_COUNT       = "signals:replay:consume_count"
REPLAY_STALE_AGE_COUNT     = "signals:replay:stale_age_count"
REPLAY_STALE_DRIFT_COUNT   = "signals:replay:stale_drift_count"
REPLAY_STALE_REGIME_COUNT  = "signals:replay:stale_regime_count"
REPLAY_STALE_PAIR_COUNT    = "signals:replay:stale_pair_count"

# Phase 2 — Bayesian Threshold (cont. 63)
BAYES_THRESHOLD_ENABLED    = "bayes_threshold:enabled"
BAYES_THRESHOLD_T_HIGH     = "bayes_threshold:t_high"
BAYES_THRESHOLD_T_LOW      = "bayes_threshold:t_low"
BAYES_THRESHOLD_REFRESH_TS = "bayes_threshold:last_refresh_ts"
BAYES_BUCKET_ALPHA         = "bayes_threshold:bucket:{lo}_{hi}:alpha"
BAYES_BUCKET_BETA          = "bayes_threshold:bucket:{lo}_{hi}:beta"

# Phase 3 — Bandit slot selector (cont. 63)
BANDIT_ENABLED             = "bandit:enabled"
BANDIT_PAIR_REGIME_ALPHA   = "bandit:pair_regime:{pair}:{regime}:alpha"
BANDIT_PAIR_REGIME_BETA    = "bandit:pair_regime:{pair}:{regime}:beta"

# Phase 4 — Utility calibration (cont. 63)
UTIL_CALIB_ENABLED         = "util_calib:enabled"
UTIL_CALIB_T_HIGH          = "util_calib:recommended_t_high"
UTIL_CALIB_RUN_TS          = "util_calib:last_run_ts"
UTIL_CALIB_UTILITY_DELTA   = "util_calib:utility_delta_vs_current"
```

---

## Code touch list

| File | Change | Lines |
|------|--------|-------|
| `signals/replay_pool.py` | NEW — push/consume/stale-prune | ~180 |
| `signals/bayes_threshold.py` | NEW — Beta posterior + T_high/T_low computation | ~140 |
| `signals/slot_selector.py` | NEW — Thompson sampling slot picker | ~120 |
| `signals/utility_calibration.py` | NEW — walk-forward T calibrator | ~180 |
| `signals/engine.py` | Producer (after every recoverable reject) + consumer (top of process_signals) + bayes T_high consume in accept_or_reject | ~120 |
| `celery_app.py` | Register 3 beat tasks (bayes refresh 5 min, util_calib nightly 03:15, replay_pool_prune 1 min) | ~40 |
| `celery_app.py:track_counterfactual` | Call bayes_threshold.record_outcome at end | ~6 |
| `execution/paper.py` + `execution/live.py` close-paths | Call bandit.record_outcome + bayes_threshold.record_outcome from close_trade | ~15 |
| `redis_keys.py` | Add new key constants | ~25 |
| `dashboard/api.py` | New endpoint `/signals/replay_pool` + `/signals/threshold_state` | ~40 |
| `PROGRESS.md` | cont. 63 entry | ~80 |
| `Dockerfile`/rebuild | scipy already in image (used by stats elsewhere) | 0 |

Total ~1100 lines new code.

---

## Success criteria (verify after deploy)

1. Within 30 min of deploy: `signals:replay:produce_count > 0` AND at least one `replay_pool_consumed` log line.
2. Within 4 h: `bayes_threshold:t_high` is set and within `[25, 60]`.
3. Within 24 h: at least one trade opened with `entry_source=replay_pool` tag.
4. Within 7 days: dashboard miss-rate (`(would_have_won and not replayed) / total_rejects`) drops by ≥ 25 % compared to last 7-day baseline.
5. No deadlock: `replay_consume_count / replay_produce_count < 0.9` (we MUST be dropping some — if 100 % consumed we lost the cap).

---

## Backward-compat / blast-radius

- Every new system has a `*_enabled` Redis flag; flip to "0" to disable instantly without restart.
- All new modules wrap in try/except at consumer call site — failure ≡ no-op, never blocks trade flow.
- Phase 3 disabled by default (`bandit:enabled = "0"`) — needs ≥ 10 trades per (pair, regime) before useful, ship enabled-by-flag-only.

---

## Active Rule Compliance for THIS design file

- **Rule 1**: cont. 63 entry will be appended to PROGRESS.md after implementation, not now.
- **Rule 2**: read `signals/engine.py:807-2122`, `celery_app.py:2043-2112`, `dashboard/api.py:1048-1073`, `redis_keys.py:1-50` before writing. All claimed file/line refs verified.
- **Rule 3**: blueprint section 15.6 mandates "Recent Missed Opportunities panel element" (existing) — this work *actuates* it. No blueprint conflict.
- **Rule 4**: all four phases ship production-grade (kill switches, cold-start floors, walk-forward validation). If any phase is simplified vs design during implementation, this file will be amended honestly.
- **Rule 5**: blueprint untouched; this is additive.
- **Rule 6**: this file is Rule 6.
- **Rule 7**: Opus 4.7 is correct for this non-trivial code creation.

---

## Sources

- [Production Trading Bots: 15 Failure Patterns (Florinelchis 2026)](https://florinelchis.medium.com/production-trading-bots-15-failure-patterns-nobody-warns-you-about-af917d263c35)
- [Utility-Weighted Forecasting and Calibration (arXiv 2601.07852)](https://arxiv.org/abs/2601.07852)
- [Position-Based Multiple-Play Bandits with Thompson Sampling (arXiv 2009.13181)](https://arxiv.org/abs/2009.13181)
- [Learning Thresholds with Latent Values and Censored Feedback (arXiv 2312.04653)](https://arxiv.org/abs/2312.04653)
- [Bayesian Kelly self-learning algorithm (Medium)](https://medium.com/@jlevi.nyc/bayesian-kelly-a-self-learning-algorithm-for-power-trading-2e4d7bf8dad6)
- [Determining Optimal Stop-Loss Thresholds via Bayesian Drawdown (arXiv 1609.00869)](https://arxiv.org/abs/1609.00869)
- [Bayesian Statistics in Finance (IBKR)](https://www.interactivebrokers.com/campus/ibkr-quant-news/bayesian-statistics-in-finance-a-traders-guide-to-smarter-decisions/)
- [Deadband Hysteresis Filter (TradingView BackQuant)](https://www.tradingview.com/script/TPvNyPwv-Deadband-Hysteresis-Filter-BackQuant/)
- [Walk-Forward Validation for Microstructure Signals (arXiv 2512.12924)](https://arxiv.org/abs/2512.12924)
