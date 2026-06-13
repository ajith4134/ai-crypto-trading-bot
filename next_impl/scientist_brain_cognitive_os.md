# Scientist-Brain Cognitive Operating System

Status: **GOAL DESIGN LOCKED 2026-06-09; implementation not started**.
Parent goals: `scientist_brain_launchpad.md` and `scientist_brain_living_intelligence.md`.
Visual control-room goal: `scientist_brain_visual_launchpad.md`.

## 1. Objective and scientific boundary

Build a governed, brain-inspired cognitive architecture around the Scientist Brain so it can:

1. Learn useful representations from continuous multi-market experience.
2. Maintain an explicit uncertain belief about hidden market state.
3. Remember individual episodes quickly without overwriting slow statistical knowledge.
4. Predict, imagine, plan, act, audit itself, and know when evidence is insufficient.
5. Improve from wins, losses, false alarms, regime changes, and rejected hypotheses.
6. Preserve hard safety constraints while every learned component remains fallible.

The target is a **functional computational brain**, not a claim of biological consciousness. Brain
regions and neuromodulators below are engineering roles and typed learning signals. A biological,
physics, quantum, or mathematical analogy is admitted only when it defines:

```text
observable inputs -> explicit computation -> bounded output -> falsifiable benchmark
```

The goal is not to activate every advanced method. It is to organize the strongest suitable methods
into one learning system and reject methods that add no incremental utility.

## 2. Current capability audit: advanced parts exist, but no unified brain exists

The repository already contains substantial learning machinery. The gap is coordination, state
quality, shared learning signals, and governance.

| Existing capability | Functional role | Current limitation to fix |
|---|---|---|
| `world_model/model.py` RSSM | latent world model / imagination | observations and trade-life sequences are too sparse; planning is not yet a full learned policy/value system |
| PPO day/minute agents in `ml/marl*` | hierarchical action controllers | mostly one-step contextual-bandit training; hour agent unused; weak observations; not true coordinated MARL |
| `ml/maml.py` | rapid regime adaptation | task construction is synthetic and weak; adaptation target is too narrow |
| EWC + replay in `ml/continual_learning.py` | slow continual learning | replay episodes lack rich state, path, action, and outcome data |
| fast/slow memory, MemRL, Q-learning | episodic/value memory | memories are fragmented and lack shared module-state embeddings and causal outcomes |
| CandleNet MAE, TFT, PatchTST, Mamba, Chronos, NHITS | sensory/temporal encoders | separate outputs; no shared latent belief or controlled representation competition |
| GNN and multiscale GNN | relational market cortex | not integrated into one dynamic world graph and shared belief state |
| curiosity / SOAR hypotheses | intrinsic motivation | curiosity is not budgeted by risk, information value, and experiment capacity |
| DSL miner, GA, AI Scientist, OPRO | hypothesis/equation generation | fragmented evaluators and promotion paths |
| SciBrain router, audit, Living Intelligence design | attention, metacognition, causal improvement | no global workspace, mature outcome loop, or integrated plasticity policy yet |
| current quantum module | quantum-inspired spectral signal | no QPU and no demonstrated quantum advantage; useful math must be benchmarked classically |

This architecture reuses these components. It does not replace working models merely to make the
system look more brain-like.

## 3. Functional brain map

### 3.1 Brainstem and autonomic system: non-negotiable safety

**Role:** keep the organism alive.

Components:

- hard leverage, liquidation, margin, exposure, cooldown, and execution constraints;
- kill switches, stale-data guards, health checks, rollback, and baseline fallback;
- Control Barrier Function-style safety projection around any learned action;
- emergency tail/anomaly reflexes from EVT, liquidity, impact, and system-health signals.

The learned brain may propose an action `a_raw`; the brainstem emits the closest admissible action:

```text
a_safe = argmin_a ||a - a_raw||^2
         subject to risk_state_next(a) in SafeSet
```

No neural, RL, LLM, quantum-inspired, or evolutionary component may bypass this layer.

### 3.2 Sensory cortex: self-supervised multimodal perception

**Role:** turn raw market streams into useful representations before asking for a trade label.

Modalities:

- candles and multi-timeframe returns;
- order flow, OFI, VPIN, spread, depth, fills, liquidations;
- funding, OI, basis, cross-asset and graph state;
- model/module outputs, regime estimates, news/sentiment when available;
- bot-internal state: capital, exposure, latency, health, and recent interventions.

Learning objectives:

