# Next-Impl: Launch-Pad of 10 (pre-qualified "always open green" pool)

Status: **DISCUSSION / DATA-GATHERING ONLY — do NOT implement yet** (owner mandate, cont. 70).
Owner restated requirement (verbatim intent):
- After picking a trade + entry price, if it opens in loss / red candle → HOLD until green/profit;
  if it won't, REPLACE that symbol with one that opens green. Goal: every open starts favorable. Long or short.
- Keep a SEPARATE table (distinct from active pairs) that always holds exactly **10** symbols (long/short/both).
- When one of the 10 gets opened as a trade, its slot is refilled by the next outside symbol that opens green.
- If all 10 are opened, refill all 10 (immediately or over time).
- Concentrate all compute/intelligence on finding + maintaining these 10.
- If a staged one "expires"/starts in loss → FLIP its direction (long that starts dropping → short).
- If a symbol OUTSIDE the 10 shows more movement + clear direction → it displaces the least-moving of the 10.

## Live system facts (verified cont. 70, 2026-06-03)
- `brain:paper_closed = 10,133` → past 1000. **P4 candle-close gate DORMANT** (pre-1000 only).
  **PPO Entry-Timing Agent ACTIVE** (≥1000 gate) — IF `models/entry_timing_agent.zip` exists, else decide_entry→"enter".
- `scanner:active_pairs` (Redis SET) = **160** symbols = watch universe. `MAX_ACTIVE_PAIRS` default 60, overridden live.
- `bot:max_open_trades = 45`.
- Blueprint (BOT_BLUEPRINT.md) has NO watchlist/launch-pad concept. Only L114 "always let trailing SL decide exit."

## Existing building blocks (REUSE — already built)
1. **scanner/main.py** — symbol ranker R-01..R-11 (5-criteria scoring) producing scanner:active_pairs.
2. **P4 candle-close confirmation** (signals/engine.py ~925): requires last CLOSED 1m candle to confirm dir
   (close>open long / close<open short); high-conviction CandleNet dir1 bypass; deadlock auto-disable
   (signal:p4:disabled). DORMANT now (paper_closed>1000). == "wait for green candle", rule-based.
3. **PPO Entry-Timing Agent** (ml/entry_timing_agent.py): action 0=wait one candle, 1=enter, 2=skip; 22-dim
   state from CandleNet 1m/5m dir/mag/trend + microstructure. == "hold until green", RL version. ACTIVE.
4. **CandleNet forecasts** per TF (5m/15m/30m/1h): dir1/dir3/dir5 (P[up]), magnitude, trend. engine ~384
   `_cn_agrees` consensus bonus (+25/+10/−15) + trend ceiling. == directional "will it open green" signal.
5. **predict-all-before-open** (predictions table, conformal width, RR gate) — only-open-when-favorable engine.
6. **signals/slot_selector.py** — Position-Based Multi-Play Thompson Sampling: fills K=max_open−n_open OPEN
   slots from a candidate pool via per-(pair,regime) Beta posterior. == "rank + fill slots" (but for OPEN slots,
   not a persistent 10-table). Default OFF (bandit:enabled).
7. Live features available per pair: 1m candles (color), OFI, VPIN, funding, ATR/vol, 24h change, volume,
   dir_accuracy, signal score/trade_potential, predict-all expected move + conformal width.

## What is NET-NEW vs existing
- A PERSISTENT, separate **launch-pad table** of exactly 10 (Redis sorted-set/hash or Postgres). NEW.
- Per-candidate **state machine**: scouting → staged(pending-green) → confirmed → fired → cooldown. NEW.
  (Today: an unconfirmed signal is DROPPED `return []`, not HELD. The "hold" intent is the new part.)
- A **refill/maintainer loop** (celery beat) keeping 10 full; ranks outside candidates; flip + displacement. NEW.
- A concrete **"movement" metric** for displacement + "least movement in the 10" (compose from existing feats). NEW.
- **TTL/expiry** per staged candidate. NEW.

## HONEST constraints to resolve in discussion (Rule 9 disconfirm)
- "Always open IN PROFIT" is impossible at t=0 (PnL = −fees−slippage at fill). Achievable = "open only on
  CONFIRMED favorable momentum (last closed candle green in dir + predictor agrees) so it tends green fast."
