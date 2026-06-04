# Next Implementation — Predicted Profit Loop (F9/F12 → actionable decisions)

Session-handoff doc for turning F9/F12 commentary into a closed-loop predictor that intervenes at decision time. Author: Claude. Date: 2026-05-29.

---

## ✅ IMPLEMENTED (first increment) — 2026-06-02 (cont. 69s) — see PROGRESS.md cont. 69s

**§4.3 EV-override shipped in SHADOW mode** (`signals/ev_override.py`). Decision-time EV
check for recoverable rejects: `p_win` = bayes per-strength-bucket posterior lower bound
(CF-trained proxy); `peak/dd` = per-direction CF segment averages (30-min refresh task).
EV = p_win·0.5·peak·notional − (1−p_win)·dd·notional → override iff EV≥min ∧ p_win≥0.55,
size_mult ∈ [0.25,0.50]. Wired at the engine replay-push choke point. DEFAULT SHADOW
(measures + audits, takes NO trade). Dashboard control: `/signals/ev_override`,
`/signals/ev_override/toggle`, button page `/signals/ev_override/panel`. Flags:
`ev_override:enabled=1`, `ev_override:live=0`.

STILL DEFERRED (Rule 4 honesty): §4.1 dedicated XGBoost CF predictor, §4.2 predicted-peak-
as-TP, §4.5 regret auto-tuner, §9.1 Meta-RL judge. p_win/peak/dd are calibrated proxies,
not per-signal regressions. LIVE-take (re-admit at reduced size) is built but gated OFF
pending the §7.5 full-deploy capital-carve decision — flip `ev_override:live=1` to enable.

---

## ⚑ VERIFIED STATUS — 2026-06-02 (cont. 69s, Rule 2 audit)

Live-state audit of how much F9/F12 contributes to the open-trade decision TODAY:

| Step (from §6) | Built | Contributes to open-trade check | Live verdict |
|----------------|-------|---------------------------------|--------------|
| §4.4 structured tags / schema | partial | weak | Migration applied (`counterfactuals.predicted_peak_profit_pct, miss_tag, miss_tag_confidence, miss_tag_evidence, decode_reason_embedding(768)` all exist). BUT of **57,124 CF rows: only 957 (1.7%) miss-decoded, 355 have a miss_tag, 316 (0.55%) have predicted_peak_profit_pct.** The decoder barely runs (throttled by the same CF backlog + LLM cost). |
| §4.1 CF-trained predictor (`p_win`, `predicted_peak` on every live signal) | ❌ | ❌ | NOT built. No `models/cf_outcome_xgb.pkl`. `signals.predicted_peak_profit_pct` not populated forward. |
| §4.2 predicted-peak-as-TP | ❌ | n/a | NOT built. (TP path is the cont.65 capital-ladder, unrelated.) |
| §4.3 gate-override at reduced size (**THE intervention**) | ❌ | ❌ | NOT built. The mechanism that would actually TAKE a high-EV rejected long does not exist. |
| §4.5 regret auto-tuner / §6 judge (9.1) | ❌ | ❌ | NOT built. (Judge decision lives in memory `[[project_judge_replaces_entry_gates]]`, paper-only, not yet wired.) |
| F9 `filter_change`→`BRAIN_FILTER_OVERRIDES` (existing actuator) | ✅ | negligible | Wired, but fed by only ~957 decoded rows → ±drift is noise-level. |

**Bottom line:** F9/F12 is still a **postmortem narrator**, exactly the §1 problem it set out to
fix. The only closed-loop actuation reaching the accept path is the bounded filter-override drift,
fed by <2% of CFs. The "predicted profit column on every signal" + the gate-override intervention
(§4.3) — the user's actual ask — are unbuilt. Checklist §8 still: only `[x] DB measured`.

**Dependency:** §4.1/§4.3 are starved by the **CF evaluation backlog** (202k pending, 12 days
behind — see `signal_monitor_replay_pool.md` cont.69s status). Fix CF throughput FIRST or any
predictor trains on stale, sparse labels.

---

## 1. Problem statement (user's words distilled)
F9/F12 currently produce **postmortem English commentary** on rejected signals that "would have won." The commentary is read-only — no gate, sizer, or TP consumes it. The bot's commentary writer never becomes the bot's decision-maker. The user wants the bot to:
- attach a **predicted profit** column to every live signal,
- use that prediction as the take-profit target (or some other actionable form),
- **intervene** in future decisions so the predicted profit actually gets captured.