```text
L_perception =
    w_mask * masked_reconstruction
  + w_latent * future_latent_prediction
  + w_cross * cross_modal_alignment
  + w_contrast * contrastive_predictive_loss
  + w_invariance * approved_invariance_loss
  + w_bottleneck * information_bottleneck_cost
```

Use the existing CandleNet MAE and sequence models as expert encoders. Add a JEPA-like latent
predictor so the system learns predictable market structure without requiring every example to have
a profit label. Representation promotion is based on downstream forecast/control utility, not
reconstruction beauty.

### 3.3 Thalamus and salience router: sparse attention and bandwidth control

**Role:** decide which evidence reaches the shared workspace and which experts receive compute.

```text
salience =
    expected_information_gain
  + decision_relevance
  + anomaly_or_changepoint
  + risk_urgency
  + memory_match_value
  - compute_cost
  - redundancy_penalty
```

Use sparse Mixture-of-Experts routing with explicit load balancing, evidence-family correlation
penalties, and abstention. Unlike the current regime router, the cognitive router selects not only
direction modules but also memory retrieval, planning depth, audit depth, and learning rate.

### 3.4 Association cortex and global latent workspace

**Role:** create one typed, uncertain, multimodal belief that specialized systems can read.

The workspace is not a free-form LLM chat. It is a bounded event bus plus a shared latent state:

```text
BeliefState_t:
  latent_mean, latent_covariance_or_ensemble
  regime_posterior, changepoint_posterior
  graph_state, liquidity_state, tail_state
  portfolio_state, execution_state
  active_goals, active_hypotheses
  retrieved_episodes, uncertainty_decomposition
  source_ids, versions, timestamp
```

Specialists compete to publish typed messages. A limited set is broadcast to planning, action
selection, memory, risk, and metacognition. This follows the useful computational part of Global
Workspace theory: integration and selective broadcast among specialized modules. It does **not**
claim consciousness.

### 3.5 Hippocampus: fast episodic memory and prioritized replay

**Role:** remember a novel event after one experience and retrieve structurally similar episodes.

Store rich episodes from the Living Intelligence ledger:

```text
EntrySnapshot -> LifeTrace -> OutcomePacket -> CounterfactualPacket
```

Each episode receives a priority:

```text
replay_priority =
    |reward_prediction_error|
  + surprise
  + tail_severity
  + hypothesis_relevance
  + model_disagreement
  + rarity
  - redundancy
```

Use pattern-separated embeddings so a rare failure is not averaged away. Replay must include wins,
false alarms, rejected actions, near misses, and safety interventions. Importance weighting corrects
the sampling bias created by prioritized replay.

### 3.6 Neocortex: slow semantic learning and consolidation

**Role:** extract stable laws across many episodes without catastrophic forgetting.

The slow system learns shared representations and world dynamics, conditional module value and
causal interactions, regime prototypes, calibrated risk/outcome models, and semantic rules.

Use replay, EWC/synaptic-intelligence-style regularization, adapters or expert expansion, and
versioned consolidation. New learning is accepted only if it preserves old benchmark competence:

```text
L_slow = L_new
       + lambda_ewc * sum_i F_i (theta_i - theta_i_old)^2
       + lambda_replay * L_replayed_episodes
       + lambda_calibration * L_calibration
```

Recent continuum-memory ideas are research candidates for multiple update frequencies, not immediate
production dependencies.

### 3.7 Basal ganglia: action selection and risk-sensitive value learning

**Role:** choose among abstain, long, short, size, timing, and management options.

Treat trading as a partially observed, constrained control problem. The action selector receives the
shared `BeliefState`, not raw disconnected indicators.

```text
strategic option: abstain | trend | reversion | breakout | hedge | unwind
tactical action: direction | delay | enter | add | reduce | hold | close
execution action: order type | size slice | price tolerance | timing
```

Use offline RL first because live exploration with real funds is unsafe. Candidate methods are
Conservative Q-Learning or Implicit Q-Learning, hierarchical/options RL, distributional and CVaR
objectives, baseline fallback, and digital-twin interaction before any live authority.

Reward is multi-objective and path-aware:

```text
r_t =
    delta_net_utility
  - lambda_tail * tail_cost
  - lambda_dd * drawdown_cost
  - lambda_impact * market_impact
  - lambda_turnover * turnover
  - lambda_uncertainty * unsupported_action
```

### 3.8 Cerebellum: fast prediction-error correction

**Role:** make rapid, low-risk corrections to timing, calibration, and execution.

Suitable tasks:

- correct forecast bias and probability calibration;
- predict slippage, fill probability, and short-horizon movement;
- adjust entry timing within a bounded policy;
- learn residual errors on top of stable baseline models.

This path learns faster than the semantic cortex but has narrow authority and bounded outputs. It is
the first target for safe online supervised learning because errors and labels mature quickly.

### 3.9 Prefrontal cortex: planning, counterfactual reasoning, and scientific control

**Role:** deliberate before changing behavior.

Components:

- RSSM/JEPA-style world model with ensemble epistemic uncertainty;
- MuZero-style value/reward/policy-relevant latent planning where useful;
- model-predictive control and bounded search over action sequences;
- Living Intelligence Counterfactual Digital Twin;
- LLM scientific council for hypothesis generation, criticism, and experiment design;
- explicit goals, compute budgets, and stopping rules.

The planner must compare imagined futures with reality. Model error limits planning authority:

```text
planning_weight = clip(1 - normalized_model_error - epistemic_uncertainty, 0, 1)
```

An ensemble disagreement or out-of-support state shortens the planning horizon and increases
baseline fallback.

### 3.10 Amygdala and insula: anomaly, uncertainty, and tail-risk reflex

**Role:** detect danger before slow reasoning finishes.

Inputs include adverse EVT probability, liquidity/impact spikes, liquidation cascades, model
disagreement, calibration failure, distribution shift, and system-health faults. Outputs are typed
`risk`, `gate`, or `size_cap` messages, never disguised direction votes. The reflex can veto or
reduce an action; it cannot invent a larger position.

### 3.11 Metacortex: self-model and calibrated ignorance

**Role:** estimate what the brain knows, where it is weak, and whether it should act.

Track aleatoric versus epistemic uncertainty; calibration by regime, pair class, direction, horizon,
and model version; out-of-distribution/support distance; and competence maps for every expert/action.

The key output is:

```text
P(action_supported | belief, evidence, model_versions)
```

The brain must be rewarded for correct abstention and penalized for confident unsupported action.

## 4. Neuromodulators as typed global learning signals

These are computational analogies, not biological claims.

| Signal | Computation | Controls |
|---|---|---|
| dopamine-like | temporal-difference/reward prediction error `delta_t = r + gamma V(s') - V(s)` | value learning, replay priority, credit assignment |
| acetylcholine-like | expected uncertainty / observation noise | attention to sensory evidence, learning rate, reliance on priors |
| norepinephrine-like | unexpected uncertainty / BOCPD changepoint surprise | exploration budget, rapid adaptation, model reset request |
| serotonin-like | patience, downside sensitivity, time preference | horizon, discounting, CVaR aversion; analogy remains explicitly limited |
| homeostatic signal | expert overuse, weight growth, compute and error imbalance | pruning, load balancing, regularization, sleep scheduling |

Every signal is bounded, versioned, logged, and attributable. No global signal directly edits live
weights; it requests a learning or evaluation action under the promotion kernel.

## 5. Core learning loops

### 5.1 Perception loop: predict before labeling

```text
observe -> encode modalities -> predict masked/future latent
-> measure prediction error and uncertainty -> improve representation in shadow
```

This consumes all market experience, including periods with no trades.

### 5.2 Belief-update loop: predictive coding and Bayesian filtering

Maintain a posterior over hidden state:

```text
p(z_t | o_1:t, a_1:t-1)
proportional_to
p(o_t | z_t) * integral p(z_t | z_t-1, a_t-1) p(z_t-1 | history) dz_t-1
```

Top-down world-model predictions meet bottom-up residuals. Precision weighting determines how much
to trust each. Active inference may be tested as a belief-update and epistemic-exploration
formalism, but it does not replace risk-sensitive RL without benchmark evidence.

### 5.3 Outcome learning loop: credit assignment by intervention

```text
episode closes -> mature horizon -> replay actual and alternatives
-> assign fault probabilities -> update hypothesis evidence
-> train only after cohort and promotion gates
```

This is the Living Intelligence Kernel. A trade outcome updates evidence; it never directly rewrites
a live formula.

### 5.4 Sleep loop: replay, consolidation, dreaming, and pruning

Run offline or in isolated shadow jobs:

1. Replay high-value episodes and matched controls.
2. Generate bounded counterfactual trajectories in the digital twin.
3. Consolidate repeated episodic structure into semantic models.
4. Recalibrate probabilities and uncertainty.
5. Run adversarial and tail-event rehearsal.
6. Prune redundant experts/features and apply homeostatic regularization.
7. Compare new versions against protected competence benchmarks.