- FLIP on a LIVE open losing trade = close+reopen opposite = double fees + whipsaw risk in chop. Cheap + safe
  only when applied to STAGED (not-yet-open) candidates. Need owner decision: staged-only vs also-live.
- 10-table vs max_open=45: if the 10-table is the SOLE source of opens, it conflicts with 45 open slots.
  Need owner decision: 10-table = sole gate (and cut max_open) vs priority pre-filter layered on current flow.

## LOCKED DECISIONS (owner, cont. 70 — answered via AskUserQuestion)
- **D1 — Table role:** Launch-pad is a **10-deep ON-DECK BUFFER**, the SOLE funnel for opens (not an open cap).
  All qualified candidates in the buffer get opened; each fired slot refills + opens immediately; cycle continues
  until `bot:max_open_trades` (~50) positions are filled. If too few qualify, it WAITS (no forcing). So: 160
  universe → rank/qualify → 10-deep buffer → open → refill → ... up to max_open.
- **D2 — Flip scope:** STAGED/SHADOW side only. The 10-table carries **shadow P/L, peak-profit, peak-loss**
  measured from the price WHEN THE SYMBOL ENTERED THE TABLE (a live MAE/MFE tracker). If the shadow shows a
  loss / adverse drift → **flip the staged direction** (long→short). No live close+reopen (avoids whipsaw+fees).
- **D3 — "Open green":** = minimize **Maximum Adverse Excursion at entry** (Sweeney MAE). Owner authorized
  ADDING external best-practice variables/data if they improve it (see RESEARCH below). Not literal t=0 profit.
- **D4 — Build approach:** Reuse CandleNet + predict-all + P4 confirmation logic + (optionally) re-arm the
  entry-timing agent; the NEW pieces are the persistent buffer table + maintainer loop + shadow MAE/MFE tracker.
- **D5 — Movement: DO NOT BLEND.** Show **3 SEPARATE COLUMNS** so we can empirically see which is most
  predictive/reliable before committing to one:
    * `mv_candlenet` = |dir3−0.5| × magnitude  (model conviction)
    * `mv_predicted` = predict-all expected move %  (model forecast)
    * `mv_realized`  = recent return / ATR  (live momentum)
  Displacement of "weakest of 10" + reliability comparison decided AFTER observing the 3 columns live.
- **D6 — Storage (CONFIRMED):** Postgres table `launch_pad` (durable + dashboard-queryable) mirrored to Redis
  hash/zset for hot reads. Maintainer writes Postgres; engine + dashboard read Redis mirror.
- **D7 — Cadence (CONFIRMED):** refill + open immediately when a slot frees; staged-candidate TTL.
- **D8 — Flip rule (CONFIRMED — owner chose my recommendation):** do NOT flip on any shadow loss. Flip only
  when BOTH: (a) shadow peak_loss exceeds an adverse threshold (e.g. MAE > k×ATR or > X%), AND (b) live
  momentum confirms the reversal (CandleNet dir3 crosses + recent candles align). **Flip cap = 2**; after the
  2nd flip the symbol → cooldown (dropped from buffer, replaced). Prevents chop flip-flop.
