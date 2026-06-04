# Next Impl — md-vs-disk Feature Audit (F8 + others)
_Created: 2026-05-26 | Status: ACTIVE_

> Triggered by user request 2026-05-26: "check all md files for f8 and any other features discussed but not implemented vs what on the disk."

---

## Source md files audited

- `/opt/trading-bot/BLUEPRINT_COMPLIANCE_AUDIT.md` (Issues #1–20, Deviations D-01..D-08; dated 2026-05-17 with later additions)
- `/opt/trading-bot/PROGRESS.md` (sessions through cont. 47 / 2026-05-25)
- `/home/ajithd747/ai-brain-crypto-bot/BOT_BLUEPRINT.md` (primary truth — Rule 1)
- `/opt/trading-bot/next_impl/f48_extensions.md`, `f49_autonomous_training.md`

---

## F8 Strategy Lifecycle — VERIFIED CLOSED

Audit Issue #4 (2026-05-17) said "Functions exist, never called." All wiring confirmed in code:

| Piece | File:line on disk | Status |
|---|---|---|
| `select_strategy()` selector | `strategy/selector.py` exists | ✅ |
| Selector called from brain | `brain/soar.py:405-406` | ✅ |
| `auto_retire_if_underperforming()` | `memory/write.py:733-734` | ✅ |
| `check_trial_eligible` + `promote_to_active` | `memory/write.py:743-757` | ✅ |
| Per-strategy router (DCA/entry/SL/sizing) | `strategy/router.py`, `strategy/save.py` (cont. 8/9/13/23) | ✅ |
| F8 health 4-side check | `tools/feature_health.py:check_F8_strategy_promote` (cont. 16) | ✅ |
| `create_experimental()` callers | `research/engine.py`, `self_play/mars.py`, `celery_app.py` | ✅ |

→ Nothing left open on F8.

---

## Features confirmed CLOSED since 2026-05-19 SIMPLIFIED list

| Feature | Closed in | Disk evidence |
|---|---|---|
| F44 Directional Hedge | D-06 / D-08 (cont. 4/10) | `risk/hedge.py`, `risk/hedge_params.py` |
| F37 Debate Rounds 2/3 | (date in cont. block) | `debate/council.py:427` round2, `:448` round3 |
| F9 Miss Decoder + Counterfactual sweeper | cont. 3 + T-04 | `celery_app.py:1767 sweep_pending_counterfactuals`, `memory/pattern_miner.py` |
| F12 Mismatch Decoder | cont. 3 (2026-05-21) | (paired with F9 in cont. 3) |
| F18 CryptoBERT/FinBERT | D-07 (cont. of 2026-05-20) | `ml/sentiment.py` rewritten + beat task |
| F10 Brain-learned scanner weights | cont. 18 (Issue #18 CLOSED) | `ml/criteria_weights.py` + `pair_selections` table |
| F34 World Model online + MPP | cont. 5 (2026-05-21) | `world_model/model.py` RSSM online train |
| F41 LOB simulator (was Gaussian RW) | cont. 4 (2026-05-21) | `self_play/mars.py` agent-based |
| F36 World-model prescreen + GA iteration | cont. 2 (2026-05-21) | `research/engine.py:167 world_model_prescreen` (no longer `return True`) + `:358 evolve`, `:545 crossover` |
| F19/F20/F24/F34 model loading (P0 #1) | (multi-cont.) | `pretrainer/main.py:175,216,296,340,407,420` all use `state_dict` |
| F19 TFT architecture (P0 #2) | (multi-cont.) | `ml/architectures.py:27 GatedResidualNetwork` + post_lstm/attn GRN |
| F30 Feature Governance (P0 #3) | bootstrap added | `main.py:111` and `celery_app.py:638` call `bootstrap_all_features`; `feature_governance/bootstrap.py` exists |
| Issue #5 web_intel parse-back | (later) | `celery_app.py:967 update_source_credibility` invoked |
| Issue #9 pattern mining | (later) | `memory/pattern_miner.py:419 mine_patterns` called from `celery_app.py:1161` |
| Issue #10 OPRO revert | (later) | `celery_app.py:285 revert_prompt` + `:290 opro_reverted` |
| Issue #13 Metacog escalate | (later) | `celery_app.py:1109-1128 evaluate_self_improvement_mechanisms + escalate_to_governance` |
| Issue #14 MemRL retrieval influences decision | cont. 14 / cont. 19 | `signals/engine.py:781 get_cluster_context` reads in pricing path |
| Trailing SL (Issue beyond #14 — Feature 4 rebuild) | cont. 18 / cont. 44 profit-lock | `risk/manager.py`, `risk/trail_params.py` |
| F45 Cross-Sectional Momentum | cont. 21 | new section in signals/engine.py |
| F48 CandleNet (1m/5m/15m + 7 extensions) | cont. 45 / 46 | `ml/candlenet.py` (TCN, GAF, exhaustion, regime head, magnitude head) |
| F49 Autonomous Self-Training Orchestrator | cont. 47 | `ml/{drift_detector,performance_monitor,auto_hpo,active_learning,model_versions,training_orchestrator,online_learner}.py` all exist |

---

## STILL OPEN — discussed in md, not implemented on disk

### 1. F16 Kelly — still global, not per-strategy/pair
**md claim:** `BLUEPRINT_COMPLIANCE_AUDIT.md` line 3984 SIMPLIFIED list — "F16 Kelly (global not per-strategy/pair)."
**Disk reality:** `ml/kelly.py:46 get_position_size_pct(paper_closed_count)` calls `get_recent_trades(min(paper_closed_count, 200))` — pulls ALL recent trades regardless of strategy_id or pair. `win_rate`/`avg_win`/`avg_loss` are global.
**Gap:** No filter by `strategy_id` or `symbol`. The blueprint's "fractional Kelly per strategy" is not built.

### 2. F14 HMM — no live fine-tuning
**md claim:** PROGRESS line 3982 "F14 HMM live fine-tuning" listed under 🔴 MISSING entirely (2026-05-19 audit). Never marked closed.
**Disk reality:** `ml/hmm.py:29-46 update_regime` only calls `model.predict(obs)` (Viterbi decode) — no `model.fit()`, `partial_fit()`, or Baum-Welch update. The HMM weights are frozen at whatever the pretrainer wrote.
**Gap:** Live regime relearning is absent.

### 3. F42 SOAR — no sub-goals / procedural memory / chunking
**md claim:** SIMPLIFIED list — "F42 SOAR (no sub-goals, no procedural memory)."
**Disk reality:** `grep -rn "sub_goal\|procedural_memory\|chunking\|impasse"` across the repo returns **zero hits**. `brain/soar.py` is a flat observe/decide/act loop; SOAR's namesake mechanics (impasse-driven sub-goaling, chunked procedural memory) don't exist.
**Gap:** "SOAR" is a name, not the cognitive architecture.

### 4. F38 Curiosity — no exploratory trade or risk curiosity
**md claim:** SIMPLIFIED list — "F38 Curiosity (no exploratory trade, no risk curiosity)."
**Disk reality:** `curiosity/engine.py:59 log_exploration_hypothesis` only pushes onto `research:hypothesis_queue` — it never opens an actual paper trade. The "exploratory trade" execution path doesn't exist. `risk_curiosity` is mentioned in F49 research notes but no module computes it.
**Gap:** Curiosity feeds the research queue but cannot itself execute trades. Risk curiosity not built.

### 5. Brain Authorities Issues #15 / #16 / #17 / #19 / #20 (cont. 18)
**md claim:** `BLUEPRINT_COMPLIANCE_AUDIT.md` lines 820-826 — labelled "Multi-session scope" / open.
- **#15** ML Sub-Agents spawn — `ml/architectures.py` is fixed; brain has no spawn-new-model authority.
- **#16** RL Agents spawn/replace — F21 MARL agents pre-defined; no spawn mechanism.
- **#17** Risk Parameters dynamic adjust — brain only reads `bot:max_open_trades/leverage`, doesn't write.
- **#19** A/B experimentation — UCB1 selector picks ONE strategy per signal; no paired shadow-mode runner.
- **#20** Data Sources add/remove — no per-data-source predictive-value evaluator.

### 6. F49 deferred items (intentional, documented)
Per `next_impl/f49_autonomous_training.md:155-159`:
- HPO not yet wired into `candlenet.train()` (engine works, integration deferred).
- Auto-rollback trigger when perf monitor flags new model (API exists, trigger manual).
- `direction_model.online_update()` not built (online_learner no-ops gracefully).

### 7. F48 deferred items
Per `next_impl/f48_extensions.md` status note — pretrainer rebuild + verification still pending; both `candlenet_1m.pth` and `candlenet_5m.pth` need to exist and pass validation gates before topic closes.

---

## Confirmed vs Proposed Decisions

**Confirmed by audit:**
- F8 fully closed — do not re-open.
- All P0 issues from 2026-05-17 audit are closed on disk.
- F48 + F49 are wired but await final pretrainer/verification.

**Proposed (user decision needed):**
- Tackle #1-5 above in priority order. The cheapest wins are #1 (F16 per-strategy Kelly — single function rewrite) and #4 (F38 exploratory-trade execution path). #3 (real SOAR) and #5 (#15/#16) are multi-session.

---

## Checklist (for follow-up sessions)

- [ ] F16 — refactor `ml/kelly.py:get_position_size_pct` to accept `strategy_id` + `symbol` and filter trades.
- [ ] F14 — add online HMM update (e.g. Baum-Welch on rolling window or replace HMM with online changepoint that exists in F26 BOCPD).
- [ ] F42 — design impasse / sub-goal / chunking before naming it SOAR; or rename module honestly.
- [ ] F38 — add exploratory-trade execution path triggered by `should_explore` (small size, separate counter).
- [ ] Issues #15/#16/#17/#19/#20 — multi-session; brain-command-authority design pass.
- [ ] F48 pretrainer rerun verification (both pth files + AUC gates).
- [ ] F49 HPO-into-candlenet wire + auto-rollback trigger + direction_model.online_update.

---

## Session Handoff

- F8 verified closed end-to-end (selector → router → lifecycle → governance).
- 5 features remain genuinely "discussed but not implemented" on disk; see "STILL OPEN" above.
- Audit md (`BLUEPRINT_COMPLIANCE_AUDIT.md`) is stale (dated 2026-05-17) — many P0/P1 issues it lists are now closed; the SIMPLIFIED list in PROGRESS line 3984 is the more accurate snapshot.
- Per Rule 6, this file remains ACTIVE until each STILL OPEN item is closed (or explicitly accepted as won't-fix) and verified via Rule 2.
