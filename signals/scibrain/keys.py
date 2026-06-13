"""SciBrain Redis keys — self-contained namespace `scibrain:*` (the live-visibility bus).

Kept here (not in the global redis_keys.py) so the SciBrain package is self-contained.
Everything is advisory/hot-mirror; Postgres is not the source of truth for these.
"""
from __future__ import annotations

ENABLED        = "scibrain:enabled"          # "1"|"0"; trade-origination authority switch
STATUS         = "scibrain:status"           # JSON heartbeat {ts, pairs, cycle_ms, ...}
INTERROGATE    = "scibrain:interrogate"      # "1"|"0"; default "0" (run Ollama interrogator)
LEAD_MODEL     = "scibrain:lead_model"       # str override for the interrogator lead model (local fallback)
CRITIC_MODEL   = "scibrain:critic_model"     # str override for the interrogator critic model (local fallback)
AUDIT_USE_CLOUD= "scibrain:audit_use_cloud"  # "1"|"0"; default "1" — route audit LLM via cloud-primary chain (local fallback)

# ── funnel / opener config (C6: tunable via Redis; defaults in gate.py/opener.py) ──
MIN_CONVICTION = "scibrain:min_conviction"   # float; default 0.15 — min conviction to be pickable
MAX_PICKS      = "scibrain:max_picks"        # int;   default 10  — max candidates a cycle returns
MAX_DIR_FRAC   = "scibrain:max_dir_fraction" # float; default 0.7 — cap on the dominant direction
CLUSTER_RHO    = "scibrain:cluster_rho"      # float; default 0.8 — signed-corr ≥ this = same risk cluster (1.0 disables)
MAX_PER_CLUSTER= "scibrain:max_per_cluster"  # int;   default 2   — max picks from one correlation cluster (0 disables)
CLUSTER_TF     = "scibrain:cluster_tf"       # str;   default 1h  — timeframe for the correlation return series
CLUSTER_N      = "scibrain:cluster_n"        # int;   default 50  — # of returns used to measure correlation
SCORER_PARALLEL= "scibrain:scorer_parallel"  # "1"|"0"; default "1" — fan the universe scan across cores
SCORER_WORKERS = "scibrain:scorer_workers"   # int;   default min(cpu-2, 8) — process-pool size for scoring
SCORER_CACHE_ENABLED = "scibrain:scorer_cache_enabled"  # "1"|"0"; default "0" — Phase 5 in-RAM universe
                                             # snapshot (bulk-load + fork-COW to workers). DEFAULT OFF: live
                                             # measurement showed it regresses (scan is compute-bound, reads
                                             # already parallel). "1" = opt in. Correct/bit-identical infra.
# Phase 5 top-K prefilter: the scan is compute-bound (~143ms/symbol), so run the heavy bank only on the
# most-active K pairs + a rotating slice (coverage). Capital-affecting (Rule 14) → SHADOW-first rollout.
PREFILTER_MODE   = "scibrain:prefilter_mode"    # "off"|"shadow"|"on"; default "off". shadow = score all +
                                             # measure top-K coverage of qualifying candidates (no risk); on = restrict.
PREFILTER_TOP_K  = "scibrain:prefilter_top_k"   # int; default 150 — heavy-scored pairs ranked by recent volatility
PREFILTER_ROTATE = "scibrain:prefilter_rotate"  # int; default 60  — extra rotating pairs/cycle so none is starved
PREFILTER_STATUS = "scibrain:prefilter:status"  # JSON {mode,k,rotate,n_selected,coverage,...} for the panel
PREFILTER_CYCLE  = "scibrain:prefilter:cycle"   # int — monotonic cycle counter driving the rotation window
PREFILTER_AGG    = "scibrain:prefilter:agg"     # HASH rolling aggregate {pick_cycles,picks_total,picks_captured,
                                             # starve_cycles,last_missed} — the PICK-level Rule-14 evidence to flip "on"
