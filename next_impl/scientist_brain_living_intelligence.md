# Scientist-Brain Living Intelligence Kernel

Status: **GOAL DESIGN LOCKED 2026-06-09; implementation not started**.
Parent goal: `next_impl/scientist_brain_launchpad.md`.

## 1. Objective

Turn the scientific CPU into a learning system that can:

1. Reconstruct why a trade was opened and how it evolved.
2. Distinguish direction, selection, timing, sizing, exit, execution, and random-outcome faults.
3. Ask what bounded equation/code change might improve that class of situations.
4. Test the change against exact historical paths and comparable cohorts.
5. Reject hindsight-only, redundant, unsafe, or statistically weak changes.
6. Promote only changes with reproducible incremental utility and a rollback path.

The goal is **not** to let an LLM rewrite the live bot after every loss. One loss is a hypothesis
seed, never proof. Wins must also be audited because a proposed "fix" may damage behavior that was
already correct.

## 2. Current evidence and why the existing audit is insufficient

The current `signals/scibrain/audit.py` runs just after a trade opens. It is therefore an
**ex-ante decision-risk audit**, not a post-outcome audit. It has no realized PnL or future path
when it proposes `REDUCE`, `CLOSE`, or a formula change.

Verified on 2026-06-09:

- GWEIUSDT audit: `wrong_direction_risk=0.7`, recommendation `REDUCE`, proposed down-weighting
  chaos. Actual result: **win, +0.9584 USDT**.
- POWERUSDT audit: `wrong_direction_risk=0.8`, recommendation `REDUCE`, proposed down-weighting
  Koopman. Actual result: **win, +0.4865 USDT**.
- POWER's narrative named wavelet as the primary driver and mean-reversion modules as the
  counter-case, but the proposed code target was Koopman. This is an ungrounded target jump.
- Across 151 closed SciBrain trades carrying audits, average predicted wrong-direction risk was
  `0.5842`, while the current direction-failure label rate was `0.3841`; Brier score was `0.3069`.
- Of 93 audits with risk >= 0.6, 50 were wins and 43 were losses; their average PnL was positive.
- Risk `0.8` occurred 18 times but only 6 were labelled direction failures.
- All 151 closed audited trades had an audit verdict that agreed with the opened direction. The
  current auditor is producing risk language but has not demonstrated useful sign disagreement.
- Descriptive, selection-biased primary-driver history also does not support immediate blanket
  down-weighting: chaos drove 20 closed SciBrain trades with 15 wins and average `+0.6315` USDT;
  Koopman drove 16 with 12 wins and average `+2.8749` USDT. These are not causal estimates, but
  they are enough to reject an untested direct downgrade.

These numbers do not prove the entries are optimal. They prove the LLM risk score is currently
uncalibrated and must not be treated as causal evidence or direct mutation authority.

Additional current limitations:

- Fusion attribution is **arithmetic contribution**, not causal contribution.
- `execution/diagnosis.py` estimates the opposite direction at the actual trade's exit price. That
  is not a path-aware opposite-policy simulation because the opposite trade would have different
  SL/TP/exit events.
- `signals_at_entry` stores module outputs and attribution, but not a complete immutable replay
  snapshot with code/config/formula versions and action propensities.
- Existing AI Scientist, OPRO, decoder actuator, GA, DSL miner, IC learner, MemRL, and research
  systems are useful but fragmented. They do not share one experiment registry and promotion gate.

## 3. Non-negotiable intelligence constitution

1. **No single trade changes a live formula.** A trade may only create or update a hypothesis.
2. **LLMs propose and criticize; deterministic evaluators decide.**
3. **Loss is not synonymous with wrong direction.** A correct direction can lose from timing,
   sizing, stop placement, impact, fees, or path noise.
4. **Win is not synonymous with correct logic.** A bad decision can win by luck.
5. **Every proposal needs a typed target, cohort, intervention, expected effect, and falsifier.**
6. **Every claimed cause must survive an intervention.** Narrative correlation is not causation.
7. **Changes are bounded, sparse, versioned, reversible, and shadow-first.**
8. **The system learns from false alarms and failed hypotheses, not only losses.**
9. **All autonomous improvement mechanisms use the same experiment and promotion kernel.**
10. **The kernel must be allowed to conclude `NO_CHANGE` or reject an attractive equation.**

