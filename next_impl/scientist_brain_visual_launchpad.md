# Scientist-Brain Visual Launchpad — Cognitive Atlas Redesign

Status: **GOAL DESIGN LOCKED 2026-06-09; implementation not started**.
Parent goals:

- `scientist_brain_launchpad.md`
- `scientist_brain_living_intelligence.md`
- `scientist_brain_cognitive_os.md`

## 1. Objective

Replace the current text-heavy Scientist-Brain panel with an advanced, interactive visual system
that makes the CPU/brain understandable at a glance and fully inspectable on demand.

The interface must answer these questions visually:

1. What is the brain sensing now?
2. Which regions/modules are active, suppressed, uncertain, or unhealthy?
3. How did evidence flow into the direction, size, and risk decision?
4. Which modules agreed, opposed, or were ignored by the router?
5. What is the current belief/regime and how uncertain is it?
6. What happened through a trade's full life, audit, causal replay, and learning cycle?
7. Which hypotheses/models are learning, shadowing, promoted, rejected, or rolled back?
8. Is the brain acting within its authority and safety boundaries?

The goal is **scientific visibility**, not visual decoration. Every glow, pulse, edge, color, and
animation must encode a real typed value. Text remains available in inspectors, but it is no longer
the primary view.

## 2. Current-state audit

The current `frontend/src/panels/ScientistBrain.tsx`:

- is a single inline-styled component;
- polls `/scibrain` every three seconds;
- renders status text, crash-radar badges, audit paragraphs, decision rows, bars, and expanded text;
- has no graph/circuit renderer, topology, temporal replay, semantic zoom, or workspace view;
- mixes summaries, evidence, audits, remediation, and raw explanations in one vertical stream;
- cannot show the Cognitive OS regions, shared belief, memory, learning, or causal experiment state.

The current API exposes recent decisions, modules, deterministic attribution, reasoning, audits,
crash radar, counters, and status. That is enough for a first circuit view, but later views require
new typed visualization payloads from the Living Intelligence ledger and Cognitive OS.

## 3. Design decision: a 2.5D Cognitive Atlas, not a literal 3D brain

A literal rotating 3D brain would look advanced but make comparison, labels, uncertainty, and causal
flow harder to read. It would also consume browser/GPU resources without adding scientific value.

The default view is a **2.5D neural-circuit atlas**:

- brain-shaped regional layout;
- crisp 2D nodes and routed edges;
- layered depth, subtle glow, particles, and field contours;
- animation only when real messages/events flow;
- stable spatial positions so the operator learns where functions live;
- semantic zoom: regions at overview, modules at mid-level, formulas/evidence at detail level.

An optional 3D/WebGL research view may be added later for the universe graph or latent manifold, but
it is not the primary control-room interface.

## 4. Main visual modes

### 4.1 Live Cognitive Atlas — default control-room view

Stable brain/circuit layout:

```text
MARKET SENSORS
  -> SENSORY CORTEX / PERCEPTION EXPERTS
  -> THALAMUS / SALIENCE ROUTER
  -> GLOBAL BELIEF WORKSPACE
  -> PREFRONTAL PLANNER + SCIENTIFIC COUNCIL
  -> BASAL GANGLIA / ACTION SELECTION
  -> BRAINSTEM / SAFETY PROJECTION
  -> EXECUTION

HIPPOCAMPUS / EPISODIC MEMORY <-> WORKSPACE <-> NEOCORTEX / CONSOLIDATION
AMYGDALA / TAIL REFLEX -----------> BRAINSTEM VETO
METACORTEX / COMPETENCE ----------> ROUTER + PLANNER + ABSTENTION
```

Visual behavior:

- a real module evaluation creates a pulse from sensor to module;
- edge direction and width show signed contribution and magnitude;
- router-selected paths brighten; suppressed paths dim but remain inspectable;
- the shared belief region shows posterior direction/regime/uncertainty, not a single certainty;
- counter-evidence travels in a distinct channel into the planner/auditor;
- the final action pulse must visibly pass through the safety layer before execution;
- veto, abstain, reduce, and baseline-fallback paths are first-class outcomes.

### 4.2 Trade Autopsy Theatre

Select any opened/closed trade and replay the entire decision and life trace on a time scrubber:

```text
pre-entry observations -> module firings -> router -> fusion -> audit
-> open/fills -> regime/evidence changes -> SL/TP/exit interventions
-> close -> outcome causal audit -> counterfactual paths -> hypothesis updates
```

Required views:

- side-by-side actual/opposite/abstain/delay/weight/SL-TP/exit counterfactual trajectories;
- synchronized price, MFE/MAE, regime, belief, module votes, risk, and action timeline;
- visual fault-class probabilities;
- “why this action” lineage from raw evidence to code/model/formula version;
- before/after view for any proposed `ChangeSpec`.

This replaces long audit paragraphs as the primary post-trade experience. Narratives remain in a
collapsible evidence drawer.

### 4.3 Learning Laboratory

Visualize the living-intelligence experiment system:

- hypothesis genealogy graph;
- proposed -> compiled -> replay -> walk-forward -> shadow -> canary -> promoted/rejected states;
- champion/challenger scorecards and confidence bounds;
- positive/negative/procedural memory;
- model and formula versions with rollback lineage;
- competence heatmap by regime, symbol class, horizon, direction, and action;
- sleep/consolidation activity, replay priorities, forgetting, and protected competencies.

No “learning” animation may imply a live change when the component is only training or shadowing.
Authority state is always visible.

### 4.4 Universe Neural Field

Display all active pairs as a dynamic relational field:

- clusters/factors as stable territories;
- directed lead-lag and contagion flow;
- shock propagation, centrality, tail state, and liquidity;
- semantic zoom from market/sector clusters to pair/module evidence;
- filters for direction, regime, evidence family, confidence, tail risk, and current positions.

At overview scale, aggregate edges and hide labels to avoid a graph “hairball.” At deeper zoom,
progressively reveal real pairs and directed links. The topology must come from `UniverseFrame`,
not arbitrary force-layout aesthetics.

### 4.5 Safety and Authority View

Make the non-learning brainstem visible:

- exposure, margin, liquidation distance, correlation clusters, tail envelope, execution health;
- proposed action versus safety-projected/actual action;
- every veto/cap/fallback and its exact constraint;
- current authority level for every module/model/hypothesis;
- stale-data, drift, OOD, degraded-model, and rollback alarms.

This view must remain readable with animations disabled.

## 5. Visual grammar: every visual channel has one meaning

### 5.1 Node semantics

| Visual property | Meaning |
|---|---|
| position | stable functional region, never arbitrary rank |
| shape | role: sensor, direction, gate/router, belief, memory, planner, risk, allocator, exit, execution |
| border style | authority: research, shadow, canary, live, veto-capable |
| fill color | current signed state or health, depending on selected mode |
| brightness | current activation/salience |
| size | decision relevance or approved importance, never raw model complexity |
| outer halo | epistemic uncertainty / support distance |
| inner ring | calibrated confidence |
| small badge | healthy, stale, OOD, drifted, abstained, vetoed, learning, rolled back |

### 5.2 Edge semantics

| Visual property | Meaning |
|---|---|
| arrow direction | actual message/evidence/action direction |
| width | absolute contribution or message magnitude |
| color | long/support, short/oppose, risk/veto, memory, learning, or neutral context |
| opacity | confidence/support |
| dashed line | hypothetical, counterfactual, shadow, or uncertain path |
| moving pulse | a real timestamped event; no decorative perpetual traffic |
| edge bundle | aggregated evidence family or cluster at overview zoom |

Long/short cannot rely on red/green alone. Use direction arrows, shape, and labels so the interface
remains readable for color-vision deficiencies.

### 5.3 Uncertainty and disagreement

Uncertainty must be visible everywhere:

- posterior/belief distributions rather than one-point certainty;
- fuzzy or wider halos for epistemic uncertainty;
- probability bands/ensembles on trajectories;
- disagreement split within the workspace;
- support-distance/OOD contour around proposed actions;
- calibration markers comparing forecast probability with observed frequency.

Animation speed must never encode confidence; it creates urgency bias. Use width, opacity, bands,
and explicit values.

## 6. Interaction model

### 6.1 Semantic zoom and focus-plus-context

Three levels:

1. **Atlas:** brain regions, broad flow, risk, belief, final action.
2. **Circuit:** individual modules/models, routed edges, contributions, uncertainty.
3. **Evidence:** formulas, feature values, versions, prompts, replay IDs, and narratives.