COOLDOWN       = "scibrain:cooldown"         # ZSET sym -> until_ts (post-open re-pick block)
COOLDOWN_SECS  = "scibrain:cooldown_seconds" # int;   default 900
CAPITAL_POOL   = "scibrain:capital_pool"     # float; default from brain_state — sizing pool
MAX_POSITION   = "scibrain:max_position_usdt"# float; default 200 — per-trade capital cap
MAX_LEVERAGE   = "scibrain:max_leverage"     # int;   default 5
OPEN_COUNT     = "scibrain:open_count"        # int — trades opened by scibrain
PICK_COUNT     = "scibrain:pick_count"        # int — candidates picked by the funnel
DECISION       = "scibrain:{sym}:decision"   # JSON Decision.to_dict()
MODULES        = "scibrain:{sym}:modules"    # JSON list[ModuleOutput.to_dict()]
REASONING      = "scibrain:{sym}:reasoning"  # JSON Interrogation.to_dict() (Ollama transcript)
LAST_DECISIONS = "scibrain:last_decisions"   # ZSET sym -> ts (recently scored, for the panel)
CRASH_RADAR    = "scibrain:crash_radar"      # ZSET sym -> crash_warning [0,1] (StatPhysSOC early-warning)

# operational counters (house rule: every path observable)
CYCLES         = "scibrain:cycles"           # int
SCORED         = "scibrain:scored_total"     # int
INTERROGATED   = "scibrain:interrogated_total"  # int
WRONG_DIR_FLAG = "scibrain:wrong_dir_flag_total"  # int — interrogator disagreed w/ fusion
DIR_CAPPED     = "scibrain:dir_capped_total"  # int — candidates dropped by the direction-balance cap
CLUSTER_CAPPED = "scibrain:cluster_capped_total"  # int — candidates dropped by the correlation-cluster cap

# ── Phase 3: Meta-Router (MoE) + regime gating ──
ROUTER_ENABLED   = "scibrain:router_enabled"    # "1"|"0"; default "1" — gate the module bank per regime
ROUTER_STRENGTH  = "scibrain:router_strength"   # float 0..1; default 1.0 — blend router gains toward 1.0 (0 = off)
FAMILY_PENALTY_STRENGTH = "scibrain:family_penalty_strength"  # float 0..1; default 1.0 — evidence-family/HSIC
                                                # redundancy discount on the directional vote (§6g.330). 0 = OFF
                                                # (instant rollback); kill switch is also scibrain:enabled.
DEACTIVATED      = "scibrain:deactivated_total"  # int — module-votes zeroed by the router (MoE deselection)
REGIME_COUNT     = "scibrain:regime:{regime}"   # int — per-canonical-regime decision counter

# ── Phase 3: per-module Information-Coefficient tracking (adaptive router weights) ──
IC_ENABLED       = "scibrain:ic_enabled"        # "1"|"0"; default "1" — record + apply adaptive IC
IC_HORIZON_MIN   = "scibrain:ic_horizon_min"    # int; default 30 — forward horizon a vote is scored against
IC_WINDOW        = "scibrain:ic_window"         # int; default 400 — rolling (vote,return) samples per module
IC_MIN_SAMPLES   = "scibrain:ic_min_samples"    # int; default 30  — below this, IC is untrusted (multiplier 1.0)
IC_PENDING       = "scibrain:ic:pending"        # ZSET member=JSON{sym,ref,votes,ts} -> maturity_ts
IC_SEEN          = "scibrain:ic:seen:{key}"     # NX marker (sym|bucket) → record ONE vote/symbol/bucket
IC_WIN           = "scibrain:ic:win:{module}"   # LIST of "vote,ret" CSV samples (LPUSH + LTRIM)
IC_MAP           = "scibrain:ic:map"            # HASH module -> rolling IC (Pearson vote vs realized return)
IC_SAMPLES       = "scibrain:ic:samples"        # HASH module -> sample count behind its IC
IC_SETTLED       = "scibrain:ic_settled_total"  # int — matured observations folded into the IC windows