## 4. Two distinct audit loops

### 4.1 Ex-ante Decision-Risk Audit

Rename/reframe the current immediate-open audit. Its job is to predict:

- probability the direction is wrong;
- probability abstaining is better;
- probability size should be reduced;
- evidence conflict and uncertainty;
- proposed emergency action, shadow-only until calibrated.

It is a probabilistic forecaster. At close, grade it:

```text
Brier_wrong = mean((p_wrong - y_wrong)^2)
calibration_bin(p) = observed_wrong_rate for forecasts near p
```

`y_wrong` is not simply `trade lost`. Until the path-aware lab exists, current
`failure_type='direction'` is only a temporary proxy.

The audit must persist the **actual provider/model**, prompt hash, schema version, latency, and all
input evidence so the auditor itself can be compared, calibrated, demoted, or replaced.

### 4.2 Ex-post Outcome Causal Audit

This runs only after close and after the required forward horizon has matured. It receives:

- immutable entry snapshot;
- full position life trace;
- realized net outcome;
- future market path;
- deterministic counterfactual replay results;
- similar historical cohorts;
- current and prior hypotheses.

It identifies which fault class is supported:

```text
direction | abstain/selection | entry_timing | sizing | exit | execution |
regime_transition | model_conflict | random/unresolved
```

It can create a `ChangeSpec`; it cannot apply one.

## 5. Immutable scientific event ledger

### 5.1 EntrySnapshot

Capture at every scored candidate and every open:

```text
snapshot_id, trade_id/candidate_id, timestamp, symbol
SensorFrame or deterministic raw-data replay pointers
UniverseFrame version/pointer
all ModuleOutputs, router state, fusion terms, selected action
all feasible alternative actions and their logging propensities
code commit/hash, config hash, formula registry version, model versions
execution quote/spread/depth/slippage state
LLM provider/model/prompt/schema versions
```

The raw state must be sufficient to reproduce the decision. Storing only an explanation is not
sufficient.

### 5.2 LifeTrace

Capture bounded events through the position:

```text
marks/candles, MFE, MAE, regime changes, module re-scores
SL/TP/DCA/partial-close changes, execution fills, fees, funding, LLM/brain interventions
```

### 5.3 OutcomePacket

At close and at standardized horizons:

```text
actual utility, net PnL, return on capital, CVaR contribution, drawdown, hold time
exit reason, failure labels with confidence
path-aware counterfactual utilities
audit forecast calibration result
```

## 6. Counterfactual Digital Twin

The central evaluator replays the same market path under bounded interventions:

```text
do(direction = opposite)
do(action = abstain)
do(entry_delay = k bars)
do(module_i_gain = 0)                 # leave-one-module-out
do(module_i_gain = bounded_candidate)
do(router_profile = candidate)
do(size = candidate)
do(SL/TP/exit policy = candidate)
```

All simulations include fees, spread, slippage, funding, leverage, liquidation, and realistic
event ordering. Market-impact-sensitive interventions are marked uncertain unless the impact model
supports them.

Utility is multi-objective:

```text
U = net_pnl / capital
    - lambda_tail * tail_loss
    - lambda_dd * drawdown
    - lambda_cost * fees_slippage_funding
    - lambda_turnover * turnover
```

For module `i` in context/cohort `C`, causal value is estimated from interventions, not prose:

```text
Delta_i(C) = E[U(policy) - U(policy with do(gain_i=0)) | C]
```

Approximate Shapley/interaction tests may be used when modules interact, but only after simpler
leave-one-out and pairwise ablations.

The best hindsight action for one path is an **oracle upper bound**, not a promotable rule. A
candidate is useful only if the same pre-entry rule improves unseen comparable paths.

## 7. Typed hypothesis and formula-change language

Every LLM or algorithm proposal compiles to a bounded `ChangeSpec`:

```json
{
  "hypothesis_id": "uuid",
  "target": "router.gain.chaos",
  "role": "gate",
  "context_predicate": "router.regime == mean_revert && statphys_soc.regime == subcritical_stable",
  "intervention": {"kind": "gain_multiplier", "old": 1.10, "candidate": 0.90},
  "parameter_bounds": [0.50, 1.25],
  "evidence_ids": ["trade-or-cohort ids"],
  "expected_effect": "improve conditional utility without increasing tail loss",
  "falsifier": "LCB(delta_utility) <= 0 or CVaR worsens",
  "complexity_cost": 1,
  "status": "proposed"
}
```

Use a safe equation DSL/registry rather than arbitrary Python edits. The existing
`ml/dsl_grammar.py`, `ml/dsl_evaluator.py`, and `ml/llm_alpha_dsl.py` are reusable foundations, but
their evaluators must pass the same no-lookahead and promotion rules.

A preferred bounded router adaptation is sparse and interpretable:

```text
gain_i'(x) = clip(gain_i(x) * exp(delta_i^T z(x)), gain_min, gain_max)
```

where `z(x)` is an approved context vector and most coefficients in `delta_i` are zero.

## 8. LLM scientific council

Use multiple constrained roles:

1. **Forensic Analyst** - summarizes the entry decision and outcome evidence.
2. **Causal Skeptic** - attacks the proposed cause, finds confounders, and requests interventions.
3. **Experiment Designer** - defines cohort, treatment, controls, metrics, and falsifier.
4. **Formula Engineer** - emits one bounded `ChangeSpec`, not unrestricted code.
5. **Independent Reviewer** - checks leakage, target grounding, semantic consistency, and whether
   the deterministic results support the conclusion.

Mandatory rejection checks:

- proposed target was not named or measured in the evidence;
- regime label is treated as directional evidence without checking signed vote;
- recommendation contradicts deterministic attribution/replay;
- change only fixes the one observed path;
- cohort support is too small;
- proposal duplicates an existing/rejected hypothesis;
- output cannot compile to the typed DSL.

Reflexion-style verbal memory is useful for hypothesis generation and avoiding repeated mistakes,
but verbal reflection never substitutes for an evaluator.

## 9. Experiment engine

Each `ChangeSpec` moves through:

```text
proposed -> compiled -> unit/synthetic tested -> historical replay
-> purged walk-forward -> rolling shadow -> paper/canary -> owner-approved live
-> retained | demoted | rolled_back | rejected
```

Evaluation requirements:

- matched cohorts by regime, pair class, liquidity, volatility, horizon, and module-state vector;
- accepted and rejected candidates, with action propensities where available;
- doubly robust/IPS off-policy estimates for selected-action bias;
- purged walk-forward folds and embargo;
- negative controls, placebo changes, and ablation;
- realistic costs and path-aware exits;
- expectancy, utility, CVaR, drawdown, calibration, and stability; win rate is descriptive only;
- minimum effective sample size, not merely 50 clock cycles;
- lower confidence bound on incremental utility above zero;
- online multiple-testing/FDR or e-value budget so endless proposals do not manufacture discoveries;
- complexity penalty so a simpler equal-performing equation wins.

When support is sparse, use baseline bootstrapping: the candidate may act only in supported
contexts and must fall back to the current champion elsewhere.

## 10. Memory architecture

The living brain keeps four memory types:

- **Episodic:** immutable snapshots, life traces, outcomes, and postmortems.
- **Semantic:** validated conditional laws such as "module X adds value in context C."
- **Procedural:** versioned promoted formulas, parameters, and rollback artifacts.
- **Negative memory:** rejected changes, failed tests, false-alarm audits, and reasons.

The existing trades/counterfactuals tables, pgvector/MemRL, improvement ledger, DSL rejected-factor
list, and feature governance can be reused, but SciBrain needs module-state embeddings and a shared
experiment registry.

## 11. Global self-improvement unification

The following existing mechanisms must become proposal producers under this kernel, not independent
promotion paths:

