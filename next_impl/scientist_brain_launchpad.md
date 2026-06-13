# Scientist-Brain Launchpad — Master Design & Build Plan

Status: **ACTIVE BUILD — 14-module core live; goal expansion design updated 2026-06-09**. Tracker:
`scientist_brain_PROGRESS.md`. Rules in force: [[rules-claude-code]] (17) + [[rules-complete-code]]
(C1–C12, no stub/skeleton/fake code). Owner: 2026-06-08.

**Current reality (2026-06-09):** owner GO was given and Phases 1–4 are substantially built.
SciBrain is currently enabled in **real LIVE mode**, not paper/testnet. Every new component declares
an explicit authority level under Rule 14's Evidence, Authority, and Influence Gate. Low-risk
observability may deploy after tests; capital-affecting authority requires evidence, rollback, and
explicit owner promotion. Every applicable opened trade records applied and non-applied influence.

## 1. Owner vision (verbatim intent)
Turn the Launchpad into a live **AI Scientist Brain**: a circuit/CPU-style infrastructure of
PhD-level math/physics/quantum modules that (a) monitors all ~491 active signal pairs across
multi-timeframes, (b) does pattern + sequence discovery, (c) detects noise and **uses it to our
advantage**, (d) uses **Ollama** to self-interrogate WHY a long/short direction was chosen — the
EDENUSDT-style post-mortem, automated, to catch the "signal right, direction wrong" fault —
(e) shows **ALL internal workings live on the Launchpad screen**, (f) has access to every bot
model (ML/RL/external deps). On every login, auto-report % complete + what's left (DONE — hook
`scripts/scientist_brain_report.sh`).

## 2. Source material (already on this VPS — REUSE, do not reinvent)
- **Master PhD notes** `memory/discussion_master_notes.md` (25 blocks): 20 pattern-science
  domains, BOCPD, GFlowNets, Kelly+correlation, Information Geometry, Koopman, RMT, TDA,
  Optimal Transport, Mean-Field, Rough Path, EVT, Free Probability. **BLOCK 16 = the circuit/CPU
  architecture (the spine of this design).** BLOCK 11 = integrated pipeline.
- **Existing launch_pad** `signals/launch_pad/` (store/qualify/maintainer/shadow/gate, ~1.2k LOC)
  + `next_impl/launch_pad_10.md` (the 7-layer "center-core" redesign, decisions D1–D9 locked).
- **Live facts**: 491 pairs in `scanner:active_pairs`; Ollama `qwen2.5:14b-instruct` +
  `deepseek-r1:8b` + `qwen2.5-coder:7b`; models/: candlenet 1m–1h, hmm_regime, gnn, tft,
  patchtst, world_model, chronos, predict_all, direction, marl agents. Box: 10 vCPU / 32 GB.

## 3. Research references (web, updated 2026-06-09)
- **FinRL-X** (arXiv:2603.21330): AI-native modular infra; **single typed interface contract** is
  the sole coupling between swappable selection/allocation/timing/risk modules (rule-based + RL +
  LLM mixed freely). → our `ModuleOutput` is that contract. Validates BLOCK 16.
- **TradingAgents** (arXiv:2412.20138, TauricResearch): bull/bear researcher agents **debate**,
  emit **typed JSON + NL reasoning**, persistent **decision-log audit trail**, fast model for
  retrieval + deep model for decision. → our Ollama interrogator = bull/bear debate that audits
  direction and writes a human-readable log to the screen.
- **DMD regime detection** (arXiv:1904.09082): DMD **reconstruction error** detects regime
  transitions / transient dynamics. → KoopmanModule's regime + predictability output.
- **Reflexion** (arXiv:2303.11366): verbal feedback + episodic memory can improve an agent, but
  reflection remains a proposal/memory mechanism, not causal proof.
- **Doubly Robust Policy Evaluation** (arXiv:1103.4601; 1503.02834) + **SPIBB**
  (arXiv:1712.06924): evaluate candidate policies from biased logs and fall back to the baseline
  where evidence is sparse.
- **AI Scientist** (arXiv:2408.06292) + **AlphaEvolve** (arXiv:2506.13131): automate the
  hypothesis/experiment/review loop and evolve code through evaluator feedback. In this bot, the
  evaluator and promotion gate remain deterministic and shadow-first.
- **DreamerV3** (arXiv:2301.04104), **MuZero** (arXiv:1911.08265), and **I-JEPA**
  (arXiv:2301.08243): world models and self-supervised latent prediction support learning a compact
  belief state, imagining futures, and planning. They justify challengers, not direct live authority.
- **Complementary Learning Systems**, predictive coding, prioritized replay, and Global Workspace
  implementations motivate a governed fast-memory/slow-consolidation/shared-workspace architecture.
