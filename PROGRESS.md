# PROGRESS — LIVE (recent sessions only)
#
# Full history (cont.1–~69, ~11.7k lines) archived 2026-06-07 → PROGRESS_ARCHIVE_2026-06-07.md
# This file is kept lean to stay UNDER the 256KB Read-tool limit (the old 925KB
# file could not be opened in one read). APPEND new sessions to the END of this file.
# To search full history: grep your term in PROGRESS_ARCHIVE_2026-06-07.md

# Bot Progress & Session Log

This file tracks every code change, what problem it fixed, and whether it worked.
Update this file each session BEFORE closing. If a fix did not work, note it here so
we don't try the same approach again.

---

## CONT. 72 — DEBATE COUNCIL: FIXED THE INVERTED VERDICT + CLOSED THE DEAD LOOP (2026-06-07)

User: "debate council never contributed — improve or replace." Implemented
next_impl/debate_council_upgrade.md cont.72 section. 3 files (all bind-mounted →
restart-only). Backups: debate/fallback.py.bak_cont72_*, memory/write.py.bak_cont72_*.

**Root cause (GROUND TRUTH, Rule 13) — the verdict was INVERTED, costing ~$1,734:**
verdict×realized-PnL JOIN over closed trades:
  full_allocation (FULL size)   3469 trades  -$0.500 avg  46.9% WR  ← LOSES
  reduced_allocation (70% size) 1174 trades  +$0.093 avg  54.6% WR  ← WINS
  exploratory (5% size)          530 trades  +$0.024 avg  39.2% WR
Win-rate gap is size-independent (Rule 9 disconfirm) → the scorer gave the MOST size
to the WORST trades. Cause: `deterministic_verdict` anchored `base = signal_strength`
then re-added regime/candlenet/ofi bonuses that Cont.71 had ALREADY baked into
signal_strength → `full` just meant "high signal_strength" = the crowded losers.
Also DEAD: `run_debate()` (LLM council) never called (orphaned since cont.69);
`debate:prior:*` empty (the async-debate that should fill it was never built →
fallback's learned-prior term was always 0); `debate:agent_weight:*` empty (no new
debate_arguments since 2026-06-02).

**The fix (3 files):**
1. **debate/fallback.py REWRITTEN** — base=50 NEUTRAL (not signal_strength). The score
   is now a pure ORTHOGONAL risk/crowding/outcome overlay using ONLY factors NOT in
   signal_strength: realized regime:dir prior (±15, PRIMARY), funding crowd (±8),
   volatility regime (vol_unit), xsmom tailwind (±4), bot-wide loss-streak (−12 cap),
   guarded cascade/vpin. Default (all-neutral) → reduced_allocation (the MEASURED
   winner). full_allocation now REQUIRES positive realized prior + aligned tailwinds;
   skip_risk needs strong negative. Thresholds full≥60 / reduced≥42 / exploratory≥30.
2. **debate/learning.py NEW** — `update_outcome_prior(regime,dir,pnl,capital)`: EWMA
   (α=0.05) of clip(net_pnl/capital × 50, −1,1) → `debate:prior:regime:{regime}:{dir}`;
   pushes win/loss to `debate:recent_outcomes` (LTRIM 20) for the loss-streak factor.
   Tracks avg-PnL MAGNITUDE (×50 scale) not win/loss sign — because bear:short is 54%
   WR but −$0.84 avg. Counters debate:prior_updates_count + debate:prior_last_ts.
3. **memory/write.py** — calls update_outcome_prior at every trade close (own flag
   `debate:prior_learning_enabled`, default on; independent of F37 governance).

**Bootstrapped** the 8 regime:dir priors from historical realized return-on-capital so
the fix works on the FIRST trade (not after EWMA warmup): unknown:long −14.8 pts,
bear:short −5.0, turbulent:short +5.3, bull:short +6.5, bull:long +3.9 (score points).