# ── Phase 4: Ex-ante Decision-Risk Audit (auto-interrogate every opened trade) ──
# Fires at OPEN, before any realized outcome → a direction-WRONGNESS forecast, not a result audit.
# Key NAMES are kept stable (restart-safety + already-persisted data); the payloads self-describe
# via audit_kind='ex_ante_decision_risk' / evaluated_at='post_open_pre_outcome'.
AUDIT_QUEUE    = "scibrain:audit_queue"      # LIST of JSON jobs {trade_id, symbol, decision, ts}
AUDIT_RESULT   = "scibrain:trade:{sym}:audit"# JSON Interrogation.to_dict() per OPENED trade (by trade_id below)
AUDIT_BY_TRADE = "scibrain:audit:{tid}"      # JSON audit result keyed by trade_id (dashboard drill-down)
WRONG_DIR_TRADES = "scibrain:wrong_direction_trades"  # ZSET trade_id -> wrong_direction_risk (flagged opens)
AUDITED_TRADES = "scibrain:audited_total"    # int — opened trades that completed the ex-ante audit
AUDIT_QUEUE_MAX = 200                          # hard cap on the queue length (drop oldest beyond this)
# remediation: the agent proposes a fix for the issue it found (action + circuit improvement)
AUDIT_ACTION   = "scibrain:trade:{tid}:action" # JSON the agent's recommendation for one trade
RECOMMENDED_ACTIONS = "scibrain:recommended_actions"  # ZSET trade_id -> ts (advise authority)
PENDING_ACTIONS = "scibrain:pending_actions" # legacy/unused; no runtime consumer
IMPROVE_LEDGER = "scibrain:improvement_ledger"# LIST of JSON proposed circuit improvements (Phase 7 feed)
# ── Phase 7a: ex-post calibration grading of the ex-ante decision-risk forecaster ──
# At close (with a realized failure_type label) the predicted wrong_direction_risk is graded vs
# the outcome proxy (Brier + reliability bins). Per-trade grade lives immutably on the trade row
# (signals_at_entry.audit_calibration); this aggregate is RECOMPUTED from those rows each run.
AUDIT_CALIBRATION  = "scibrain:calibration"            # JSON aggregate (n, brier, base rate, bins)
CALIB_GRADED_TOTAL = "scibrain:calibration_graded_total"  # int — trades whose forecast was graded

# ── Phase 7a: bounded LifeTrace + OutcomePacket on close (the outcome-truth ledger) ──
# Built idempotently per closed scibrain trade onto the immutable row (signals_at_entry.lifetrace /
# .outcome_packet); this aggregate is RECOMPUTED from those rows each run (double-count-proof).
OUTCOMES_AGG          = "scibrain:outcomes"                # JSON aggregate (n, mean utility/roc, horizons)
OUTCOME_HARVESTED_TOTAL = "scibrain:outcome_harvested_total"  # int — trades that got an OutcomePacket
OUTCOME_HORIZONS_MIN  = "scibrain:outcome_horizons_min"   # CSV mins; default "15,60,240" — forward horizons
UTIL_LAMBDA_TAIL      = "scibrain:util_lambda_tail"       # float; default 0.5  — realized-downside weight
UTIL_LAMBDA_DD        = "scibrain:util_lambda_dd"         # float; default 0.25 — drawdown (|MAE|/cap) weight
UTIL_LAMBDA_COST      = "scibrain:util_lambda_cost"       # float; default 1.0  — fee/cost weight

# ── Phase 7a: path-aware Counterfactual Digital Twin (actual/opposite/abstain policy replay) ──
TWIN_FEE_RATE         = "scibrain:twin_fee_rate"          # float; default 0.0004 — round-trip notional taker fee (matches paper close)
TWIN_DERIVED_SL_ATR_MULT = "scibrain:twin_derived_sl_atr_mult"  # float; default 1.5 — fallback SL = mult×ATR for pre-policy trades