- **CQL** (arXiv:2006.04779), **IQL** (arXiv:2110.06169), distributional RL, CVaR control, and
  Control Barrier Functions define the safe-learning direction: offline first, tail-aware, and
  projected through non-learning safety constraints.
- Full brain-like learning research and staged architecture:
  `next_impl/scientist_brain_cognitive_os.md`.
- **React Flow**, **Cytoscape.js**, **Sigma.js**, semantic zoom, and information-loss-aware graph
  layout research support a readable interactive Cognitive Atlas instead of a text feed or
  decorative 3D brain. Full visual redesign: `next_impl/scientist_brain_visual_launchpad.md`.

## 4. Architecture — the CPU (BLOCK 16, made concrete)
```
  SENSOR BUS (Layer 0)  ── live features → per-pair SensorFrame (in RAM)
        │
  MATH MODULE BANK (Layer 1)  ── the "transistors / logic gates"
        │   each module = one PhD-math concept, emits the SAME typed ModuleOutput:
        │   Koopman · BOCPD · RMT · TDA · WaveletSpectral · Chaos · InfoTheory ·
        │   NoiseHarvest · Kalman · HMM-regime   (more added one-at-a-time)
        │
  META-ROUTER (Layer 2, MoE)  ── activates the right modules per pair × regime
        │
  FUSION ALU (Layer 3)  ── IC/info-geometry-weighted aggregation →
        │                  direction · conviction · expected_move · size_frac (Kelly)
        │
  OLLAMA SCIENTIST INTERROGATOR (Layer 4)  ── bull/bear debate over the module outputs:
        │   "why this direction? what's the counter-evidence? wrong-direction risk?"
        │   structured JSON verdict + NL narrative  (qwen2.5:14b / deepseek-r1)
        │
  DECISION → launch_pad buffer (existing store/qualify/maintainer)  →  engine open
        │
  LIVE VISIBILITY (Layer 5)  ── every module output, router choice, fusion math, and full
                                Ollama transcript streamed to Redis scibrain:* → dashboard panel
        │
  ONLINE RECALIBRATION (Layer 6)  ── fired-slot realized outcome → update module IC + router weights
```
Intelligence emerges from **composition**; each module is interpretable, debuggable, swappable
(BLOCK 16). No monolith, no black box.

### 4a. Expanded two-speed CPU architecture (goal update 2026-06-09)
The original per-symbol circuit is necessary but insufficient for the full scientist goal. Most
of the current 14 modules inspect one symbol at a time, so they can miss market-wide factor flow,
directed contagion, causal leaders, and distribution migration. The expanded CPU has four planes:

1. **Hot Pair Core** — the existing per-symbol modules. Runs every scan and emits standard
   `ModuleOutput` objects. Hard cap: approximately 20 admitted hot modules, not 50–100 votes.
2. **Universe Core** — computes the expensive cross-market objects once per cycle from an in-RAM
   `UniverseFrame`: return/feature matrices, sparse factors, directed lead-lag graph, Laplacian,
   transport distances, clusters, and market-tail state. It then emits standard per-symbol
   `ModuleOutput` objects, preserving the single interface contract.
3. **Risk & Control Plane** — tail-risk, portfolio allocation, market impact, and optimal stopping.
   These components may cap size, block risk, or manage exits; they do **not** get disguised as
   directional votes.
4. **Discovery Sandbox** — SINDy/PySR/GFlowNet and speculative physics/quantum ideas run offline or
   at `observe` authority. Promotion requires measured incremental information, not mathematical
   novelty.

This prevents correlated variants of the same price trend from outvoting genuinely independent
evidence. The goal is **orthogonal information and correct control**, not the largest module count.

### 4b. Cognitive Operating System — make the CPU learn as one governed brain

Detailed locked design: `next_impl/scientist_brain_cognitive_os.md`.

The existing repository already has an RSSM world model, PPO controllers, MAML, EWC/replay,
fast/slow memory, self-supervised CandleNet, GNNs, Transformers, Mamba, curiosity, evolutionary
research, and quantum-inspired spectral math. The missing capability is not another isolated model.
It is a cognitive spine that coordinates these parts around a shared uncertain state and common
learning/promotion rules.

Functional regions:

1. **Brainstem/autonomic safety** — immutable risk/execution constraints, barrier projection,
   baseline fallback, kill switch, and rollback. Learning never bypasses this region.
2. **Sensory cortex** — multimodal self-supervised encoders trained by masked, contrastive, and
   future-latent prediction rather than only sparse trade labels.
