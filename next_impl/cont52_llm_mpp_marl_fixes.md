# Cont. 52 — LLM Cooldown + MPP Baseline + MARL Deadlock Guard

**Date:** 2026-05-27
**Trigger:** "Why no trades happening" — root-caused to MARL minute agent always
returning `skip` (100% of 8,014 calls, no log). Adjacent issues: LLM all-providers
cooldown synchronisation; MPP `mean_reward` uniformly negative.

## Confirmed root cause (Rule 3 — two-pass on disk)
- `signals/engine.py:1162` set `accepted=False` on MARL minute=`skip` with **no log**.
- `models/marl_minute_agent.zip` (May 25 checkpoint) had collapsed to all-skip policy.
- `llm/providers.py:29` `_COOLDOWN_SECONDS = 300` → after a burst all 5 providers
  cool down together and their TTL windows expire together → next burst hits all
  again synchronously.
- `world_model/model.py:623` picks `best_action = argmax(action_scores)` over
  *absolute* values; baseline is never subtracted → reported `mean_reward` is the
  raw NN output, biased negative by training-data prior.

## Plan (all three fixes, ranked by blast radius)

### F-A — LLM provider cooldown desynchronisation
File: `llm/providers.py`
- Lower `_COOLDOWN_SECONDS` 300 → 75 (just above 60 s rate-limit window,
  + safety margin, matches Groq/LiteLLM-Router community guidance: 62-75 s).
- Add per-call **jitter** (random 0-20 s additive) on each `mark_cooldown` so
  providers exit cooldown at staggered times rather than simultaneously.
- Add **proactive RPM check**: track per-provider call count in 60-s rolling
  Redis window; skip the provider (without burning a 429) when in-window
  count ≥ 80 % of `free_rpm`.
- Keep 86400 s TTL for status 404 (model not found — won't recover until
  config change).
- Source: LiteLLM `cooldown_time=62`; Groq community FAQ; Portkey docs.

### F-B — MPP no-trade baseline normalisation
File: `world_model/model.py`
- After the rollout loop in `plan_best_action`, subtract the `hold` action's
  score from all other actions' scores → `relative_scores[a] = score[a] −
  score["hold"]`.
- Pick `best_action = argmax(relative_scores)` (mathematically equivalent for
  ranking — hold becomes 0; trades positive ↔ they beat passive holding).
- Return `mean_reward = relative_scores[best_action]` so the dashboard /
  consume side sees a sign-meaningful number (positive ⇒ "model predicts this
  trade beats holding").
- Action-selection ordering is preserved; only the reported scalar changes
  sign sometimes. No downstream gate change required.
- Source: arXiv 2506.04358 (risk-aware RL reward), arXiv 2511.00190 (deep RL
  for trading).

### F-C — MARL minute / day anti-deadlock guard
File: `signals/engine.py`, `brain/soar.py`
- Already added skip-side logging + deadlock counter in cont. 52 step 1.
- Add **runtime check**: when `marl:minute:deadlock_detected == "1"`, the
  F21 block in `signals/engine.py` short-circuits as if no checkpoint is
  loaded (`minute_action` forced to `"enter"`).
- Mirror for `marl:day:deadlock_detected` in `brain/soar.py` (day-agent
  capital sizing) so a future day-side regression doesn't silently shrink
  every trade to zero.
- The flag is reset only when a human renames/replaces the checkpoint and
  restarts (deliberate: prevent silent re-enable of a broken model).

## Files to modify
1. `llm/providers.py` (cooldown TTL + jitter + RPM proactive check)
2. `world_model/model.py` (`plan_best_action` baseline subtraction)
3. `signals/engine.py` (deadlock-flag short-circuit for F21)
4. `brain/soar.py` (deadlock-flag short-circuit for F21 day agent)

## Verification (Rule 3)
- After rebuild + restart, watch for:
  - `mpp_planned mean_reward=...` — should now show **positive** values when
    trades are expected to beat hold, negative only when hold genuinely wins.
  - `llm_provider_call_complete` events distributed across 2-3 providers
    rather than all hitting the same provider.
  - No `marl_minute_rejected action=skip` logs (checkpoint still renamed,
    so passthrough is "enter").
  - Trades continue to open at the rate observed post-fix (~3 per minute).

## After-impl handoff
- Delete this file once cont. 52 lands and the verification block passes.

### Late addition — Fix D (100 % capital deployment, user-mandated mid-session)
- `signals/engine.py` F12 block: new `bot:full_deploy_mode` Redis flag (default "1"
  when missing; explicit "0" to revert) bypasses both F12 upward scaling AND
  the engine-side F16 Kelly down-cap.
- Engine recomputes `balance / slots_remaining` locally rather than trusting
  `brain_state.default_capital_usdt` (which carries Kelly / Day-Agent down-caps
  baked in by `brain/soar.py:_act()`).
- Brain still logs `marl_day_sizing` and `kelly_sizing` every cycle for the
  dashboard, but engine ignores those values in full-deploy mode — that's
  log noise, not a functional issue. Could clean up later by gating those
  log lines on `not bot:full_deploy_mode`.
- Verification: log event `capital_full_deploy` shows `capital=fair_share`
  matching `balance / slots_remaining` (no longer Kelly-floored at 5 %).
- Memorised as [[full-deploy-mode]] — mandatory paper + live trading.
- New patterns mined → memorize:
  - "Silent rejection without log is the worst kind of bug — every reject
    path needs a log + counter."
  - "RL checkpoints can drift into degenerate policies; always guard the
    consume side with a deadlock detector."