# ── Phase 7a: module-state embeddings + matched-cohort retrieval ──
# Each closed scibrain trade gets a fixed-length module-state embedding (per-module direction·
# conviction + market context) immutably on the row (signals_at_entry.module_embedding); retrieval
# finds comparable trades by name-aligned module cosine + Gaussian context (NOT prose). This
# aggregate (roster + leave-one-out retrieval-quality) is RECOMPUTED from those rows each run.
COHORT_AGG            = "scibrain:cohort"                 # JSON aggregate (n_embedded, roster, retrieval_quality)
COHORT_EMBEDDED_TOTAL = "scibrain:cohort_embedded_total"  # int — trades that got a module_embedding

# ── Phase 7b: typed bounded ChangeSpec + single experiment registry ──
# Every LLM/algorithm proposal compiles to a bounded ChangeSpec against an allow-listed target and is
# stored ONCE in this registry (deduped by content fingerprint), moving through the validated lifecycle.
# Tier-0: the registry RECORDS hypotheses; it has NO authority to apply them (that's the Phase-7c gate).
EXPERIMENTS_REG     = "scibrain:experiments:reg"        # HASH hypothesis_id -> JSON record {spec,status,history}
EXPERIMENTS_FP      = "scibrain:experiments:by_fp"      # HASH fingerprint -> hypothesis_id (dedup index)
EXPERIMENTS_INDEX   = "scibrain:experiments:index"      # ZSET hypothesis_id -> created_ts (recency)
EXPERIMENTS_SUMMARY = "scibrain:experiments"            # JSON aggregate (n, by_status, recent) for the panel
EXPERIMENTS_PVALS   = "scibrain:experiments:pvals"      # LIST rolling bootstrap p-values (online BH/FDR guard)

# ── Phase 7b: generalized hypothesis memory (so failed ideas are not repeated) ──
# Keyed by '<target>|<increase|decrease|zero>' — a coarser key than the registry's exact fingerprint.
MEMORY_NEG     = "scibrain:memory:negative"   # HASH signature -> {n, reasons, hypothesis_ids} (rejected/demoted/rolled_back)
MEMORY_POS     = "scibrain:memory:positive"   # HASH signature -> {...} (retained / validated laws)
MEMORY_SUMMARY = "scibrain:memory"            # JSON aggregate (counts + top signatures) for the panel