3. **Thalamus/salience router** — sparse attention and MoE compute allocation based on information
   gain, decision relevance, anomaly, risk urgency, redundancy, and compute cost.
4. **Association cortex/global workspace** — one typed `BeliefState` with latent uncertainty,
   regime, graph, liquidity, tail, portfolio, active hypotheses, and full lineage.
5. **Hippocampus/neocortex** — fast rich episodic memory plus slow replay/EWC-protected semantic
   consolidation, preserving rare events without catastrophic forgetting.
6. **Basal ganglia/cerebellum** — offline risk-sensitive hierarchical action selection plus fast,
   bounded residual correction for calibration, timing, and execution.
7. **Prefrontal/metacortex** — world-model planning, deterministic counterfactuals, LLM scientific
   deliberation, competence maps, calibrated ignorance, and rewarded abstention.
8. **Amygdala/insula** — fast tail/anomaly/liquidity/model-disagreement reflexes that can veto or
   reduce risk but cannot create a larger position.

The “living” learning cycle is:

```text
observe -> self-supervised representation -> uncertain belief update -> retrieve episodes
-> plan/select safely -> record full life trace -> causal outcome replay
-> prioritize memory -> consolidate during isolated sleep jobs -> propose bounded challenger
-> deterministic evaluation -> shadow/canary/owner promotion or rejection
```

Typed neuromodulator analogies coordinate learning: reward-prediction error, expected uncertainty,
unexpected uncertainty/changepoint surprise, downside-sensitive patience, and homeostatic
load/pruning signals. They are logged control messages, not permission for direct live weight edits.

Quantum/physics expansion remains strict: tensor networks, density-matrix beliefs, neural operators,
multiscale/RG, and open-system forgetting are research challengers only. Each needs a concrete
observable, compute-matched classical baseline, and out-of-sample incremental utility. Biological
or quantum terminology is never evidence.

## 5. The single interface contract (the spine — FinRL-X principle)
```python
@dataclass(frozen=True)
class SensorFrame:        # Layer-0 output, one per symbol
    symbol: str
    candles: dict[str, np.ndarray]    # tf -> OHLCV(+vol) matrix, newest-last
    ofi: float; vpin: float; funding: float; oi_delta: float; liq_burst: float
    cn_forecasts: dict[str, dict]     # tf -> CandleNet {dir1,dir3,mag3,trend,...}
    predict_all: dict | None
    ts: float

@dataclass(frozen=True)
class ModuleOutput:       # EVERY math module returns exactly this
    module: str
    direction: float          # signed vote in [-1,+1]  (sign = long/short, mag = strength)
    conviction: float         # [0,1] self-confidence
    expected_move_pct: float | None
    horizon_min: int          # the holding horizon this module speaks to
    regime_tag: str | None    # 'trending'|'mean_revert'|'chaotic'|'noise'|...
    features: dict            # the raw numbers it used (full transparency)
    explanation: str          # one-line human-readable ("DMD dominant mode |λ|>1 → +trend")
    reliability_ic: float | None   # rolling Information Coefficient (Phase 3 fills this)
    ts: float

@dataclass(frozen=True)
class Decision:           # Fusion ALU output
    symbol: str; direction: str           # 'long'|'short'|None
    conviction: float; expected_move_pct: float | None
    size_frac: float                      # Kelly × p(green) × conviction
    contributing: list[ModuleOutput]; regime: str; ts: float
```
A module fails by returning a low-conviction `ModuleOutput` (never raises, never None) so the
circuit degrades gracefully (BLOCK 16 robustness; C4 every-path-handled).

### 5a. Contract extensions required by the expanded goal
Keep `ModuleOutput` as the only input to fusion, but add explicit semantics so unlike components
cannot be mixed accidentally:

```python
role: Literal["direction", "gate", "risk", "context", "allocator", "exit"]
evidence_family: str   # trend, reversion, topology, cross_asset, tail, microstructure, ...
shadow_only: bool
```

Add `UniverseFrame`, built once per cycle and shared read-only:

```python
@dataclass(frozen=True)
class UniverseFrame:
    symbols: list[str]
    returns_by_tf: dict[str, np.ndarray]   # rows=time, cols=symbols
    feature_matrix: np.ndarray
    correlation: np.ndarray
    directed_lead_lag: np.ndarray
    liquidity: np.ndarray
    ts: float
```

Universe modules may compute one market object, then emit one normal `ModuleOutput` per symbol.
Fusion remains simple, while the CPU gains genuine cross-market intelligence.

## 6. Module bank — concrete math + PHYSICS + QUANTUM (maps to the 20 domains)
The bank is deliberately **physics-forward** — most of the edge ideas in the master notes are
physics, not just statistics. Grouped by discipline:

### 6a. PHYSICS modules (PhD-level physics scientist)
| Module | Domain | Physics | Real computation | Output meaning |
|---|---|---|---|---|
| **Koopman** | D7 | Ergodic theory / Hamiltonian dynamics; DMD from fluid dynamics | DMD on TF candle matrix → eigenvalues λ + reconstruction error | dominant-mode growth = trend dir; recon-error = regime transition (arXiv:1904.09082) |
| **Chaos** | D5 | Nonlinear dynamical systems | Hurst (R/S) + Lyapunov-exponent proxy + delay-embedding | H>0.5 trend / H<0.5 revert; λ>0 ⇒ unpredictable → caps conviction |
| **StatPhysSOC** | D6 | Statistical mechanics, self-organized criticality, phase transitions | susceptibility / power-law-tail / variance-divergence near critical point | crash / fat-tail early-warning (market near phase transition) |
| **MeanFieldIsing** | D17 | Ising model / mean-field games | order-parameter (net alignment) + coupling from OI/funding/flow | herding / crowding magnitude & direction (each trader vs the mean) |
| **LangevinHawkes** | D19 | Langevin / Fokker-Planck SDE + Hawkes self-exciting process | drift+diffusion fit; Hawkes intensity on trade/liq clustering | jump/cluster risk; self-exciting continuation vs exhaustion |
| **RMT** | D9 | Random Matrix Theory (nuclear physics origin) | Current V1: per-symbol SSA lag-covariance eigenspectrum + iteratively fitted Marchenko-Pastur noise edge | denoised structured trend vs noise; true universe covariance/factor separation belongs in the Universe Core |

### 6b. QUANTUM module (PhD-level quantum physics)
| Module | Domain | Quantum | Real computation | Output meaning |
|---|---|---|---|---|
| **Quantum** | D8 | Quantum info / QML | Von Neumann entropy of the (normalised) density/correlation matrix; Quantum-PCA spectral split; QFT frequency extraction | regime disorder (entropy) + dominant cycle (QFT) + quantum-PCA signal subspace. Quantum-*inspired* on CPU (no QPU); upgradeable to real QML later |

### 6c. MATH / STATS / SIGNAL modules
| Module | Domain | Real computation | Output meaning |
|---|---|---|---|
| **BOCPD** | Blk6/14 | run-length posterior, hazard λ matched to horizon | P(regime change) → gates conviction |
| **TDA** | D4 | persistent homology on delay-embedded price cloud | H1 persistence spike = structural break/crash topology |
| **WaveletSpectral** | D15 | CWT/DWT multi-scale energy + noise-floor estimate | scale where signal>noise; noise floor feeds NoiseHarvest |
| **InfoTheory** | D3 | Bandt-Pompe permutation entropy + Shannon sign entropy + within-symbol volume→price transfer entropy | predictability-gated momentum; cross-pair directed information belongs in CausalLeadLag |
| **NoiseHarvest** | D19 | OU fit (θ,μ,σ) on residual after trend strip | mean-reversion band → "use noise to our advantage" entries |
| **Kalman** | D2 | constant-velocity state filter | fair-value + velocity → direction & mispricing |
| **HMM-regime** | — | reuse `models/hmm_regime.pkl` | discrete regime label feeding the router |

Each is a standalone file `signals/scibrain/modules/<name>.py`, unit-runnable on a SensorFrame.
**VS-1 ships one physics module (Koopman — ergodic/DMD) + BOCPD**; Phase 2 adds the rest of the
physics + the quantum module one at a time.

### 6d. Sufficiency verdict
The current 14-module bank is **enough for a strong Version-1 pair-level scientific core**. It is
not enough for the complete owner vision because important information domains remain absent:

- Cross-pair structure is mostly missing; current modules are overwhelmingly single-symbol.
- Sequence order is weakly represented; many modules see summaries rather than path geometry.
- Tail probability is not explicitly calibrated, despite real-money operation.
- The fusion layer can count correlated evidence more than once.
- `size_frac` is a proxy, not a true correlation-aware portfolio Kelly solution.
- Entry intelligence is much more developed than optimal exit/execution control.

Adding more physics-sounding modules without fixing these gaps would make the CPU less reliable.

### 6e. Approved expansion modules — prioritized by incremental information