```text
SciBrain remediation ledger
AI Scientist / strategy research
OPRO prompt changes
F9/F12 decoder actuator
GA parameter evolution
LLM alpha DSL factors
online IC/router learning
world-model / direction-model updates
```

Every mechanism receives the same versioning, evaluator, shadow, canary, rollback, and dashboard
contract. No subsystem may bypass it because its delta is "small."

## 12. Research foundations

- Reflexion (arXiv:2303.11366): verbal feedback plus episodic memory can improve agent behavior.
- Doubly Robust Policy Evaluation and Learning (arXiv:1103.4601; 1503.02834): evaluate candidate
  policies from biased logged actions with lower bias/variance.
- Safe Policy Improvement with Baseline Bootstrapping (arXiv:1712.06924): fall back to the baseline
  where evidence is insufficient.
- Causal Forests (arXiv:1510.04342): estimate heterogeneous treatment effects by context.
- Conformal Risk Control (arXiv:2208.02814): calibrate a policy against explicit risk losses.
- Online multiple testing with e-values (arXiv:2311.06412): control false discoveries across a
  continuous hypothesis stream.
- The AI Scientist (arXiv:2408.06292): automate idea, experiment, result, and review stages.
- AlphaEvolve (arXiv:2506.13131): evolve code through evaluator feedback and population diversity.

These works justify architecture patterns. They do not prove a trading change is profitable.

## 13. First implementation order

1. Rename the current immediate-open display to **Decision-Risk Audit** and add outcome calibration.
2. Build the immutable EntrySnapshot/LifeTrace/OutcomePacket ledger.
3. Build a path-aware digital twin for actual/opposite/abstain/module-ablation/action replays.
4. Add deterministic target-grounding checks and reject inconsistent LLM recommendations.
5. Introduce typed `ChangeSpec` and a single experiment registry.
6. Connect close events to the Ex-post Outcome Causal Audit.
7. Evaluate router-gain changes first; they are bounded and interpretable.
8. Unify existing self-improvement mechanisms under the promotion kernel.
9. Add champion/challenger shadow and canary promotion with automatic rollback.
10. Only then permit formula discovery/evolution to propose broader changes.

## 14. Integration with the Cognitive Operating System

Detailed design: `next_impl/scientist_brain_cognitive_os.md`.
Visual replay/control-room design: `next_impl/scientist_brain_visual_launchpad.md`.

The Living Intelligence Kernel is the scientific learning and promotion constitution for the wider
Cognitive Operating System. It supplies the experience, causal evaluator, and governance that keep
the brain-like components from becoming an uncontrolled collection of self-modifying models.

The relationship is:

```text
Living Intelligence ledger
  -> rich episodic memory
  -> shared BeliefState/world-model training sequences
  -> causal replay and competence evidence
  -> ModelChangeSpec/ChangeSpec
  -> one experiment and promotion kernel
```

The Cognitive OS adds a typed global workspace, self-supervised sensory learning, fast episodic and
slow semantic memory, uncertainty-aware world-model planning, offline risk-sensitive hierarchical
RL, metacognition, and sleep/consolidation. Every one of those components remains a proposal
producer or shadow challenger until it passes the same deterministic gates in this document.

Additional non-negotiable rules:

1. A learned latent representation is not evidence unless it improves a downstream falsifiable
   task without leakage.
2. A world-model dream is synthetic evidence and must be anchored by real-path replay.
3. RL is trained offline/digital-twin first and falls back to the current baseline outside support.
4. Fast online learning is restricted to bounded residual/calibration tasks before broader action
   authority is considered.
5. Memory replay is importance-weighted and includes wins, false alarms, abstentions, and rejected
   actions, not only losses.
6. Metacognitive competence and abstention are evaluated as first-class outputs.
7. Hard risk/execution controls are outside end-to-end learning and cannot be optimized away.

The primary human interface for this kernel is the **Trade Autopsy Theatre** and **Learning
Laboratory** defined in the visual Launchpad goal. They replay actual and counterfactual paths,
fault probabilities, evidence lineage, `ChangeSpec` effects, experiments, promotion state, and
rollback history visually while preserving exact values and immutable evidence IDs in drill-down.