# ── Phase 2b: read-only in-RAM UniverseFrame (cross-market substrate, built once per cycle) ──
# The Universe Core builds ONE shared frame per funnel cycle (returns/feature/correlation/lead-lag/
# liquidity matrices) that future cross-market modules (SparseFactorResidual, SpectralGraphContagion,
# CausalLeadLag, OptimalTransportRegime…) read in-process. The full matrices live in RAM; this small
# market-state digest is the live-visible mirror. Tier-0: pure observability, no trading authority.
UNIVERSE_STATE    = "scibrain:universe:state"    # JSON UniverseFrame.to_digest_dict() (breadth/crowding/factor share)
UNIVERSE_BUILDS   = "scibrain:universe:builds_total"  # int — frames actually (re)built
UNIVERSE_SKIPS    = "scibrain:universe:skips_total"   # int — cycles that could NOT build a usable frame (Rule 12)
UNIVERSE_TFS      = "scibrain:universe_tfs"       # CSV; default "1h,15m" — timeframes the frame aligns
UNIVERSE_PRIMARY_TF = "scibrain:universe_primary_tf"  # str; default "1h" — corr/lead-lag/digest TF
UNIVERSE_LOOKBACK = "scibrain:universe_lookback"  # int; default 120 — bars per symbol per TF
UNIVERSE_INTERVAL = "scibrain:universe_interval_s"# int; default 60 — Universe-Core cadence (>funnel's 20s; RPCA ~seconds, structure slow)
UNIVERSE_MIN_SYMBOLS = "scibrain:universe_min_symbols"  # int; default 20 — below this the frame is unusable
# Universe-Core MODULES (cross-market scorers over the frame): run ONCE/cycle in the main process; each
# emits a per-symbol ModuleOutput published here for the per-symbol scorer to fold into fusion. Brand-new
# directional modules ship shadow_only (RECORDED + IC-evaluable, never applied) until they prove IC.
UNIVERSE_MODULES_ENABLED = "scibrain:universe_modules_enabled"  # "1"|"0"; default "1" — run the Universe-Core bank
UNIVERSE_CONTRIB  = "scibrain:universe:contrib:{sym}"  # JSON list[ModuleOutput.to_dict()] — universe votes for {sym}
UNIVERSE_MODULES_SUMMARY = "scibrain:universe:modules"# JSON {modules, n_symbols, ms, ts} for the panel
UNIVERSE_MODULE_RUNS = "scibrain:universe:module_runs_total"  # int — universe-module passes completed
# VS-V5 Universe Neural Field — bounded real topology snapshot (clusters/MDS nodes/directed lead-lag
# edges/digest) published once per frame rebuild from the in-RAM matrices for the dashboard field view.
UNIVERSE_FIELD        = "scibrain:universe:field"        # JSON UniverseGraphSnapshot (clusters,nodes,edges,meta)
UNIVERSE_FIELD_BUILDS = "scibrain:universe:field_builds_total"  # int — field snapshots published

# ── Phase-7c BOUNDED CANARY (owner-approved, one context/role at a time, auto-rollback) ──
# A canary ACTUALLY applies one passed ChangeSpec's bounded router-gain override, but ONLY in its one
# (module, regime) context, ONLY when the owner has explicitly approved AND the master switch is on. It
# is the ONLY path that grants bounded_canary (capital-affecting) authority; default OFF = zero effect.
CANARY_ENABLED    = "scibrain:canary:enabled"     # "1"|"0"; DEFAULT "0" — master switch; no canary applies unless 1
CANARY_ACTIVE     = "scibrain:canary:active"       # JSON of the ONE active canary (override + lineage) or absent
CANARY_APPROVAL   = "scibrain:canary:approval:{hid}"  # "1" — OWNER sets this to approve a SPECIFIC hypothesis
CANARY_PROMOTE_OK = "scibrain:canary:promote:{hid}"   # "1" — OWNER sets this to approve canary→live for {hid}
CANARY_REQUESTS   = "scibrain:canary:requests"     # HASH hid -> JSON request (passed shadow, awaiting owner approval)
CANARY_HISTORY    = "scibrain:canary:history"      # LIST of JSON events (request/approve/rollback/promote), capped
CANARY_ROLLBACK_LCB = "scibrain:canary:rollback_lcb"  # float; DEFAULT 0.0 — auto-rollback if live LCB(Δutility) < this
CANARY_MIN_TRADES = "scibrain:canary:min_trades"   # int; DEFAULT 12 — min canary-influenced closes before a verdict
CANARY_MAX_HOURS  = "scibrain:canary:max_hours"    # int; DEFAULT 72 — max canary duration before a forced decision
CANARY_ROLLBACK_TRIGGER = "scibrain:canary:rollback_now"  # "1" — manual immediate rollback of the active canary

# ── Phase-7c UNIFIED PRODUCER BUS (design §11 "Global self-improvement unification") ──
# Every autonomous self-improvement mechanism (LLM council, OPRO, DGM, AI-Scientist, GA, feature-gov,
# metacog, direction-model, strategy-pool, DSL-miner, F9/F12 decoder, bayes-threshold) becomes a PROPOSAL
# PRODUCER under the ONE experiment registry + promotion gate — never an independent promotion path. This
# module enumerates them, gives them a single submit() door into experiments.register, and AUDITS whether
# any ENABLED producer still self-applies a live parameter outside the kernel (an open "bypass"). Tier-0:
# it records, routes, and reports; it has NO authority to apply anything itself.
UNIFY_MODE      = "scibrain:unify:mode"        # "observe"|"enforce"; DEFAULT "observe" — observe REPORTS bypasses
                                               # (byte-identical live behavior); enforce additionally refuses a
                                               # producer's self-apply that lacks a kernel-approved hypothesis.