“Dreams” are synthetic training/evaluation scenarios, never direct evidence of profitability.

### 5.5 Developmental curriculum

Do not train the full brain end-to-end from the start. Grow competence in order:

```text
stage 1: data integrity + immutable experience
stage 2: self-supervised perception + calibrated forecasts
stage 3: shared belief state + memory retrieval
stage 4: world model + counterfactual planning
stage 5: offline hierarchical/risk-sensitive RL
stage 6: meta-learning, continual consolidation, and scientific self-improvement
```

Each stage must pass fixed benchmarks before the next receives authority.

## 6. Mathematical and physics research program

### 6.1 Required foundations

- **POMDPs and Bayesian filtering:** the market state is hidden and observations are noisy.
- **Predictive coding / variational inference:** learn from precision-weighted prediction errors.
- **Information bottleneck, MDL, and rate-distortion:** preserve decision-relevant information
  while penalizing representation and hypothesis complexity.
- **Information geometry and optimal transport:** measure belief, regime, and model-manifold drift.
- **Dynamical systems, Koopman, reservoirs, and state-space models:** model multiscale nonlinear
  temporal evolution with interpretable spectral and efficient recurrent tools.
- **Causal representation and interventions:** distinguish predictive correlation from changes that
  survive `do(...)` tests.
- **Stochastic optimal control, HJB, Lyapunov, and barrier functions:** optimize while preserving
  hard safety sets.
- **Non-equilibrium/statistical physics:** use entropy, criticality, relaxation, and energy-budget
  concepts only where they produce measurable diagnostics.
- **Renormalization/multiscale coarse-graining:** test whether stable macro-state variables can be
  learned across horizons; treat the deep-learning/RG link as a design clue, not proof of edge.
- **Neural operators/SPDE surrogates:** sandbox tools for learned dynamics across functions/scales;
  promote only if they beat simpler sequence/world models.

### 6.2 Quantum and quantum-inspired program

No claim is made that the biological brain is quantum or that this VPS has a quantum advantage.
Approved research directions are concrete:

1. **Tensor networks / matrix-product states:** compressed representations of high-order
   cross-asset/time/module interactions; benchmark parameter count, latency, and utility against
   low-rank, Transformer, Mamba, and GNN baselines.
2. **Density-matrix belief representation:** positive-semidefinite trace-one representation for
   mixtures/interactions; test entropy/Bures metrics against ensemble covariance and probability
   simplex methods.
3. **Quantum-inspired sampling and linear algebra:** use only when a classical implementation
   improves compute or representation; compare against strong classical baselines.
4. **Quantum kernels or variational circuits:** QPU/simulator sandbox only. Require a matched
   classical-kernel benchmark, realistic data-loading cost, and out-of-sample advantage.
5. **Open-system/decoherence analogy:** optional explicit model of belief forgetting and environment
   coupling; reject unless it improves calibration or regime adaptation.

Important negative result: quantum-inspired classical algorithms have removed claimed quantum
speedups in some ML settings. Therefore “quantum” is never itself an admission argument.

### 6.3 Advanced ML/RL/neural portfolio: use by role, not by count

“Use all advanced ML” means the research system can evaluate the important method families against
the role they are suited for. It does **not** mean every method runs in the live decision path.

