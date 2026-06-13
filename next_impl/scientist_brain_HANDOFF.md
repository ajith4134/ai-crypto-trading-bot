# Scientist-Brain — Session Clear-Point Handoff (2026-06-10)

Document-and-Clear per Rule 16. A fresh session: read this, then `scientist_brain_PROGRESS.md`
(authority) + the topic design `scientist_brain_launchpad.md` (Rule 3). Live state below is verified.

## What this session shipped (all LIVE, all Level-3 verified)
1. **Phase 2b — read-only in-RAM `UniverseFrame`** (`signals/scibrain/universe_frame.py` +
   `contracts.UniverseFrame`): one cross-market substrate built per Universe-Core cycle in the brain
   MAIN process — aligned returns_by_tf, correlation, directed lag-1 lead-lag, feature_matrix,
   liquidity, + a market-state digest. Cached behind a cycle guard; `get_current_frame()` accessor.
2. **Phase 2b — 3 Universe-Core SHADOW modules** (`signals/scibrain/universe_modules/`):
   - `sparse_factor_residual.py` — robust PCA (PCP/ALM) → idiosyncratic residual REVERSAL.
   - `spectral_graph_contagion.py` — Laplacian/Fiedler (via scipy `eigsh` k=2) + centrality +
     directed heat-diffusion → leader→follower contagion FOLLOW.
   - `causal_lead_lag.py` — sparse CONDITIONAL Granger (factor-residualized multivariate regression +
     top-N-parents stability selection) from liquid leaders → causal FLOW.
3. **Rule changes** (`/home/dicktator4134/CLAUDE_CODE_RULES_COMPLETE.md` + memory
   `rules_claude_code.md` + `MEMORY.md`): **Rule 3** → "Goal/Plan-Doc First" (read the TOPIC's own
   `next_impl/<topic>.md`+PROGRESS, NOT the generic BOT_BLUEPRINT.md). **Rule 20** (new, always-on
   when working an ordered plan) → "Finish-Before-Next": current step fully Level-3 + goal-doc
   acceptance + wired + tracker, before the next step.

## Live architecture (how the Universe Core works — verified)
- `gate.funnel_pairs` (main process, each funnel cycle): `universe_frame.build_or_get(r, universe)`
  then `run_universe_modules(r, frame)`. ts-guard → RPCA/eigsh recompute ONLY when the frame rebuilds.
- `run_universe_modules` runs the bank under `threadpoolctl.threadpool_limits(1)` and publishes each
  module's per-symbol `ModuleOutput` to `scibrain:universe:contrib:{sym}`.
- `runner.score_symbol` (fork workers) folds `scibrain:universe:contrib:{sym}` into the per-symbol
  outputs BEFORE router/fuse/ic_record. `shadow_only=True` ⇒ fusion RECORDS them (counterfactual_only,
  authority observe, effect 0.0) but NEVER applies them to a live pick; ic_tracker still grades them.
- Dashboard `/scibrain` serves `universe` (digest) + `universe_modules` (which ran).

## Key facts / gotchas for next session
- **BLAS thread oversubscription is the universe-core's worst enemy.** The box runs load ~8–10
  (parallel scorer + candlenet/cn_train). Unpinned numpy in the main process thrashed (SFR RPCA 14s);
  pinned to 1 thread → 0.17s (~80×). ALWAYS pin heavy main-process linalg. Universe-core now ~0.8s
  for all 3 modules; skip-cycle funnel ~4s.
- Universe-Core cadence default = **60s** (`scibrain:universe_interval_s`), deliberately slower than
  the 20s funnel. Kill switch `scibrain:universe_modules_enabled`.
- New directional modules MUST enter `shadow_only=True` (Rule 14 observe authority). Promotion to a
  live vote needs matured-IC evidence + owner approval (Tier-2).
- LOAD-CHECK: bind-mount single-file edits do NOT hot-reload — `docker compose restart brain dashboard`.
  pycache is uid-mismatched → compile with `py_compile.compile(..., cfile=/tmp/...)`.
- Dashboard token for live `/scibrain` checks: `docker exec trading-bot-dashboard-1 python -c
  "import dashboard.api as api; print(api._make_token('verify'))"`.

## VERIFICATION-PENDING (carry forward)
- Settled IC for all 3 shadow modules needs the 30-min horizon to mature (recording path proven: their
  votes are in `scibrain:ic:pending`). Check `HGET scibrain:ic:map <module>` next session — that is the
  incremental-IC evidence the §6g ablation/promotion gate (Phase 7c) will judge for any promotion.

## Phase 2b status: 9/16. NEXT (in order):
- **RoughPathSignature** — hot-pair (per-symbol) level-2/3 log-signature of `[return, volume, OFI, OI]`
  paths; captures event ORDER. NOTE: this is a HOT-PAIR module (consumes SensorFrame), goes in
  `signals/scibrain/modules/`, NOT a UniverseModule. Enters shadow_only.
- then OptimalTransportRegime, EVTLargeDeviationTail, InformationGeometryHealth, … (see tracker).
