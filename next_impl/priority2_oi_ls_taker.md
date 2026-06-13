# Priority 2 — Open Interest + Long/Short + Taker Buy/Sell Ratio

Topic slug: `priority2_oi_ls_taker`
Opened: 2026-06-07 (cont. 74). Continues the 2026-06-06 audit's "5 Implementation
Priorities", item 2. Priority 1 (dead features) = DONE cont. 73 (4/5 revived).

## Why (audit + blueprint, Rule 3 verified)
The strongest crypto-native signals the bot has ZERO of:
- **OI change rate** — BOT_BLUEPRINT.md:2706 "Rising OI + rising price = long strength;
  rising OI + falling price = short strength". The audit's **Gate 3**.
- **Long/Short ratio** — crowding. >70-75% one side → squeeze risk (mean-reversion,
  audit Block 14). The audit's **Gate 4**.
- **Taker buy/sell ratio** — who is the aggressor. Strong 5m momentum signal.

Blueprint:638 confirms OI/funding are "Binance API (already in stack) ✅ Free".
All 3 are free, unauth `fapi.binance.com/futures/data/*`, low weight, no model needed
to START producing.

## Confirmed state (Rule 2/9/13 — read at source)
- `exchange/client.py:297 get_open_interest()` exists but is **point-in-time** and only
  used by `scanner/main.py:430` for the composite rank — NOT a change-rate feature.
- `ml/criteria_weights.py` has an `open_interest_score` sub-score (scanner composite),
  also point-in-time. NOT OI velocity, NOT LS, NOT taker.
- NO producer writes OI-change / LS / taker as Redis features. NO FEATURE_COLUMNS entry.
- `coinglass_liq_refresh_task` (celery_app.py:3886) uses Coinglass API, NOT Binance
  `/futures/data` → **no duplication**, no shared weight.
- python-binance Client HAS: `futures_open_interest_hist`, `futures_global_longshort_ratio`,
  `futures_top_longshort_position_ratio`, `futures_taker_longshort_ratio` (verified live).

## Design — mirror `data/onchain_netflow.py` (proven producer template)
New producer `data/oi_ls_taker.py`:
- `is_disabled()` deadlock kill-switch (`oils:disabled`, 80% reject over 50+ calls),
  `_bump_call`/`_bump_reject(reason)` (Rule 12 silent-failure counters: `oils:reject:{r}`).
- `refresh_one(pair)` → 3 Binance `/futures/data` pulls (period=5m, limit=30 for z-history):
  1. OI hist → `oi_now`, `oi_change_5m` (pct), `oi_change_z` (z over last 30), and
     `oi_price_div` = sign(Δoi)·sign(Δprice) ∈ {-1,0,+1} (the blueprint Gate-3 sign).
  2. global + top LS ratio → `ls_global_ratio`, `ls_top_ratio`, `ls_crowd_z`
     (z of top-trader ratio vs its own 30-sample history; + = crowded long).
  3. taker buy/sell → `taker_ratio` (buyVol/sellVol), `taker_ratio_z`.
- `refresh_all()` celery-beat entrypoint, top-N active pairs (`oils:max_pairs`, default 50,
  Redis-tunable) — WEIGHT-BOUNDED (3 calls × N every 5 min; reuse launch-pad/scanner pair
  list so we cover what we actually trade, not all 200+).
- `get_oi_bonus(pair, direction)` + `get_ls_bonus(pair, direction)` → small ±strength nudge
  for the engine (Step C), cold-start-safe (missing key / disabled → 0).

Redis keys (all `{pair}:`, str floats, TTL 600s; new block in `redis_keys.py`):
`oi_now, oi_change_5m, oi_change_z, oi_price_div, ls_global_ratio, ls_top_ratio,
ls_crowd_z, taker_ratio, taker_ratio_z`. Health: `oils:{disabled,call_count,reject_count,
updated_at}`.

Client: add 3 thin wrappers to `exchange/client.py` WITH `_track_weight()` (Rule 12 — keep
production fapi weight accounted; the bot is weight-sensitive post cont.69x micro-ws work):
`get_open_interest_hist`, `get_longshort_ratio`, `get_taker_ratio`.

## STAGED rollout (each stage independently reversible, paper mode)