#### Tier A — build after UniverseFrame/cache; highest expected value
| Component | Plane / role | Real computation | Why it is new |
|---|---|---|---|
| **SparseFactorResidual** | Universe / direction+context | Robust PCA or low-rank+sparse decomposition; OMP/LASSO residual support | Separates market/sector factor flow from idiosyncratic alpha. The current RMT module is per-symbol SSA, not a true universe covariance decomposition. |
| **SpectralGraphContagion** | Universe / context+direction | Directed/signed asset graph, Laplacian spectrum, Fiedler gap, heat-kernel shock diffusion, centrality | Detects leaders, followers, clusters, contagion hubs, and where information has not propagated yet. |
| **CausalLeadLag** | Universe / direction | Sparse conditional Granger/PCMCI-style lag graph with stability selection; restricted to liquid leaders/clusters | Distinguishes directed predictive flow from correlation and bivariate transfer entropy. |
| **RoughPathSignature** | Hot pair / direction+context | Level-2/3 log-signature of lead-lag transformed `[return, volume, OFI, OI]` paths; online linear/nearest-motif forward-return scorer | Captures event order and path interaction: rise-then-volume is different from volume-then-rise even when summaries match. |
| **OptimalTransportRegime** | Universe / gate+context | Sliced Wasserstein/Sinkhorn distance from current rolling distribution to learned regime and winner/loser prototypes | Detects geometric distribution migration that KL, HMM labels, and point estimates miss. |
| **EVTLargeDeviationTail** | Risk / gate+size-cap | POT/GPD tail fit, tail asymmetry, exceedance clustering, expected shortfall, rare-event rate | Converts “fat-tail warning” into calibrated adverse-tail probability and a direct risk envelope. |

#### Tier B — valuable after Tier A proves the expanded contracts
| Component | Plane / role | Real computation | Purpose |
|---|---|---|---|
| **InformationGeometryHealth** | Recalibration / context | Fisher-Rao/Bures distance, MMD/HSIC, curvature/drift of module-output distributions | Detects model-manifold drift, nonlinear redundancy, and when historical IC is no longer transferable. |
| **MultifractalRG** | Hot pair / gate+horizon | q-order structure functions, nonlinear `tau(q)`, crossover scales, rough-volatility/Hurst surface | Selects the valid horizon and detects scale-regime transitions; must prove value beyond Chaos+Wavelet+SOC. |
| **ErgodicMixing** | Hot pair / gate+horizon | mixing-time / Ruelle-resonance proxy, KS/permutation entropy, ergodicity-breaking statistic | Estimates alpha half-life and whether the current sample is representative enough to trust. |

#### Control-plane components — higher economic value than more entry votes
| Component | Role | Goal |
|---|---|---|
| **CorrelationKellyAllocator** | allocator | Solve portfolio-level fractional Kelly with covariance, cluster caps, tail penalty, and available margin. Replace the current per-trade Kelly proxy. |
| **HJBOptimalStoppingController** | exit | Treat hold/close as a stochastic control and optimal-stopping problem; shadow against the existing SL/TP ladder before any authority. |
| **MarketImpactReflexivityController** | risk+execution | Estimate spread/slippage/Kyle-lambda/own-impact; reduce size or delay opens when the bot would erase its own edge. |

### 6f. Concepts retained in the research sandbox, not active votes
- **Additional “quantum” modules:** no count-for-show additions. The current Quantum module is
  quantum-inspired CPU spectral math, not a QPU. Quantum channels/Bures geometry belong inside
  InformationGeometryHealth unless they prove distinct incremental IC.
- **Free probability:** fold useful spectral tools into SparseFactorResidual/RMT; do not create a
  duplicate directional vote.
- **SPDE, CFT, statistical field theory, instantons:** scientifically valuable research language,
  but too assumption-heavy for the hot path until a concrete observable, estimator, and falsifiable
  trading hypothesis exist.
- **Malliavin calculus:** reserve for options/payoff sensitivity or stochastic-control gradients;
  current futures inputs do not justify a standalone vote.
- **Mean Field Games:** reserve until agent/population or deeper order-book-state data exists;
  MeanFieldIsing already covers the available crowding inputs.
- **Representation/category theory:** use as design constraints for invariance/equivariance, not as
  independent trade votes.
- **SINDy/PySR/GFlowNet:** use in the Discovery Sandbox to propose equations/modules; never let
  generated formulas directly trade without the full admission gate.

### 6g. Module admission constitution
No new component enters the live decision path merely because it runs or sounds advanced. It must:

1. State a falsifiable hypothesis and a distinct `evidence_family`.
2. Identify its unique input and prove it is not a renamed existing signal.
3. Define its correct role: direction, gate, risk, allocator, context, or exit.
4. Return deterministic, bounded, fully auditable output and abstain safely.
5. Meet a declared CPU/memory/cadence budget.
6. Pass synthetic invariants and adversarial tests.
7. Show incremental out-of-sample IC/utility **conditional on the existing bank**.
8. Survive ablation: removing it must measurably reduce performance or safety.
9. Declare authority and record an influence manifest on every applicable decision; promotion uses
   effective support, drift/failure telemetry, incremental utility/risk evidence, and rollback
   rather than an arbitrary cycle count.