UNIFY_SUMMARY   = "scibrain:unify:summary"     # JSON aggregate (producers, modes, open_bypasses) for the panel
UNIFY_SUBMITS   = "scibrain:unify:submits"     # HASH proposer -> count of ChangeSpecs submitted via the bus
UNIFY_LAST      = "scibrain:unify:last:{proposer}"  # JSON {hypothesis_id, deduped, ts} — last submission per producer
# Kernel-owned audit ledger for bounded_recorded producers: a fast online controller (e.g. bayes T_high)
# COMPUTES a value but the live write goes THROUGH producers.apply_controller — bounded-clamped + recorded
# here — so it routes its apply through the kernel instead of self-applying (design §11 no-bypass).
UNIFY_CONTROLLER      = "scibrain:unify:controller:{proposer}"       # LIST of JSON change records (capped, newest first)
UNIFY_CONTROLLER_LAST = "scibrain:unify:controller_last:{proposer}"  # JSON {target, value, applies, ts} — last apply
# ModelChangeSpec ledger for model_recorded producers (prompts/code/factors/hypotheses/strategies) whose
# knob can't compile to the bounded scalar/router DSL. They record a typed, versioned change under the ONE
# kernel here (provenance + evidence + lineage) instead of a SILENT independent path — accounted + audited.
UNIFY_MODEL      = "scibrain:unify:model:{proposer}"        # LIST of JSON ModelChangeSpec records (capped, newest first)
UNIFY_MODEL_LAST = "scibrain:unify:model_last:{proposer}"   # JSON {model_id, kind, records, ts} — last model change