### Step A — PRODUCER ONLY (additive, ZERO behaviour change) ← START HERE
New `data/oi_ls_taker.py` + 3 client wrappers + `redis_keys.py` block + celery beat
(`oi-ls-taker-refresh`, every 5 min, default queue) + docker-compose bind-mount on
`celery_worker`. Populates the 9 Redis keys. Does NOT touch FEATURE_COLUMNS, does NOT
change any trade decision. Verify keys non-zero on majors. **Safe, no Rule 14 needed.**

### Step B — FEATURE WIRING (model shape change → Rule 14 SHADOW) [APPROVAL]
Append 4 cols to `prediction/features.py` FEATURE_COLUMNS: `oi_change_z, oi_price_div,
ls_crowd_z, taker_ratio_z`. 0-fill historically (no historical reconstruction — SAME
pattern as cvd_z/ofi_l1/ofi_accel, features.py:78-88); online river learner gets them
live via `live_features`. **Forces XGB retrain** (vector shape changes; note: gate already
OFF pending a 32→35 retrain per PROGRESS — fold these in there). Rule 14: 50 shadow cycles
before trusting PnL deltas.

### Step C — ENGINE GATES (Gate 3 + Gate 4, live behaviour) [APPROVAL]
Wire `get_oi_bonus`/`get_ls_bonus` into `signals/engine.py accept_or_reject` as SOFT,
Redis-toggleable strength nudges (mirror the cont.70b structural session penalty + netflow
get_bonus pattern — gate-not-kill, deadlock-safe). Default conservative magnitudes.
Rule 14 shadow + `oils:*_gate_enabled` toggles.

## Verify (Rule 4/12)
- Step A: `redis-cli get BTCUSDT:oi_change_z / ls_crowd_z / taker_ratio_z` non-zero on
  majors; `oils:call_count` rising, `oils:reject_count`≈0, `is_disabled`=False.
- Every reject path emits a counter (Rule 12). Producer logs once per cycle.

## Deploy
All bind-mounted (data/, prediction/, signals/, exchange/) → restart the relevant worker /
brain. No image rebuild (disk ~90%). Paper mode → reversible.

## Status (cont. 74)
- [x] **Step A — producer: BUILT + DEPLOYED + VERIFIED LIVE.** refresh_all 50/50 ok,
      0 failed, 5.71s; oils:call_count=200, reject_count=0, not disabled. 9 keys/pair
      non-zero+coherent (BTC oi_price_div=1, ETH=-1, SOL ls_global=3.44). Beat every 300s.
      DISCONFIRMED testnet risk (BINANCE_TESTNET=False, endpoints return live data).
      Deploy: bind-mounted data/oi_ls_taker.py + exchange/client.py + redis_keys.py into
      celery_worker (recreate) + restarted celery_beat (schedule reload). No rebuild.
- [x] **Step B — feature wiring: DONE + DEPLOYED + VERIFIED.** FEATURE_COLUMNS 35→39
      (oi_change_z, oi_price_div, ls_crowd_z, taker_ratio_z); inline-literal reads in
      live_features. Width-safe (xgb dormant-on-mismatch; online bundle self-resets).
      Live in brain live_features (non-zero/varied) + online learner. Folds into the
      pending XGB 32→retrain.
- [x] **Step C — engine gates: DONE + DEPLOYED + VERIFIED.** engine.py accept_or_reject
      folds get_oi_bonus (Gate 3) + get_ls_bonus (Gate 4); soft ±4/±6, toggleable
      (oils:oi_gate_enabled/oils:ls_gate_enabled default ON). Synthetic-pair test: OI
      +6/-6, LS -6/+6, toggle-off→0. Rule 14: watch ~50 paper cycles before live.

## ⚠ Cross-cutting blocker found (cont. 74) — SYSTEMIC CELERY BACKLOG
default=11k, predict_all=7k, microstructure=6k → beat-scheduled producers starve.
NETFLOW (Priority 1) is ~29h stale for this reason. FIXED for Priority 2 by routing
oi_ls_taker_refresh_task to the idle cn_train queue (+ mounts) — scheduled run now
5.5s, keys fresh. The broader backlog is pre-existing + bigger than P2 → recommend as
the NEXT priority (worker concurrency / purge default / re-home light producers).