10. Be rejected, merged, or demoted if redundant, unstable, or economically useless.

The router must also apply an **evidence-family correlation penalty** so five trend-derived modules
cannot count as five independent confirmations.

### 6h. Living Intelligence Kernel — causal self-improvement, not loss-chasing
Detailed locked design: `next_impl/scientist_brain_living_intelligence.md`.

The current opened-trade LLM audit is an **ex-ante Decision-Risk Audit**. It runs immediately after
open, before the outcome exists. It may predict danger, but it cannot know what caused a loss or
which formula would improve future trades.

Verified evidence on 2026-06-09 proves why this distinction matters:

- GWEIUSDT risk `0.7` and POWERUSDT risk `0.8` both produced `REDUCE` recommendations; both trades
  subsequently won (`+0.9584` and `+0.4865` USDT).
- Across 151 closed audited SciBrain trades, average predicted wrong-direction risk was `0.5842`
  while the current direction-failure label rate was `0.3841`; Brier score was `0.3069`.
- 93 high-risk (`>=0.6`) audits contained 50 wins and 43 losses with positive average PnL.
- All 151 closed audited trades had an audit verdict that agreed with the opened direction; the
  risk score has not yet demonstrated useful sign-disagreement skill.
- POWER's narrative blamed wavelet/conflicting mean-reversion evidence but proposed changing
  Koopman, demonstrating that an LLM recommendation can be internally ungrounded.

Therefore:

1. A loss or warning creates a **typed hypothesis**, never a direct formula/code mutation.
2. The system must grade and calibrate the auditor itself from realized outcomes and false alarms.
3. A second **Ex-post Outcome Causal Audit** runs only after close/horizon maturity.
4. A path-aware digital twin replays `opposite`, `abstain`, delayed-entry, module-ablation,
   bounded-weight, size, SL/TP, and exit-policy interventions with costs.
5. Causal value comes from cohort interventions:

```text
Delta_i(C) = E[U(policy) - U(policy with do(gain_i=0)) | context C]
```

6. LLM roles propose/criticize/plan experiments; deterministic evaluators decide.
7. All proposals compile to a bounded `ChangeSpec`/equation DSL, then pass purged walk-forward,
   off-policy evaluation, ablation, minimum effective sample, multiple-testing control, shadow,
   canary, owner promotion, and rollback.
8. Existing SciBrain remediation, AI Scientist, OPRO, F9/F12 actuator, GA, DSL miner, IC learner,
   and model updates must converge on one experiment registry and cannot bypass its gate.

## 7. Ollama Scientist Interrogator (Layer 4 — the "replace Claude" piece)
Per candidate (and MANDATORY per fired trade): build a structured prompt containing the
SensorFrame summary + every ModuleOutput + the Fusion Decision, and run a **bull/bear debate**
(TradingAgents pattern) on `qwen2.5:14b-instruct`, with `deepseek-r1:8b` as the reasoning
critic. Forced JSON schema:
```json
{ "verdict_direction":"long|short|none", "confidence":0-1,
  "key_drivers":[...], "counter_evidence":[...],
  "wrong_direction_risk":0-1, "agrees_with_fusion":true,
  "narrative":"plain-English EDENUSDT-style explanation" }
```
`wrong_direction_risk` + `agrees_with_fusion==false` = the **right-signal-wrong-direction
risk forecaster**. Full transcript persisted to `scibrain:<sym>:reasoning` and the trade row
(provenance) → shown live on the panel. It becomes a real post-mortem only after the Outcome
Causal Audit joins the forecast to realized path-aware evidence and grades the forecaster.

## 8. Live visibility (Layer 5) — Cognitive Atlas, not a wall of text

Detailed locked redesign: `next_impl/scientist_brain_visual_launchpad.md`.

The current `ScientistBrain.tsx` successfully exposes decisions, module evidence, attribution,
audits, and crash radar, but it is still a text/table feed. The target Launchpad is a stable,
interactive 2.5D neural-circuit atlas where every visual element encodes a real typed value.

Primary visual flow:

```text
sensors -> perception/modules -> thalamic router -> shared belief/fusion
-> planner/scientific council -> action selector -> brainstem safety -> execution

episodic memory <-> workspace <-> slow consolidation
tail/anomaly reflex -----------> safety veto
metacognitive competence ------> routing/planning/abstention
```

Required modes:

1. **Live Cognitive Atlas** — real signal pulses, active/suppressed paths, contribution,
   disagreement, uncertainty, final action, safety projection, and authority.