# ── Phase-7d global latent WORKSPACE (read-only, built from current modules/models; design §3.4) ──
# A bounded event-bus + shared BeliefState assembled from the latest scored Decision: each contributing
# module publishes a typed CognitiveMessage; a limited salient subset is "broadcast". Tier-0: pure read,
# no trading authority — the integration substrate the planner/metacognition will consume.
WORKSPACE        = "scibrain:workspace"                     # JSON {symbol, messages, belief, broadcast, ts}
# ── Phase-7d THALAMUS / salience router (read-only attention + bandwidth control; design §3.3) ──
# Scores each workspace message (info gain + decision relevance + anomaly + risk urgency + memory match
# − compute cost − evidence-family redundancy), selects a SPARSE load-balanced evidence subset, and
# allocates bounded BUDGETS for memory retrieval / planning depth / audit depth / compute — scaled DOWN
# under system load so risk/execution is never starved. Tier-0: pure read, no trading authority.
THALAMUS         = "scibrain:thalamus"                      # JSON {ranked, evidence_selected, budgets, ts}
# ── Phase-7d METACOGNITION / metacortex competence maps (read-only self-model; design §3.11) ──
# Per-component CompetenceRecords (calibration, utility, support, drift) + the derived uncertainty facets
# (epistemic vs aleatoric, support distance / OOD, abstention utility) and the key metacognitive output
# P(action_supported | belief, evidence, versions). Tier-0: pure read, no trading authority.
METACOG          = "scibrain:metacognition"                 # JSON {components, global, p_action_supported, ts}
# ── Phase-7d BRAINSTEM / non-negotiable safety (CBF-style projection + baseline fallback; design §3.1) ──
# The hard safety floor OUTSIDE all learning: a learned a_raw is projected to the closest ADMISSIBLE
# a_safe = argmin||a-a_raw||² s.t. risk_state_next ∈ SafeSet (hard size/leverage caps, max-open, kill
# switches, stale-data, crash/tail reflex). No learned component can bypass it; on any fault → baseline
# fallback (abstain). Tier-0 here: reports the SafeSet + provides the projection contract (pure function).
BRAINSTEM        = "scibrain:brainstem"                      # JSON {safe_set, reflexes, invariants, ts}
# ── Phase-7d WHOLE-BRAIN dashboard: the BrainPulse (design §Phase-E step 17) ──
# ONE versioned aggregate of every cognitive-OS region (brainstem/thalamus/workspace/metacortex/action/
# tail/learning) + the cross-cutting workspace-broadcast / uncertainty / competence / authority / compute
# views. Read-only; assembled from the per-region surfaces. The whole-brain "is it healthy + what is it
# attending to + does it know what it knows" snapshot for the operator.
BRAIN_PULSE      = "scibrain:brain_pulse"                    # JSON {regions, broadcast, uncertainty, competence, authority, compute, ts}
# ── Phase-7e PERCEPTION training report + health diagnostics (design §3.2 / §8 evaluation constitution) ──
PERCEPTION_REPORT = "scibrain:perception:train_report"      # JSON shared-latent SSL train+eval report (ml/train_shared_latent)
TRAINING_HEALTH   = "scibrain:training_health"              # JSON {report, auc_ci, issues, health, ...} — auto-diagnosed training issues
# ── Phase-7e HIPPOCAMPUS / episodic memory (rich Episodes + pattern-separated embeddings; design §3.5) ──
EPISODIC_MEMORY   = "scibrain:episodic"                     # JSON {episodes, health, issues, ts} — rich Episode bank + replay priority
ABSTENTION_MEMORY = "scibrain:abstention"                  # JSON {classes, abstention_reward, skill, issues} — rewarded correct abstention + preserved non-trade events
# ── Phase-7f WORLD MODEL (RSSM on rich ledger sequences; design §3.6/§9) ──
WORLD_MODEL_REPORT = "scibrain:world_model:report"         # JSON RSSM rich-sequence train+imagination report (ml/world_model_train)
WORLD_MODEL_HEALTH = "scibrain:world_model_health"         # JSON {report, imagination, issues, health} — world-model diagnostics
# ── Phase-7f HIERARCHICAL CONTROLLERS (day/hour/minute reframe + coordination tests; design §3.7/§8-417) ──
HIER_CONTROLLERS_REPORT = "scibrain:hierarchical_controllers:report"  # JSON shadow coordination-gain + ablation report (ml/hierarchical_controllers)
HIER_CONTROLLERS_HEALTH = "scibrain:hierarchical_controllers_health"  # JSON {coordination, ablation, issues, health} — controller diagnostics
# ── Phase-7f OFFLINE RL CHALLENGERS (CQL/IQL over abstain/enter/manage/exit + support-aware fallback; §3.7/§8-416) ──
OFFLINE_RL_REPORT = "scibrain:offline_rl:report"           # JSON shadow CQL/IQL off-policy eval + support-aware fallback (ml/offline_rl_challengers)
OFFLINE_RL_HEALTH = "scibrain:offline_rl_health"           # JSON {twin_utility, fallback, option_support, issues, health}
# ── Phase-7f RISK-SENSITIVE POLICY (distributional/CVaR + costs + CBF safety projection; design §3.7/§3.10/§8-416) ──
RISK_POLICY_REPORT = "scibrain:risk_policy:report"         # JSON shadow CVaR-vs-mean tail eval + safety projection (ml/risk_sensitive_policy)
RISK_POLICY_HEALTH = "scibrain:risk_policy_health"         # JSON {eval, tradeoff, issues, health} — risk-sensitivity diagnostics
# ── Phase-7f META-LEARNING (real regime/cohort tasks + protected competence; upgrades ml/maml.py; §3.7/§5.4-7/§8-419) ──
META_LEARNING_REPORT = "scibrain:meta_learning:report"     # JSON shadow few-shot regime/cohort adapt + protected-competence (ml/meta_regime_tasks)
META_LEARNING_HEALTH = "scibrain:meta_learning_health"     # JSON {few_shot, protected_competence, integrity, issues, health}
# ── Phase-7e NEOCORTEX slow consolidation (EWC + protected old-competence regression test; design §3.6) ──
CONSOLIDATION_REPORT = "scibrain:consolidation:report"      # JSON EWC consolidation report (ml/consolidation)
CONSOLIDATION_HEALTH = "scibrain:consolidation_health"      # JSON {report, issues, health, ...} — forgetting diagnostics
# ── Phase-7f CEREBELLUM (bounded calibration/timing/slippage residual learners — FIRST limited canary authority; §3.8/§5.5 step-12) ──
CEREBELLUM_CANARY    = "scibrain:cerebellum:canary"         # owner master switch; absent/"1"=ARMED, "0"=disarmed (DEFAULT ON 2026-06-13)
CEREBELLUM_RESIDUALS = "scibrain:cerebellum:residuals"      # JSON {calibration, slippage, timing} hot-path residual params
CEREBELLUM_REPORT    = "scibrain:cerebellum:report"         # JSON full trainer diagnostic + authority verdict
CEREBELLUM_HEALTH    = "scibrain:cerebellum_health"         # JSON typed health/issues + effective authority (Tier-0 read)
CEREBELLUM_APPLIED   = "scibrain:cerebellum:applied"        # int counter prefix (:calibration/:timing) — Rule-21 live evidence
CEREBELLUM_LAST      = "scibrain:cerebellum:last"           # JSON last-applied adjustment prefix (:calibration/:timing)
CEREBELLUM_SKIPS     = "scibrain:cerebellum:skips"          # int per-symbol consecutive-skip counter prefix (anti-starvation)