Selecting a node keeps its neighborhood in full detail while dimming unrelated context. The user can
pin two nodes/versions/trades for comparison. A minimap and breadcrumb preserve orientation.

### 6.2 Time control

- live/pause/replay;
- cycle and event step-forward/back;
- trade-life scrubber;
- before/after regime or version comparison;
- bookmark/export a reproducible visual state with exact evidence IDs and timestamps.

### 6.3 Command and filter surface

Read-only controls first:

- search symbol, trade, module, hypothesis, model, or version;
- filter by region, role, evidence family, authority, regime, health, and risk;
- switch live brain / autopsy / lab / universe / safety mode;
- animation intensity and reduced-motion toggle;
- hide/show explanations, confidence, uncertainty, counterfactuals, and rejected evidence.

No dashboard interaction changes live authority or formulas during the initial redesign.

## 7. Layout blueprint

Desktop/wide-screen composition:

```text
+--------------------------------------------------------------------------------+
| top command strip: mode | live/replay | belief | action | risk | health | time |
+------------------+---------------------------------------------+---------------+
| region navigator |                                             | inspector     |
| + filters        |        COGNITIVE ATLAS / UNIVERSE FIELD     | evidence      |
| + minimap        |        stable graph + real signal pulses    | formula       |
|                  |                                             | uncertainty   |
+------------------+---------------------------------------------+---------------+
| synchronized timeline: market | belief | modules | risk | action | learning    |
+--------------------------------------------------------------------------------+
```

Responsive behavior:

- large display: full atlas + inspector + timeline;
- laptop: collapsible navigator/inspector;
- mobile: summary/safety view only; no dense graph required.

## 8. Visualization data contracts

The frontend must not infer scientific meaning from prose. Add typed backend payloads:

```text
BrainGraphSnapshot:
  snapshot_id, ts, selected_symbol/trade
  nodes[], edges[], regions[], belief, action, authority, health

BrainNode:
  id, region, role, label, state, activation, salience
  signed_value, confidence, epistemic_uncertainty, support_distance
  authority, health, source_version, evidence_ids

BrainEdge:
  id, source, target, message_kind, evidence_family
  signed_value, magnitude, confidence, hypothetical, shadow, evidence_ids

BrainPulse:
  event_id, edge_id/path_ids, kind, magnitude, ts, duration_ms, evidence_ids

TradeReplayFrame:
  trade_id, frame_ts, market, belief, modules, router, risk, action
  actual_path, counterfactual_paths, interventions, outcome_state

LearningGraphSnapshot:
  hypotheses, experiments, model_versions, memory/replay, competence, authority

UniverseGraphSnapshot:
  symbols/clusters, factor state, directed flows, contagion, positions, tail/liquidity
```

Payloads are versioned and deterministic. Graph nodes/edges point back to immutable evidence IDs.
Use snapshots/deltas so the frontend does not poll and redraw the entire brain every event.

## 9. Recommended implementation stack

The current frontend has React and `lightweight-charts` but no graph/circuit library.

Recommended hybrid:

1. **React Flow** for the stable Cognitive Atlas and experiment/promotion graphs. It provides custom
   React nodes, custom/animated edges, minimap, controls, selection, zoom/pan, and layout support.
   The atlas is tens of rich nodes, where custom semantic node design matters more than raw scale.
2. **Sigma.js + Graphology** for the Universe Neural Field after `UniverseFrame` exists. Sigma is
   designed for interactive graphs with thousands of nodes/edges and separates graph algorithms
   from rendering.
3. **Existing lightweight-charts** for synchronized market/trade-life timelines.
4. **SVG/Canvas overlays and CSS variables** for belief fields, uncertainty halos, pulses, and
   regional brain silhouette.
5. **Web Workers** for layout/aggregation and replay preprocessing so visualization cannot starve
   the dashboard thread.

Do not start with Three.js. Reserve WebGL/Three.js for a later optional latent-manifold or 3D
universe research view. The primary atlas needs precise labels, comparison, accessibility, and
stable spatial memory more than spectacle.

## 10. Performance and safety budgets