2. **Trade Autopsy Theatre** — synchronized replay of entry, life trace, exit, causal audit,
   actual/counterfactual paths, fault probabilities, and proposed formula/model change.
3. **Learning Laboratory** — hypothesis genealogy, experiment/promotion state, champion/challenger,
   rollback lineage, memory/replay, consolidation, and competence maps.
4. **Universe Neural Field** — semantic-zoomed cross-asset factors, clusters, lead-lag, contagion,
   shock propagation, positions, liquidity, and tail state.
5. **Safety & Authority** — proposed versus actual action, every cap/veto/fallback, model health,
   OOD/drift, exposure, margin, and current authority.

Recommended stack: React Flow for the rich stable Cognitive Atlas/experiment graphs, Sigma.js +
Graphology for the future universe graph, existing lightweight-charts for synchronized timelines,
and SVG/Canvas overlays for uncertainty and real event pulses. Three.js/WebGL remains optional
research for views where 3D proves better understanding; a rotating decorative brain is rejected.

The frontend receives versioned `BrainGraphSnapshot`, `BrainPulse`, `TradeReplayFrame`,
`LearningGraphSnapshot`, and `UniverseGraphSnapshot` payloads with immutable evidence IDs. It must
not infer scientific meaning from prose. Semantic zoom, stable layouts, uncertainty visualization,
reduced motion, accessible non-color encodings, exact-value inspectors, and text/table fallbacks are
mandatory.

## 9. Integration — scibrain REPLACES the Launch-Pad as the trade-origin (owner-confirmed 2026-06-08)
**Corrected goal:** the AI Scientist *replaces* launch_pad. Flow: Pair Scanner (~466 active pairs)
→ scibrain scores ALL of them → picks symbol+direction → **opens trades directly** (owner choice:
full replace; initially verified in paper and subsequently promoted to real LIVE). The launch_pad
gate/qualify/maintainer funnel is retired. NOT a shadow scorer feeding launch_pad.
- `signals/scibrain/gate.py` — `funnel_pairs(r)` mirrors the launch_pad gate contract but PICKS
  from the scanner universe via the circuit: score all → filter (conviction ≥ min) → rank →
  direction-balance + cooldown → ordered Decisions. `enabled(r)` = `scibrain:enabled=1`.
- `signals/scibrain/opener.py` — owns the open: for each pick, capital_usdt = scibrain Kelly
  `size_frac` × pool (capped by max_position); leverage = `risk.manager.assign_leverage`; SL =
  `risk.manager.compute_initial_sl`; qty = capital×lev/mark; then `engine.open_trade(params)`
  with full provenance (primary_driver, attribution, regime). BYPASSES the legacy ~40-gate
  gauntlet + generate_candidate_signals (scibrain replaces them).
- `signals/engine.py:process_signals` — a guarded scibrain branch at the top: if
  `scibrain.gate.enabled(r)` → delegate to scibrain.opener and return (legacy flow untouched when
  off). Kill switch `scibrain:enabled` instantly reverts.
- Reuses every existing feature in Redis (candles, OFI, VPIN, funding, OI, CandleNet) + the proven
  SL/leverage/executor; no new data feeds. Ollama via the existing client.
- CURRENT SAFETY FACT: SciBrain is now running with real funds. Existing cooldown/dedup/executor
  guards remain. New components declare `observe|advise|bounded_canary|live|veto` authority and
  cannot affect behavior beyond that authority. Applied and non-applied influence is persisted.

## 10. Compute budget (10 vCPU / 32 GB)
- **Hot Pair Core:** existing 8-worker pool; target no more than ~20 admitted hot modules.
- **Universe Core:** build `UniverseFrame` once, then run 4–6 cross-market modules once per
  15–60s cadence, not once per symbol. Cache matrices and graph factorizations in RAM.
- **Risk/Control Plane:** tail and allocator refresh on 30–300s cadence; exit controller evaluates
  open trades only.
- **Ollama/cloud audit:** top candidates + every fired trade, never all symbols.
- **Hard performance gates:** no Redis round-trip storm; current warm scan must not regress;
  after in-RAM cache, target sub-second pair scan plus independently budgeted universe refresh.
- **Capacity rule:** when a new component exceeds budget, optimize/move it to slower cadence or
  reject it. Do not starve the SL monitor or live executor.

## 11. Deploy / ops (carry-over facts)
- `signals/` is **bind-mounted into brain** → brain-side = restart only. Maintainer/celery beat =
  **REBUILD** (verify executor first, [[feedback_verify_task_executor]]). Dashboard = **REBUILD**.