# ── OptimalTransportRegime (§6e Tier-A) — outcome-learned OT prototype library (STATEFUL module) ──
OT_REGIME_PROTOS  = "scibrain:ot:protos"          # JSON list of regime-prototype clouds (capped library)
OT_CLOUD_WINNER   = "scibrain:ot:cloud:winner"    # JSON FIFO list of std feature vectors that PRECEDED up-moves
OT_CLOUD_LOSER    = "scibrain:ot:cloud:loser"     # JSON FIFO list of std feature vectors that PRECEDED down-moves
OT_PENDING        = "scibrain:ot:pending"         # ZSET member=JSON{s,x,p,t} -> maturity_ts (await fwd outcome)
OT_STATE          = "scibrain:ot:state"           # JSON regime read + prev min-dist (dashboard + migration velocity)
OT_SETTLED_TOTAL  = "scibrain:ot:settled_total"   # int — matured samples folded into the winner/loser clouds

# ── InformationGeometryHealth (§6e Tier-B) — bank-level recalibration/health monitor (main process) ──
INFOGEO_HEALTH    = "scibrain:infogeo:health"      # JSON report: per-module drift/IC-transfer/redundancy
INFOGEO_BASELINE  = "scibrain:infogeo:baseline"    # JSON {module: [mean, var]} EWMA baseline (drift ref)
INFOGEO_RUNS      = "scibrain:infogeo:runs_total"  # int — health assessments completed

# ── Module ablation/prune/demote report (§6g.8/§6g.10, VS-12) — ADVISORY (main process) ──
ABLATION_REPORT   = "scibrain:ablation:report"     # JSON {ts, verdicts:{mod:{status,reasons,...}}, summary}
ABLATION_RUNS     = "scibrain:ablation:runs_total" # int — ablation assessments completed

# default TTL for per-symbol hot keys (seconds) so the panel never shows stale brains
HOT_TTL = 900


def sym_key(template: str, symbol: str) -> str:
    return template.replace("{sym}", symbol)


def mod_key(template: str, module: str) -> str:
    return template.replace("{module}", module)