| Method family | Candidate brain role | Required comparison or rejection test |
|---|---|---|
| masked autoencoders, CPC, JEPA, multimodal contrastive learning | sensory representation | downstream forecast/control utility and leakage-free linear probes |
| Transformers, TFT, PatchTST, Chronos, NHITS | long/irregular temporal experts | compare calibration, latency, and incremental utility by horizon |
| selective state-space models / Mamba | efficient long-sequence memory | compare against Transformer and simple state-space baselines |
| GNN, temporal GNN, equivariant/sheaf challengers | dynamic relational market cortex | prove value beyond correlation/factor/standard-GNN baselines |
| ensembles, Bayesian neural challengers, conformal calibration | epistemic uncertainty and supported action | selective-risk, coverage, calibration, and abstention utility |
| RSSM, latent dynamics, MuZero-style models | imagination and planning | multi-step calibration and improved actions, not only prediction loss |
| diffusion/flow/generative sequence models | diverse scenario and tail rehearsal | coverage of real held-out paths; synthetic data cannot count as outcome evidence |
| offline, hierarchical, distributional, CVaR, and constrained RL | action selection/control | support-aware baseline improvement under realistic costs and tails |
| multi-agent RL | role-specialized coordination | distinct agents, shared state, real trajectories, coordination gain, and ablation |
| continual learning, EWC, adapters, progressive experts | slow learning without forgetting | new-task gain versus protected old-competence regression |
| MAML/meta-RL, hypernetworks, test-time adaptation | rapid regime adaptation | adaptation speed versus false adaptation and forgetting |
| sparse Mixture-of-Experts | thalamic routing and conditional compute | utility per compute, load balance, and redundancy reduction |
| active learning, curiosity, intrinsic motivation | choose valuable observations/experiments | realized information gain within explicit risk/compute budgets |
| causal representation/discovery and invariant learning | transportable state and intervention hypotheses | intervention, negative-control, and cross-regime invariance tests |
| neuro-symbolic models, safe DSL, program synthesis | interpretable equation/rule proposals | compilation, boundedness, falsification, complexity, and replay tests |
| evolutionary search, NAS, GFlowNets | diverse architecture/hypothesis generation | novelty plus evaluator value; never direct-to-live |
| reservoir, liquid-state, neural ODE/SDE models | fast nonlinear temporal substrate | beat simple recurrent/state-space models under equal compute |
| spiking/neuromorphic models | event-driven microstructure research only | reject unless event timing, power, or latency benefit is measurable on available hardware/data |

The default result for a method is rejection or sandbox retention. A simpler model wins whenever
utility is equal.

## 7. Unified learning contracts

Add these goal-level contracts before adding more learners:

```text
CognitiveMessage:
  source, role, evidence_family, belief_delta, uncertainty
  salience, horizon, support_distance, source_version, evidence_ids, ts

BeliefState:
  shared latent + calibrated posterior summaries + source/version lineage

Episode:
  EntrySnapshot + LifeTrace + OutcomePacket + CounterfactualPacket

LearningSignal:
  kind, magnitude, target, confidence, evidence_ids, authority, ts

CompetenceRecord:
  component, context, task, calibration, utility_delta, support, drift, version

ModelChangeSpec:
  bounded model/data/objective/update change plus expected effect and falsifier
```

All learning paths publish through the same experiment registry and promotion kernel defined in the
Living Intelligence goal.

## 8. Evaluation constitution

The system is not more intelligent merely because it has more models or lower training loss.

### 8.1 Component-level tests

- representation: future-latent utility, linear probes, stability, leakage tests;
- belief state: calibration, likelihood, state-transition prediction, OOD detection;
- memory: retrieval precision, novelty preservation, replay value, forgetting;
- world model: multi-horizon latent/reward/continuation calibration and planning usefulness;
- policy: offline policy value, CVaR, drawdown, support distance, baseline improvement;
- metacognition: selective-risk curve, abstention utility, error detection;
- sleep/consolidation: new-task gain versus protected old-task regression;
- router/workspace: incremental utility per compute and evidence redundancy.

### 8.2 System-level intelligence tests

The candidate brain must prove:

1. Better calibrated decisions, not only higher conviction.
2. Higher net utility and lower tail risk on purged walk-forward data.
3. Correct abstention under unsupported or shifted conditions.
4. Faster adaptation after real changepoints without catastrophic forgetting.
5. Improved counterfactual/planning choices when the world model is accurate.
6. Graceful degradation when any learned component is removed or corrupted.
7. Reproducible causal improvement from promoted changes.
8. CPU, memory, and latency compliance without starving risk/execution.

Every comparison uses champion, ablation, simple baseline, and compute-matched baseline.

## 9. Implementation order

### Phase A: cognitive spine

1. Complete the immutable Living Intelligence event ledger and path-aware digital twin.
2. Define `CognitiveMessage`, `BeliefState`, `LearningSignal`, and `CompetenceRecord`.
3. Build a read-only shared workspace from current module/model outputs with full lineage.
4. Add uncertainty decomposition, support distance, and metacognitive abstention reporting.

### Phase B: perception, memory, and belief

5. Train/evaluate a shared self-supervised latent from existing encoders; no action authority.
6. Upgrade episodic memory to rich episodes with prioritized, importance-weighted replay.
7. Build slow semantic consolidation with protected competence tests.
8. Upgrade the RSSM world model using real state/action/life-trace sequences and ensembles.

### Phase C: planning and safe learning

9. Add bounded latent planning and compare it with deterministic digital-twin actions.
10. Reframe existing PPO/MARL agents as hierarchical controllers and retrain offline on rich
    trajectories.