**Verified (Rule 4):** all 3 AST-clean; new modules import in the live brain; 8 sanity
cases pass — signal_strength=95 and default BOTH → reduced (inversion gone); unknown:long
→ exploratory; bull/turbulent:short favored; full_allocation + skip_risk both reachable
under aligned tailwinds / stacked risk-off. Brain restarted + healthy (the CancelledError
in logs is the old process's asyncio teardown, not new code).

**NOTE / next check:** Rule 14 — paper mode, but re-run the verdict×PnL JOIN after ~50+
new closes; the full_allocation cohort's avg PnL should rise toward/above reduced's. The
LLM council (run_debate, update_beliefs_on_close) is left as documented-orphan — not
revived (phi3 ~4 tok/s + the user's own cont.69 decision); the realized-outcome loop
replaces its intended contribution.

---

## CONT. 71 — SIGNAL-STRENGTH FORMULA REWRITE (research-backed) (2026-06-07)

Implemented next_impl/signal-strength-research-rewrite.md. Rewrote 8 of the per-source
scorers + weight table in signals/engine.py (generate_candidate_signals, ~lines 697–820).
Bind-mounted → restart-only. Backup: signals/engine.py.bak_cont71_20260607_101117.

**The 9 changes:**
1. OFI → rolling directional z-score (Cont 2023): EWMA-std `{pair}:ofi_abs_std_ewma`; 3σ
   aligned→95, on-mean→50, 3σ against→5 (was saturating magnitude×binary-align).
2. VPIN → centered toxicity multiplier (baseline→50, 2×→100, 0.5×→25); no longer
   direction-multiplied (was double-counting OFI's order-flow); weight 0.10→0.06.
3. **Regime (most critical) → empirical `_REGIME_SCORES` table.** bear+short HARD ZERO
   (was 20; -$3,507/4,173 trades); bull+short 60 (was 0; +$1,098 profitable);
   turbulent+short 55 / turbulent+long 25; bull+long 88. Per-cell Redis override
   `signals:regime_score:{regime}:{direction}` (+ legacy bear:short key still honored).
4. TFT → EWMA-std z-score (was abs×5000, saturated at 100 for any bias>0.02%).
5. PatchTST → continuous change_pct z-score (was discrete +5/-3 → 100 instantly);
   guarded `'change_pct' in dir()`; falls back to old bonus map on exception.
6. CandleNet → temperature-calibrated dir3 probability (Guo 2017, T=2.0); uses the real
   model probs instead of discrete vote→score; guarded `'_forecasts' in dir()`.
7. Sentiment → direction-aligned (long uses sentiment, short uses 1-sentiment); neutral
   0.5→50 both. Was abs(sent-0.5)×200 (scored bullish reading same on long OR short).
8. hist_acc → Beta-Binomial shrinkage toward 50% (k0=20); n=0→50, n=20@60%→55,
   n=200@60%→59. Reads the REAL source brain:directional_accuracy:{pair} JSON.
9. Weights rebalanced to base-sum 1.00: ofi .25, regime .20→.22, tft .15, candlenet .12,
   hist_acc .13→.08, patchtst .08, vpin .10→.06, sentiment .05→.04.

**Two deliberate deviations from the spec (Rule 2/4/9/12 forced):**
- Change 7: spec read `{pair}:dir_accuracy`/`{pair}:dir_count` which DO NOT EXIST
  (verified _safe_dir_accuracy reads `brain:directional_accuracy:{pair}` JSON, rate on a
  0-100 scale). Implementing verbatim would have pinned hist_score=50 forever (silent
  fail). Rewrote to the real key. Also dropped the spec's redundant `*credibility`
  multiply (it contradicted the spec's own worked examples; pure Beta posterior is correct).
- Change 9: spec's static weight table silently dropped the F12 metacognition actuator
  override (metacognition/actuator.py is a LIVE producer of ofi/regime/tft weight deltas).
  Preserved the override on the new base weights to avoid orphaning it (Rule 12).

Verified: py_compile OK; numeric sanity asserts pass (bear+short=0, sentiment/OFI/VPIN/
CandleNet/Bayesian all behave). NOT yet deployed — needs `docker-compose restart brain`.
Rule 14: paper mode, but watch 50 shadow cycles + bear+short trade count before judging PnL.

---

## CONT. 70g — SIGNAL-MONITOR METRIC FIXES (2026-06-05)

Audit found the Signal Monitor panel was overstating/mislabeling. Fixed all three
(dashboard now bind-mounted → api.py changes are restart-only; frontend npm-built):

**#1 Shadow Win Rate — was DRIFTED.** Panel read an incremental Redis counter
(`SHADOW_WIN_RATE`, 41.8% / 44912) that never reconciled to the table. Rewrote
`/signals/shadow_win_rate` to compute LIVE from the counterfactuals table →
**36.68% (34494/94036)**, the single source of truth. Added `optimistic`/`note`
metadata. Also fixed `pending_eval` to use an 84h window (72h maturity + slack) so
the by-design maturity lag isn't counted as a backlog (now 0).

**#2 Path-aware Missed Opportunities.** Stored `peak_profit_pct` is a point-in-time
move at the 72h checkpoint (no peak, no drawdown, no stop path). Added `_cf_path_metrics`
that walks the real 15m candle path over the 72h window to surface TRUE
`path_peak_pct` (MFE) + `path_dd_to_peak_pct` (drawdown endured to reach it) +
`path_mae_pct` (worst adverse). On-demand, Redis-cached 24h (`cf:path:{pair}:{ms}`),
fapi-ban-guarded, best-effort (never breaks the panel; only the ~10 shown rows, each
computed once). Live proof: HYPE chkpt +19.66% → true peak +21.6% / dd -0.2% (real
clean miss); SKYAI peak +9.3% but -35% MAE; JTO needed -6% dd for +7.7% (stop-killers).

**#3 Relabel + staleness fix (SignalMonitor.tsx).** Shadow Win Rate now tagged
"point-in-time, excl. stop-loss (optimistic)"; missed-opps show chkpt (optimistic)
beside true peak + drawdown; the "Newest sample 72h ago → stale" false alarm fixed
(72h is the by-design maturity edge; only warns >96h or real backlog).

NOT changed: the all-LONG miss pattern (model long under-conviction) is the kline-retrain
track, not a metric bug. The table's `would_have_won` itself is still point-in-time
(full path-aware recompute of all 94k rows would need hot-loop kline fetches → API-ban
risk; deferred). Bundle main.72a1ce2f.js. Deploy: restart dashboard + npm build.

---

## CONT. 70f — PRICE-MOVEMENT COLUMNS on scanner + launch tables (2026-06-05)

**Goal (owner):** show each pair's price movement % over 15m/30m/1h in BOTH the Pair
Scanner table and the Launch-Pad table.

**Delivered:** TRAILING (rolling, last 15m/30m/1h) movement % columns on both tables,
computed LIVE at the dashboard API from the 1m candles in Redis (`{pair}:1m:candles`,
newest-first; idx N = N minutes ago; 65 retained → 1h ok). Helper `_trailing_move(r,pair)`
in `dashboard/api.py`; `/pairs/active` + `/launchpad` enrich each row. Frontend:
`PairScanner.tsx` + `LaunchPad.tsx` add `15m/30m/1h` coloured cells.

**Scope change (same session):** owner first asked for BOTH trailing AND since-entry
checkpoints; a since-entry path was built (maintainer wrote `scanner:move:{pair}` /
`launchpad:move:{slot}` Redis hashes, frozen at 15/30/60m marks) then **REMOVED on owner
request** ("remove since slot entry columns"). Reverted: dropped the `↪` columns from both
panels, `_since_move` from api.py, `_refresh_movement` from maintainer.py, and deleted the
orphaned Redis hashes. Trailing-only remains.

**Deploy infra change:** added `- ./dashboard:/app/dashboard` bind-mount to the dashboard
service in docker-compose.yml → **api.py is now restart/recreate-editable, NO 34GB image
rebuild** (mirrors the ./signals + ./memory pattern). One-time `docker compose up -d
dashboard` to apply the mount; thereafter `restart dashboard` picks up api.py edits.
Frontend deploys via `npm run build` (./frontend/build is bind-mounted into nginx).

**Verified:** `/pairs/active` + `/launchpad` return `trail_15m/30m/60m` only (no since_*);
SUIUSDT launch slot −0.68/−1.95/−2.79%; orphan move keys = 0 and not regenerating
(confirms new maintainer code live); dashboard up; bundle main.4dd24180.js.
(Note: very thin pairs with flat/stale 1m candles read 0.00% — data property, not a bug.)

---

## CONT. 70e — P1 REPLAY → LAUNCH-PAD INTEGRATION (2026-06-05)

**Goal (owner):** stop the replay pool being dead (consume_count=0); surface recoverable
rejected signals as EXTRA slots on top of the base 10 ("table grows to 11, 12, …"), tagged
permanently as replay so they're visible now AND auditable later (how did replay signals do).

**Reconciled root cause (Rule 9):** consume_count=0 was NOT full-deploy slot starvation
(the cont.69s theory). With launchpad:enabled=1 the engine HARD-BYPASSES the legacy replay
consumer (engine.py:1783 `[] if _lp_mode`) — the funnel is the sole opener (D1). Producer
kept writing (produce_count 5446→10330) into a pool nothing read.

**Implementation (additive replay slots, id ≥ 1001):**
- DB: `source`/`replay_reason`/`replay_strength` added to `launch_pad` + `launch_pad_history`.
- `store.py`: REPLAY_SLOT_BASE=1001, `is_replay_slot`, `create_replay_slot` (INSERT),
  `delete_slot`, clear_slot DELETEs replay slots (base slots still reset to empty),
  insert_history carries the source tag.
- `maintainer.py`: `_sync_replay_slots` stages fresh recoverable pool entries as extra
  slots (same qualify gate, replay entry dictates direction), capped at
  launchpad:replay_max_slots (5); base displacement excludes replay ids; replay expiry →
  DELETE + counter. Guard: active only when BOTH launchpad:replay_slots_enabled=1 AND
  launchpad:enabled=1 (else engine's legacy consumer owns the pool → no double-consume).
- `gate.py`: a replay slot that fires → `replay_pool.mark_consumed()` (consume_count finally
  moves) + history row source='replay'+trade_id. funnel_pairs already reads ALL mirror slots,
  so replay slots open through the SAME gate (D1 preserved). No engine.py change needed.
- `redis_keys.py`: LAUNCHPAD_REPLAY_* added BUT bypassed at runtime via local string consts
  in maintainer.py — only ./signals is bind-mounted; redis_keys is baked in the image, so a
  redis_keys attr ref would need a 34GB rebuild. (The redis_keys entries activate on next
  image rebuild; harmless until then.)

**Deploy:** ALTER TABLE (done) + restart brain + celery_worker_candlenet (both bind-mount
./signals — NO image rebuild). Enabled on paper: launchpad:replay_slots_enabled=1,
launchpad:replay_max_slots=5.

**Verified end-to-end (Rule 2/4):** controlled inject of 6 active non-buffer pairs → 5
staged into slots 1001-1005 (cap respected), table grew to 15, one qualified green + visible
to funnel_pairs, ZERO symbol dup vs base buffer (dedup works), base maintainer unaffected
(10 slots healthy, tasks succeeding), history insert with new cols OK. Synthetic test slots
+ test-inflated counters cleaned afterward.

**Honest limitation:** under launchpad-only mode the pool is fed mostly by pairs ALREADY in
the base buffer (only buffer pairs reach the reject path) → dedup-skipped → organic staging
is modest (mainly when a recoverable pair is displaced/expired out of its base slot, second
chance within 15-min TTL). Higher yield when launchpad is OFF.

**Enhancements (owner request, same day):**
- **Dynamic cap:** `launchpad:replay_max_slots` now treats `0` as UNLIMITED — stage ALL
  fresh replay signals (10, 15, …), naturally bounded by the pool's own max_entries (100).
  Set to 0 live. Verified: injected 8 → all 8 staged (slots 1001-1008, table→18).
- **"(replay)" trade tag:** added `trades.entry_source` (default 'scanner'); `gate.on_open`
  sets it to 'replay' for replay-slot opens (trades.id is uuid → cast). Surfaces in BOTH
  open + closed views ("SYMBOL (replay)") — `OpenTradesTable.tsx`/`ClosedTradesTable.tsx`
  render a badge when `entry_source==='replay'`; flows through `SELECT *`/`SELECT t.*` so
  NO api.py change (api baked, but memory/ + frontend/build are bind-mounted → frontend
  rebuilt via `npm run build`, nginx serves new bundle, no 34GB image rebuild). Verified
  live: NEARUSDT short opened from a replay slot → tagged entry_source='replay'.
- **BUG FIX (pre-existing cont.70):** `launch_pad_history.trade_id` was `bigint` but
  `trades.id` is `uuid` → EVERY fired-slot history insert silently failed
  (`launchpad_on_open_clear_failed`), leaving 1664 history rows with NULL trade_id and
  breaking the reliability-study join for BOTH scanner AND replay. Retyped to `uuid`
  (all rows were NULL → safe). Functional-tested: fired+replay history row w/ uuid inserts.

**Revert:** `redis-cli SET launchpad:replay_slots_enabled 0` (instant, code-safe).
The study join now works: `SELECT h.*, t.net_pnl_usdt FROM launch_pad_history h JOIN
trades t ON t.id=h.trade_id WHERE h.source='replay'` (and trades view: filter entry_source).

---

## CONT. 70d — LIVE AUDIT: SCANNER / LAUNCHPAD / F9-F12 (2026-06-05, no code change)

Read-only health audit (Rule-2 live state, Rule-9 disconfirm). No trading behaviour
changed; only next_impl docs + this log updated.

**1. Pair scanner "200 → 33" — NOT a bug.** Categorised mode reranks every ~20 min;
the active count is whatever survives the mover-filter cascade, not a fixed cap. Live
logs: `rejections={'vol':239,'mcap':33,'blacklist':2}` → the $150M 24h-quote-volume
floor (`scanner:min_quote_volume_usd`, default since cont.51) rejects 239 of ~306 perps
in today's low-volume down-market (e.g. ARB qv=$41.8M < $150M). CoinGecko buckets
gaming/lst/depin map to 0 perps so the 100-core only fills ~25; tail-padding re-applies
the same $150M filter → lands at ~32. Volume is real (not stale). Lever to widen:
`redis-cli SET scanner:min_quote_volume_usd 50000000`. Left at $150M (quality-over-qty).

**2. Launch pad — HEALTHY end-to-end.** Maintainer alive (`maintain_count=5730`,
ran <30s ago), `launchpad:enabled=1`, all 10 slots full (9 green/1 staged), 9 open
paper positions matching slots. `open_count=229`, displace=1559, refill=62, flips=38.
Gates all emit counters (candle_close_no_confirm=52631, dir_mismatch=5374, etc.).
*Verified-not-a-bug:* slots past `ttl_expires_at` (AIUSDT 6.5h) persist by design —
TTL only evicts `not qualified` slots (maintainer.py:158). Caveats: 8/9 open are short
(market-driven, −4..−5% day); `mv_predicted` anti-correlated with `mv_realized`
(known degenerate-model / kline-corpus issue, upstream of launchpad).

**3. F9/F12 "help open better trades" — WORKING (loop closed + actuating).**
Postmortem RAG → actuator → engine is live. `brain:filter_overrides` carries real
bucketed deltas (bull|40-44 `min_signal_strength_delta=-10`, n=532, ips_n=1087,
ips_optimal_delta_raw=-20); `brain:scorer_overrides`={regime:-0.15,tft:-0.15,ofi:+0.15};
engine consumes via get_bucket_delta (engine.py:1240) + get_scorer_overrides (768) +
confidence (risk/manager.py:1961). **Open design decision (left unchanged):** all
filter deltas are `bull|*` and no `_global_fallback` is set → F9 filter-loosening is
DORMANT in the current `turbulent` regime (scorer side still active). Choose: keep
regime-gated (safer) vs add conservative global fallback.

**4. CF pipeline — HEALTHY (Rule-9 correction).** Initially read as "201k unevaluated
backlog"; age-split query disconfirmed it: mature (>84h) NULL backlog = **0**; the 201k
NULLs are immature rows inside the 72–84h maturity window (oldest 62h). 28k/day created
= 28k/day evaluated. The cont.69s June-2 fix holds. The signal_monitor next_impl file's
2026-06-02 audit table (CF row "12 days behind") was stale — corrected with a 2026-06-05
re-verification block.

**Docs touched:** next_impl/f9_f12_revolutionary_uses.md (checklist ticked + open item),
ips_threshold_optimiser.md (checklist ticked), signal_monitor_replay_pool.md (2026-06-05
re-verify block). Only genuinely-open work surfaced: P1 replay pool consumer-starved
under full_deploy_mode (needs slot-carve decision).

---

## CONT. 70 / 70b — LIVE SL WEDGE ROOT-CAUSED & FIXED (2026-06-04)

**User symptoms (LIVE):** SL stops moving after a trade crosses TP1/TP2; profit never
locked; dashboard 2 open vs Binance 1; new-trade Peak +/- column blank; Binance balance
not shown on dashboard.

**TRUE ROOT CAUSE (cont. 70b — verified empirically):** Binance routes futures
STOP_MARKET trigger orders through the **CONDITIONAL/algo order system**. The create
response carries `algoId` (NOT `orderId`); these orders are **invisible to
`futures_get_open_orders`** (live only in `futures_get_open_algo_orders`); and a 2nd
`closePosition` algo stop is rejected `APIError(-4130) "... with GTE and closePosition ...
existing"`. Because `execution/live.py::_arm_stop` read `order.get("orderId")` (always
None) it never stored the pointer, never cancelled the prior stop, and `cancel_order`
(orderId) can't cancel an algo order anyway → every SL move hit -4130. `_backoff_call`
RAISES on -4130; the per-trade loop (`risk/manager.py:525`) had **no per-iteration
try/except** (only handler OUTSIDE the loop at :2041) → one trade's -4130 **aborted the
whole SL-monitor tick for every position** → frozen SL, dead profit-lock, blank peak,
~1 -4130/sec hammering the live API. Live-only (paper never places real stops).

**FIX (bind-mount deploy, no rebuild):**
- `exchange/client.py`: + `get_open_algo_orders()` and `cancel_algo_order(algo_id)`
  (the conditional-order list/cancel endpoints). `place_stop_market_order` unchanged
  (still closePosition — fine once cancel works).
- `execution/live.py::_arm_stop`: cancel prior + SWEEP via the ALGO endpoints; capture
  `order.get("algoId") or orderId` and store as the `sl_order_id` pointer; **wrong-side
  guard** (never place a stop on the profit side of mark → skips arm, logs
  `live_stop_wrongside_skip`). `modify_sl` made **best-effort** (exchange-arm failure is
  caught + counted `trail:modify_sl_arm_failed_count`; DB `trailing_sl_level` always
  written so the mark-monitor close still works → can never wedge the loop again).
  `close_trade` cancels the stop via `cancel_algo_order` + algo sweep.
- `risk/manager.py`: wrapped the TP1 + TP2 checkpoint `engine.modify_sl` calls in
  try/except (blast-radius containment, defense in depth).
- `docker-compose.yml`: bind-mount `./execution` + `./exchange` into brain (were baked;
  that's why the FIRST restart didn't pick up the live.py edit — bind-mount-map rule).

**VERIFIED end-to-end on live:** forced re-arm of SOLUSDT → `live_stop_armed
order_id=1000001857071509` (real algoId), Redis pointer stored, exactly ONE algo stop
left (old swept, new placed), **0 -4130 / 0 sl_monitor_error** since deploy, loop healthy
(`n_trades=2`), SOL+HYPE protected by exchange algo stops. ADA exited near breakeven
(-$0.56 trailing_sl — its +10.6% peak had already retraced before the fix; nothing
recoverable). WLD -$0.47.

**Also done/flagged this session:**
- **TAO ghost** (DB open, Binance flat): closed on Binance 15:09 @219.77, realized
  -$3.77; reconciled DB via `write_trade_close` (reason `trailing_sl`). **No
  position-reconciliation loop exists** in the codebase — ghosts never auto-close
  (future work: add a periodic DB-vs-`positionAmt` reconciler).
- **Exit-reason disables (user mandate "only SL should exit"):** set
  `risk:dead_trade_disabled=1` and `risk:filtered_obi_force_exit_enabled=0` (live Redis,
  no restart). OTHER non-SL exits still active (time_barrier_max_hold @48h, mtf_15m
  reversal, other frontier force_close features, regime_flip) — pending user decision.
- **Dashboard Binance balance blank:** data is fine (`account:balance_usdt`≈144,
  api returns `real_balance`, `bot:mode=live`). The SERVED React bundle
  (`frontend/build/static/js/main.2d90f0e0.js`) is STALE — zero refs to `real_balance`;
  the "Binance Balance" tile in `frontend/src/panels/SummaryBar.tsx` was never rebuilt.
  Fix = `npm run build` in `frontend/` + redeploy. NOT done.

## CONT. 70c — LIVE FEES NOT RECORDED + DASHBOARD BALANCE + DB↔BINANCE MISMATCH (2026-06-04)

**User:** closed-table total doesn't match real Binance loss (~$7 actual vs ~$3 shown);
fees not shown; Binance balance missing from dashboard.

**Findings (Binance income = source of truth, live today):** realized -5.02, commission
-1.33, funding +0.01 → **NET -6.34**. The "$3 vs $7" was the TAO ghost (-$3.77) missing from
the closed table until reconciled (cont. 70).

1. **Live trades recorded fees_usdt=0** (root): `execution/live.py` read
   `order.get("commission")` off the market-order ACK, which is always 0. FIX: added
   `BinanceClient.get_account_trades()` + `LiveExecutionEngine._resolve_order_commission()`
   (sums real commission from the fills by orderId); `open_trade` stashes entry fee in
   `trade:{id}:entry_fee_usdt`, `close_trade` totals entry+exit → `fees_usdt`, `net_pnl`.
   Bind-mount deploy + brain restart. Backfilled the 8 live closed trades from Binance
   (fees ≈0.10-0.15 each; live closed now gross -7.66 / fees 1.05 / net -8.70).
2. **Dashboard Binance-balance tile missing** (root): `frontend/src/App.tsx` *imported*
   `SummaryBar` but never RENDERED `<SummaryBar/>` → webpack tree-shook it out (bundle was
   byte-identical every rebuild, no `real_balance`). FIX: render `<SummaryBar/>` at top of
   App; `npm run build` → new bundle `main.4ff32de2.js` (contains the tile); nginx
   bind-mounts `frontend/build` so it's live after a hard refresh. Data/API were always fine
   (`account:balance_usdt`, `/bot/status`→`real_balance`, `bot:mode=live`).
3. **DB ≠ Binance — MISSING LIVE TRADES (unresolved):** Binance has an **ARB +$3.99 live
   win** and more ONDO (-1.53) that have NO DB record at all (likely opens that placed a
   Binance order but failed the DB write during the -4130 chaos). So even after fee backfill
   the DB live net (-8.70) ≠ Binance net (-6.34); gap = the missing ARB win (+3.91) + ONDO
   (~-1.4). There is **no Binance→DB reconciler** — RECOMMENDED next: a periodic job that
   syncs closed trades / realized PnL / fees from `futures_income_history` as source of truth.
   Did NOT hand-insert trades.

## CONT. 70d — GHOST: SL-HIT TRADE STAYS "OPEN" IN DB (already-flat close) (2026-06-04)

**User:** HYPEUSDT hit its SL and closed on Binance but the dashboard kept showing it OPEN.

**Root cause:** the exchange-native (algo) stop flattens the position FIRST; the bot's
mark-monitor then also hits the SL and calls `close_trade`, which places a `reduceOnly`
market order → Binance rejects `-2022 "ReduceOnly Order is rejected"` (no position) →
`_backoff_call` RAISES. `_guarded_close` had set `trade:{id}:closing` (120s TTL) BEFORE the
throw and never cleared it (delete was after the throwing call), and the top-of-loop
`if r.get(_closing_key): continue` then SKIPPED the trade for 120s. So: close throws → DB
row never written → flag stuck → trade skipped 120s → flag expires → retry → -2022 again.
Infinite 120s loop, permanent ghost (DB OPEN, Binance FLAT). Confirmed: -2022 every 2 min.

**Fix (bind-mount, restart):**
- `execution/live.py::close_trade`: wrap the reduce-only order; on `-2022/-2021/-4131/"no
  position"` treat as **already flat** (don't raise), set `order={}`, resolve exit_price
  from `trailing_sl_level` (the stop trigger) else mark, and PROCEED to `write_trade_close`
  → the DB row is closed instead of ghosting.
- `risk/manager.py::_guarded_close`: `try/except/finally` — close failures can't escape the
  per-trade loop and the `closing` flag is ALWAYS cleared.

**Verified:** HYPE auto-closed on the next tick (`live_close_position_already_flat` →
`trailing_sl`), 0 -2022 after, open trades = {SOL} = Binance. Backfilled HYPE to Binance
truth (realized -4.00, comm -0.15, net -4.15; the stop filled with slippage below trigger).

**Still recommended:** a Binance→DB reconciler. The mark-monitor only self-heals this ghost
while mark is still past the SL; if price recovers above the SL before the monitor catches
it (mark>SL for a long), the DB row would stay open. A periodic positionAmt/income sync is
the robust fix (also covers the missing ARB +3.99 / ONDO opens from cont. 70c).

## CONT. 70e — BINANCE→DB RECONCILER BUILT (2026-06-04)

Closes the whole class of DB↔Binance drift (the HYPE ghost, the missing ARB +3.99 win,
the ONDO gap). New module `risk/reconciler.py`, launched as a background asyncio task from
`risk.manager.monitor_trailing_sl` (main.py is baked → spawning from bind-mounted risk/ is
restart-only). **Binance is the source of truth.** Every 120s, live mode only:
1. **GHOSTS** — any `is_paper=false` trade still `open` in the DB whose Binance
   `positionAmt==0` → closed on the exchange but never recorded → `write_trade_close` with
   REAL realized/commission/funding from `futures_income_history` (exit_reason
   `reconciled_exchange`, added to `trades_exit_reason_check`). 90s grace period so a
   just-placed order isn't mistaken for a ghost. ONLY mutation it makes.
2. **DRIFT** — Binance net (realized+comm+funding, 24h) vs DB net of is_paper=false closed
   (24h); logs `reconciler_drift_detected` + stores `reconciler:{binance_net_24h,db_net_24h,
   drift_24h}` in Redis when |drift| > `reconciler:drift_alert_usdt` (default 1.0). Report
   only — does NOT fabricate entry data for unknown trades.
Toggles: `reconciler:enabled` (default on), `:grace_s` (90), `:drift_check_enabled` (on),
`:drift_alert_usdt` (1.0). Counter `reconciler:ghosts_closed_count`.

**Verified:** dry-run left the legit SOL open position untouched (positionAmt>0 → skipped),
0 false closes; drift detector reported binance_net -10.51 vs db_net -12.85 → drift -2.35
(the missing ARB win + gaps). Deployed: `reconciler_started interval_s=120`, brain healthy.

NOTE: part 2 only REPORTS missing trades (e.g. ARB). Auto-creating DB records for trades that
executed on Binance with no DB row (guessed entry/capital) is deferred — too risky. If wanted,
v2 could synthesize closed rows from `futures_account_trades` entry/exit fills.


================================================================================
## [ older cont.1–69 history archived → PROGRESS_ARCHIVE_2026-06-07.md ]
================================================================================

## cont. 70 (2026-06-03) — All-TF corpus refresh: 4h/1d added to the rolling loop

PROBLEM (from cont.69y audit): bulk-topup + backfill derive their interval list from
ml/klines_corpus.py `DEFAULT_LOOKBACK_DAYS`, which only had 1m/5m/15m/30m/1h. So 4h/1d were
NEVER refreshed — 4h frozen at 2024-10-30, 1d ~18d behind (files mtime May 16). Fine TFs were
fresh to Jun 1-2.
FIX: added "4h":730 (2y), "1d":1825 (5y) to DEFAULT_LOOKBACK_DAYS (coarse bars = few rows, deep
HTF history is cheap). This single edit fixes BOTH loops since both default
intervals=list(DEFAULT_LOOKBACK_DAYS).
AUTO-REFRESH LOOP: already existed — beat `bulk-topup-corpus-daily` -> bulk_topup_corpus_task
(queue cn_train) @ 01:30 UTC, ban-immune via data.binance.vision daily zips. Now covers all 7 TFs
automatically. NO new beat needed.
SAFETY: verified DEFAULT_LOOKBACK_DAYS has ONLY one real consumer (klines_corpus itself; the
celery_app hit is a docstring). All trainers/feature builders read explicit intervals
(xgb default 15m, fixed TF tuples) — none iterate the dict, so adding 4h/1d cannot break training.
DEPLOY: ml/ is a DIR bind-mount (live) but the worker had the module cached -> restarted
celery_worker_cn_train; verified in-process dict = {...,'4h':730,'1d':1825}.
ONE-TIME GAP BRIDGE: top-up only pulls recent daily files (won't bridge 4h's 19-month gap), so
launched a one-time deep backfill (monthly zips) for intervals=["4h","1d"] over 200 active pairs
in background on cn_train. Progress in Redis corpus:htf_backfill:{status,done,ok,fail}; log
/tmp/backfill_htf.log. ~6s/pair, ~20min. After this the daily loop keeps them current.

## cont. 70b (2026-06-03) — Track 1: structural-edge entry gate LIVE (regime + session)

Why: cont.69v (N=6642) proved the edge is STRUCTURAL (regime + UTC session), NOT per-trade model
score (chasing a "better candle model" is the wrong lever — candle OHLCV @5m-1h is ~0.54 AUC
ceiling; confirmed by our 69y multi-TF test lift -0.004 AND the LOB literature "better inputs >
deeper model"). See next_impl/better_direction_models_roadmap.md for the full 4-track program.

WHAT (signals/engine.py accept_or_reject, inserted before the strength check):
- (a) REGIME GATE: reject entries when market_regime=="unknown" (cont.69v: unknown -0.97%cap, the
  only losing regime). Deadlock-safe: if >80% of last-50+ signals are unknown (classifier stuck),
  force-pass so the bot never starves. Counters structural:regime_gate:{reject_count,
  deadlock_pass_count} + signal:reject:regime_unknown (silent-rejection rule).
- (b) SESSION GATE: SOFT strength penalty (+6, never a hard reject) in net-negative UTC hours
  {4,6,9}. Redis-tunable structural:session_{bad_hours,penalty}. Counter structural:session_gate:
  penalty_count.
Both gate-NOT-kill + Redis-toggleable (structural:{regime,session}_gate_enabled, default ON).
Redis keys SET durably (regime/session enabled=1, bad_hours="4,6,9", penalty=6.0).

DEPLOY: brain runs the gate (brain/soar.py:432 -> process_signals) + bind-mounts signals/ ->
restart brain only (no rebuild). VERIFIED LIVE: total_seen=43, regime rejects=0 (regime valid),
session penalty=43 (current hour=04 UTC is a bad hour -> correctly penalized), no exceptions.

REVERSIBLE: `redis-cli SET structural:regime_gate_enabled 0` / `structural:session_gate_enabled 0`.
CAVEAT: 17-day mostly-bull sample -> "unknown is bad / these hours are bad" may shift on regime
change; gates are soft/toggleable by design. Re-check session hours after a regime shift.

DEFERRED — Track 1b (archetype tilt fade>momentum): NOT done here. The signal dict carries NO
archetype field at accept_or_reject (archetype lives in the strategy router/DB), so it belongs in
strategy selection/sizing, not the entry gate. Scoped into strategy/router.py as a follow-up
(would no-op silently if jammed into the gate — Rule 2).

NEXT: Track 2 — microstructure online direction model (OFI/CVD already live in Redis via micro_ws;
wire into candle_online_trainer train==serve + start OFI/CVD corpus logging). Plan:
next_impl/microstructure_online_model.md (to create).

## cont. 70c (2026-06-03) — Track 2: microstructure features into the online model LIVE

Goal: get cont.69v's winning microstructure signal (depth/OFI/CVD) INTO the predictive model.
Rule-10 audit found OFI/vpin/bid_ask_imbalance were ALREADY in prediction/features.py
FEATURE_COLUMNS (online + xgb predict-all). REAL gap = CVD + OFI derivatives micro_ws/cvd_producer
emit live but the model never saw.

ADDED 3 cols to FEATURE_COLUMNS (32 -> 35), populated in live_features():
- cvd_z   = CVD z-scored over {pair}:cvd_history (raw cvd scale varies ~200x/pair -> _cvd_z helper,
            clip [-5,5]; raw was unusable). 
- ofi_l1  = {pair}:micro:ofi_l1 (top-of-book OFI, already [-1,1]).
- ofi_accel= {pair}:micro:ofi_accel (already bounded).
Live-only (no historical reconstruction; 0-fill offline, same rationale as kline_features OHLCV-only).
Verified populated live: BTC cvd_z=0.71 ofi_l1=0.41 ofi_accel=-0.55 (real, not zeros).

SHAPE-CHANGE handled (2 guards, both verified):
1. prediction/xgb_predictor.py predict(): feature_count DORMANCY guard -> old 32-col bundle returns
   None (logged once) instead of crashing. predict-all gate is OFF so ZERO live impact.
2. prediction/online_predictor.py _ensure_bundle(): n_features guard -> auto-discards a stale-width
   persisted bundle + recreates fresh (river relearns online). VERIFIED: logged
   online_predictor_reset_on_feature_change old_n=32 new_n=35; bundle now 35-col. Self-heals future
   feature changes (Tracks 3/4).
NOT a parity surface: ml/direction_model.py _build_live_features is a SEPARATE 7-feature model
(own scaler) — left untouched (touching it would break it).

DEPLOY: prediction/ is COPY-baked (NO bind-mount anywhere) -> REQUIRED full image rebuild
(trading-bot-app:latest) twice (features+xgb guard, then online guard). brain ALSO imports
live_features (engine.py:1008 feature_vector snapshot = train==serve) so recreated brain +
celery_worker_candlenet (predict_all/microstructure queues) + celery_worker_cn_train. Verified
IMAGE (docker run grep: 35 cols, guards present) AND in-process after recreate (Docker COPY cache
rule). Track 1 gate survived recreate (signals/ bind-mount + Redis keys persist).

WHERE THE NEW FEATURES FLOW: (a) ONLINE river model live now (relearning post-reset); (b)
feature_vector snapshot on every trade -> future OFFLINE retrains learn them. Legacy
predict_all_xgb.pkl (FEATURE_COLUMNS, train_from_history is CLI-only, gated off, superseded by
predict_all_kline.pkl) stays dormant until manually retrained — acceptable. Kline predict-all
(OHLCV 18-col) unaffected (different feature set).

DEFERRED: Track 1b archetype tilt (strategy router); Track 3 TLOB f57; Track 4 Kronos/FinCast prior.

## cont. 70d (2026-06-03) — Track 1b: archetype capital tilt LIVE (fade>momentum)

cont.69v: dominant edge axis = strategy ARCHETYPE (fade/mean-reversion WIN; momentum/breakout/
continuation + ML-confirmers LOSE). Implemented a CAPITAL tilt (not a gate): boost fade winners
1.3x, cut momentum losers 0.6x, clipped [0.25,2.0]. Gate-NOT-kill — losers keep trading at reduced
size (UCB selector keeps learning; 17-day-bull + small-N noise can self-correct).

WHERE: signals/engine.py. Two module-level helpers (_strategy_name_cached: id->name Redis-cached
5min; _archetype_capital_mult: exact-name match -> mult) + applied in the sizing chain right AFTER
the F8 capital_pct_mult block (~line 2671), multiplying capital_usdt.

WHY engine.py not strategy/router.py: strategy/ is NOT a brain bind-mount (only signals/ml/risk are)
-> putting it in the bind-mounted engine.py avoids an image rebuild (just `docker compose restart
brain`). engine.py already imports db_conn for the name lookup.

EXACT-NAME match (NOT substring) — critical: swing_sweep_fade & premium_index_z_fade are cont.69v
LOSERS despite "fade"; exchange_netflow_inflow_fade is a WINNER. Substring matching would
misclassify. Name lists are Redis-tunable (structural:archetype_{fade,momentum}_patterns); default
lists hardcoded from cont.69v.

REDIS keys (durable): structural:archetype_tilt_enabled=1, archetype_fade_mult=1.3,
archetype_momentum_mult=0.6. Counters structural:archetype_tilt:{boost,cut}_count.

VERIFIED: logic 4/4 (funding_extreme_fade->1.3, donchian_breakout->0.6, unlisted->1.0,
swing_sweep_fade->0.6 [trap case passed]); FIRING LIVE (boost=1 cut=2 within mins); brain clean
restart (Restarts=0, all_startup_checks_passed). The startup CancelledError @limit_entry.py:208 is
the OLD process tearing down on restart — benign, not a crash.
NOTE: stage strategies with strategy_id=NULL (e.g. stage1_ofi_momentum) aren't tilted (no UUID) ->
no-op, acceptable. REVERSIBLE: redis-cli SET structural:archetype_tilt_enabled 0.

TRACKS REMAINING: Track 3 TLOB f57 (greenfield), Track 4 Kronos/FinCast prior (greenfield).

## cont. 70e (2026-06-03) — Track 2 DEPLOY-GAP FIX + shadow-ablation measurement harness

USER TASK: "measure Track 2 first" (microstructure AUC lift) before building Track 3/4.

RULE-2 DISCOVERY (the measurement surfaced a cont.70c deploy bug): the 3 microstructure
features NEVER reached the live online model. On-disk online_predictor bundle was 32-col,
n_updates=189150 — cvd_z/ofi_l1/ofi_accel absent. ROOT CAUSE: candle_online_train_task (the
dominant ~189k-update path, trained=200/~22s) executes on the DEFAULT `celery_worker`, which
cont.70c NEVER recreated — still 32-col baked prediction/features.py. cont.70c verified
brain/candlenet/cn_train at 35 cols but those aren't the task's executor. Secondary: brain
(learn_from_close, 35-col) + celery_worker (learn_from_candle_close, 32-col) wrote the SAME
shared pickle at different widths -> restart-time width-reset thrashing; celery_worker
dominated persists so disk stayed 32-col. Coverage sweep: stale 32-col also on data_feed,
scanner, web_intel, dashboard, watchdog (none train the online model -> deferred).

FIX (Step 1, no rebuild — image a1607d2 already built): `docker compose up -d --no-deps
celery_worker` -> 35-col. VERIFIED: bundle auto-reset 32->35 on first tick, n_updates=400
(re-warmed past MIN=200) within ~1 tick. brain + celery_worker now BOTH 35-col -> thrashing
ended. The 3 features finally train the dominant candle path.

MEASUREMENT (Step 2 — why a harness was needed): online_predictor is plain SGD tracking ONLY
n_updates (NO AUC metric); the 3 cols have NO historical reconstruction (live-only) -> a
retrospective ablation is impossible. Only honest option = FORWARD shadow ablation.
NEW ml/shadow_ablation.py (pure shadow: never trades/gates/touches online_predictor):
mirrors f50e enqueue/drain on its OWN watermark/pending keys (reuses f50e _read_candles +
_resolve_label so samples == what the live model learns), feeds TWO prequential SGD logistic
heads (35-col "full" vs 32-col "ablated" = drop the 3) with identical hyperparams to
online_predictor.direction. Prequential: predict-then-fit; rolling 3000-row window
(p_full,p_ab,y) -> roc_auc each tick. Publishes shadow_abl:{auc_full,auc_ablated,lift,n,
positive_rate,status}. Kill switch shadow_abl:disabled; never raises; counters.
WIRING: celery_app.py shadow_ablation_task @ queue=microstructure (candlenet worker: 35-col
image + ml/ + celery_app.py bind-mounts -> NO rebuild) + beat "shadow-ablation" @60s + route.
DEPLOY: restart celery_beat + celery_worker_candlenet only.

QUALITY FIXES found while verifying (Rule 2/4): (1) candlenet worker concurrency=2 -> two
forks kept separate in-memory models from stale disk (n_updates frozen at 2400) -> made
tick() reload-from-disk + persist EVERY tick (task fires once/60s so no concurrent writer;
forks now share via pickle, n_updates accumulates monotonically — verified 1200->1600->2000).
(2) Added _WARM_BEFORE_EVAL=1000 warm-gate: only record predictions once BOTH heads converged
(wider 35-col head warms slower -> early cold preds biased lift; saw it swing +0.0143 ->
-0.0150 during warm-up — NOT a real signal, Rule 9). Reset state+window+watermarks; restart.

VERDICT (full 3000-row window, post-warm-gate): lift = -0.0035; auc_full 0.5563 vs auc_ablated
0.5598; both heads ~0.556. Trend across the fill: +0.0129 (n=995) -> +0.0044 (1795) -> -0.0088
(2595) -> -0.0035 (2995). Lift oscillates in [-0.009,+0.013] around ~0 = NOISE. The 3
microstructure cols add NO robust per-candle directional edge -> CONFIRMS the roadmap input-
ceiling diagnosis. Reconciles with cont.69v (microstructure winner depth_weighted_ofi was a
trade-level fade STRATEGY, not a per-candle direction predictor). DECISION: do NOT promote
microstructure to a hard gate (P6); keep as passive features (harmless; may aid regression/
sizing heads). Lever = better INPUTS (Track 3 LOB depth / Track 4 foundation prior), not more
candle-aligned features. Harness stays live (shadow, zero cost): `redis-cli get shadow_abl:lift`.
REVERSIBLE: redis-cli SET shadow_abl:disabled 1 (+ remove beat entry). Online_predictor fix is
not reversible (correctly 35-col now).

## cont. 70f (2026-06-03) — Track 4: foundation VOL/MAGNITUDE prior for sizing + SL (Chronos REVIVE)

DECISION (user-approved): deliver Track-4's goal (soft foundation vol prior for sizing/SL-TP, NOT
a direction gate — cont.70e proved direction is at ceiling) by REVIVING the dormant Chronos
forecaster instead of greenfield Kronos. Blueprint-correct (f55 sequences Kronos AFTER Chronos is
measured). Kronos deferred to P5 (only if Chronos proves weak).

AUDIT (Rule 2): ml/foundation_forecast.py (F50g Chronos-Bolt) already EXISTS + model DOWNLOADED
(models/chronos_bolt_base) but was DORMANT (no producer task ever called predict() -> zero keys)
AND MISWIRED (engine.py:559 used only dir1h as a weak ±8 DIRECTION bonus, threw away the vol band).
Plus version bug: predict_quantiles(context=...) -> installed chronos needs POSITIONAL inputs ->
TypeError -> silent None. And requested q05/q95 but Bolt only trained on [0.1..0.9].

P1 FIX (ml/foundation_forecast.py): positional inputs; quantile_levels [0.1,0.5,0.9] -> honest
q10/q50/q90; added spread_frac=(q90-q10)/anchor (the vol payload). DATA: only the 1h candle buffer
is deep enough live (~300; 1m/5m/15m/close_history ~65) -> switched to 1h candles, CONTEXT 256
(min 96), HORIZON 3 (=3h band), TTL 1200. Removed the TFT auto-defer (TFT gives no vol band).
Verified: ~300ms/pair steady (first call ~7s warmup); BTC spread_frac 2.66%, ETH 3.0%, SOL 3.2%.

P2 PRODUCER (ml/foundation_forecast.run_forecast_sweep + celery_app.foundation_forecast_task @
queue=predict_all, beat "foundation-forecast-sweep" @600s). Bounded active-pair set, kill switch
foundation:disabled, counters foundation:sweep_{ok,none,total}. Candlenet worker (torch + ml/
bind-mount + model) -> NO rebuild. VERIFIED end-to-end via celery dispatch: sweep_ok=60 none=0,
68 {pair}:foundation_forecast keys live.

P3 CONSUMERS (soft, gate-not-kill, Redis-tunable, user-approved show-diff-first):
 (a) SIZING — signals/engine.py after archetype tilt (~2673): vol-uncertainty size mult =
     clip(vol_prior:size_ref/spread_frac, [0.5,1.25]); wider band -> size DOWN. Counters
     vol_prior:size:{down,up}_count.
 (b) SL FLOOR — risk/manager.py after F48 CandleNet-mag floor (~172): atr_distance = max(atr,
     mark*spread_frac*vol_prior:sl_band_k[0.5]); only WIDENS SL. Counter
     vol_prior:sl_floor_applied_count.
 TP BLEND DEFERRED (interacts with tuned TP ladder + profit-lock; needs separate measurement).
 Redis defaults SET durably: vol_prior:{size_enabled=1,size_ref=0.03,size_min=0.5,size_max=1.25,
 sl_floor_enabled=1,sl_band_k=0.5}. DEPLOY: brain bind-mounts signals/+risk/+ml/ -> restart brain
 only (no rebuild). VERIFIED LIVE: size:down_count=1, sl_floor_applied_count=1, 0 tracebacks,
 brain clean.

REVERSIBLE: redis-cli SET vol_prior:size_enabled 0 / vol_prior:sl_floor_enabled 0 /
foundation:disabled 1.
NEXT: P4 — forward-measure realized RR / SL-hit-rate WITH vs WITHOUT the vol prior (needs trades
to accumulate; tag trade metadata with applied mult/floor, then compare). P4 is the f55 gate for
whether Kronos (P5) is ever worth building. Plan: next_impl/track4_vol_sizing_prior.md.

================================================================================
cont. 70 (2026-06-03 ~21:00) — LAUNCH-PAD: P4 deployed live + P5 built + funnel enabled (PAPER)
================================================================================
Owner directive: rebuild celery_beat + celery_worker_candlenet; CHANGE OF PLAN — skip the
shadow-only phase, take the launch-pad funnel DIRECT to live trading (paper opens + asked for
real money). Plan doc: next_impl/launch_pad_10.md (D1-D9 locked).

DONE:
- REBUILD: celery_beat + celery_worker_candlenet rebuilt+recreated (celery_app.py is BAKED, not
  bind-mounted → restart was insufficient; prior session's beat ran the 10:26 schedule w/o the
  launch_pad task). VERIFIED: worker grep launch_pad_maintain_task=3 (was 0); beat reloaded.
  P4 MAINTAINER NOW LIVE: launchpad:maintain_count incrementing, last_run_ts set, buffer 10/10
  confirmed_green. Runs on predict_all queue → candlenet worker (signals/ IS bind-mounted there
  so the launch_pad pkg was already present; only celery_app.py needed the rebuild).
- P5 BUILT: new signals/launch_pad/gate.py (funnel_pairs reads launchpad:slots Redis mirror,
  qualified-green only, movement-ranked; direction_ok; on_open retires slot→fired+cooldown+
  open_count). 4 surgical hooks in signals/engine.py::process_signals, ALL gated by _lp_mode
  (launchpad:enabled=="1"): (1) funnel pairs override + WAIT-on-none-qualified, (2) skip replay
  fetch + (3) skip bandit re-rank in funnel mode, (4) enforce slot direction, (5) on_open after
  opened_trade_ids.append. Legacy 160-flow byte-identical when disabled. ast-parse clean; brain
  restarted (bind-mounts signals/), no traceback.
- ENABLED on PAPER: launchpad:enabled=1. TRADING_MODE=paper, BINANCE_TESTNET=true UNCHANGED.

NOT verified-live yet (ROOT-CAUSED, not a code bug — Rule 10 reachability): funnel never reached
because brain ACT phase (brain/soar.py::_act) returns BEFORE process_signals on two pre-existing
gates: (a) line 316 capacity: open_trades(15) >= bot:max_open_trades(15); (b) line 350
capital_starved: free_balance=$9.57 → capital_per_trade=$2.87 < $5 MIN_TRADE_USDT. Paper account
is capital-exhausted + at capacity → NO new opens at all (funnel or legacy). Funnel engages
automatically once a trade closes (frees slot + capital). Live-open proof needs capital freed
(close trades / top-up virtual_balance) — NOT done unilaterally; asked owner.

REAL MONEY BLOCKED (did NOT flip): loaded key is TESTNET (BINANCE_TESTNET=true). Testnet keys
don't auth vs production; prod key-auth unverified (memory project_live_trading_blocked). Requires
PROD keys loaded + auth-verified BEFORE TRADING_MODE=live. Refused to flip on a testnet key.

DEGENERATE-BUFFER WARNING: buffer 9 short / 1 long, 8/10 slots already underwater on shadow PnL
despite all "qualified green" (= momentum-confirmed at qualify time, NOT profitable). Same pattern
as feedback_predict_gate_disabled / conformal. Exactly what the skipped shadow phase would surface.
Safe on paper; reckless on real money.

REVERSIBLE: redis-cli set launchpad:enabled 0 → instant revert to legacy flow.

CORRECTION (cont. 70 cont., ~21:25) — the "capital_starved" blocker above was a TRANSIENT
($9.57 momentarily; later $400 free). The REAL reason the funnel never engaged after the 21:06
restart: brain runs image trading-bot-app:latest with redis_keys.py BAKED (root file, NOT
bind-mounted). `docker compose restart brain` restarts the EXISTING container — it does NOT
recreate from the rebuilt image → brain kept a stale redis_keys.py missing the LAUNCHPAD_* consts.
Symptom: every cycle logged `launchpad_funnel_skipped error="module 'redis_keys' has no attribute
'LAUNCHPAD_ENABLED'"` → silent fallback to LEGACY flow (that's how 3 LONGs XLM/BAS/HEI opened in a
bull regime at 21:14, NOT funnel opens). FIX: `docker compose up -d brain` (RECREATE, not restart)
→ brain now has redis_keys.LAUNCHPAD_ENABLED; funnel ENGAGED: `launchpad_funnel_active n_pairs=9`.
LESSON (restart≠recreate for baked code; sister to verify-task-executor + docker_copy_cache).

FUNNEL VERIFIED WORKING but STARVED OF OPENS by a direction conflict (NOT a bug): buffer is 9/10
SHORT, but live regime=BULL. Every short buffer candidate is correctly rejected by the bot's
existing short-blocking gates: signal:reject:short_htf_bullish (HTF bullish), sentiment_0.39
blocks_short, micro_jump_veto. launchpad:open:reject:dir_mismatch=3 (CandleNet staged short but
the engine signal was long in the bull regime → P5 direction-enforce rejected). NET: funnel
selects 9, downstream gates pass 0 → 0 funnel opens. open_count=0. This is the degenerate-buffer
problem (CandleNet short-bias vs bullish regime) — the model-quality issue (kline corpus reframe),
NOT the funnel. Funnel will open once buffer direction reconciles with regime. Do NOT disable the
protective short-blocking gates to force opens.

NEXT: P6 dashboard panel (user asked why the 10-table isn't on the dashboard — answer: never
built) + P7 blueprint section. Memory project_launch_pad written + corrected this session.

================================================================================
cont. 70 cont. (~21:45) — REGIME CLASSIFIER WAS BROKEN (stuck "bull" in a -5% market) — FIXED
================================================================================
User challenged my "bull regime" claim (crypto actually down). VERIFIED they were right.
ROOT CAUSE (ml/hmm.py + data/feed.py + pretrainer/main.py), 4 compounding bugs:
 1. INPUT: data:returns:recent = CROSS-SECTION (latest tick of ~100 different pairs) was
    Viterbi-decoded as one asset's TIME SERIES; model TRAINED on pooled DAILY returns
    (pretrainer/main.py:96, all_returns.extend across 30 symbols, NO outlier filter) →
    train(daily)/serve(minute-cross-section) mismatch → live data always collapsed into the
    near-zero state. Instantaneous snapshot also too noisy (breadth flipped 58-down→70-up in min).
 2. LABELS positional not calibrated: _REGIMES={0:bull,1:bear,2:turbulent}; model means_ were
    [≈0, 39.26(degenerate), +0.84%, -0.31%] → state0(FLAT) mislabeled "bull", state2(+0.84%)
    "turbulent".
 3. 4 states, 3 labels: state3 (most-negative, stickiest=truly bearish) fell through
    .get(3,"bull"); plus exception/default "bull".
 4. Degenerate state (mean 39.26=3926%) from an unfiltered bad daily return.
CASCADE: false "bull" → sentiment gate (engine.py:1628) is REGIME-KEYED: bull→block_short_above
 =0.10 (blocks shorts at sentiment>0.10); live sentiment ~0.38 → ALL shorts blocked. bear→0.60
 (shorts pass). The wrong regime alone starved the funnel's 9 shorts. (short_htf_bullish is
 CandleNet-dir3-driven, regime-independent, still legit; micro_jump_veto order-book, legit.)
GROUND TRUTH: BTC -2.85%/24h (web: -4..6.5%), alt-universe median -0.86%, 135/200 (68%) declining
 → BEAR. My earlier "degenerate short buffer / gates protecting you" was WRONG — the short bias
 was CORRECT for the real market; the REGIME was the bug.
FIX (Rule-5 redesign): ml/hmm.py rewritten — regime = BROAD-MARKET composite, NOT BTC-only (user
 caught that BTC-only ignores the alts we trade). composite = btc_weight(0.35)*BTC_24h +
 0.65*alt_median_24h + alt declining-breadth + turbulent overlay (alt dispersion / BTC hourly
 vol). Uses STABLE {pair}:change_24h across scanner:active_pairs (200, all have it), NOT noisy
 ticks. Any retrained HMM now gets CALIBRATED labels (sort means_) + degenerate-model rejection;
 HMM path OFF by default (regime:use_hmm=1 to opt in). NO bull defaults. Redis-tunable:
 regime:{btc_weight,bear_pct=1.5,bull_pct=1.5,bear_breadth=0.58,turbulent_dispersion=8.0,
 turbulent_hourly_std=0.015}.
DEPLOY: ml/ bind-mounted into brain → `docker compose restart brain` loaded it (NOT a recreate —
 contrast redis_keys.py which is BAKED). STOPGAP first (current_regime=bear + F14 off to freeze
 the broken HMM), then F14 re-enabled after fix. VERIFIED LIVE: regime_updated regime=bear
 reason=down_trend_or_breadth composite=-1.39 btc_24h=-2.85 alt_median=-0.6 alt_breadth_down=0.643
 alt_n=199; current_regime=bear; a SHORT (HUMAUSDT) opened post-fix. TODO if HMM ever re-enabled:
 pretrainer/main.py train_hmm needs outlier filter + calibrated labels + consistent frequency.
REVERSIBLE: regime thresholds are Redis keys; regime:use_hmm gates the HMM; old model untouched.

================================================================================
cont. (2026-06-04 ~05:30) — TRADE HALT ROOT CAUSE + TURBULENCE BUG FIXED
================================================================================
SYMPTOM: zero new trades for ~2.5h (last trade 02:06; now 04:40+). 0 open, 456
closed/24h. All-short recent history (degenerate short bias).

ROOT CAUSE (proven, not guessed): the SOAR loop's stage>=2 LLM macro-veto.
- brain/soar.py _act: `if decision.trade is False and decision.llm_available(True): return`
  → returns BEFORE process_signals, skipping the ENTIRE per-pair pipeline (all 200 pairs).
- DECIDE phase asks an LLM "open trades this cycle?". Local ollama phi3:mini 500s on
  every real prompt (num_ctx=8192 > n_ctx_train=2048 → context overflow, 8s then HTTP 500),
  so it falls back to CLOUD (mistral) which SUCCEEDS and answers trade=false every cycle
  (regime turbulent + sentiment 0.35). Cloud success → llm_available effectively True → veto fires.
- PROOF: every reject counter frozen for 70s+ (per-pair loop never ran); brain:decisions_log
  showed uniform trade=false; _act gates (bot:running=1, capacity 0/15, balance 420, turbulence
  breaker 1.0<2.5) all PASS → only the LLM veto could block. active_pairs=200 (was a red herring;
  key is scanner:active_pairs not active_pairs).

FIX 1 (brain/soar.py): DEMOTED LLM macro-veto to advisory. Now gated behind Redis
  `brain:llm_macro_veto_enabled` (default "0"=OFF). When OFF: log
  `llm_macro_veto_advisory_ignored` + incr brain:llm_macro_veto:advisory_count, then
  fall through to process_signals (per-pair gates decide). When "1": old hard-block
  (incr brain:llm_macro_veto:blocked_count). Per-pair pipeline already has turbulent-OFI
  tightening + short guard + sentiment/predict-all/conformal + the turbulence breaker, so
  the macro LLM "no" was redundant AND a non-deterministic single-point bot halt.

TURBULENCE BUG (user asked to verify inputs):
- regime=turbulent CORRECT (broad composite, BTC -3.7%). global_sentiment 0.12-0.35 CORRECT/LIVE.
- turbulence_index = BROKEN: old formula sqrt(mean(((x-mean)/std)^2)) is a statistical
  IDENTITY ≡ 1.0 forever (standardized vector always has variance 1). Measured nothing;
  breaker (>2.5) could never fire; LLM misread constant "1.00" as alarming. Verified
  empirically: mean(z^2)=1.0 exactly on live 100-pair vector.
FIX 2 (data/feed.py): turbulence = current cross-sectional dispersion(std) / rolling-median
  of dispersion history (Redis list turbulence:dispersion_hist, cap 500, warmup 30 → neutral
  1.0 until then). Calm→~1.0, broad dislocation→>1 (CAN exceed 2.5, breaker meaningful again).

DEPLOY: both files are in NON-bind-mounted dirs (brain/ not mounted; data_feed no mounts)
  → REBUILT trading-bot-app:latest (bfd6d621c055→new), verified edits IN IMAGE
  (grep llm_macro_veto_enabled=2, dispersion_hist=1), recreated brain + data_feed.
VERIFIED LIVE: advisory_count climbing (7→17), blocked_count=0, short_htf reject moving
  again (process_signals runs), turbulence dispersion_hist warming (11→30).

REMAINING (pre-existing, NOT this fix): (a) bot only wants SHORT; shorts blocked by
  short_htf_bullish because predictor HTF dir3 leans UP even in down market = model
  miscalibration (see kline corpus reframe). Short guard is intentional (-$11.5k/7d loss
  bucket) — do NOT disable; fix = model retrain. (b) THROUGHPUT: ollama phi3:mini still
  500s every cycle (~20-30s wasted/cycle) — now harmless (advisory) but slows scan; fix =
  switch OLLAMA_RESEARCH_MODEL to qwen2.5-coder:7b or cap num_ctx<=2048.
REVERSIBLE: set brain:llm_macro_veto_enabled=1 to restore old hard veto; turbulence fix
  self-heals (warmup) and degrades to 1.0 on error.

--------------------------------------------------------------------------------
cont. (2026-06-04 ~07:12) — DECIDE LLM MODEL SWITCH (kill phi3 500s)
--------------------------------------------------------------------------------
phi3:mini decide 500s root cause = num_ctx 8192 > phi3 n_ctx_train 2048 (context
overflow on every REAL decide prompt) + phi3 ~33s warm >> 8s budget. Benchmarked
@num_ctx=8192: llama3.2:3b=6.8s/128k-ctx, qwen2.5-coder:7b=19.3s, qwen2.5:3b=timeout,
phi3=32.8s+500. Chose llama3.2:3b for decision_model+router_model (config.yaml).
NOTE: real decide prompts run 8-25s on this 6-CPU box (bigger than the bench
prompt + concurrent nomic-embed) — so an 8s/12s timeout still client-cancelled
(logged as 500/499). FIX: decide timeout now Redis-tunable `llm:decide_timeout_s`
(soar.py code default 20, Redis override set to 25 = proven clean). VERIFIED:
steady-state 0 failures / all 200; only cold-start (model reload on restart)
produces a transient 500 burst (~15s aborts) — unavoidable, harmless.
OLLAMA_RESEARCH_MODEL (.env) set to qwen2.5-coder:7b for BACKGROUND jobs (separate
from decide). config.yaml is BAKED → rebuilt image #2 for the model change.
brain/soar.py now BIND-MOUNTED (added ./brain:/app/brain to compose) → future
soar.py edits are restart-only, not a 13-min rebuild.
RESIDUAL/RECOMMENDED: decide is advisory (macro-veto demoted) yet still runs a
10-25s synchronous LLM call every cycle, worst-case ~25s grazes the timeout under
load. Robust next step = THROTTLE decide to ~1/min (restart-only now). Reversible:
revert config.yaml models to phi3:mini; del llm:decide_timeout_s; remove brain mount.

--------------------------------------------------------------------------------
cont. (2026-06-04 ~08:12) — DECIDE LLM THROTTLED → DISABLED (advisory, slow box)
--------------------------------------------------------------------------------
After the model switch, the throttle (1/min) still left residual 500s: a warm
real decide prompt runs 6-26s on this 6-CPU box (huge variance under load;
OLLAMA_NUM_PARALLEL=2 queues concurrent calls), routinely exceeding any timeout →
500 → cloud fallback. Chasing the timeout is futile. Since the verdict is ADVISORY
(macro-veto demoted, gates nothing), added a MASTER SWITCH `llm:decide_enabled`
(Redis, default "0"=OFF) in soar.py _decide. OFF → skip the local LLM entirely,
verdict=ML_ONLY, ZERO ollama calls/500s, loop ~5-6s/cycle (was ~33s with phi3
per-cycle). Throttle (`llm:decide_interval_s`=60) + timeout (`llm:decide_timeout_s`)
machinery preserved for re-enable on a faster box.
SIDE EFFECT (good): with verdict=ML_ONLY (llm_available=False), _act skips the
macro-veto branch entirely (advisory_count stops climbing) → straight to
process_signals. VERIFIED clean window: 0 brain /api/generate, 0 ollama_failure,
decisions every ~5-6s, 6 trades opened/30min, turbulence_index=1.119 (fix live,
no longer stuck 1.0). All Redis-tunable + restart-only (brain bind-mounted).

────────────────────────────────────────────────────────────────────────────
2026-06-04 (cont. 71) — PROFESSOR SESSION PARTIALLY REVERTED (owner request)
────────────────────────────────────────────────────────────────────────────
Owner: "revert all changes done by the professor session, bring bot to its
original state before professor" — with two constraints added mid-task:
(1) leave live/paper toggle + real-money config alone; (2) keep PROFESSOR_AUDIT.md
and the phase1 next_impl md.

REFUSED a git revert (surfaced the conflict): professor commit cf13bb1 = 359
files / 265,058 insertions, bundling ALL May16→Jun4 work into one commit (parent
ef82e0a dated 2026-05-16). git reset/revert would have destroyed ~3 weeks of kept
work (SL Capital Ladder, predict-all, launch_pad, kline corpus, cont.70b SL fix).

SURGICAL revert performed instead:
 • UNFROZE 10 self-mod beat tasks — removed _PHASE0_FROZEN_TASKS .pop() block from
   celery_app.py (bind-mounted). Verified live: 78 scheduled tasks, all 10 present
   (feature-governance-check, refresh-bayes-threshold, ga-evolve-params,
   dgm-code-rewrite, ai-scientist-hypotheses, update-pair-lists-from-decoder,
   metacog-daily-eval, decode-pending-misses, decode-pending-mismatches,
   evolve-strategy-pool). Deploy: docker compose restart celery_beat.
 • REMOVED professor code scaffolding — rm -rf brain/professor/ (incl. Phase-1
   ledger/backtest/metrics/xsmom + Phase-2.1 shadow), audit_snapshots/,
   tools/professor_metrics.sql. Removed the log_shadow hook in signals/engine.py
   (~L2542). Deploy: docker compose restart brain. Verified clean (no ImportError;
   online_learner/pending_entry/SL monitor all running).
 • KEPT: PROFESSOR_AUDIT.md, next_impl/phase1_decision_ledger_backtest.md.
 • LEVERAGE LEFT AT 5x (bot:leverage=5) — NOT reverted to 20x: owner's don't-touch
   constraint + 20x breaks the kept 5x-derived SL ladder + F-024 survival risk.
 • Live/paper toggle, execution/live.py, live-auth (verify_prod_auth.py,
   .env.bak.prelive, exchange/client.py auth), ControlPanel live/paper UI: untouched.

Net state: Phase-0 FREEZE undone (autonomy restored), Phase-1/2.1 instruments
removed, 5x leverage de-risk RETAINED. git tags pre-rebuild-baseline* unchanged.
No image rebuild (both files bind-mounted). py_compile OK on both edited files.

--------------------------------------------------------------------------------
## 2026-06-06 — VPS MIGRATION SESSION (new VPS: 10vCPU / 32GB RAM)
--------------------------------------------------------------------------------

### Changes Made This Session

**1. ETHERSCAN_API_KEY added**
- File: `.env` — added `ETHERSCAN_API_KEY=8F9J62987Q99YR1N2ZFDSISUZB9T3ZIHMP`

**2. BRAIN_STAGE updated**
- File: `.env` — `BRAIN_STAGE=1` → `BRAIN_STAGE=4` (bot is at Stage 4 / 10,641 trades)
- File: `config.yaml` — `evolution_stage: 1` → `evolution_stage: 4`

**3. Leverage de-risked**
- File: `config.yaml` — `leverage_min: 20` → `3`, `leverage_max: 20` → `5`

**4. Bear+short regime fixed (root cause of -$3,507 on 4,173 trades)**
- File: `signals/engine.py` line 279 — removed bear+short from regime_bonus (+15 bonus removed)
- File: `signals/engine.py` line 706 — demoted bear+short regime_score 100→20.0 (Redis-tunable via signals:bear_short_regime_score)
- File: `signals/multi_tf_cascade.py` line 416 — removed bear+short from cascade +10 confidence boost
- All three changes leave bull+long fully promoted. Bear+short de-ranked, not banned.

**5. Redis maxmemory upgraded**
- File: `docker-compose.yml` — `--maxmemory 768mb` → `--maxmemory 4gb`

**6. Docker resource limits upgraded (10vCPU/32GB utilisation)**
- brain: memory 2G→4G
- data_feed: memory 1G→2G
- celery_worker: memory 3G→5G, cpus 1.0→2.0, concurrency 3→4
- celery_worker_candlenet: memory 7000M→12G, cpus 1.5→2.5, CANDLENET_MAX_SAMPLES 200k
- celery_worker_cn_train: memory 6000M→14G, cpus 2.0→4.0, CANDLENET_MAX_SAMPLES 300k
- ollama: cpus 5.0→7.0, MAX_LOADED_MODELS 3, NUM_PARALLEL 3

**7. BINANCE_TESTNET bug fixed in mode switch (root cause of always-failing paper↔live switch)**
- File: `dashboard/api.py` line 528
- Bug: when switching to paper mode, watchdog payload contained `BINANCE_TESTNET=true`
  → brain restarted trying testnet endpoint with mainnet keys → crash loop → "persistently unstable"
- Fix: `"binance_testnet": "false"` always — TRADING_MODE controls paper/live, not BINANCE_TESTNET
- Redis: set `bot:mode = paper` (key was nil, causing inconsistent dashboard display)
- Dashboard container restarted to pick up fix

**8. Model permissions fixed**
- `sudo chown -R 999:999 /opt/trading-bot/models/` — fixed uid 1000 → uid 999 (container user)

**9. PROGRESS.md + MEMORY.md updated**
- All session changes documented here
- Claude Code Rules (all 17) saved to persistent memory

### Confirmed Working
- All 17 containers running ✓
- Bot running (bot:running=1) in paper mode ✓
- BINANCE_TESTNET=false ✓
- brain:stage=4, brain:paper_closed=10625 ✓
- Mode switch paper↔live: FIXED (testnet bug patched)

---

## CONT. 71 — VPS MIGRATION FIXES + LAUNCH PAD 50 SLOTS (2026-06-06)

**1. Ollama LLM re-enabled (new 32GB VPS)**
- Redis: `llm:decide_enabled = "1"` — was "0" on old 6-CPU VPS due to 6–26s timeouts
- docker-compose.yml already upgraded: cpus 7.0, MAX_LOADED_MODELS 3, NUM_PARALLEL 3

**2. Prediction gate disabled (both predictors broken)**
- XGBoost: feature shape mismatch — model trained on 32 features, live has 35
- Online predictor: sklearn version mismatch → "SGDRegressor not fitted" on new VPS
- Fix: `prediction:gate_enabled = "0"` + `prediction:gate_auto_arm = "0"` (disables auto-rearm)
- Signals now flow through without prediction gating until predictors are retrained

**3. Scanner fixed — 14 pairs → 200+ pairs**
- Root cause: `_DEFAULT_MIN_QUOTE_VOLUME_USD = $150M` rejected all pairs in bear market
- Fix 1: `scanner:min_quote_volume_usd = "1000000"` ($1M floor via Redis override)
- Fix 2: `scanner:min_marketcap_usd = "50000000"` ($50M mcap floor via Redis override)
- Fix 3: `scanner:max_active_pairs = "300"` via Redis override
- Code change: `scanner/main.py` lines 940–949 — added Redis override for `scanner:max_active_pairs`

**4. Launch-Pad expanded from 10 to 50 slots**
- Redis: `launchpad:depth = "50"` — was unset (defaulted to 10)
- Postgres: INSERT slots 11..50 into `launch_pad` table (40 new empty rows; total = 50)
- No code change needed — `store.get_depth()` reads Redis key; maintainer and dashboard API use it
- Dashboard header now shows: `Launch-Pad — On-Deck Buffer (N/50)`
- Maintainer will fill all 50 slots on next tick

### Confirmed Working
- All 17 containers running ✓
- Bot running (bot:running=1) in paper mode ✓
- BINANCE_TESTNET=false ✓
- brain:stage=4, brain:paper_closed=10641 ✓
- Ollama LLM: enabled (llm:decide_enabled=1) ✓
- Scanner: 200+ active pairs ✓
- Launch-Pad: 50 slots ✓

### Pending / Watch List
- Reddit API credentials still empty (REDDIT_CLIENT_ID/SECRET in .env)
- HTTPS/TLS not configured (no certbot)
- GCP VPC firewall: configure ports 22+80 only (currently open)
- strategy_id UUID mismatch (stage1_ofi_momentum, stage2_sentiment_ofi have no DB rows → trades log strategy_id=NULL)
- Binance API settings: whitelist VPS IP 35.194.221.181
- XGBoost predictor: needs retrain (32→35 features) before prediction gate can be re-enabled
- Online predictor: needs reset+retrain from closed trade history (sklearn version mismatch)
- HTF short guard still blocking many signals (3,058 rejections) — threshold tuning needed
- MPP systematically recommends open_short for long signals in current regime → -15% strength penalty

--------------------------------------------------------------------------------
## CONT. 73 — PRIORITY 1: REVIVE DEAD FEATURES (2026-06-07)
--------------------------------------------------------------------------------
Continues the audit's "5 Implementation Priorities". User chose: ALL of Priority 1.
Plan: next_impl/priority1_fix_dead_features.md

CONFIRMED dead (Rule 2/9/13): last 3000 signals — bid_ask_imbalance, exchange_netflow_z,
liq_nearest_above/below_pct all avg-abs 0.00000; pattern_cluster_id>=0 in 0/3000.

### Step 1 — liquidation features: FIXED + DEPLOYED + VERIFIED LIVE
Root cause: data/liquidation_levels.py `_fetch_coinalyze` hit `/v1/liquidations` → HTTP 404
(x30). The 404s drove reject_frac=1.0 → is_disabled() auto-disabled the WHOLE producer
(liq:disabled=1) → refresh_one short-circuited → liq_nearest_* never written (0-fill).
Self-perpetuating: even though the proxy path works, the provider-reject accounting killed it.
Fixes (all in data/liquidation_levels.py, bind-mounted to celery_worker):
 1. Correct endpoint `/v1/liquidation-history`, symbol `{base}USDT_PERP.A`; parse `l`/`s`
    (long/short liquidation USD) → 2 directional clusters (longs liq BELOW price, shorts
    ABOVE) at ±_atr_band. Real exchange data → cascade direction for majors.
 2. New `_atr_band()` helper (3× mean 1m range, 1% floor); `_fetch_proxy` refactored to reuse.
 3. Coinalyze gated to `_COINALYZE_PAIRS` majors whitelist (Redis-tunable `liq:coinalyze_pairs`)
    — free tier 429s above a few req/cycle; all other pairs use proxy (no call/reject).
 4. Deadlock accounting fix: provider failures (429/404/empty) are telemetry-only now
    (`liq:reject:{reason}`); deadlock numerator `liq:reject_count` counts only refresh_one
    PRODUCER failures (all_sources_failed). Proxy backstops all → reject_frac≈0, no re-disable.
Deploy: docker-compose.yml — added ./data/liquidation_levels.py + ./data/onchain_netflow.py
bind-mounts to celery_worker (data/ was image-baked; disk ~90% full → no rebuild). Process
reload via `docker restart trading-bot-celery_worker-1` (compose up -d no-ops on .py-only edits).
VERIFIED: refresh_all 300/300 ok, 4.55s, is_disabled=False, call=300/reject=0. BTC/ETH
src=coinalyze dir=long; alts src=proxy. liq_nearest_above/below_pct now NON-ZERO. Beat 300s, TTL 600s.

### Step 2 — exchange_netflow_z: BLOCKED ON DATA (left disabled, honest)
Confirmed live: cryptoquant http_401, glassnode http_401 (both paid-key), coinmetrics
community `400: metric FlowNetExUSD not supported` (PRO-only). Proxy derives alts from BTC
native z → also dead. ETHERSCAN key not wired into providers. NOT a pipeline bug — no free
source. Clearing netflow:disabled would re-fail+re-disable. Recommend separate scoped task
(paid feed OR custom Etherscan CEX-wallet netflow builder).

### Steps 3-4 — bid_ask_imbalance + pattern_cluster_id: PENDING (new producers needed)
 - bid_ask_imbalance: NO producer writes {pair}:bid_ask_imbalance (features.py:169 reads it).
   Derive from L1 book / {pair}:micro:ofi_l1 (micro_ws, cont.70 Track 2).
 - pattern_cluster_id: NO producer writes pattern:cluster_id:{pair} (features.py:162 reads it).
   Needs a clustering-assignment task.

### Step 3 — bid_ask_imbalance: FIXED + DEPLOYED + VERIFIED LIVE
Root cause: NO producer wrote {pair}:bid_ask_imbalance (features.py:169 reads it).
Fix (signals/microstructure.py): added true L1 top-of-book imbalance
(bb_q-ba_q)/(bb_q+ba_q) in compute_book_features — DISTINCT from the top-N `ofi`
(=micro:ofi_l1) — and write_micro now setex `{pair}:bid_ask_imbalance` (TTL 90s).
Both producers covered: micro_ws (primary, sub-second) via new
./signals/microstructure.py bind-mount; celery_worker_candlenet (REST fallback,
microstructure queue) already mounts ./signals. Deploy: compose up -d micro_ws +
restart candlenet worker. VERIFIED via prediction.features.live_features: bid_ask
BTC 0.63 / ETH 0.77 / BNB 0.42 (non-zero, varied, != ofi_l1). Was 0.0 on 3000 signals.

### CONSUME-SIDE end-to-end (Rule 4): live_features() now returns non-zero
bid_ask_imbalance + liq_nearest_above/below_pct (0.01 floor) + liq_cascade_prob
(0.63/0.98/0.76). netflow_z still 0.0 (Step 2 blocked, expected). NOTE: new signal
ROWS only persist on trade-open; bot was in turbulent/low-conf gating window
(brain healthy: OFI fresh, launchpad_funnel active, bot:running=1) so the DB-row
manifestation lags — but the builder is confirmed live.

### PRIORITY 1 NET (cont. 73): 3 of 5 dead features REVIVED + verified
(liq_nearest_above/below_pct, liq_cascade_prob, bid_ask_imbalance). 1 blocked-on-data
(exchange_netflow_z — no free source; needs paid feed or Etherscan wallet builder).
1 scoped as a focused ML build (pattern_cluster_id — clustering assign task).
Files: data/liquidation_levels.py, signals/microstructure.py, docker-compose.yml
(3 new bind-mounts). All bind-mounted, no image rebuild. Paper mode.

### Step 4 — pattern_cluster_id: FIXED + DEPLOYED + VERIFIED LIVE (cont. 73)
Root cause: embedding capture (capture_pattern_embeddings, every 1m) WAS running
(stream pattern:embeddings = 100k entries) BUT (a) no cluster model was ever fit
(models/pattern_clusters.pkl absent → predict_cluster always -1), and (b) nothing
wrote the consumer key pattern:cluster_id:{pair} (features.py:162 read it → always -1).
Fixes:
 1. Fit the model: ran pretrainer.pattern_cluster_train on the 100k live embeddings →
    HDBSCAN, 164-166 clusters, models/pattern_clusters.pkl (54MB).
 2. Assigner folded into pattern/live_capture.py capture_for_pair: after building the
    28-dim embedding it predict_cluster()s and setex pattern:cluster_id:{pair} +
    pattern:cluster_strength:{pair} (TTL 180s). Same embedding that feeds training →
    train/inference distributions identical (Phase-B design). Rule 12 counters
    pattern:assign:{total,noise,error}.
 3. clusterer.load() now mtime-cached (was re-reading 54MB pickle per call; 300×/min).
 4. New celery task train_pattern_clusters (default queue) + beat entry every 6h
    (crontab minute=17 hour=*/6) → keeps clusters fresh as structure drifts.
Deploy: ./pattern bind-mounted to celery_worker; celery_app.py edit → restart
celery_worker + celery_beat. VERIFIED: 237/300 pairs assigned; BTC=54 ETH=52 BNB=124
XRP=5 DOGE=108 (strength 1.0), SOL=-1 (noise, legit). live_features pattern_cluster_id
= 54.0/52.0 (was -1 on 0/3000). Retrain task ran live (166 clusters, re-saved 17:14).

### PRIORITY 1 FINAL (cont. 73): 4 of 5 dead features REVIVED + verified end-to-end
(liq_nearest_above/below_pct, liq_cascade_prob, bid_ask_imbalance, pattern_cluster_id).
1 blocked-on-data (exchange_netflow_z — no free provider). Files this session:
data/liquidation_levels.py, signals/microstructure.py, pattern/live_capture.py,
pattern/clusterer.py, celery_app.py, docker-compose.yml (4 new bind-mounts), +
pretrainer/pattern_cluster_train.py run once. All bind-mounted, no rebuild. Paper mode.

### DASHBOARD — show all ML/RL/pretraining models (cont. 73, user request)
The /models endpoint (dashboard/api.py) covered 17 models but omitted several that
exist on disk / as training jobs. Added 6 rows (now 23 total): CandleNet 30m + 1h
(were trained, not surfaced), Pattern Clusters (HDBSCAN — NEW Step 4, shows
n_clusters/assigned/captured from pattern:cluster_train:last + pattern:assign:*),
Online Predictor (SGD, honest stale/fitted=False), Predict-All XGB + Kline, Shadow
Ablation, Chronos-Bolt (pretrained foundation). MLModelsPanel.tsx renders rows
GENERICALLY from /models (30s poll) → NO frontend rebuild needed. Deploy: dashboard/
bind-mounted → docker restart trading-bot-dashboard-1. VERIFIED: models_status()
returns 23 rows incl. Pattern Clusters active (166 clusters, assigned 2281/4829).

--------------------------------------------------------------------------------
## CONT. 74 — PRIORITY 2 STEP A: OI + LONG/SHORT + TAKER PRODUCER (2026-06-07)
--------------------------------------------------------------------------------
Continues the audit's "5 Implementation Priorities", item 2 (the strongest
crypto-native signals the bot had ZERO of). Plan: next_impl/priority2_oi_ls_taker.md.
Rule 17 staged: A=producer (additive, done), B=feature wiring (Rule 14, approval),
C=engine gates (Rule 14, approval).

CONFIRMED gap (Rule 2/9): get_open_interest (client.py:297) is point-in-time, used
only by scanner composite; ml/criteria_weights open_interest_score also point-in-time.
NO producer wrote OI-velocity / LS / taker as features. coinglass_liq_refresh uses
Coinglass API (no Binance /futures/data overlap). python-binance HAS the 4 methods.

DISCONFIRMED the testnet risk (Rule 9): /futures/data endpoints are production-only;
probed live — BINANCE_TESTNET=False and all 3 return real data. Bonus: OI-hist returns
sumOpenInterest + sumOpenInterestValue, so price=value/oi is derivable from the SAME
call → free oi_price_div, and the 30-sample series gives z-scores INLINE (no Redis
rolling window, unlike netflow).

### Step A — PRODUCER: BUILT + DEPLOYED + VERIFIED LIVE (additive, no behaviour change)
New data/oi_ls_taker.py (mirrors onchain_netflow template): is_disabled() deadlock
kill-switch (oils:disabled, 80%/50-call), _bump_call/_bump_reject (Rule 12 counters),
refresh_one (4 isolated calls: OI hist, global LS, top LS, taker), refresh_all
(top-N active pairs, oils:max_pairs default 50, ThreadPoolExecutor max_workers=8 —
I/O-bound, ceiling is fapi weight not the 10 vCPU). Writes 9 keys/pair (TTL 600s):
oi_now, oi_change_5m, oi_change_z, oi_price_div(=sign Δoi·sign Δprice, Gate 3),
ls_global_ratio, ls_top_ratio, ls_crowd_z(Gate 4), taker_ratio, taker_ratio_z.
+ get_oi_bonus/get_ls_bonus consumer helpers (for Step C, 0-safe/disabled-safe).
3 thin weight-tracked client wrappers in exchange/client.py (get_open_interest_hist,
get_longshort_ratio[top], get_taker_ratio). redis_keys.py: OI/LS/taker block.
celery_app.py: oi_ls_taker_refresh_task (default queue) + beat 'oi-ls-taker-refresh'
every 300s.

DEPLOY (disk ~90% → no rebuild): docker-compose.yml — bind-mounted data/oi_ls_taker.py
+ exchange/client.py + redis_keys.py into celery_worker (both baked-old: verified
HAS_OI_NOW/HAS_OI_HIST False pre-mount). `compose up -d celery_worker` (recreate, mounts).
Restarted celery_beat to reload the schedule (celery_app.py already mounted line 508;
IN_BEAT_SCHEDULE False→True).

VERIFIED LIVE: refresh_all 50/50 ok, 0 failed, 5.71s. oils:call_count=200 (50×4),
reject_count=0, disabled unset. Keys non-zero/varied/coherent: BTC oi_price_div=1
(conviction) ls_global=1.97 ls_crowd_z=-1.52 taker=1.31; ETH oi_price_div=-1
(divergence) taker=0.94; SOL ls_global=3.44 (crowded-long retail). Beat fires every 300s.

### Steps B + C — SCOPED, NEED APPROVAL (Rule 14 shadow gate)
B: append oi_change_z, oi_price_div, ls_crowd_z, taker_ratio_z to FEATURE_COLUMNS
(0-fill historical like cvd_z/ofi_l1; online learner gets live). FORCES XGB retrain
(fold into the pending 32→35 retrain). C: wire get_oi_bonus/get_ls_bonus into
engine accept_or_reject as SOFT toggleable nudges (Gate 3 + Gate 4). Both → 50 shadow
cycles before trusting PnL.

Files cont.74: data/oi_ls_taker.py(new), exchange/client.py, redis_keys.py,
celery_app.py, docker-compose.yml(3 new mounts). All bind-mounted, no rebuild. Paper mode.

### CONT. 74 (cont.) — PRIORITY 2 STEPS B + C: FEATURE WIRING + ENGINE GATES (2026-06-07)
User approved "B then C". Both DEPLOYED + VERIFIED LIVE (paper, Rule 14 shadow watch).

STEP B — feature wiring. Appended 4 cols to prediction/features.py FEATURE_COLUMNS
(35→39): oi_change_z, oi_price_div, ls_crowd_z, taker_ratio_z. Read in live_features
via INLINE key literals (f"{pair}:oi_change_z" …) — NOT redis_keys constants — same
pattern as ofi_l1/cvd_z, so features.py needs no redis_keys mount in every consumer.
SAFE width change (verified at source): xgb_predictor.predict (line 102-122) goes
DORMANT on a feature_count mismatch until retrain (already dormant, gate off);
online_predictor._ensure_bundle (122-144) DISCARDS+recreates the river bundle at the
new width and relearns online. No crash path. (Confirmed: candle_online_train_task
still succeeds post-recreate.)

STEP C — engine gates. signals/engine.py accept_or_reject now folds
oils_bonus = get_oi_bonus + get_ls_bonus (data/oi_ls_taker.py) into direction_conf,
beside netflow/cascade/qlib. Gate 3 (OI×price div): div=±1 & |oi_change_z|≥1.0 →
±4/±6 aligned-with-move. Gate 4 (LS crowd): |ls_crowd_z|≥1.5 → penalise joining the
crowded top-trader side, reward fading. SOFT (gate-not-kill), Redis-toggleable inside
the helpers (oils:oi_gate_enabled / oils:ls_gate_enabled, default ON), 0 on cold-start/
disabled. Verified firing on a synthetic pair: OI +6/-6, LS -6/+6; toggle-off→0.

DEPLOY: bind-mounted prediction/features.py into brain + celery_worker +
celery_worker_candlenet; data/oi_ls_taker.py + redis_keys.py into brain (engine
consumer). signals/ + exchange/ already dir-mounted in brain. Recreated brain +
2 workers. Brain healthy (paper, no import errors, processing signals).

### CRITICAL FINDING (Rule 13) — SYSTEMIC CELERY BACKLOG starves scheduled producers
While verifying Step A's beat, found the async layer is badly congested:
  default=10991, predict_all=7384, microstructure=6398, candlenet=605 (cn_train/
  airllm/llm/celery/web_intel=0). Consequence: beat-scheduled producers land at the
  TAIL and effectively never run. NETFLOW (Priority 1's revived feature) is ~29h STALE
  (netflow:updated_at 1780753485 vs now ~1780858000) for this exact reason. My Step A
  "verified" earlier passed only because I called refresh_all() DIRECTLY; the SCHEDULED
  task was starved on default.
FIX for Priority 2 (scoped): routed oi_ls_taker_refresh_task to the idle dedicated
cn_train queue (exact task_routes entry wins over celery_app.* catch-all) + mounted the
producer/client/redis_keys into celery_worker_cn_train. VERIFIED: scheduled task now
received+succeeds in 5.5s, 50/50, keys fresh. cn_train retrains are infrequent
(daily/2x-week) so the 5-min beat runs promptly nearly always.
BROADER backlog (default/predict_all/microstructure 6-11k; netflow + likely many other
producers stale) is a PRE-EXISTING systemic problem, NOT from this work, and is bigger
than Priority 2 — recommend as the next priority (raise worker concurrency / purge stale
default backlog / re-home light producers off default). Left netflow as-is (separate fix).

PRIORITY 2 NET (cont. 74): Steps A+B+C all BUILT + DEPLOYED + VERIFIED LIVE. Producer
fresh on cn_train; 4 new features in the vector + online learner; OI/LS engine gates
soft+toggleable. Files: data/oi_ls_taker.py(new), exchange/client.py, redis_keys.py,
prediction/features.py, signals/engine.py, celery_app.py, docker-compose.yml(mounts).
All bind-mounted, no rebuild. Paper mode. Rule 14: watch ~50 paper cycles before live.

--------------------------------------------------------------------------------
## CONT. 74 — CELERY BACKLOG: FIND & FIX ALL BOTTLENECKS (2026-06-07)
--------------------------------------------------------------------------------
Plan: next_impl/celery_backlog_fix.md. Triggered by the Priority-2 finding that
scheduled producers were starved. RESOLVED: default 10931→0, predict_all 7428→0,
microstructure 6436→0, candlenet 610→0 — all FLAT at 0 over 90s steady-state.

DIAGNOSIS (Rule 2/9/13, measured live):
 - default packed with ~15 idempotent producers, each ~159 stale copies (one per
   missed beat cycle). No `expires` → unbounded pileup. Worker alive but OUTRUN.
 - THE bottleneck: launch_pad_maintain_task MEASURED runtime 464-622s (10+ min) but
   scheduled every 20s → ~30× oversubscription; overlapping 10-min runs permanently
   pinned the candlenet worker's 4 slots, starving latency-critical prediction_refresh
   / microstructure_scan / candlenet_infer (forecasts had been expiring ~96% per cont.65).
 - coinglass_liq_refresh = 146 API calls every 60s (heavy slot-eater).
 - candlenet queue = 609 stale candlenet_infer_all (heavy 12-19s inference) clogging.

FIXES (all bind-mounted, no rebuild, paper mode):
 1. celery_app.py — GLOBAL auto-`expires` injected after beat_schedule: every periodic
    entry gets options.expires = clamp(2.5×cadence, 45s, 3600s). Celery now DROPS any
    copy not consumed in time → every queue self-caps regardless of load. VERIFIED:
    13 stale candlenet tasks dropped post-deploy. THE durable structural fix.
 2. celery_app.py — launch_pad_maintain 20s→300s (15× load cut; the decisive fix).
    coinglass_liq_refresh 60s→300s (5× fewer 146-call bursts).
 3. docker-compose.yml — celery_worker conc 4→6, celery_worker_candlenet 2→4 (10 vCPU).
 4. One-time PURGE of default/predict_all/microstructure/candlenet (~25k stale
    idempotent refresh tasks; next beat re-enqueues fresh).

DEPLOY GOTCHA (important, Rule 9): single-file bind mounts + atomic-rename edits
(Edit tool writes temp+rename → NEW inode) leave the CONTAINER pinned to the OLD inode.
`docker restart` does NOT re-resolve it — must `up -d --force-recreate`. Confirmed via
inode mismatch (container 297939 vs host 297940). Force-recreated all celery svcs.

CORRECTIONS to earlier framing (Rule 9 — disconfirmed):
 - netflow's ~29h staleness is NOT a backlog symptom: netflow:disabled=1 (Priority 1
   left it OFF — no free data provider). Independent of the queue. Left disabled.
RESULT (steady-state, verified): candlenet 5m forecast TTL fresh (was expiring 96%),
cvd/ofi fresh, oils refreshing on cn_train, all containers up.

OLLAMA (user ask — 32GB): OLLAMA_MAX_LOADED_MODELS 3→4, OLLAMA_KEEP_ALIVE 5m→30m
(kill the ~18s reload thrash; 4 small models ~9-11GB fit easily). No hard mem_limit
(kept — a cgroup ceiling OOM-kills mid-inference). Model rec: LLM heavy-lift already
cloud-primary (Llama-3.3-70B); local is fallback+embeddings; CPU-only → no 32B local.

BUGS FOUND (not backlog blockers; fail fast — flagged for follow-up):
 - update_pattern_registry_task raises NameError("name '_log' is not defined") every
   run → pattern registry never updates. Pre-existing, unrelated to this work.

Files: celery_app.py (expires loop + 2 cadences + cont.74 P2 route), docker-compose.yml
(2 concurrencies + 2 ollama env). PROGRESS.md is now 925KB/12.9k lines — NEEDS ARCHIVING
(exceeds the 256KB Read limit; that's the "not accessible" the owner hit).

### CONT. 74 — 3 FOLLOW-UP FIXES (2026-06-07)
1. update_pattern_registry_task NameError FIXED: `_log` referenced ×3 (lines 4380/4390/
   4392) but never defined → NameError every run (pattern registry never updated).
   Added `import structlog as _sl; _log=_sl.get_logger()` (codebase per-task pattern).
   VERIFIED: task now succeeds ({'status':'ok',scanned/updated/failed}), no NameError.
   Deploy: force-recreate celery_worker_candlenet (predict_all queue runs it).
2. PROGRESS.md ARCHIVED: was 925KB/12.9k lines → exceeded the 256KB Read limit (the
   owner's "not accessible"). Full file copied → PROGRESS_ARCHIVE_2026-06-07.md (nothing
   lost); live PROGRESS.md rebuilt lean = top recent block (1-426) + recent tail
   (cont.70→74) = 1261 lines/87KB, Read-accessible. Old cont.1–69 history lives in the
   archive (grep it). APPEND new sessions to the END here.
3. OLLAMA (32GB) confirmed live: OLLAMA_MAX_LOADED_MODELS 3→4, OLLAMA_KEEP_ALIVE 5m→30m,
   NUM_PARALLEL=3 (verified in container env). No hard mem_limit (kept).
   DISK CORRECTION (Rule 9): df shows 296G / 26% used / 211G FREE — the repeated
   "disk ~90% full → no rebuild" comments across compose are STALE (old VPS). Image
   rebuilds + model pulls are actually fine now. (qwen2.5:14b local quality bump
   available on request — needs code wiring to be used; not pulled.)

### CONT. 74 — qwen2.5:14b WIRED + OLD-VPS STALE DATA FIXED (2026-06-07)
1. LOCAL LLM QUALITY BUMP: pulled qwen2.5:14b-instruct (9.0GB) and wired it as the
   research/heavy-lift local model via .env OLLAMA_RESEARCH_MODEL (qwen2.5-coder:7b →
   qwen2.5:14b-instruct). chat_ollama default (300s timeout, ≤384 tok) reads it;
   researcher.py uses the default → gets the bump. SAFE: decide() uses llama3.2:3b
   explicitly (untouched); llm_alpha_dsl uses its own _PROPOSER/_VALIDATOR models with
   tight 60-120s timeouts (does NOT read OLLAMA_RESEARCH_MODEL) → not affected. Research
   is cloud-PRIMARY (Llama-3.3-70B); 14b is the rare local fallback. VERIFIED end-to-end:
   chat_ollama resolved qwen2.5:14b-instruct, replied 'WIRED OK' in 72.6s cold (warm
   after, KEEP_ALIVE=30m). Recreated celery_worker for the .env change. Revert: set
   .env OLLAMA_RESEARCH_MODEL back. Disk after pull: 29% used / 203G free.
2. OLD-VPS STALE DATA FIXED: the "disk ~90% full / 96% full / no image rebuild / once
   disk is healthy / cold rebuild risked ENOSPC" comments throughout docker-compose.yml
   were STALE from the old 8vCPU/20GB box — df shows 296G / 26-29% used / 200G+ free.
   sed-corrected all of them to reflect reality (rebuilds + pulls are fine now; mounts
   kept for fast iteration, not disk pressure). grep confirms 0 stale disk claims remain;
   compose config valid; only COMMENT lines changed (no functional diff). No stale
   hardware assumptions found in .py/config.yaml/.env (grep clean). Correct "10 vCPU /
   32GB new box" upgrade comments left as-is (accurate).

### CONT. 74 — NO-TRADE DEADLOCK ROOT-CAUSED & FIXED (2026-06-07)
SYMPTOM: zero trades for ~4.5h (last trade 15:26 UTC). ROOT CAUSE (Rule 2/9/10/13):
adaptive-threshold deadlock. The Bayesian accept gate get_adaptive_min_strength()
(engine.py:1500) returned bayes_threshold:t_high=50 (util-calib pushing →55), but
turbulent-regime signals max ~42 post-MPP (no +15 bull-long bonus; MPP disagreement
knocks longs ~47→42). strength(42) < 50 → ALL rejected signal_too_weak (live: max
new_strength 41.98 vs 50; 73 signals/8min all rejected) → no trades → no new win/loss
data → threshold stays pinned at 50. Self-starving loop. (EV-override can't rescue:
model p_win~0.33.) NOT caused by my edits — drought began 15:26, before Priority 2 (19:00).
DISCONFIRMED: regime legitimately turbulent (BTC +1.5% but choppy); T_high vestigial
elsewhere; the GA floor (~30) was being overridden by the adaptive 50.
FIX (deadlock breaker, signals/bayes_threshold.py get_adaptive_min_strength): a
Redis-tunable consumer-side CAP `bayes:t_high_cap` (fallback _T_HIGH_MAX=60) clamps the
EFFECTIVE accept threshold WITHOUT fighting the 5-min refresh — the refresh may still
compute 50, but the engine never sees a gate above the cap. Set bayes:t_high_cap=40 →
strongest ~42 signals trade (MPP-confirmed cluster), weak ~34 cluster still filtered.
Deploy: signals/ bind-mounted → force-recreate brain. VERIFIED: get_adaptive_min_strength
returns 40.0 (was 50); first trade after drought = PIPPINUSDT long 20:09:24; multiple
trade_open in 90s. Tunable/reversible LIVE: redis-cli set bayes:t_high_cap 60 (or DEL)
to restore. Rule 14: paper mode — watch the new trades' win-rate before trusting; raise
the cap if the ~40-42 signals underperform. FOLLOW-UP: MPP over-penalty (longs ~47→42)
+ the deadlock-breaker belongs in code (auto-lower t_high after N hours no-trade).

--------------------------------------------------------------------------------
## CONT. 74 — KURAMOTO ENSEMBLE-COHERENCE REPLACES hist_acc (2026-06-07)
--------------------------------------------------------------------------------
Plan: next_impl/signal-strength-research-rewrite.md Change 7 SUPERSEDED. The earlier
Bayesian Beta-Binomial shrinkage fix for hist_acc was replaced by a forward-looking
Kuramoto synchronization order parameter (decision locked this session).

WHY: hist_acc (past-trades win-rate) is BACKWARD-LOOKING + circular — trade outcomes
feed back into the score that picks trades (same self-reinforcing family that froze the
adaptive threshold, cont.74 no-trade deadlock). Replaced with a measure of how strongly
the bot's INDEPENDENT forecasters AGREE on this direction RIGHT NOW.

IMPLEMENTATION (signals/engine.py:840-907, weight table :959):
 - Each direction-aligned sub-score s_j in [0,100] (100=for trade, 0=against, 50=neutral)
   maps to a phase on the directional axis: phi_j = (s_j-50)/50 * (pi/2).
 - Order parameter directional projection coh_dir = Sum(w_j*sin phi_j)/Sum(w_j) in [-1,1]
   = net coherent conviction. coherence_score = 50*(1+coh_dir) in [0,100].
 - Pure coherence r = |Sum w_j e^{i phi_j}|/Sum w_j in [0,1] logged to {pair}:coherence_r.
 - Inputs (w_j = reliability): ofi 0.25, regime 0.22, tft 0.15, candlenet 0.12,
   patchtst 0.08, sentiment 0.04. Excludes vpin (non-directional) + itself (no circular).
 - NO past-PnL input => cannot deadlock or overfit thin samples.
 - Weight table: ("coherence", hist_score, 0.10) — slot that was hist_acc.

TOGGLE + SHADOW A/B (Rule 14): signals:coherence_enabled (unset/!=0 => coherence is LIVE
default; =0 => legacy win-rate). Legacy Beta-Binomial win-rate STILL computed each cycle
from brain:directional_accuracy:{pair} for the A/B; divergences (>=1.0) logged to
signals:strength_shadow:{pair} + counter signals:strength_shadow:count.

VERIFIED LIVE (Rule 2/9/10/13, measured this session):
 - py_compile OK. Host inode 297932 == container inode 297932 (no inode-pin issue; brain
   created 20:26 AFTER the 20:24 edit => running new code). No force-recreate needed.
 - REACHABILITY PROVEN: signals:strength_shadow:count = 4829; *:coherence_r populated
   across many pairs (e.g. MEMEUSDT 0.83, SQDUSDT 0.81, OPNUSDT 0.95).
 - Sample shadow: "coherence=63.93 legacy_winrate=47.62 r=0.83 dir=long" — both paths
   compute correctly, meaningful divergence.
 - Legacy fallback reads REAL data (126 brain:directional_accuracy:* keys), not silent 50.
 - coherence_enabled unset => coherence is the LIVE default (confirmed).

WHAT'S LEFT:
 1. Rule 14 shadow EVALUATION: 4829 divergences logged but no analysis yet of whether
    coherence would have out-traded legacy. Shadow currently logs divergence TEXT only —
    does not track realized outcome per branch. Needs an offline join (shadow record ->
    subsequent trade PnL) before fully trusting coherence over legacy. ~50 cycle gate.
 2. PHASE 2 (deferred, not started): transfer-entropy per-pair reliability gate —
    effective = coherence * f(TE). Trust the ensemble consensus only when the pair is
    actually predictable (TE>0); discount toward neutral when TE~0 (pure noise). Roll out
    ONLY after Phase 1 validates in shadow (Rule 14 — prove reliable piece first).

Files: signals/engine.py (Kuramoto block + weight slot). Bind-mounted, paper mode.

### CONT. 74 — KURAMOTO SHADOW A/B ON THE DASHBOARD (2026-06-08)
Surfaced the coherence-vs-legacy shadow data (already logging live) on the dashboard so
the owner can watch it. Mirrors the existing _EV_OVERRIDE_PANEL_HTML pattern exactly.

ADDED to dashboard/api.py (bind-mounted ./dashboard, restart-only — no rebuild):
 - GET  /signals/coherence/shadow  (auth) — reads signals:coherence_enabled, the
   running signals:strength_shadow:count, scans signals:strength_shadow:{pair} (300s
   TTL), parses "coherence=/legacy_winrate=/r=/dir=", returns live source + aggregate
   (avg coherence, avg legacy, avg |Δ|, coh>leg / leg>coh split, avg r) + per-pair rows
   sorted by |Δ|.
 - POST /signals/coherence/toggle  (auth) — flips signals:coherence_enabled (1=Kuramoto
   coherence default, 0=legacy win-rate). Reversible LIVE; A/B keeps logging either way.
 - GET  /signals/coherence/panel   (public HTML) — self-contained viewer: login → live
   badge, stat tiles, divergence table (green Δ where coherence>legacy), COHERENCE/LEGACY
   switch buttons, 5s auto-refresh. Rule 12: empty state shows an explicit "keys expired
   (300s TTL)" message, never a blank panel.

VERIFIED LIVE (Rule 2/9/10): py_compile OK; dashboard restarted (dir bind-mount, uvicorn
reloaded); all 3 routes REGISTERED; panel serves HTTP 200 w/ correct title; data route
enforces auth (401 w/o token); direct coroutine call returned REAL data —
coherence_live=True, divergence_total=5174 (was 4829 at first read 8h earlier ⇒ brain
actively logging), 7 live pairs, avg_coherence 60.51 vs avg_legacy 50.07, all 7
coherence>legacy, avg_r 0.882. nginx.conf:36 `location /signals/` proxies it; external
fetch via nginx:80 returned HTTP 200. URL: http://35.194.221.181/signals/coherence/panel

CAVEAT (honest, Rule 4): the panel shows DIVERGENCE, not yet a win/loss scoreboard. The
legacy win-rate clusters at ~50 (Beta-Binomial prior on thin samples = uninformative),
so coherence mostly diverges UPWARD from an uninformed baseline — expected, and the
reason hist_acc was replaced. Whether coherence picks BETTER trades still needs the
shadow-record→realized-PnL join (the outstanding Rule-14 evaluator, not built yet).

Files: dashboard/api.py (3 endpoints + panel HTML). Bind-mounted, paper mode.

### CONT. 74 — COHERENCE-vs-LEGACY PREDICTIVENESS SCOREBOARD (2026-06-08)
Built the Rule-14 evaluator that turns the shadow A/B from a divergence log into a real
predictiveness scoreboard: of the closed trades that snapshotted BOTH scores, which one
(Kuramoto coherence vs legacy past-trades win-rate) actually predicts winners?

WHY a forward-collector (Rule 13, honest): signals_at_entry was NULL on ALL existing
trades — the component scores were NEVER persisted, and the live shadow keys are 300s TTL.
Cannot backtest from data that doesn't exist. So persist both scores at trade-open from
now on, join to realized net_pnl at close, accumulate going forward.

PRODUCER (persist both scores per trade):
 - signals/engine.py: strength-return dict now carries "shadow_scores"
   {coherence_score, legacy_winrate_score, coherence_live, coherence_r} — all already in
   scope from the Kuramoto block. Both open_trade dicts (limit + market paths) pass
   signals_at_entry = json.dumps(signal["shadow_scores"]).
 - memory/write.py: write_trade_open INSERT adds the signals_at_entry column + value
   (28 cols == 28 placeholders, verified BALANCED).

CONSUMER (the scoreboard):
 - signals/coherence_eval.py (NEW): state() queries closed trades whose signals_at_entry
   carries both scores, then per source computes (a) win-rate + avg net_pnl by score
   bucket [0-50,50-60,60-70,70-80,80-100], (b) Spearman rank IC vs net_pnl
   (dependency-free: avg-rank + Pearson-on-ranks), (c) tercile win-rate spread. Verdict =
   higher signed Spearman IC. Honest caveat surfaced: coherence is LIVE (gates trades),
   legacy is shadow → ranks the two on TAKEN trades, not all candidates (selection bias);
   n<40 flagged low-confidence. Clean "collecting" state when no snapshotted trades yet.
 - dashboard/api.py: GET /signals/coherence/scoreboard (auth) + a scoreboard section
   wired into the coherence panel (buckets table for each source, IC stat tiles, verdict
   badge, 5s refresh alongside the live divergence table).

DEPLOY GOTCHA (Rule 10, caught pre-ship): ./signals was NOT mounted on the dashboard —
the new coherence_eval.py was invisible there (dashboard imports signals.* from the BAKED
image). Added `- ./signals:/app/signals` to dashboard volumes (mirrors brain); compose
valid; force-recreated dashboard. File now VISIBLE in container.

VERIFIED (Rule 2/9/10/13): all 4 files py_compile OK; brain restarted clean (restarts=0,
scoring loop flowing, no errors from the engine edits); evaluator imports + runs in
dashboard → status "collecting" n_trades=0 (correct — no post-deploy trades closed yet);
/signals/coherence/scoreboard registered + 401 w/o auth; panel serves 200 + scoreboard
wired; ROLLBACK INSERT round-trip proved the column accepts the jsonb and the evaluator's
exact operators (->>'coherence_score'::float, ? 'coherence_score') read back coh=63.9/
leg=47.6/has_key=t with zero pollution.

WHAT'S LEFT: pure data accumulation — the scoreboard shows "collecting" until trades
opened after this deploy close (paper mode). No code gap. PHASE 2 (TE reliability gate)
still deferred until this scoreboard shows coherence's IC holding up over ~40+ trades.

Files: signals/engine.py, signals/coherence_eval.py (new), memory/write.py,
dashboard/api.py, docker-compose.yml (dashboard ./signals mount). Bind-mounted, paper mode.

### CONT. 74 — THROUGHPUT: FULL UNIVERSE + 100-DEEP LAUNCH-PAD + 5-MIN RE-RANK (2026-06-08)
Owner asked to widen the funnel: scan all symbols, deeper launch-pad, faster re-rank.
All three are LIVE Redis knobs (no code change, instantly reversible).

CHANGES (redis):
 - scanner:max_active_pairs 300 -> 531 (full universe; $100k quote-vol floor still ranks
   + filters → settled at 443 active of 531, the rest below the liquidity floor).
 - launchpad:depth 20 -> 100 (LAUNCHPAD_DEPTH; store.py:71).
 - scanner:rerank_interval_minutes (unset→default 20) -> 5 (scanner/main.py:1056,
   clamp [5,180]). NOTE: live cadence was already 20min in categorised mode, NOT the
   legacy 8h (8h only applies when scanner:categorised_mode_disabled=1) — earlier "8h"
   framing corrected (Rule 9).

VERIFIED (Rule 2/9/10/14): restarted scanner to apply immediately. active_pairs 300→443
within ~20s; fapi:ban_status=ok throughout (no rate-limit hit — the expensive klines
stages are top-N≈50 bounded, so universe size barely moves per-scan API cost; only
frequency does, and 5min held fine). Celery latency queues (predict_all/microstructure/
candlenet) all 0 — no cont.74 backlog regression (default=295, self-capped by the
expires injection). brain launchpad_funnel_active still n_pairs≈16.

OPEN NUANCE (flagged to owner): launchpad:depth=100 is a CEILING, not a floor. The funnel
fills slots only with pairs passing the launch-pad "green momentum" qualifier (~16 qualify
now). To actually evaluate ~100/cycle either (a) loosen the green qualifier, or (b) set
launchpad:enabled=0 to evaluate the full 443 active set each cycle (full-universe mode).
Left as owner decision — not changed.

REVERT: redis SET scanner:max_active_pairs 300 / launchpad:depth 20 / DEL
scanner:rerank_interval_minutes. Paper mode. Watch fapi:ban_status + cycle latency as the
5-min cadence + 100-depth ramp over the next hours.

### CONT. 74 — GATES→META-LABELING: PLAN + PHASE 0 CAPTURE (2026-06-08)
Owner approved replacing the veto gauntlet with the 3-layer meta-labeling decision
architecture (side → calibrated P(profit) → Kelly size). Plan-of-record written; Phase 0
(rich data capture) built. NO decision logic changed yet — shadow-first (Rule 14).

PLAN: next_impl/gates_to_metalabeling_redesign.md — full staged rollout, per-gate
principled map (conformal abstention / e-values / log-opinion-pool / BOCPD / Hawkes / TE),
what stays HARD (liquidity floor, max position, daily-loss, max-open — risk constraints),
altitude rule (physics→features, decision-theory→decisions), guardrails + toggles.

DATA SUBSTRATE (verified, Rule 13): 11,345 closed trades carry net_pnl (label); 11,194
also carry feature_vector; recent 200 closed have 45-52 feature keys (avg 48, RICH —
enough to train Layer-2 v1 NOW, not weeks out). GAP: feature_vector lacks the engine's 8
sub-scores (has_any_subscore=f) — the most decision-relevant inputs. Selection bias noted:
labels are from gauntlet-allowed trades (valid for meta-labeling, which refines a primary
signal; cannot judge killed trades — revisit with rejected-candidate counterfactuals).

PHASE 0 BUILT (signals/engine.py decision snapshot → signals_at_entry): extended the
shadow_scores dict (already persisted per trade, cont.74) to also capture the full
component vector at decision time — ofi/regime/tft/candlenet/patchtst/vpin/sent_score +
composite + direction_conf, alongside the coherence/legacy A/B. Additive only; starts the
labeled-data clock for the richer model. All vars confirmed in-scope at the return; brain
restarted clean (restarts=0, scoring loop healthy, no errors). New keys populate on next
trade open (persistence path proven by the rollback INSERT test last turn).

NEXT (own session): Phase 1 — train meta_label.py P(profit) on the 11k history, calibrate,
run in SHADOW with a /signals/metalabel/scoreboard (mirror the coherence scoreboard),
measure IC vs realized PnL before any gate change.

Files: signals/engine.py (snapshot extended), next_impl/gates_to_metalabeling_redesign.md
(new plan), PROGRESS.md. Bind-mounted, paper mode.

### CONT. 74 — DEAD FORECASTERS ROOT-CAUSED: data_feed ZOMBIE (2026-06-08)
Owner asked to fix the patchtst producer (LONGSEQ_FORECAST 0/444) and tft coverage
(PRICE_FORECAST 43/444, 1h-only). Root-caused, NOT a model/governance problem.

ROOT CAUSE (Rule 2/9/10/13): the data_feed container was a ZOMBIE. Last log line
2026-06-07 16:04:17 — ~13h of silence after a flood of "redis:6379 Name or service not
known → Connection refused". When redis got a new IP during the cont.74 force-recreates,
data_feed's cached connection broke and its asyncio loop DIED — but the process stayed up,
so Docker reported "Up 35h" (healthy). The dead loop = no patchtst poll, no candle poll
(CANDLES len=0), no GNN/TE refresh. Disconfirmed two wrong hypotheses first: F20 governance
(active=True) and per-call model reload (both models mtime-CACHE _load(); not the cause).

FIX 1 (the producer): docker compose restart data_feed → loop reconnected, 0 redis errors,
heartbeat resumed. VERIFIED: patchtst_polled ok=23/30, longseq_forecast keys 0→24. patchtst
REVIVED. (Direct get_longsequence_forecast(BTCUSDT) also returns + writes correctly.)

FIX 2 (coverage, data/feed.py _poll_patchtst_forecasts_and_log): measured cost is ~7-9s/
pair CPU inference (30 pairs = 211s of the 300s cycle) — the BINDING constraint. Bumping
batch would overlap polls (cont.74 backlog redux). Replaced the arbitrary smembers[:30]
with a ROTATING stable-ordered window (patchtst:poll_offset advances each cycle) + tunable
patchtst:poll_batch (default 30, clamp 5-60) → coverage now SPREADS across the active set
over the 30min TTL (~180 pairs warm) instead of re-warming the same 30. Deployed, data_feed
healthy.

NOT DONE (honest, needs decisions):
 - FULL-universe (444) patchtst + MULTI-TF tft coverage is blocked by ~9s/pair CPU cost.
   Real fix = BATCHED inference (stack N pairs into one forward pass) — a refactor of
   ml/patchtst.py + ml/tft.py. Proposed, not built.
 - tft "all timeframes": nothing CONSUMES non-1h tft today (only engine.py:57, hardcoded
   "1h"). Producing all TFs would burn CPU for unread data. Needs consumer wiring (blend
   multi-TF into tft_score = a decision change, Rule 14) before producing them. Proposed.
 - DURABILITY LANDMINE: data_feed does NOT auto-reconnect when redis's IP changes — every
   redis recreate re-zombies it (silent, Docker shows healthy). The real fix is a
   reconnecting redis client / resolver in data_feed (mirrors the nginx resolver fix).
   HIGH priority follow-up — this will recur on the next redis restart.

Files: data/feed.py (patchtst poll rotation + tunable batch). Bind-mounted, paper mode.

### CONT. 74 — TASK 3 DONE: data_feed AUTO-RECONNECT (2026-06-08)
Fixed the recurring silent-death landmine (data_feed zombies when redis IP/connection
changes). Two-part, deployed to data_feed, VERIFIED by an actual redis restart.

PART A — self-healing shared client (redis_client.py): added health_check_interval=30
(PING idle conns before reuse → dead ones discarded + fresh conn RE-RESOLVES the `redis`
hostname), socket_keepalive=True, socket_connect_timeout=5, Retry(ExponentialBackoff,3) +
retry_on_error=[ConnectionError,TimeoutError]. NO socket_timeout (would break blocking
ops). Added reinit() to rebuild client+pool. Benefits ALL services once they load it.

PART B — loop supervisor (data/feed.py): root cause was gather(return_exceptions=True)
SWALLOWING a stale-pool ConnectionError → data_loop() died while process+other loops
stayed up = zombie (Docker showed healthy 13h). _supervise(coro_factory,name) wraps each
of the 4 loops: on crash → log LOUD (data_loop_crashed_restarting) + bump
data_feed:loop_restart:{name} (Rule 12) + redis_client.reinit() + backoff + RESTART. A
loop can no longer die silently.

DEPLOY: redis_client.py + data/feed.py are BAKED (not mounted) — added single-file
bind-mounts to data_feed in docker-compose.yml + force-recreate (immediate, no rebuild).
Verified IN container: health_check_interval present, _supervise present.

VERIFIED (Rule 10 — real redis restart): restarted redis (persists: appendonly=yes,
118k keys survived). data_feed SURVIVED — newest log 06:08:35 post-restart, 51
longseq_forecast keys written after, supervisor restarts=0 (client self-healed the
dropped connection transparently — no loop crash needed). Brain (old client) also survived
this one. Caveat: redis kept the same IP this restart, so DNS-reresolve path not directly
hit; the connection-drop recovery (the actual prior failure mode) WAS exercised + passed.

FOLLOW-UP: image REBUILD propagates the self-healing client to ALL 17 services (brain/
scanner have long loops with the OLD client → still at theoretical risk). Recommended,
not yet done. Also observed: data_loop polls are HEAVY (candles ~175s, patchtst ~178s/30
pairs) → motivates TASK 1 (batched inference).

Files: redis_client.py, data/feed.py (supervisor), docker-compose.yml (data_feed mounts).

### CONT. 74 — TASK 1 (patchtst half): BATCHED INFERENCE (2026-06-08)
The forecaster coverage ceiling was ~6-9s/pair CPU inference (30 pairs = 211s/cycle).
Fixed with batched inference for patchtst.

ml/patchtst.py: added get_longsequence_forecast_batch(pairs, chunk=128) — gathers every
pair with full 256-step context, stacks into one [B,256,1] tensor, runs ONE forward pass
per chunk (transformer batches natively), writes all LONGSEQ_FORECAST keys via a single
Redis pipeline. data/feed.py _poll_patchtst_forecasts_and_log rewired: drops the per-pair
loop + rotation, calls the batch over the WHOLE active set (patchtst:poll_cap=0=all).

VERIFIED (direct, in data_feed): 39 forecasts / 40 pairs in 13.8s = 0.36s/forecast vs
~7s/pair before = ~19× faster. Full-universe (444) coverage now feasible per cycle (was
~52min, now ~couple min, mostly candle I/O which could be pipelined later). Deployed via
new ml/ dir-mount on data_feed (no inode gotcha) + force-recreate.

REMAINING (task 1 tft half + task 2): TFT batched inference (same pattern, different
output = quantile forecast) and the multi-TF producer+consumer wiring (task 2, shadow-
gated). NOT done — focused next step.

### CONT. 74 — TASK 1 patchtst: LIVE FULL-UNIVERSE COVERAGE CONFIRMED (2026-06-08)
Live batched poll: patchtst_polled attempted=455 elapsed_s=193 mode=batched ok=334 →
336 longseq_forecast keys live (was 24-51). Full active universe now covered in one
cycle (193s < 300s budget); ~11× more coverage than the old 30-pair loop, less wall-time.
Remaining 193s is candle I/O (455 sequential lrange), NOT inference (forward pass is
sub-second) → future optimization = pipeline the candle fetches. patchtst is now a real
full-universe input, not a dead feature.

### CONT. 74 — TASKS 1+2+3 COMPLETE: REBUILD + tft BATCH + MULTI-TF (2026-06-08)
All three landed and verified live.

TASK 1 (rebuild): built trading-bot-app:latest fresh (docker build; no compose build
directive — image is built directly; COPY . . bakes all source; ~17min, slow only due to
NO .dockerignore shipping 3GB models/+historical as context — future speedup). Recreated
ALL 17 services via `docker compose up -d`. VERIFIED: self-healing redis_client now
FLEET-WIDE (brain/scanner/dashboard all have health_check_interval — the zombie landmine
is closed everywhere, not just data_feed). Temp data_feed bind-mounts (redis_client.py,
data/feed.py, ml/) REMOVED — all baked now. redis/postgres NOT recreated (different
images) → state intact (127k keys, bot:running=1, active_pairs=475). 0 redis errors.

TASK 2 (tft batched inference, ml/tft.py): get_price_forecast_batch(pairs,timeframe,
context,chunk) — uniform-context stacked forward pass, pipelined writes. Measured: 1h
57/60 in 2.6s, 15m 58/60 + 5m 59/60 in 0.1s (~0.05s/fc). Per-TF context (1h=100 to match
live single-path; short TFs=50 since their candle lists hold ~65).

TASK 3 (multi-TF wiring): PRODUCER data/feed.py _poll_tft_forecasts (F19, batched, tfs
tunable via tft:poll_tfs default 5m,15m,1h). CONSUMER engine.py: blends per-TF q50 biases
(5m=.2,15m=.3,1h=.5) → tft_bias_multitf, SHADOW by default. Toggle tft:multi_tf_enabled
(default 0 = 1h stays LIVE; blend only shadow-logged to signals:tft_mtf_shadow:* + count).
Live trading UNCHANGED until the toggle flips (Rule 14).

VERIFIED LIVE post-rebuild: brain scoring (bayes/launchpad/mpp/signal logs); patchtst 363
keys (full universe); tft PRICE_FORECAST 1h=373 / 15m=454 / 5m=466 (MULTI-TF now live!);
tft_mtf_shadow count=96 (A/B logging); multi_tf toggle empty=0 (shadow-safe). All 17 up.

NEXT (owner decision, when shadow validates): flip tft:multi_tf_enabled=1 to promote the
blend; add .dockerignore (models/ + data/historical are runtime-mounted) for fast rebuilds.
Files: redis_client.py, data/feed.py, ml/patchtst.py, ml/tft.py, signals/engine.py,
docker-compose.yml (image rebuilt + temp mounts dropped).

### CONT. 74 — OLLAMA FALSE-DEGRADED FIX + DISABLE filtered_obi_severe_flip (2026-06-08)
1. OLLAMA recurring "degraded" (dashboard /llm/providers): root cause = ollama_degraded
   was pure staleness (no success in 600s). The local 14b is the RARE fallback (cloud-
   primary research), idle by design → re-degraded every 10min no matter how often warmed.
   FIX (dashboard/api.py, mounted/live): idle != degraded. degraded now = (last_success_ts
   is None) AND (fail_count > 0) = genuinely broken only; added separate idle flag. Same
   for _LOCAL_PROVIDERS rows. VERIFIED: ollama_local degraded=False idle=False (succ=5,
   age=105s), primary_healthy=True. Permanent — once succeeded, last_success_ts persists.
2. DISABLED filtered_obi_severe_flip force-exit: redis SET risk:filtered_obi_force_exit_
   enabled 0 (the existing kill switch, exit_signals.py:133 — the only emitter). Severe
   flips now downgrade to ×0.7 SL-tighten (trailing manages exit) instead of force-close.
   Matches the code's own 7d evidence (185 trades -$571, booking losses on then-green
   trades). Reversible: SET ...=1. AOF-persisted; reverts to default "1" only on redis flush.

### CONT. 75 — VIRTUAL-BALANCE RACE FIX + CLEAN 1000 REBASE (2026-06-08)
Owner report: virtual balance updated wrong around open / total not adding up to 1000.
CONFIRMED (Rule 2/9/13): account:virtual_balance drifted from the ledger — stored 133.59 vs
ledger 458.93 (-$325), starving the bot (60 slots, 1 open). ROOT CAUSE: every balance
mutation in execution/paper.py was a NON-atomic read-modify-write (GET->compute->SET). The
key is mutated by TWO processes — brain (open/close/DCA) AND dashboard (manual close-all +
mode-switch close loops) — so cross-process interleaving lost updates (drift down). Caught
the race live: a manual SET got clobbered by the still-running old code mid-cycle.
FIX (execution/paper.py): _reserve_balance() = server-side Lua atomic check-and-deduct for
opens + DCA; close/partial restores switched to INCRBYFLOAT; open reserves FIRST then
persists, refunds on write failure. docker-compose.yml: mounted ./execution into the
dashboard service (it ran baked-in non-atomic code). Restarted brain+dashboard (volume
mounts, no rebuild) — both verified loading atomic code (_reserve_balance x5).
REBASE (owner: "clean 1000 baseline"): paused opens -> set virtual_balance = 1000 - deployed
(=1000, open=0) + session:start_balance=1000 + session:start_ts=now -> resumed. VERIFIED:
total equity (free+deployed) = 1000.00, drift = 0.00, holding under live trading. The
single global running balance no longer carries the 11,333 pre-session trades (-$421.90);
only trades from 2026-06-08 14:17 UTC count. Files: execution/paper.py, docker-compose.yml.
Memory: [[finding-virtual-balance-race]].

TRADES-NOT-OPENING (same session): CONFIRMED via logs (Rule 2/10 — path runs, picks=3
from 342 scored, but opened=0): every scibrain open threw psycopg2 "can't adapt type
'dict'". ROOT CAUSE: scibrain/opener.py passed feature_vector as a RAW DICT; write_trade_open
(memory/write.py:125) inserts it straight into SQL, so it must be a JSON string (legacy path
does json.dumps at engine.py:1247). Worked earlier only because feature_vector was empty
(None) before CandleNet forecasts populated it. FIX (opener.py): json.dumps(_fv) if non-empty
else None — mirrors legacy + the adjacent signals_at_entry. Rule 12: the generic open-error
except had a log but NO redis counter (failure was counter-invisible) → added
scibrain:open:reject:error. VERIFIED post-restart: 3 trades opened (1000SATS/1000BONK short,
FIL long, driver=chaos), free+deployed=1000.00. Files: signals/scibrain/opener.py.

### CONT. 75 — SCIBRAIN PHASE 2 MODULE BANK (6 new modules) + CRASH RADAR + mtf-reversal off (2026-06-08)
PHASE 2 module-bank expansion — 6 new pure-numpy PhD modules, all fully wired into the
deterministic MODULES registry (so they drive BOTH the picking gate AND the visibility
panel) and VERIFIED live (smoke + run_cycle on real frames):
  - StatPhysSOCModule (statphys_soc): Hill tail-index + critical-slowing-down (rising
    rolling var & lag1-autocorr) + vol-of-vol + downside skew -> criticality + crash_warning.
  - MeanFieldIsingModule (ising): mean-field magnetization from OFI/funding/OI/momentum
    "spins" -> contrarian when crowded, mild-trend when ordering.
  - LangevinHawkesModule (langevin_hawkes): AR(1) OU drift/diffusion (revert-to-fair)
    blended with Hawkes self-excitation (continuation when clustered).
  - RMTModule (rmt): SSA trajectory-matrix eigenspectrum + ITERATIVELY-fitted
    Marchenko-Pastur noise edge -> denoised-trend direction. (Fixed: must run on centered
    log-PRICE not returns — returns are MP-noise; and conviction tracks decisiveness not
    signal_frac which is ~1 for any price level.)
  - TDAModule (tda): exact H0 sublevel-set persistent homology via union-find -> persistence
    entropy + dominant range -> structure-aware reversion.
  - WaveletSpectralModule (wavelet): Haar DWT multi-scale energy + Donoho noise floor (SNR)
    + denoised-trend slope.
Bug found+fixed during verify: SOC/RMT _MIN_BARS were 80 but the sensor bus serves ~65 5m
bars -> they abstained forever; lowered to 60/55. Files: signals/scibrain/modules/{statphys_soc,
ising,langevin_hawkes,rmt,tda,wavelet}.py + modules/__init__.py.
DASHBOARD VISIBILITY (crash radar): runner.score_symbol now feeds scibrain:crash_radar ZSET
(sym->crash_warning, top-200, drop<0.2) from the SOC module; keys.py CRASH_RADAR; dashboard
/scibrain response carries crash_radar + new /scibrain/crash_radar endpoint (enriched:
criticality/regime/Hill-alpha/skew/fused-dir); ScientistBrain.tsx Crash Radar strip; frontend
rebuilt (main.583cd9a1.js, served by nginx). VERIFIED: run_cycle scored 58 pairs/2.1s, 12
modules/symbol, crash_radar=21 pairs (QUSDT 0.82...), endpoint returns enriched rows.
OPS: per owner, disabled the mtf_15m_reversal force-close exit — redis SET
trail:mtf_15m_reversal_enabled 0 (manager.py:660 kill switch; had force-closed 7 trades this
session; reason="mtf_15m_reversal_confirmed"). Reversible: SET ...=1. Read live each tick.
TRADES-NOT-OPENING (capital gate, owner: "lower min position → more concurrent trades"):
after the rebase the bot deployed ~$875 into 10 trades (free ~$130) and brain/soar.py
capital_starved PAUSED all new opens. ROOT CAUSE (Rule 2, soar.py:420-446): the legacy
auto-scale reserves free balance across ALL max_open slots
(max_affordable = balance/(slots_remaining×reserve)); at 60 slots + $172 free that's
$3.44/slot < $5 floor → pause. That fair-share sizing is LEGACY-engine logic; SciBrain sizes
each trade itself within bot:min/max_position_usdt against live balance (opener floors at
max(min_cap,5), PAPER executor re-checks atomically). FIX (brain/soar.py): gate the
capital_starved pause behind `not scibrain:enabled` — legacy mode unchanged, scibrain mode
relies on its own floor. Also set bot:min_position_usdt 50→20 (owner) so more concurrent
smaller trades fit. VERIFIED post-restart (redis_ready 16:10:28): zero capital_starved after,
funnel opens every cycle — BIOUSDT (driver=wavelet) + POWERUSDT opened, sizing ~$35; free
136.68 + deployed 858.66 = 995.34 (=1000 + session realized, no drift). Reversible:
scibrain:enabled=0 restores the legacy guard. File: brain/soar.py.

### CONT. 75 — SCIBRAIN PHASE 2 COMPLETE: InfoTheory + HMM (retrained) → 14-module bank (2026-06-08)
Finished the Phase 2 module bank (owner: "both: InfoTheory + retrain HMM"):
- InfoTheoryModule (info_theory.py): Bandt-Pompe permutation entropy (predictability gate) +
  Shannon sign-entropy + plug-in transfer entropy volume→price (within-pair lead-lag) →
  predictability-weighted momentum. Cross-PAIR TE deferred to Phase 5 (needs the full-universe
  panel in RAM). Honestly low-conviction on noisy 5m (markets ~random at that scale) — fires
  when structure emerges.
- HMM RETRAIN: models/hmm_regime.pkl was DEGENERATE (means [-1e-5,.026,3.93,-.003]; the 3.93
  state = an unfiltered +393%/day glitch return) so ml/hmm.py._calibrate rejected it and the
  HMM was effectively dead. FIX: pretrainer/main.py train_hmm now filters |r|>0.5 before
  fitting (matches the serving _OUTLIER_RET); retrained on 499 symbols' 1d.csv → healthy
  means [-.003,.070,-.0,.003] (all |mean|<=0.5), calibrates clean.
- HMMRegimeModule (hmm_regime.py): reuses the healthy pkl (loaded+calibrated once, health-
  gated — rejects any future degenerate checkpoint), classifies each pair's regime from
  daily-scale returns (1h→daily aggregation, correct train/serve scale) via predict_proba;
  regime-weighted expected daily return → slow directional context; LIVE (bear @ posterior
  0.87 on the current tape).
Registered both in MODULES → 14-module bank VERIFIED live: smoke + funnel stream all 14
(koopman,chaos,statphys_soc,ising,langevin_hawkes,rmt,noiseharvest,kalman,quantum,tda,wavelet,
info_theory,hmm_regime,bocpd); cycle ~16.4s (<20s throttle; Phase 5 will parallelize). Trades
still opening (14 open, free+deployed=996.42, no drift). Files: signals/scibrain/modules/
{info_theory,hmm_regime}.py + __init__.py, pretrainer/main.py, models/hmm_regime.pkl (retrained).
Phase 2 module bank now 100% (physics+quantum+math+stats); next open: Phase 1b correlation-
cluster cap + parallel scorer (Phase 5), then Phase 3 meta-router.

---

## 2026-06-11 — CPU/lag remediation: disable orphaned + dormant beat tasks (owner-approved)

PROBLEM: VPS lag. load1 ~7.9/12 cores; microstructure queue backlogged 300+. Measured
worker-seconds/task over 20-min log window (excluded all scibrain_* per owner).

ROOT CAUSES (verified live, non-scibrain):
  1. launch_pad_maintain_task — 1843s/run, fired every 300s. SUPERSEDED by Scientist Brain
     (launchpad:enabled=0, never opens trades — confirmed task docstring + engine). Two copies
     were pinning BOTH candlenet worker slots (concurrency=2) → starved microstructure_scan.
  2. Predict-all pipeline DORMANT: prediction:gate_enabled=0 → signals/engine.py:1677 skips the
     entire prediction block, so predictions never gate a live trade. Zero predictions:* keys.
     Yet capture_pattern_embeddings burned 1520s/20min (95s/run @ 60s cadence → pinned ~1.3 cores).

FIX: celery_app.py — added a pop-loop after beat_schedule def removing 9 entries:
  launch-pad-maintain, capture-pattern-embeddings, train-pattern-clusters, candle-online-train,
  prediction-refresh, update-pattern-registry, calibration-drift-check, auto-arm-prediction-gate,
  shadow-ablation. (coinglass_liq_refresh left ON — owner declined.) REVERT = delete the block.

VERIFIED LIVE (Rule 19, Level-3): restarted celery_beat (78 entries, 9 confirmed removed) +
celery_worker_candlenet (killed the 2 in-flight launch_pad runs; acked-on-receipt so no redeliver).
  microstructure backlog 336 -> 0 (drained; worker cleared 557 microstructure_scan in 90s)
  load1 7.91 -> 6.45 ; default/predict_all queues -> 0 ; disabled-tasks-active = 0.
NOTE: pattern:cluster_id (468 keys) now frozen at last value — soft-degrades debate/fallback only;
predict-all gate is off so no live trade impact. Fully reversible if predict-all is armed later.

---

## 2026-06-11 — Dashboard lag + "Stop button doesn't stop the bot" — ROOT CAUSE & FIX

SYMPTOMS: dashboard laggy/unresponsive; pressing Stop didn't halt the bot.

DISCONFIRM (Rule 9): set bot:running=0 directly in Redis → brain halted immediately
(brain_act_skipped_bot_stopped ×4, scibrain funnel stopped). So the BACKEND stop gate
(brain/soar.py:421, before process_signals@557) WORKS. Stop button POST sets bot:running=0
(dashboard/api.py:231) — also correct. The problem was the request never getting served.

ROOT CAUSE: dashboard/api.py WebSocket handler (/ws/dashboard, ~L3290) called
pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0) — a SYNCHRONOUS blocking
call running on the SINGLE uvicorn worker's asyncio event loop. With any dashboard tab open
(holds a WS conn) it froze the whole worker up to 1s per tick, starving EVERY HTTP request:
polled panels lagged AND POST /bot/stop queued behind the freeze → "Stop doesn't work".
Evidence: /health (itself async+sync DB+redis) measured 2647ms spikes with the blocking code.
Compounded by 61 async-def endpoints doing sync redis/psycopg (87 call sites) — short, but the
WS 1s freeze dominated.

FIX: rewrote the WS loop to drain non-blocking (timeout=0.0) each tick, then await asyncio.sleep(0.05).
Removes the event-loop freeze; HTTP requests run between ticks.

VERIFIED LIVE (Rule 19 L3): restarted dashboard (startup_complete). Held a real WS conn
(101 Switching Protocols) + published 80 CH_PRICE_UPDATE msgs to exercise the drain loop, while
timing /health: 4-8ms (avg 31, max 299) vs 2647ms before. Freeze gone.
NOTE: bot is currently STOPPED (bot:running=0 from the disconfirm test) — operator can Start from
the now-responsive dashboard. OPTIONAL hardening (not done): uvicorn --workers N + convert the
hot async-def endpoints to plain def (threadpool) so per-request sync redis/DB never blocks the loop.