- visualization is read-only and isolated from trading processes;
- target 60 FPS while idle/interacting and at least 30 FPS during bounded event bursts;
- animate only visible/relevant edges; cap concurrent pulses;
- use snapshot deltas and batch updates;
- pause/reduce animation when the tab is hidden or reduced-motion is requested;
- aggregate universe links by cluster/evidence family at overview;
- labels appear by semantic zoom or selection, not all at once;
- graph/layout computation runs in browser workers or separately budgeted API jobs;
- dashboard failure must have zero effect on the brain/executor;
- every visual state has a text/table fallback and exact-value inspector.

## 11. Accessibility and scientific-integrity rules

1. Use color plus shape/pattern/arrow; never color alone.
2. Support reduced motion, keyboard navigation, and high contrast.
3. Do not use permanent decorative pulses or random neuron firing.
4. Do not imply causality with an edge unless it is typed as causal/interventional evidence.
5. Clearly distinguish actual, predicted, hypothetical, counterfactual, shadow, and live.
6. Keep stable layouts between updates; movement falsely suggests structural change.
7. Preserve raw values, formulas, versions, and evidence IDs in the inspector.
8. Make uncertainty and missing/stale data visible instead of silently smoothing them.
9. Never hide a safety veto, fallback, or degraded component to make the brain look healthier.
10. A beautiful visualization that slows risk/execution or misleads the operator is rejected.

## 12. Vertical-slice implementation order

### VS-V1: live circuit atlas from existing `/scibrain`

- split the current monolithic panel into typed data adapter, atlas, inspector, timeline, and
  fallback table;
- map current modules -> router -> fusion -> audit -> action/safety into a stable graph;
- animate only real decision-cycle pulses;
- click a module/edge to inspect current explanation, contribution, confidence, regime, and version;
- preserve existing crash radar/audit information in compact visual drawers.

### VS-V2: belief, uncertainty, router, and safety

- add router gains/deactivated paths and evidence-family grouping;
- add belief/regime/disagreement field and uncertainty halos;
- add safety/authority path and clearly show proposed versus actual action;
- add live/pause/cycle replay and reduced-motion mode.

### VS-V3: trade autopsy theatre

- connect EntrySnapshot/LifeTrace/OutcomePacket and Decision-Risk/Outcome Causal Audits;
- synchronized trade-life timeline and graph replay;
- actual versus counterfactual trajectories and fault-class visualization;
- formula/model/version lineage and `ChangeSpec` before/after comparison.

### VS-V4: learning laboratory

- hypothesis/experiment/promotion genealogy;
- model/formula champion/challenger and rollback views;
- memory/replay/consolidation and competence maps;
- whole-brain Cognitive OS region activity and authority.

### VS-V5: universe neural field

- connect `UniverseFrame`, cross-asset graph, factors, lead-lag, contagion, tail, and positions;
- semantic zoom and cluster aggregation;
- shock-flow replay and pair-to-brain drill-down;
- only then evaluate optional WebGL/3D research views.

## 13. Research foundations

- [React Flow](https://reactflow.dev/) provides customizable node-based React interfaces with
  built-in zooming, panning, selection, minimaps, controls, custom nodes, and custom edges.
- [Cytoscape.js](https://js.cytoscape.org/) demonstrates mature interactive graph visualization,
  layouts, graph analysis, stylesheets, gestures, serialization, and performance tradeoffs.
- [Sigma.js](https://www.sigmajs.org/) is designed for browser interaction with graphs containing
  thousands of nodes and edges, fitting the future Universe Neural Field.
- [Three.js](https://threejs.org/docs/) provides WebGL/WebGPU rendering and interaction tools, but
  is intentionally reserved for optional views where 3D adds measurable understanding.
- Holten, *Hierarchical Edge Bundles* (IEEE TVCG, 2006): bundle hierarchical adjacency to reduce
  connection clutter.
- [Multi-level interactive graph visualization with semantic zoom](https://arxiv.org/abs/1906.05996):
  preserve meaningful graph structure across levels of detail.
- [EntOptLayout](https://arxiv.org/abs/1904.03910): graph layout should reduce information loss and
  reveal modules, not merely create an attractive force-directed hairball.

These tools and papers justify visualization methods. The final design must still be tested with
real operator tasks: identify a decision driver, detect counter-evidence, find uncertainty, explain a
veto, replay an outcome, and locate a proposed learning change faster and more accurately than the
current text panel.