- **D9 — Dashboard (CONFIRMED):** render the 10-table as a live panel on the dashboard (http://34.85.74.56).

## RESEARCH (web, cont. 70 — owner-authorized D3)
Goal reframed as **minimize MAE at entry** (don't open into adverse excursion). Techniques to fold into the
staging/qualify gate (additive to existing CandleNet consensus + P4 candle-close confirm):
- **MAE/MFE (Sweeney):** track peak unrealized loss/gain per setup; only stage setups whose historical MAE
  distribution (per pair×regime) is low. Our shadow P/L peak-loss column IS the live MAE per candidate.
- **Momentum-flip trigger (don't catch falling knife):** require a flip into the trade direction before firing
  — e.g. higher-low for long / lower-high for short, or MA-cross + up-volume. Maps to CandleNet trend flip.
- **Exhaustion / RSI filter:** skip extreme-momentum spikes (avoid buying the top of the next leg). We already
  have exhaustion_score_1m/5m in the entry-timing agent state — reuse as a stage gate.
- **Volume confirmation:** require up-day / rising volume on the breakout candle before firing.
Candidate NEW variables to add to the table/qualifier: historical_MAE_pXX (per pair×regime), structure_flag
(HL/LH confirmed), rsi_exhaustion_flag, breakout_volume_ok. Sources in chat.

## PROPOSED 10-TABLE COLUMN SCHEMA (for discussion)
| symbol | dir (L/S) | table_entry_price | table_entry_ts | shadow_pnl_% | peak_profit_% | peak_loss_%(MAE) |
| mv_candlenet | mv_predicted | mv_realized | qualified(green?) | flips_count | state | ttl_left |
state ∈ {scouting, staged, confirmed_green, fired→cooldown, flipped}.

## Deploy notes (when we get there)
- monitor_trailing_sl + signal flow run in BRAIN (main.py); brain bind-mounts risk/+signals/. New celery beat
  maintainer would run in celery_worker/celery_beat → those need REBUILD (no bind-mount). Verify executor first.
- Dashboard surface for the 10-table → dashboard needs frontend build + restart.

## PHASED IMPLEMENTATION PLAN (all decisions locked — awaiting owner GO before code)
**P1 — Storage + schema (Migration NNN).** New Postgres table `launch_pad` (cols per schema above) + Redis
mirror keys `launchpad:slots` (hash slot→json), `launchpad:cooldown` (zset symbol→ts). Rollback SQL included.
Files: migrations/0NN_launch_pad.sql, redis_keys.py (+ keys). ~80 LOC. RISK: low.

**P2 — Shadow MAE/MFE tracker.** Per buffered symbol, track from table_entry_price each tick: shadow_pnl%,
peak_profit%, peak_loss%(MAE). Pure read of live mark + stored entry; no orders. File: new
`signals/launch_pad/shadow.py`. ~120 LOC.

**P3 — Qualify gate ("open green").** Reuse P4 candle-close confirm (re-armed for buffer use regardless of
paper_closed) + CandleNet consensus, hardened with research: momentum-flip (HL/LH), exhaustion/RSI filter,
breakout-volume. Outputs qualified? flag + the 3 movement columns (mv_candlenet/mv_predicted/mv_realized).
File: new `signals/launch_pad/qualify.py`. ~180 LOC. Reuses existing engine helpers — no new data feeds.

**P4 — Maintainer loop (celery beat, every ~10–30s).** Keeps buffer full from the 160 universe; ranks outside
candidates; displaces "weakest of 10" when a stronger outside mover appears (weakest decided after we observe
the 3 columns — initially lowest mv_realized); applies D8 flip rule (threshold + reversal confirm + cap 2);
TTL-expires stale candidates. File: new `signals/launch_pad/maintainer.py` + celery_app.py beat task.
~250 LOC. **RUNS IN celery_worker/celery_beat → REBUILD required** (no bind-mount). Verify executor (R: verify-task-executor).

**P5 — Engine open hook.** signals/engine.process_signals opens ONLY from buffer slots that are
qualified-green, while open_count < max_open(~50); on open → mark slot fired→cooldown, trigger immediate
refill. Gated by `launchpad:enabled` kill switch (default OFF → legacy 160-flow unchanged until we flip it on).
Files: signals/engine.py (~60 LOC). Silent-rejection counters on every skip (R: silent-rejection).

**P6 — Dashboard panel.** Live 10-table panel (all columns incl. the 3 movement values + shadow MAE/MFE).
Files: dashboard/api.py endpoint + frontend component. **dashboard needs frontend build + restart** (rebuild).

**P7 — Blueprint doc + memory.** Add launch-pad section to BOT_BLUEPRINT.md; new memory
project_launch_pad. Update PROGRESS.md.

**Deploy order:** P1→P2→P3→P4 behind `launchpad:enabled=0` (shadow-only, observe the 3 columns + flip
behaviour live with ZERO trading impact for a few days) → then P5 flip-on → P6 panel. R8 footer each step.
Kill switch `launchpad:enabled=0` instantly reverts to current behaviour.

## Related memory
[[project_predict_all_before_open]] [[feedback_profit_lock]] [[feedback_silent_rejection]]
[[feedback_rl_deadlock_detector]] [[project_judge_replaces_entry_gates]] [[feedback_verify_task_executor]]
