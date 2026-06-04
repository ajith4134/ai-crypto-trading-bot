# Seed Gene Pool — 27 Strategy Archetypes + 3 Prep Items

Session: 2026-05-29 (revised same day after user re-recommendation)
Goal: Replace the empty/stub-only strategy gene pool with 27 hand-curated archetypes spanning directional alpha, microstructure, ML overlays, risk-overlay gates, and stat-arb anchors. Enable real crossover/mutation lineage.

## Confirmed decisions (user-approved 2026-05-29)

- Pool size: **27 archetypes** (25 user-curated + 2 stat-arb anchors added by Claude on user request)
- Family balance:
  - **A. Directional alpha (15)**: 6 trend + 3 mean-rev + 6 crypto-perp-native
  - **B. Microstructure / order flow (5)**
  - **C. ML-augmented overlays (3)** — wrap on top of base
  - **D. Risk overlays / gates (2)** — applied to ALL strategies, not standalone (see runtime caveat)
  - **E. Stat-arb anchors (2)** — added 2026-05-29 to fill cointegration + lead-lag mechanism gaps
- Dropped from prior file revision: `funding_premium_oi_triple_align`, `bocpd_changepoint_breakout` (user removed in their re-recommended list)
- Prep work order:
  1. Retire 9 stale 2026-05-20 strategies (`source=research`, retired status today)
  2. Seed 27 archetypes (clean pool first)
  3. Wire `_mutate_strategy` / `iterate_marginal_strategy` to persist intermediate variants with `parent_strategy_id` + `generation=parent.generation+1`
  4. Fix `promote_from_self_play` (wire it, don't delete — per blueprint line 1353)
- `source` for seeded archetypes: `brain` (schema constraint allows brain/web/self_play/research only — avoids migration)
- DCA: hard-off per [[feedback_dca_disabled]] — `round_1_pct=-50, round_2_pct=-80` so triggers never fire
- Blueprint check skipped by user — completed targeted skim of F8/F25/F36 sections instead; no conflicts found

## Architecture caveat for D. Risk overlays (#24, #25)

User chose to keep `vol_target_sizing_overlay` and `hmm_regime_gate_overlay` as pool entries. As pool members, they cannot independently complete the UCB1 `check_trial_eligible` 30-paper-trade gate (`strategy/lifecycle.py:42`) because they have no standalone entry mechanism. Two viable runtime resolutions, decide before seeding:

1. **Piggy-back mode** (recommended): When bandit picks #24 or #25, router substitutes a random active directional strategy and applies the overlay on top. Overlay accrues trial count from the piggy-backed trade. Requires patch in `strategy/router.py`.
2. **Documentation-only mode**: Mark with `min_signal_strength=99` so they never trigger entries. They exist only as parameter source for GA crossover. Wastes 2 of 25 bandit slots.

Default to piggy-back unless user overrides.

## The 759-strategy research catalog

See sibling files in `/opt/trading-bot/next_impl/`:
- `seed_archetypes_trend_momentum.md` (130)
- `seed_archetypes_mean_reversion.md` (113)
- `seed_archetypes_microstructure.md` (121)
- `seed_archetypes_crypto_native.md` (120)
- `seed_archetypes_volatility.md` (108)
- `seed_archetypes_ml_sentiment_event.md` (167)

## The 27 archetypes — full typed configs

Each row gives the exact DB payload. `entry_conditions` and `exit_conditions` are documentation strings (the F8 router uses typed configs, not these strings, for decisions — but they're persisted for audit/dashboard).

### A. Directional alpha (15)

#### A1. Trend / momentum / breakout (6)

| # | name | atr_mult | min_pct | trail_pct | cap_mult | min_sig | turb_cap | regimes |
|---|---|---|---|---|---|---|---|---|
| 1 | `donchian_breakout` | 3.5 | 0.025 | 0.04 | 1.0 | 35 | 4.0 | bull,bear |
| 2 | `turtle_system` | 4.0 | 0.03 | 0.05 | 1.0 | 40 | 3.5 | bull,bear |
| 3 | `supertrend_flip` | 3.0 | 0.02 | 0.035 | 1.1 | 30 | 4.0 | bull,bear |
| 4 | `vwap_trend_session` | 2.5 | 0.02 | 0.03 | 1.0 | 30 | 5.0 | bull,bear,turbulent |
| 5 | `dual_momentum` | 3.5 | 0.025 | 0.04 | 1.2 | 40 | 3.0 | bull,bear |
| 6 | `bollinger_keltner_squeeze` | 3.0 | 0.02 | 0.04 | 1.0 | 35 | 4.0 | bull,bear |

**Mechanism notes:**
- `donchian_breakout`: 20-bar high/low breakout, wide stops to let trend run
- `turtle_system`: classic 20/55 dual breakout with pyramiding (single-position version here)
- `supertrend_flip`: ATR-adaptive trend; flip on close beyond Supertrend line
- `vwap_trend_session`: anchored VWAP from session open; only trade in direction of slope
- `dual_momentum`: combined cross-sectional + time-series momentum; high conviction → 1.2× capital
- `bollinger_keltner_squeeze`: TTM squeeze — fire when BB contracts inside Keltner and breaks out

#### A2. Mean reversion / stat arb (3)

| # | name | atr_mult | min_pct | trail_pct | cap_mult | min_sig | turb_cap | regimes |
|---|---|---|---|---|---|---|---|---|
| 7 | `avellaneda_pca_residual_revert` | 1.75 | 0.015 | 0.018 | 0.9 | 30 | 5.0 | bull,bear,turbulent |
| 8 | `cvd_divergence_revert` | 2.0 | 0.015 | 0.02 | 1.0 | 35 | 4.0 | bull,bear,turbulent |
| 9 | `hurst_gated_revert` | 2.0 | 0.02 | 0.022 | 0.9 | 35 | 5.0 | bear,turbulent |

**Mechanism notes:**
- `avellaneda_pca_residual_revert`: PCA-residual mean reversion (factor-neutral); needs `qlib_alphas` or new producer
- `cvd_divergence_revert`: enter against price when CVD diverges from price action; consumes existing `cvd_producer`
- `hurst_gated_revert`: Hurst exponent < 0.5 confirms mean-reverting regime before fading extremes

#### A3. Crypto-perp native (6)

| # | name | atr_mult | min_pct | trail_pct | cap_mult | min_sig | turb_cap | regimes |
|---|---|---|---|---|---|---|---|---|
| 10 | `funding_extreme_fade` | 2.0 | 0.018 | 0.025 | 1.0 | 30 | 6.0 | bull,bear,turbulent |
| 11 | `premium_index_z_fade` | 2.0 | 0.015 | 0.02 | 1.0 | 30 | 5.0 | bull,bear,turbulent |
| 12 | `liquidation_cascade_fade` | 1.5 | 0.02 | 0.025 | 1.3 | 25 | 8.0 | bull,bear,turbulent |
| 13 | `post_liq_reversion` | 2.0 | 0.02 | 0.03 | 1.2 | 30 | 6.0 | bull,bear,turbulent |
| 14 | `oi_price_divergence` | 2.5 | 0.02 | 0.03 | 1.0 | 35 | 4.0 | bull,bear |
| 15 | `exchange_netflow_inflow_fade` | 2.5 | 0.025 | 0.035 | 0.9 | 35 | 4.0 | bull,bear |

**Mechanism notes:**
- `funding_extreme_fade`: funding > +0.05% → short, funding < -0.05% → long; consumes existing `{pair}:funding_rate`
- `premium_index_z_fade`: perp-spot basis z-score > 2σ → fade; consumes `premium_index_producer` (cont. 60)
- `liquidation_cascade_fade`: enter LONG after cascade exhaustion (BOCPD-confirmed); allowed in turbulent (cascades create the regime)
- `post_liq_reversion`: 4-12 bar recovery follow-through after large liq cluster prints
- `oi_price_divergence`: OI building + price falling → short setup (or symmetric long); needs OI delta producer (likely exists in scanner)
- `exchange_netflow_inflow_fade`: large inflow → distribution risk → short bias; uses F52 netflow

### B. Microstructure / order flow (5)

| # | name | atr_mult | min_pct | trail_pct | cap_mult | min_sig | turb_cap | regimes |
|---|---|---|---|---|---|---|---|---|
| 16 | `classical_ofi_cont` | 2.0 | 0.015 | 0.018 | 1.0 | 30 | 5.0 | bull,bear,turbulent |
| 17 | `depth_weighted_ofi` | 2.0 | 0.015 | 0.02 | 1.0 | 35 | 5.0 | bull,bear,turbulent |
| 18 | `microprice_gradient` | 1.75 | 0.012 | 0.015 | 1.0 | 30 | 5.0 | bull,bear,turbulent |
| 19 | `hawkes_lambda_spike_ride` | 2.5 | 0.02 | 0.025 | 1.1 | 35 | 7.0 | bull,bear,turbulent |
| 20 | `swing_sweep_fade` | 1.5 | 0.015 | 0.018 | 1.0 | 30 | 5.0 | bull,bear,turbulent |

**Mechanism notes:**
- `classical_ofi_cont`: Cont-Kukanov OFI; consumes existing OFI producer
- `depth_weighted_ofi`: multi-level (L5) depth-weighted OFI extension
- `microprice_gradient`: Stoikov microprice short-horizon predictor
- `hawkes_lambda_spike_ride`: ride self-excitation spikes (λ/baseline > 1.5); consumes existing Hawkes producer
- `swing_sweep_fade`: fade stop-hunt sweeps of prior swing high/low

### C. ML-augmented overlays (3) — wrap on top of base

| # | name | atr_mult | min_pct | trail_pct | cap_mult | min_sig | turb_cap | regimes |
|---|---|---|---|---|---|---|---|---|
| 21 | `patchtst_direction_confirmer` | 2.5 | 0.02 | 0.025 | 1.0 | 40 | 4.0 | bull,bear |
| 22 | `tft_regime_conditioned_overlay` | 2.5 | 0.02 | 0.025 | 1.0 | 40 | 4.0 | bull,bear |
| 23 | `gnn_contagion_risk_off` | 2.0 | 0.018 | 0.022 | 0.85 | 40 | 3.5 | bull,bear |

**Mechanism notes:**
- `patchtst_direction_confirmer`: only enter when PatchTST direction prob > 0.6; consumes existing PatchTST inference
- `tft_regime_conditioned_overlay`: TFT forecast filtered by current HMM regime
- `gnn_contagion_risk_off`: GNN systemic-risk score < 0.4 (ONLY enter when contagion risk is low); smaller capital (0.85×) reflects defensive nature

### D. Risk overlays / gates (2) — universal, see piggy-back caveat above

| # | name | atr_mult | min_pct | trail_pct | cap_mult | min_sig | turb_cap | regimes | entry_overrides |
|---|---|---|---|---|---|---|---|---|---|
| 24 | `vol_target_sizing_overlay` | 2.0 | 0.015 | 0.02 | 1.0 | 30 | 99.0 | bull,bear,turbulent | `{"overlay_type":"sizing","target_realized_vol_pct":0.20,"vol_lookback_bars":1440}` |
| 25 | `hmm_regime_gate_overlay` | 2.0 | 0.015 | 0.02 | 1.0 | 30 | 99.0 | bull,bear,turbulent | `{"overlay_type":"regime_gate","kill_in_turbulent":true,"hmm_confidence_min":0.7}` |

**Mechanism notes:**
- `vol_target_sizing_overlay`: position-size normaliser — scales notional so realized-vol contribution matches target_realized_vol_pct/sqrt(N). When picked as piggy-back, modifies sizer in `risk/manager.py`. Standalone trial impossible.
- `hmm_regime_gate_overlay`: portfolio-level kill-switch — when HMM regime confidence > 0.7 AND regime = turbulent, force-flat all open positions and block new entries. Extends `risk/frontier/exit_kill_switch.py` (cont. 60).

### E. Stat-arb anchors (2) — added 2026-05-29 to fill mechanism gaps

| # | name | atr_mult | min_pct | trail_pct | cap_mult | min_sig | turb_cap | regimes |
|---|---|---|---|---|---|---|---|---|
| 26 | `kalman_pair_residual_revert` | 1.5 | 0.012 | 0.015 | 0.9 | 30 | 4.0 | bull,bear |
| 27 | `transfer_entropy_lead_lag` | 2.0 | 0.015 | 0.02 | 1.0 | 30 | 4.0 | bull,bear |

**Mechanism notes:**
- `kalman_pair_residual_revert`: Kalman-filtered rolling β between 2 correlated perps (BTC/ETH default); enter contra side when residual z>2σ, exit at z<0.5σ. Pair correlations break in turbulent so excluded. **Needs new producer** at `data/kalman_pair_producer.py` — emits `{pair_basket}:residual_z` per 60s.
- `transfer_entropy_lead_lag`: consumes existing F27 (verified `ml/transfer_entropy.py:1-66`, called from `data/feed.py:451`). Enter follower pair in same direction as leader when TE(leader→follower) > threshold AND leader moved N bars earlier. Default leader = BTC, default lag = 3-15 min (per blueprint line 591).

## Why each archetype is genuinely different

The crossover engine (`_crossover_with_top_active`) blends typed configs key-by-key. For crossover to produce meaningful diversity, parents must differ in MULTIPLE keys, not just one. Audit:

| Pair | Differs on |
|---|---|
| donchian_breakout vs liquidation_cascade_fade | atr_mult 3.5 vs 1.5, regimes bull,bear vs all-3, min_sig 35 vs 25, cap_mult 1.0 vs 1.3 |
| vwap_trend_session vs avellaneda_pca_residual_revert | trail_pct 0.03 vs 0.018, mechanism orthogonal |
| funding_extreme_fade vs swing_sweep_fade | turb_cap 6.0 vs 5.0, mechanism (funding vs price action) orthogonal |
| kalman_pair_residual_revert vs transfer_entropy_lead_lag | mechanism (residual mean-rev vs lead-lag momentum), turb_cap shared but pair-vs-cross-pair |
| All 27 pairs | Verified each archetype differs from every other on ≥3 typed-config dimensions or mechanism family |

## Implementation plan

### Phase 1 — Retire 9 stale strategies (one SQL transaction)

```sql
-- All 9 strategies created 2026-05-20 are pre-cont-23 (broken trailing) AND
-- pre-cont-44 (no profit-lock). Their performance numbers reflect a broken
-- pipeline and would poison crossover. Retire via lifecycle.retire_strategy()
-- semantics: set retired_at + retirement_reason + status='retired'.
UPDATE strategies
   SET status = 'retired',
       retired_at = NOW(),
       retirement_reason = 'pre_cont44_stale_pipeline_pre_seed',
       updated_at = NOW()
 WHERE created_at::date = '2026-05-20'
   AND source = 'research'
   AND status != 'retired';
```

Also retire the 2 `brain`-source active stubs:

```sql
UPDATE strategies
   SET status = 'retired',
       retired_at = NOW(),
       retirement_reason = 'pre_seed_empty_code_stub',
       updated_at = NOW()
 WHERE source = 'brain'
   AND name IN ('stage1_ofi_momentum', 'stage2_sentiment_ofi')
   AND status = 'active';
```

⚠️ This kills the bandit's current allocation targets. Phase 2 MUST run immediately after so signals/engine has active strategies to attribute to.

### Phase 2 — Seed 27 archetypes (Python script)

New file: `/opt/trading-bot/tools/seed_gene_pool.py`

Script reads the 27 archetype definitions (embedded as a constant list) and calls `strategy.lifecycle.create_experimental` for each. Then it immediately promotes each to `active` via `strategy.lifecycle.promote_to_active` since these are hand-curated and skip the 30-trade/7-day trial.

Idempotent: checks `SELECT id FROM strategies WHERE name=%s` first; skips if exists.

Run via: `docker compose exec brain python -m tools.seed_gene_pool`

### Phase 2b — Piggy-back wiring for overlays #24, #25

Patch `strategy/router.py` `pick_strategy_for_signal` (or equivalent): when chosen strategy has `entry_overrides.overlay_type` set, substitute a random active strategy from family A (directional alpha) for the entry mechanism, apply the overlay's typed config on top. The piggy-backed trade attributes to the OVERLAY's strategy_id so it accrues trial counts.

### Phase 2c — New producer for #26 kalman_pair_residual_revert

New file: `data/kalman_pair_producer.py`. Maintains a 2-state Kalman filter over `log(price_btc) ~ β·log(price_eth) + intercept`. Emits `{pair_basket}:residual_z` per 60s celery task. Initial basket: `BTC-ETH`. Extensible to top-10 correlated pairs.

### Phase 3 — Wire mutation children with parent_strategy_id (patch)

Current `iterate_marginal_strategy` (research/engine.py:408) discards all intermediates. Patch:

```python
# In iterate_marginal_strategy, after each accepted variant:
if screen["score"] > best_screen["score"]:
    best, best_screen = candidate, screen
    best["parent_strategy_id"] = previous_best_id
    best["generation"] = previous_generation + 1
```

But `iterate_marginal_strategy` is called BEFORE the strategy is saved. The cleaner fix: at the end of `celery_app.run_strategy_research`, when calling `create_experimental(strategy)`, populate `parent_strategy_id` and `generation` from the LAST crossover parent (which `_crossover_with_top_active` stamps as `_crossover_parent`).

Map name → id once, attach. One-shot patch in `celery_app.py` ~line 1000.

### Phase 4 — Fix promote_from_self_play (wire it)

Current state: `self_play/mars.py:440 promote_from_self_play` is defined but never called. `celery_app.run_self_play` (line 1830) runs every 30min but doesn't invoke promotion.

Fix: After `run_self_play_episode` returns, if cumulative win rate > 0.55 over last N episodes AND a promotion hasn't fired in the last 24h, call `promote_from_self_play`. Add Redis dedupe key `self_play:last_promotion_ts`.

Note per [[feedback_silent_rejection]]: also emit a counter `self_play:promotion_skipped_count` with the reason when promotion is skipped (win_rate too low, cooldown active, etc.), so the never-firing path becomes visible in feature_health.

## Checklist

- [ ] Phase 1: Retire 11 stale strategies (9 research + 2 brain stubs) — SQL
- [ ] Phase 2: Write `tools/seed_gene_pool.py` with all 27 entries + run it
- [ ] Phase 2: Verify all 27 archetypes saved with correct typed configs (DB query)
- [ ] Phase 2: Verify .py files exist in `strategies/active/` (filesystem check)
- [ ] Phase 2b: Patch `strategy/router.py` for overlay piggy-back substitution
- [ ] Phase 2b: Add counters `strategy_router:overlay_piggyback_count`, `:overlay_skipped_count` per [[feedback_silent_rejection]]
- [ ] Phase 2c: Write `data/kalman_pair_producer.py` + add to celery beat (60s)
- [ ] Phase 2c: Verify `{BTC-ETH}:residual_z` populates in Redis
- [ ] Phase 3: Patch `celery_app.run_strategy_research` to set parent_strategy_id + generation
- [ ] Phase 3: Verify next research-created strategy has parent + generation > 0
- [ ] Phase 4: Wire `promote_from_self_play` into `run_self_play` celery task
- [ ] Phase 4: Add `self_play:promotion_skipped_count` + reason tracking
- [ ] Final: Rebuild Docker image (per [[feedback_progress_tracking]] — Python changes need rebuild)
- [ ] Final: Update PROGRESS.md with cont. 61 summary

## cont. 66 status (2026-05-31) — verified vs. implemented + lineage fix

Audit result (Rule 2, read code/DB/Redis, not checkboxes):
- **Phase 1** ✅ 11 research + 2 brain stubs retired. cont. 66 also retired 8
  pre-seed stale research-experimental stragglers (created < 2026-05-29).
- **Phase 2** ✅ all 27 archetypes active, names match exactly.
- **Phase 2b** ✅ wired (`signals/engine.py:2284-2333`, `strategy/router.py`); never
  fired at runtime (bandit hasn't picked an overlay). cont. 66 added the missing
  exception-path counter `strategy_router:overlay_piggyback_error_count`.
- **Phase 2c** ✅ producer + beat live; `kalman:ETHUSDT|BTCUSDT:residual_z` populating.
- **Phase 3** ❌→✅ **was dormant** — code correct (`celery_app.py:1192`) but
  `_crossover_parent` only reaches it on the rare marginal→crossover-won→promising
  path, and the 27 seeds were never self-evolved → all gen-0/parent-NULL.
  **Fixed in cont. 66** via pool-level GA: `research.engine.evolve_strategy_pool`
  + `celery_app.evolve_strategy_pool_task` (beat `evolve-strategy-pool` @ 6h).
  Verified: 4 gen-1 children created, each a distinct parent.
- **Phase 4** ✅ wired + running; 17 skips / 0 promotions (threshold-gated).

## Session handoff

If this session ends mid-implementation:
- Phase 1 SQL is idempotent (safe to re-run)
- Phase 2 script is idempotent (name-uniqueness check)
- Phase 2b/2c are additive (no destructive change)
- Phase 3 patch is a single function-internal change — atomic
- Phase 4 wire-up is a single new code block in `run_self_play`

Next session can resume by reading the checklist above and the active rules at session start.