11. Add CQL/IQL, distributional/CVaR objectives, baseline fallback, and safety projection.
12. Give the cerebellar residual/calibration path limited shadow learning authority first.

### Phase D: whole-brain learning

13. Add typed neuromodulatory learning signals and sleep/consolidation jobs.
14. Add sparse cognitive routing/global broadcast with compute and redundancy penalties.
15. Connect model, router, representation, memory, and policy changes to one promotion kernel.
16. Add quantum-inspired tensor/density-matrix and neural-operator experiments only as
    compute-matched sandbox challengers.

### Phase E: visible cognition

17. Publish versioned `BrainGraphSnapshot`/`BrainPulse` views of the shared workspace, routing,
    belief, uncertainty, action, safety, memory, and competence.
18. Build a stable Cognitive Atlas with semantic zoom and exact evidence drill-down.
19. Add Trade Autopsy and Learning Laboratory replay only after immutable ledger/evaluator data
    exists.
20. Add the Universe Neural Field only after `UniverseFrame` and cross-asset graph contracts exist.

## 10. Explicit non-goals and rejection conditions

- Do not call disconnected models a brain.
- Do not call contextual-bandit PPO “multi-agent intelligence” until agents have real trajectories,
  distinct roles, shared state, and coordinated objectives.
- Do not let an LLM, RL agent, world model, or dream trajectory mutate live behavior directly.
- Do not optimize win rate as the main reward.
- Do not force all models into one latent if specialist information is lost.
- Do not use biological names as evidence.
- Do not add quantum, field-theory, neural-operator, spiking, or neuromorphic components without a
  concrete benchmark and compute-matched baseline.
- Do not train end-to-end across hard risk controls.
- Do not promote a self-improving component that cannot detect and report its own competence loss.

## 11. Primary research foundations

Neuroscience and cognitive architecture:

- [Predictive Coding Theories of Cortical Function](https://arxiv.org/abs/2112.10048).
- [A Complementary Learning Systems Approach to Temporal Difference Learning](https://arxiv.org/abs/1905.02636).
- [Prioritized Experience Replay](https://arxiv.org/abs/1511.05952).
- [Deep Learning and the Global Workspace Theory](https://arxiv.org/abs/2012.10390).
- [Synaptic Plasticity as Bayesian Inference](https://arxiv.org/abs/1410.1029).

Representation, world models, and memory:

- [DreamerV3](https://arxiv.org/abs/2301.04104).
- [MuZero](https://arxiv.org/abs/1911.08265).
- [I-JEPA](https://arxiv.org/abs/2301.08243) and [V-JEPA 2](https://arxiv.org/abs/2506.09985).
- [Mamba](https://arxiv.org/abs/2312.00752).
- [MAML](https://arxiv.org/abs/1703.03400).
- [Nested Learning](https://arxiv.org/abs/2512.24695) and [Titans](https://arxiv.org/abs/2501.00663).

Safe, offline, hierarchical, and risk-sensitive RL:

- [Conservative Q-Learning](https://arxiv.org/abs/2006.04779).
- [Implicit Q-Learning](https://arxiv.org/abs/2110.06169).
- [The Option-Critic Architecture](https://arxiv.org/abs/1609.05140).
- [A Distributional Perspective on Reinforcement Learning](https://arxiv.org/abs/1707.06887).
- [Risk-Sensitive and Robust Decision-Making: a CVaR Optimization Approach](https://arxiv.org/abs/1506.02188).
- [Control Barrier Function Based Quadratic Programs](https://arxiv.org/abs/1609.06408).

Mathematics, physics, causality, and quantum-inspired computation:

- [The Information Bottleneck Method](https://arxiv.org/abs/physics/0004057).
- [Towards Causal Representation Learning](https://arxiv.org/abs/2102.11107).
- [Active Inference: Demystified and Compared](https://arxiv.org/abs/1909.10863).
- [A Data-Driven Approximation of the Koopman Operator](https://arxiv.org/abs/1408.4408).
- [An Exact Mapping Between Variational RG and Deep Learning](https://arxiv.org/abs/1410.3831).
- [Fourier Neural Operator](https://arxiv.org/abs/2010.08895).
- [Supervised Learning with Quantum-Inspired Tensor Networks](https://arxiv.org/abs/1605.05775).
- [A Quantum-Inspired Classical Algorithm for Recommendation Systems](https://arxiv.org/abs/1807.04271).

These sources justify candidate mechanisms and architecture patterns. They do not prove trading
edge, biological equivalence, or quantum advantage.