---

## 2. Confirmed data (queried 2026-05-29, 30-day window)
| Metric | Value |
|---|---|
| Counterfactuals tracked | 32,124 |
| cf_wins (rejected but would've won) | 10,392 (32.35%) |
| avg peak profit on cf_wins | 15.99% (median 9.95%, p75 18.21%) |
| avg drawdown on cf_losses | −12.26% |
| miss_decode_reason rows that are NULL or freeform essay | ~95% |
| dominant structured reason (the few that exist) | all variants of `signal_too_weak` |
| dominant (direction, regime) of cf_wins | long × bull = 9,815 / 10,392 (94%) |

**Two load-bearing conclusions:**
1. **Asymmetric EV gap**: avg win peak (+15.99%) vs avg loss drawdown (−12.26%) → a CF-trained predictor that beats coin-flip on which is which is automatically positive-EV.
2. **The commentary itself is the bottleneck**: 95% of `miss_decode_reason` is empty or unique English. There is no aggregatable signal to feed back. Structure the cause before predicting the cure.

---

## 3. Confirmed (from code, read 2026-05-29) — CORRECTS §2

**F9 and F12 are in `metacognition/decoders.py` (232 LOC) + `metacognition/actuator.py` (227 LOC). The loop IS already closed — partially.** My original §2 framing ("nothing reads the commentary") was wrong. Verified by reading source.

What actually exists today:
- F9 emits JSON `{decode_reason, filter_change: {action, magnitude, rationale}}` from LLM
- F12 emits JSON `{decode_reason, scorer_change: {action, magnitude, rationale}}` from LLM
- F9's `filter_change` → `actuator.apply_filter_change()` → Redis `BRAIN_FILTER_OVERRIDES` → `signals/engine.py` layers onto GA defaults at brain_stage 3
- F12's `scorer_change` → Redis `BRAIN_SCORER_OVERRIDES` → similar consume path
- Bounded action vocab (F9: 5 actions; F12: 7), bounded magnitudes (small/medium), hard drift caps (±10 strength, ±0.15 weight), F30/F46 governance gate, Redis audit counters
- `counterfactuals.miss_decode_reason` column holds the prose ONLY. The actionable structured payload goes to Redis, never to Postgres

**The actual gap** (the user's exact intuition, sharpened):
- The LLM picks `"small"` or `"medium"` — labels, not numbers
- It never sees or emits the actual **predicted miss size in $** for the rejected signal
- So a decode for a 5% miss and a decode for a 150% miss both produce the same `±0.02` Redis delta
- The loop cannot be proportional to the size of the regret because the numeric quantity isn't in the schema

## 3.1 Revised Step 1 — `predicted_peak_profit_pct` becomes the load-bearing number (~150 LOC + 1 migration)
1. Extend F9 prompt + JSON schema to require `predicted_peak_profit_pct` (numeric) and `tag` (enum from a 8-tag vocab) alongside the existing `decode_reason` + `filter_change`. F12 same with `predicted_pnl_diff_usdt`.
2. Migration adds:
   - `counterfactuals.predicted_peak_profit_pct numeric(8,4)`
   - `counterfactuals.miss_tag text` (enum constraint)
   - `counterfactuals.miss_tag_evidence jsonb`
3. Extend `_apply()` in `actuator.py` to read `predicted_peak_profit_pct` and scale magnitude proportionally (still bounded by `_F9_MAX_DRIFT`). Logic:
   - `if predicted < 5%`: magnitude stays small
   - `if 5% ≤ predicted < 20%`: magnitude bumps to medium
   - `if predicted ≥ 20%`: magnitude = medium × min(predicted/20, 3.0) — still capped at MAX_DRIFT
4. Also writes `predicted_peak_profit_pct` to a new `signals.predicted_peak_profit_pct` column at decode time AND projects it forward when a similar signal recurs (joined on (pair, direction, regime, signal_strength bucket)).

This Step 1 alone gives the user a "predicted_profit column on signals" — their direct ask — using infrastructure that already exists.

## 3.2 Pending (background agent still in flight, can confirm or supersede §3.1)
The agent will also report: LLM provider/cost per call, exact celery batching cadence (currently at celery_app.py:2188 for F9, :2308 for F12), and whether any other consumer reads `miss_decode_reason` today. None of those change §3.1's surface — only its dev-day estimate.

---

## 4. Six directions (pick any combination)

### 4.1 — CF-trained profit predictor (SPINE — required by 4.2, 4.3, 4.5, 4.6)
- New artifact: `/opt/trading-bot/models/cf_outcome_xgb.pkl` (LightGBM/XGBoost; small, fast, no GPU)
- **Features** (all available at brain_stage 3): `signal_strength`, `market_regime`, `direction`, pair embedding, `timeframe`, `debate_verdict`, `direction_confidence`, `potential_score`, all gate-input booleans/numerics at decision moment
- **Targets** (multi-head): `peak_profit_pct` (regression) AND `would_have_won` (classification)
- **Output attached to live signals**: `predicted_peak_profit_pct`, `predicted_drawdown_pct`, `p_win`, `model_version`
- **Retraining**: nightly cron on rolling 90-day CF + closed-trades window
- **Schema delta**: `ALTER TABLE signals ADD COLUMN predicted_peak_profit_pct numeric(8,4), ADD COLUMN p_win numeric(5,4), ADD COLUMN predicted_drawdown_pct numeric(8,4), ADD COLUMN predictor_version text;`

### 4.2 — Predicted profit AS take-profit (direct user ask)
- Replace fixed-bar TP with `TP_dynamic = predicted_peak × 0.7` (give back 30% to the trailing runner)
- Floor: `1 × ATR(14)` so noise doesn't flat-exit
- Ceiling: respect ≥80% profit-lock ratchet (memory: `feedback_profit_lock`)
- Wired in `risk/exit_manager.py` (path to confirm via agent)

### 4.3 — Gate-override at reduced size (THE intervention path)
- For every gate that today says "reject", compute `EV_$ = p_win × predicted_peak × notional − (1−p_win) × predicted_drawdown × notional`
- If `EV_$ > $X` AND `p_win > 60%` AND segment-R² > 0.4 (from 4.6) → **override**, take the trade at 25–50% normal size
- New table: `gate_overrides(signal_id, gate_name, ev_estimate_usd, sizing_factor, realized_pnl)` for closed-loop audit
- This is the bot stopping its silent rejections and instead taking calibrated risk

### 4.4 — Structured cause-tag schema (kill freeform commentary)
- Replace freeform `miss_decode_reason` with strict JSON-first F9/F12 output:
  ```json
  {"tag": "signal_too_weak|regime_mismatch|gate_X_blocked|cooldown|sizer_zero|…",
   "tag_confidence": 0.0-1.0,
   "evidence": {"feature": "value", ...},
   "prose": "<existing essay, kept as audit log>"}
  ```
- Validator drops rows whose `tag` is not in the enum (silent-rejection rule says we log the drop in Redis counter)
- Aggregatable tags become the input to the auto-tuner (4.5)
- 1-day change, unblocks measurement of everything else → **build this first**

### 4.5 — Per-gate regret auto-tuner (closed loop, no human)
- Weekly cron, per `(gate × regime × direction)` segment:
  - `regret_$ = Σ(predicted_peak × notional on cf_wins from this gate) − Σ(realized loss × notional on cf_losses prevented)`
- Decision:
  - `regret_$ > +$Y for 2 consecutive weeks AND gate is parametric` → loosen by 1 step (bounded by GA-set min/max)
  - `regret_$ < −$Y` → tighten by 1 step
  - Else: hold
- Hard guardrails: min/max enforced by `feedback_strategy_override_clamp` memory (signal-strength clamp [15,40])
- Emits a digest to the user every Sunday for sanity check

### 4.6 — Shadow execution & promotion (ground the predictor in reality)
- Every rejected signal with `predicted_peak > 5%` fires a 0.1% notional shadow trade (real money, tiny size)
- 24-hour outcome logged → `shadow_outcomes(signal_id, predicted, realized, segment)`
- Compute **rolling segment R² (predicted vs realized)** weekly:
  - R² > 0.4 in a segment → predictor allowed to drive sizing/overrides for that segment (4.3)
  - R² < 0.2 → demote, fall back to current gate behavior in that segment
- This is the only protection against ML overfit on the CF dataset

---

## 5. Even-more-advanced lens (different direction)
**Each gate becomes a Beta-binomial bandit.** Every CF outcome is an arm pull; reward = `realized_peak − would-have-been-drawdown`. Thompson sampling decides per-signal whether to honor the rejection. Gates that consistently lose money on their rejections get statistically muted without any GA fitness path. Unifies 4.3 and 4.5 under one mechanism and is more sample-efficient than weekly regret aggregation.

Tradeoff: more invasive blueprint change; harder for the user to reason about than per-gate thresholds. Recommend as a **phase 2** once 4.5 has run for 4+ weeks.

---

## 6. Sequencing — LOCKED by user 2026-05-29 (hybrid path)

User decision: **Tier 1 #1, #2, #3 first → then SOTA Meta-RL-Crypto judge replaces #4, #5, #6.**

| Order | Direction | Days | Status |
|---|---|---|---|
| 1 | 4.4 structured tags (JSON-first F9/F12 output) | 1 | locked |
| 2 | 4.1 CF-trained predictor (`predicted_peak_profit_pct`, `p_win`, `predicted_drawdown_pct` on every signal) | 2–3 | locked |
| 3 | 4.2 TP from prediction (TP = predicted_peak × 0.7) | 1 | locked |
| 4 | **9.1 Meta-RL-Crypto judge replaces the predictor as the deployment surface** | 4–5 | locked (replaces 4.3+4.5+4.6) |
| 5 | 9.2 Trade-R1 DSR reward shaping inside the judge's meta-trainer | 1 | locked |

≈9–11 dev-days total. Same headline duration but cleaner architecture than the 6-direction plan.

### 6.1 Judge authority — LOCKED
**Full authority on entry, gates only on exit. Paper-only first.**
- Brain stage 3: judge's output replaces ALL existing entry gates. Gates F1–F12 still run to populate the judge's feature vector, but their veto power is removed from the entry path.
- Exit path: existing trailing SL, profit-lock ratchet, breakeven shield, chandelier, wickless TP — all preserved unchanged.
- **Paper-mode only** for the first 4 weeks. Live promotion requires shadow-R² > 0.4 across long-bull (94% of CFs) AND positive paper-mode realized PnL over ≥500 trades.
- **Kill switch**: judge auto-disabled if reject rate or take rate moves >3σ from rolling baseline (re-use `feedback_rl_deadlock_detector` pattern).

### 6.2 Prose handling — LOCKED
F9/F12 LLM prose **kept as audit log only**. JSON tag + numeric predictions become the load-bearing output. Prose stored in `signals.audit_prose text` column; never read by gates/sizer/TP. Aligns with silent-rejection-rule (every decision still has a structured trail).

---

## 7. Open questions for user (Rule 5 — these may justify blueprint redesign)
1. **ML target shape**: regression on peak_pct, classification on would_have_won, or both heads?
2. **Override sizing**: fixed 25% / 50% / Kelly-fraction × p_win?
3. **Shadow notional**: 0.1% real money or simulated-only?
4. **Deprecate F9/F12 prose?** Or keep prose as audit log alongside the JSON tag?
5. **Full-Deploy mode interaction**: with `bot:full_deploy_mode=1` already routing 100% capital, where does shadow notional come from — carve-out from main allocation or top-of-stack reserve?

---

## 8. Checklist (Rule 6)
- [x] DB measured (cf rates, regret, segment dominance)
- [ ] F9/F12 code paths confirmed (background agent — pending)
- [ ] User picks which directions to greenlight
- [ ] Blueprint section drafted for each greenlit direction
- [ ] Schema migration written + reviewed
- [ ] Predictor training pipeline scaffolded
- [ ] Shadow execution path + R² guard wired
- [ ] Sunday digest cron added

---

## 9. State-of-the-art replacements for F9/F12 (online research, 2026)

The user asked: is there a better, more advanced version of F9/F12 already out there? Three research lines from late-2025 / 2026 are directly relevant. Each maps onto a specific gap in the current postmortem-commentary design.

### 9.1 Meta-RL-Crypto — actor/judge/meta-judge (Sep 2025, arxiv 2509.09751)
**This is the most direct replacement for F9/F12 as currently designed.**

Current F9/F12 = end-of-window narrator. Meta-RL-Crypto's **judge** = online evaluator that fires *at decision time*, producing three structured outputs:
- `action_quality_score` (does this trade align with current state?)
- `confidence_adjustment` (how uncertain is the judge?)
- `magnitude_recommendation` (sizing factor)

Architecture:
```
actor (proposes trade) → judge (evaluates state + action) → outputs gate/size signal
                                ↓
                       feedback to next state
                                ↓
                       meta-judge updates judge weights weekly
```

Concrete port to our bot:
- F9/F12 become a single `judge` model invoked at brain_stage 3 (not post-exit)
- Judge consumes: proposed signal, current portfolio state, all gate inputs
- Judge emits: `(p_take, sizing_factor, predicted_peak, predicted_drawdown)` — directly feeds 4.1 and 4.3
- Meta-judge runs weekly on closed trades + CFs, retrains judge with reward = `realized_pnl - predicted_pnl`
- **Replaces the postmortem narration entirely.** Audit prose can still be generated for human review but is no longer the "output."

### 9.2 Trade-R1 — DSR reward shaping (Jan 2026)
Tackles the "reasoning quality vs outcome" problem. Today our GA optimizes on realized PnL alone, which means a lucky bad reason gets reinforced. Trade-R1's **DSR (Dynamic-Effect Reward)** ties reinforcement to whether the reasoning that produced the trade was *valid*, not just whether it made money:

```
if profitable:   reward = pnl × (0.5 + reasoning_score)
if unprofitable: reward = pnl × (2 − reasoning_score)
```

- A win with bad reasoning gets discounted (anti-lucky-fool)
- A loss with good reasoning gets softened (don't punish the right call)
- **Reasoning score** = how internally consistent the signal's feature evidence is

Concrete port:
- Add `reasoning_score` to every signal (LLM-judged or rule-based check across F1–F12 evidence)
- Apply DSR multiplier in the regret auto-tuner (4.5) and predictor training (4.1)
- This is the cure for our `signal_too_weak` over-rejection problem: if the rejection reasoning is weak, regret penalty is amplified; if strong, regret is dampened — so we don't loosen a gate just because of noise

### 9.3 Causal Transformer for Counterfactual Outcomes (CT, arxiv 2204.07258)
Canonical architecture for estimating "what would have happened under treatment A vs treatment B" with time-varying confounders. Exactly the math for "what would this rejected trade have made?" — current heuristic CF tracking is a weak approximation.

Concrete port (phase 2):
- Replace the heuristic peak_profit_pct tracker with a CT model
- Trained on closed trades (treatment = "we took it") + CFs (treatment = "we rejected it")
- Handles selection bias: rejected signals weren't drawn from the same distribution as accepted ones
- Output: same shape as 4.1's predictor but with *causal* (not just predictive) interpretation

### 9.4 CryptoForecastCF — gradient-based CF explanations (2025, PMC12840074)
Less directly applicable. Generates "what historical perturbation would have flipped the prediction." Useful as an **explanation layer** for the judge's outputs, not as a decision driver. Optional — only if user wants gradient-attribution dashboards.

### 9.5 What this means for sequencing
The §6 sequence still holds, but reframe:
- §4.4 (structured tags) = prerequisite, unchanged
- §4.1 (predictor) → upgrade to **judge model** (Meta-RL-Crypto §9.1). Same training data, different deployment surface (decision-time, not postmortem)
- §4.5 (regret auto-tuner) → upgrade with **DSR shaping** (§9.2). Same cron, smarter reward signal
- §4.2, §4.3, §4.6 unchanged
- **Phase 2** (post-launch): swap the predictor for a Causal Transformer (§9.3) once CT data volume is sufficient (~50k trades)

### 9.6 Open source we should look at before building
- **freqtrade + FreqAI** (github.com/freqtrade/freqtrade) — open-source adaptive ML self-tunes to live market. Already does some of 4.1. Worth reading their `freqai` module before scaffolding ours.
- **asavinov/intelligent-trading-bot** — different feature-engineering pipeline philosophy; possible inspiration for §4.4 structured tags.

---

## 10. Session handoff
- Started: 2026-05-29
- Source data: `counterfactuals` table, 30-day window, queried directly
- Related memories: `[[feedback_profit_lock]]`, `[[feedback_strategy_override_clamp]]`, `[[feedback_silent_rejection]]`, `[[feedback_full_deploy_mode]]`, `[[feedback_rl_deadlock_detector]]`
- Background agent (general-purpose) in flight: F9/F12 code read; will refine sections 4.1 evidence-features and 4.4 schema once it returns
- Next session: start with user's direction picks from §7, then refine §4.1 / §4.4 with agent's findings