- Kill switch `scibrain:enabled=0`; per-skip Redis counters (Rule 12); deadlock auto-disable.
- Real-money fact: SciBrain is currently live. New modules use independent authority switches and
  require risk-scaled evidence, ablation, effective support, rollback, and explicit owner promotion
  before first capital-affecting authority. There is no fixed cycle-count gate.
- Promotion unit is one component/role at a time. Never promote an entire expansion batch at once.

## 12. Build phases = fully-working vertical slices (C5; tracked in PROGRESS)
- **VS-1 (Phase 1):** SensorFrame+ModuleOutput contracts → SensorBus → **Koopman + BOCPD** →
  Fusion ALU v1 → Ollama interrogator → Redis stream → dashboard panel v1 → wired into buffer at
  `scibrain:enabled=0` → **runs on real live data, output shown.** This proves the whole circuit
  end-to-end with 2 real modules before scaling the bank.
- **VS-2..8:** existing 14-module core → meta-router → fired-trade direction audit → full-universe
  parallel scorer → dashboard → online recalibration.
- **VS-9:** `UniverseFrame` + SparseFactorResidual + SpectralGraphContagion, `observe` authority.
- **VS-10:** CausalLeadLag + RoughPathSignature + OptimalTransportRegime, one at a time.
- **VS-11:** EVTLargeDeviationTail + CorrelationKellyAllocator, risk/allocator shadow authority.
- **VS-12:** InformationGeometryHealth + evidence-family redundancy penalty + automatic prune/demote.
- **VS-13:** HJBOptimalStopping + MarketImpactReflexivity, shadowed against actual exits/execution.
- **VS-14:** immutable EntrySnapshot/LifeTrace/OutcomePacket + calibrated Decision-Risk Audit.
- **VS-15:** path-aware Counterfactual Digital Twin + Ex-post Outcome Causal Audit.
- **VS-16:** typed ChangeSpec + LLM scientific council + deterministic experiment registry.
- **VS-17:** unified champion/challenger promotion kernel for every self-improvement subsystem.
- **VS-18:** typed CognitiveMessage/BeliefState/LearningSignal/CompetenceRecord + read-only global
  latent workspace and metacognitive abstention report.
- **VS-19:** shared self-supervised perception challenger + rich episodic replay + protected slow
  consolidation, all at declared non-capital authority until promoted.
- **VS-20:** upgraded ensemble world model on real trajectories + bounded latent planning.
- **VS-21:** offline hierarchical CQL/IQL/distributional-CVaR controllers + safety projection,
  evaluated against the existing policy and deterministic digital twin.
- **VS-22:** typed neuromodulation + sleep/consolidation + whole-brain competence dashboard.
- **VS-V1:** split the current text-heavy panel and build a live stable Cognitive Atlas from the
  existing `/scibrain` payload with real event pulses and exact-value inspector.
- **VS-V2:** belief/uncertainty/router/safety/authority visualization plus pause/cycle replay.
- **VS-V3:** Trade Autopsy Theatre from immutable snapshots, life traces, outcomes, and digital-twin
  counterfactuals.
- **VS-V4:** Learning Laboratory for hypotheses, experiments, versions, memory, and competence.
- **VS-V5:** semantic-zoomed Universe Neural Field after `UniverseFrame` and cross-market graphs.

## 13. Goal decisions locked / next design order
- Owner GO and VS-1 decisions are complete; cloud-primary audit is the working reasoning path.
- The 14-module bank remains the **V1 core**. There is no target to reach 50–100 active votes.
- Build order for the expanded goal:
  1. Fix current live-path correctness and establish explicit authority/influence controls.
  2. Build the Living Intelligence instrument before expanding/evolving the bank:
     immutable snapshots → path-aware replay → typed hypotheses → evaluator → promotion kernel.
  3. Build in-RAM `UniverseFrame` and evidence-family metadata under that evaluator.
  4. Add SparseFactorResidual and SpectralGraphContagion first.
  5. Add CausalLeadLag, RoughPathSignature, OptimalTransportRegime one at a time.
  6. Add calibrated tail risk and true portfolio Kelly before more exotic entry modules.
  7. Add optimal stopping/impact control after shadow data is sufficient.
  8. Build the Cognitive OS spine and read-only shared belief after the immutable ledger exists.
  9. Upgrade perception, episodic memory, slow consolidation, and the world model before retraining
     action policies.
  10. Train offline risk-sensitive hierarchical policies; grant no learned live authority until
      metacognition, safety projection, and whole-system evaluation pass.
  11. Build visual VS-V1/V2 from existing data in parallel with the foundation, then unlock
      Autopsy/Learning/Universe views only when their typed backend evidence exists.
- Each component is independently accepted, merged, demoted, or rejected by the admission
  constitution. The scientist CPU must be able to conclude that a beautiful concept has no edge.
