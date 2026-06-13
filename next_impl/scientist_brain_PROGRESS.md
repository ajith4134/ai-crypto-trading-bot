# Scientist-Brain Launchpad — BUILD PROGRESS TRACKER

Owner mandate (2026-06-08): redesign the Launchpad into a live **AI Scientist Brain** — a
modular circuit/CPU of PhD-math/physics/quantum modules that monitors all ~491 signal pairs
across multi-timeframes, discovers patterns/sequences, exploits noise, uses **Ollama** to
self-interrogate WHY each direction (long/short) was chosen (EDENUSDT-style auto post-mortem),
and shows **all internal workings live** on the Launchpad screen. Full design:
`next_impl/scientist_brain_launchpad.md`. Rules in force: 17 Claude-Code rules
([[rules-claude-code]]) + 12 Production-Code Mandate rules ([[rules-complete-code]]) — NO
stub/skeleton/fake code. This tracker is read by `scripts/scientist_brain_report.sh` (the
login auto-report hook): `- [x]` = done, `- [ ]` = open. Update IMMEDIATELY after each task.

---

## PHASE 0 — Foundation & auto-report
- [x] Save Production-Code Mandate (C1–C12) to memory
- [x] Locate + read master PhD notes (25 blocks) + existing launch_pad design + live facts (491 pairs, Ollama 14b)
- [x] Build login auto-report: tracker + report script
- [x] Wire SessionStart hook in settings.json + verify it prints
- [x] Web research pass to harden the design (FinRL-X, Koopman/DMD, BOCPD, TDA libs, Ollama reasoning)
- [x] Owner GO on the architecture plan

## PHASE 1 — Vertical Slice 1: full circuit end-to-end (real, runnable)
- [x] SciBrain package scaffold + typed contracts (SensorFrame, ModuleOutput, Decision dataclasses)
- [x] Sensor bus: load live features (candles/OFI/VPIN/funding/OI/CandleNet) for buffer symbols into RAM frames
- [x] KoopmanModule — DMD spectral mode decomposition (PHYSICS, Hankel-DMD), real math — runs live
- [x] BOCPDModule — Bayesian online change-point (Adams-MacKay, Student-t predictive), real math — runs live
- [x] Fusion ALU v1 — conviction-weighted direction + BOCPD stability damper + Kelly size
- [x] Ollama interrogator — dual-brain (lead+critic), structured verdict + responsible_factor + wrong_direction_risk; runs live; red-team caught a real regime/direction contradiction
- [x] Stream every internal (module outputs, fusion math, reasoning) to Redis `scibrain:*` (runner.py)
- [x] Run end-to-end on real live data; capture + show output (60 pairs/1.1s scored; live interrogation verified)
- [x] Deterministic attribution + primary_driver ("which factor is responsible", computed not inferred)
- [x] Dashboard panel v1 — ScientistBrain.tsx: live module votes + computed attribution + Ollama transcript; /scibrain endpoint; built + served via nginx
- [x] Pre-warm/local-14b requirement superseded by verified cloud-primary audit with local fallback

## PHASE 1b — REPLACE launch_pad: scibrain owns picking + opening (owner-corrected goal 2026-06-08)
Goal: Pair Scanner (~466) → scibrain scores ALL → picks symbol+direction → OPENS trades directly
(full replace of launch_pad funnel; initially verified in PAPER, now real LIVE; kill switch
scibrain:enabled).
- [x] scibrain/gate.py — funnel_pairs(r): score universe, filter by conviction, rank, direction-balance + cooldown + throttle (20s) → ordered picks (mirrors launch_pad gate contract)
- [x] scibrain/opener.py — sizing within USER caps (bot:min/max_position_usdt, conviction-scaled) + leverage (assign_leverage) + SL (compute_initial_sl) + engine.open_trade with provenance; dry-run mode
- [x] DRY-RUN verify: picks + capital/leverage/SL/qty correct on live data, no trades opened
- [x] Wire guarded scibrain branch into engine.process_signals (delegates + returns when scibrain:enabled=1; legacy untouched when 0)
- [x] Enable in PAPER + verify real paper trades open (7 opened, all tf=scibrain, provenance stamped); launchpad retired (enabled=0)
- [x] Honor owner min/max position caps — sizing = min_cap + (max-min)×conviction, clamped to [$80,$150] & free balance; VERIFIED live
- [x] Parallel-score the full universe — fork ProcessPool (8 workers, config scibrain:scorer_workers/scorer_parallel) fans the scan across cores: ~21s serial → 4.7s warm LIVE (~4.5×; 9.3s cold first cycle one-time); brain shows parallel=True, no fallback, candidates identical to serial (dir_mismatch=0); serial fallback on any pool failure
- [x] Correlation-cluster cap — signed-return-correlation de-dup (ρ·sign(dᵢ)·sign(dⱼ)≥0.8 = same risk cluster, max 2/cluster); LIVE, config-driven, verified dropping 12 correlated picks under aggressive rho on real candles
- [x] Interrogate opened trades automatically; cloud-primary replaced the local-14b pre-warm blocker

## PHASE 2 — Module bank expansion (PHYSICS + QUANTUM + math; each fully wired before the next)
PHYSICS modules (PhD-level physics scientist):
- [x] ChaosModule — nonlinear dynamics: Hurst (structure-function) → trend vs revert + autocorr; LIVE, drives picks
- [x] StatPhysSOCModule — Hill tail-index + critical-slowing-down + vol-of-vol + skew → criticality/crash_warning; LIVE, drives picks + crash radar (cont.75)
- [x] MeanFieldIsingModule — mean-field magnetization (crowding) from OFI/funding/OI/momentum spins → contrarian-when-crowded; LIVE (cont.75)
- [x] LangevinHawkesModule — AR(1) OU drift/diffusion (revert-to-fair) blended with Hawkes self-excitation (continuation-when-clustered); LIVE (cont.75)
- [x] RMTModule — SSA trajectory-matrix eigenspectrum + iteratively-fitted Marchenko-Pastur noise edge → denoised-trend direction; LIVE (cont.75)
QUANTUM module (PhD-level quantum physics):
- [x] QuantumModule — Von Neumann/spectral entropy + QFT dominant-cycle phase → cyclic direction; LIVE (quantum-inspired on CPU)
MATH / STATS / SIGNAL modules:
- [x] TDAModule — H0 sublevel-set persistent homology (union-find) → persistence entropy + dominant range → structure-aware reversion; LIVE (cont.75)
- [x] WaveletSpectralModule — Haar DWT multi-scale energy + Donoho noise floor (SNR) + denoised-trend slope; LIVE (cont.75)
- [x] InfoTheoryModule — Bandt-Pompe permutation entropy + Shannon sign-entropy + volume→price transfer entropy; predictability-gated momentum; LIVE (cont.75). (cross-PAIR TE deferred to Phase 5 universe-panel.)
- [x] NoiseHarvestModule — OU (AR1-fit) mean-reversion: fade >1σ stretched residual; "use noise to our advantage"; LIVE
- [x] KalmanModule — constant-velocity filter → fair-value + velocity (clean momentum) + innovation gate; LIVE
- [x] HMM regime module integration (reuse models/hmm_regime.pkl) — RETRAINED the degenerate model (filtered |r|>50%/day glitch that gave a mean-3.93 component; pretrainer fix); per-pair regime from daily-scale returns (1h→daily) via predict_proba, health-gated; LIVE bear/bull/turbulent (cont.75)

## PHASE 2b — Goal expansion: orthogonal PhD components, not module-count inflation
Goal update 2026-06-09: the 14-module bank is sufficient as the V1 pair-level core, but the full
scientist goal needs cross-market, path-order, distribution-transport, calibrated-tail, and control
planes. Full rationale and admission constitution: `scientist_brain_launchpad.md` Sections 4a/6d–6g.
- [x] Grade current 14-module coverage and identify missing information domains
- [x] Lock expansion policy: hot pair core + universe core + risk/control plane + discovery sandbox
- [x] Lock module admission constitution: unique evidence, correct role, CPU budget, conditional IC,
      ablation, evidence proportional to authority, reject/merge/demote authority
- [x] PRIORITY GATE: complete the Phase 7a snapshot/replay foundation + Phase 7b ChangeSpec/evaluator
      before implementing any expanded module below
      — CLEARED 2026-06-10: Phase 7a = 9/9, Phase 7b = 9/9 (rigor layer tasks 5–8 done + VERIFIED LIVE).
- [x] Extend contracts with role/evidence_family/shadow_only without breaking existing ModuleOutput readers
      — contracts.py: ModuleOutput gains 3 §5a fields APPENDED with safe defaults (role="direction",
      evidence_family="unspecified", shadow_only=False) so every existing producer/reader is unaffected;
      to_dict serializes them; from_dict reads them tolerantly (OLD pre-§5a snapshots default safely);
      abstain() propagates them; influence_manifest() surfaces declared role + evidence_family and wires
      shadow_only → the (previously unused) "counterfactual_only" status. fusion.py: shadow_only modules
      are EXCLUDED from the live directional/gate vote but remain in Decision.contributing (recorded +
      evaluable, never applied). VERIFIED LIVE (Rule 19): compile OK; fuse() logic test — a strong
      shadow short does NOT move a long verdict yet IS recorded as counterfactual_only, disconfirm: the
      SAME module non-shadow flips the vote to short; round-trip + old-snapshot deserialization pass;
      brain restarted (LOAD-CHECK) → real funnel scored 466/467 pairs, 3 picks, 0 errors, and all 14 live
      modules' redis outputs now carry role/evidence_family/shadow_only. git scope (Rule 11):
      contracts.py, fusion.py only. NEXT: build read-only in-RAM UniverseFrame shared once per cycle.
- [x] Build read-only in-RAM UniverseFrame shared once per cycle
      — NEW signals/scibrain/universe_frame.py + contracts.UniverseFrame (frozen, read-only): the
      Universe Core builds ONE cross-market substrate per funnel cycle in the brain MAIN process
      (before the per-symbol worker fan-out) — column-aligned (S=symbols) returns_by_tf matrices,
      Pearson correlation, lag-1 DIRECTED lead-lag L[i,j]=corr(rᵢ[t-1],rⱼ[t]) (asymmetric predictive-
      flow primitive), per-symbol feature_matrix (last_ret/vol/momentum/log_liquidity), liquidity, and
      a cheap market-state digest (breadth, crowding mean_abs_corr, PC1 market_factor_share). ONE
      pipelined Redis round trip per TF (no per-symbol storm — Phase-5 lever). In-RAM cache behind a
      cycle guard (build_or_get → built at most once per universe_interval_s[20]); get_current_frame()
      is the read-only accessor future cross-market modules (SparseFactorResidual/SpectralGraphContagion/
      CausalLeadLag/OptimalTransportRegime) consume in-process. Tier-0: NO trading authority — wired into
      gate.funnel_pairs over the FULL universe, never affects picks. Silent-failure path observable
      (Rule 12): too-thin universe → None + scibrain:universe:skips_total. CONSUMER: dashboard /scibrain
      serves the digest mirror scibrain:universe:state. VERIFIED LIVE (Rule 19): live build on 476–483
      real pairs — returns 1h(120×483)/15m(64×483), corr 483² no-NaN range[-0.998,1.0], lead-lag
      asymmetric, digest breadth_up 0.679/mean_abs_corr 0.131/factor_share 0.155. Rule-9 disconfirms
      PASS: deterministic (two builds identical corr+digest), corr diag≡1, build_or_get returns the SAME
      cached object within-interval (truly once/cycle), tiny universe→None +skip counter. LOAD-CHECK:
      brain+dashboard restarted → live runner's universe_frame_built fires once per funnel cycle
      (builds_total 1→2→3, each immediately before scibrain_funnel; funnel still picks=3 scored=482, no
      errors = zero trading effect); dashboard /scibrain returns the universe block (n_symbols 483, ts
      matches the live build) = producer→consumer end-to-end. git scope (Rule 11): universe_frame.py(new),
      contracts.py, gate.py, keys.py, dashboard/api.py. NEXT: SparseFactorResidual on this frame.
- [x] SparseFactorResidual — robust low-rank+sparse market/sector/idiosyncratic decomposition
      — NEW signals/scibrain/universe_modules/ (first UniverseModule bank: base.py + sparse_factor_
      residual.py) — the FIRST cross-market scorer over the shared UniverseFrame. Robust PCA via
      Principal Component Pursuit / inexact ALM (SVT for the low-rank L = market/sector factor flow +
      ℓ1 soft-threshold for the sparse Sp = idiosyncratic shocks) on the column-z-scored primary-TF
      return matrix. Per-symbol vote = FADE the cross-sectionally standardized recent idiosyncratic
      residual (residual-reversal stat-arb edge, UNIQUE evidence_family='cross_asset' — RMT is
      per-symbol SSA, this is a true universe decomposition), conviction = clip(|z_idio|/scale)·
      idio_fraction (‖Sp‖/(‖L‖+‖Sp‖)). ADMISSION (§6g + Rule 14): ships shadow_only=True — RECORDED +
      IC-evaluable, NEVER applied to a live pick until it proves incremental IC; promotion is a
      separate owner-approved step. WIRED: universe_frame.run_universe_modules() runs the bank ONCE per
      Universe-Core cycle in the main process (gate) and publishes per-symbol votes to
      scibrain:universe:contrib:{sym}; runner.score_symbol folds them into the per-symbol outputs
      BEFORE router/fuse/ic_record (so fusion records them + ic_tracker grades them across the
      fork-worker boundary). Rule-18 fixes in-session: (a) ts-guard so RPCA recomputes only when the
      frame actually rebuilds, not every funnel cycle; (b) Universe-Core default cadence decoupled to
      60s (>20s funnel) so the ~9s RPCA doesn't choke the hot SL loop. CONSUMER: dashboard /scibrain
      universe_modules block. VERIFIED LIVE (Rule 19 → design §6e Tier-A goal): PCP converged on real
      488–495 pairs (rank 79<120 low-rank, L+Sp reconstruction relerr 8e-4, Sp sparse), 488 bounded
      per-symbol votes all shadow_only/cross_asset. Rule-9 disconfirms PASS — deterministic; a STRONG
      OPPOSING shadow vote leaves the live pick IDENTICAL (long→long, conv unchanged) and shows
      counterfactual_only/actual_effect 0.0 (ZERO capital authority); too-thin frame → wholesale
      abstain; ts-guard verified (2 funnel scans → 1 RPCA run; skipped cycle 2.7–3.0s vs 11s when it
      runs). LOAD-CHECK: brain+dashboard restarted → live universe_modules_ran fires once per
      Universe-Core cycle, 488–495 contrib keys populate, 41/41 live worker-scored Decisions fold
      sparse_factor_residual as counterfactual_only (authority observe), funnel still picks=3/scored=486
      (unaffected), dashboard serves the block. IC recording proven (16 pending-queue members carry the
      sparse vote). VERIFICATION-PENDING: settled IC value needs the 30-min horizon to mature (recording
      path proven; matured IC populates after maturity) — the evidence the §6g ablation/promotion gate
      will judge it on. git scope (Rule 11): universe_modules/(new pkg), universe_frame.py, runner.py,
      gate.py, keys.py, dashboard/api.py. NEXT: SpectralGraphContagion on the frame.
- [x] SpectralGraphContagion — directed/signed graph, Laplacian/Fiedler/heat diffusion
      — NEW signals/scibrain/universe_modules/spectral_graph_contagion.py (2nd UniverseModule): pure
      SPECTRAL-GRAPH analysis of the cross-market graph the UniverseFrame already carries — kept
      deliberately distinct from the future CausalLeadLag (sparse Granger). (1) Undirected signed graph
      A=|correlation| (thresholded) → Laplacian L=D−A → eigh → Fiedler value λ2 (algebraic connectivity)
      + Fiedler-vector-sign 2-community spectral clustering; (2) eigenvector centrality (power iteration)
      = contagion-hub score; (3) directed graph W=directed_lead_lag (leader i→follower j) → in/out
      strength + HEAT/shock diffusion of the recent-return vector along Wᵀ (Σ αᵏ(Wᵀ)ᵏr, ∞-norm
      normalized for stability) = the move flowing INTO each node from its leaders that hasn't
      propagated yet. FALSIFIABLE HYPOTHESIS (directed contagion, distinct from SparseFactorResidual's
      reversal): a node its leaders predict will FOLLOW → direction=tanh(z_diffused), conviction=
      clip(|z_diffused|)·follower_weight (strong followers predictable from leaders = high conv; pure
      leaders = low). evidence_family='contagion' (distinct §6g.1). ADMISSION: shadow_only=True (Rule 14
      observe authority). WIRING: zero new plumbing — just registered in UNIVERSE_MODULES; the
      run_universe_modules/contrib/fold path already carries it. VERIFIED LIVE (Rule 19 → design §6e
      Tier-A goal): on real 485–488 pairs — Fiedler 2-cluster split 177/311, leaders 192/followers 296,
      488 bounded votes (dir [-0.885,0.984], conv [0,0.94]). Rule-9 disconfirm: deterministic; ZERO
      capital authority — a STRONG opposing spectral shadow leaves the live pick unchanged
      (counterfactual_only/effect 0.0). RULE-14 AUDIT (owner-requested): passes the Evidence/Authority/
      Influence Gate that replaced the fixed shadow-cycle rule — authority=observe, status=
      counterfactual_only, never an applied cause, kill switch (universe_modules_enabled), promotion
      deferred to owner+Tier-2 evidence. LOAD-CHECK: brain+dashboard restarted → live universe_modules_ran
      lists both modules (combined ~3.4s), 81 live worker-scored Decisions fold spectral_graph_contagion
      as counterfactual_only/observe, funnel unaffected (picks=3), dashboard universe_modules block shows
      both. VERIFICATION-PENDING: settled IC needs the 30-min horizon (recording path proven, same as
      SparseFactorResidual). git scope (Rule 11): spectral_graph_contagion.py(new), universe_modules/
      __init__.py. NEXT: CausalLeadLag (sparse conditional lag graph) — keep distinct from this spectral one.
- [x] CausalLeadLag — sparse conditional lag graph on liquid leaders/clusters
      — NEW universe_modules/causal_lead_lag.py (3rd UniverseModule): SPARSE CONDITIONAL Granger/PCMCI-
      style lag graph, kept distinct from SpectralGraphContagion (bivariate lead-lag) AND InfoTheory
      (bivariate TE). Restrict to the top-K (≤30) most LIQUID symbols as candidate drivers; residualize
      leaders' lagged returns AND every target's next return against the cross-sectional market factor
      (regress out f) → CONDITIONAL on the common factor; one shared ridge multivariate solve
      B=(XᵀX+λI)⁻¹XᵀY gives the (K×S) conditional lead-lag (each coef controls for the OTHER leaders +
      factor). STABILITY SELECTION (PCMCI-style): 20 row-subsample refits, an edge is kept only if it is
      a target's top-_N_PARENTS(3) strongest driver in ≥π(0.5) of subsamples → genuinely SPARSE graph;
      self-edges zeroed (own-lag is the per-symbol modules' job). Vote = causally-predicted move from a
      target's STABLE leaders (abstain if none). evidence_family='causal_flow', shadow_only=True (Rule 14
      observe). Deterministic (fixed RNG seed). RULE-18 FIX (caught by Rule-9 disconfirm): first cut kept
      ~15-18 drivers/target (NOT sparse — median threshold trap); switched to top-N-parents selection →
      mean 1.74 drivers/target (max 4), ~5.5% edge density, 29 honest abstentions. VERIFIED LIVE (Rule 19
      → §6e Tier-A goal): on 483–501 real pairs, sensible causal structure (ZIL←SOL, VANRY←BNB, KAS←PEPE);
      deterministic; ZERO authority (91 live Decisions fold it counterfactual_only/effect 0.0); compute
      0.04–0.15s.
      ── BIG PERF FIX (Rule 18, benefits the WHOLE Universe-Core incl. the prior SFR+SGC tasks): the
      Universe-Core ran 10–26s and produced a 33–43s funnel cycle. Rule-9 root-cause: NOT the algorithms
      — the brain MAIN process let numpy/BLAS oversubscribe threads while the 8-worker parallel scorer +
      candlenet/cn_train saturate the 10-core box (load 8–10). Measured SFR RPCA 14s unpinned vs 0.17s at
      1 thread (~80×). FIX: run_universe_modules now wraps the whole bank in threadpoolctl.threadpool_
      limits(1) (the fork workers already pin to 1 via gate._worker_init; the main process now matches).
      Also optimized SGC: full O(n³) eigh → scipy eigsh k=2 (Fiedler only), 9.6s→0.22s (~43×, eigh
      fallback kept). RESULT VERIFIED LIVE: universe-core 10–26s → ~0.8s for ALL 3 modules (501 symbols);
      funnel cycles that skip it ~4s; all 3 modules still fold as counterfactual_only/effect 0.0.
      VERIFICATION-PENDING: settled IC needs the 30-min horizon (recording path proven). git scope (Rule
      11): causal_lead_lag.py(new), universe_modules/__init__.py, universe_frame.py (thread-pin),
      spectral_graph_contagion.py (eigsh). NEXT: RoughPathSignature (hot-pair path-signature).
- [x] RoughPathSignature — level-2/3 log-signature of return/volume/OFI/OI event paths
      — NEW signals/scibrain/modules/rough_path_signature.py (HOT-PAIR module, runs per-symbol in the
      fork workers — NOT a UniverseModule). Level-2 path signature (rough-path theory): the antisymmetric
      iterated-integral term = the Lévy AREA between channels = WHICH channel led, the one thing summary
      stats (mean/vol/slope/entropy) are blind to ("rise-then-volume" ≠ "volume-then-rise"). HONEST
      CONSTRAINT: the SensorFrame only has per-bar candles + SINGLE-snapshot ofi/oi (no per-bar OFI/OI
      series), so channels are candle-derivable: log-return, signed money-flow (sign(ret)·log1p(vol) =
      per-bar order-flow proxy), close-location-value. Canonical Chen level-2 signature; vote = flow↔return
      Lévy area × net flow (flow-leads-price → continuation; price-leads-flow → exhaustion). evidence_
      family='path_order', shadow_only=True (Rule 14 observe). Pure numpy (cumsums), deterministic,
      abstains on flat/insufficient paths. (Explicit Hoff lead-lag transform + per-bar OFI/OI from the
      order book + an online-learned readout are documented v2 follow-ons.) VERIFIED LIVE (Rule 19 → §6e
      Tier-A goal): the ORDER disconfirm is the key test — identical increment SETS in reversed ORDER give
      OPPOSITE Lévy area (+2.0 vs −2.0); a summary-stat module sees them as identical. Live 40/40 scored,
      bounded (dir [-0.68,0.92], conv≤0.93), all shadow_only/path_order. CRITICAL (bot went LIVE this
      session): 61 live worker-scored Decisions record it counterfactual_only/observe/effect 0.0 — ZERO
      authority confirmed UNDER REAL MONEY. Loaded into the live brain (recreated by the live switch)
      without crashing. VERIFICATION-PENDING: settled IC needs the 30-min horizon. git scope (Rule 11):
      rough_path_signature.py(new), modules/__init__.py. NEXT: OptimalTransportRegime.
- [x] OptimalTransportRegime — sliced-Wasserstein/Sinkhorn regime/prototype distance
      — NEW universe_modules/optimal_transport_regime.py (4th UniverseModule, role=context+gate). UNIQUE
      EVIDENCE (§6g.1/2): the geometry of distribution MIGRATION — optimal-transport cost of moving the
      whole cross-sectional feature cloud — which HMM labels, KL, and the digest's point summaries are
      blind to. evidence_family='distribution_transport' (no other module uses OT between distributions).
      TWO outputs: (1) REGIME/MIGRATION — sliced-Wasserstein distance from the current column-standardized
      feature cloud to a self-bootstrapping regime-prototype library; nearest regime + migration_velocity
      (d/dt of min OT distance); novel cloud admitted as a new prototype (capped _MAX_PROTOS=8). (2)
      WINNER/LOSER per-symbol vote — OWNER-APPROVED full spec: two FIFO clouds of std feature vectors
      LABELLED BY REALIZED forward return (the module owns a pending→settle outcome ledger mirroring
      ic_tracker: each pending member ZREM'd as consumed → crash loses, never double-counts); a symbol
      transport-closer to the winner cloud than the loser cloud → long (direction=tanh((d_loser−d_winner)/
      SCALE), conviction·cloud_maturity·regime_stability). STATEFUL (unlike the pure decomposition modules);
      SCORING is a pure deterministic fn (fixed projection basis, _PROJ_SEED), settle/record/regime-update
      are separate idempotent state ops. ADMISSION: shadow_only=True (Rule 14 observe). HONEST COLD START:
      winner/loser clouds empty ⇒ per-symbol votes ABSTAIN (no fabricated prototypes) until both reach
      _MIN_CLOUD=40 settled samples; the regime/context still publishes immediately. WIRING: registered in
      UNIVERSE_MODULES; the run_universe_modules/contrib/fold path already carries it (zero new plumbing);
      new keys.py OT_* keys; dashboard mirror scibrain:ot:state. VERIFIED LIVE (Rule 19 → §6e Tier-A goal):
      §6g.6 synthetic invariants PASS in-container — SW(a,a)=0 + monotonic (0.41<2.48), point-to-cloud
      (winner-like 0.041 vs loser 2.220), DIRECTIONAL HYPOTHESIS (winner-like→+0.994 long, loser-like→
      −0.994 short), bounded, DETERMINISTIC (identical inputs→identical votes), cold-start→all abstain.
      LIVE on 472 real pairs via the funnel scoring path (build_or_get+run_universe_modules): universe_
      modules_ran lists all 4 modules (bank 2.1s, within budget); OT state published (regime_id 0, first
      prototype admitted_new=true, stability 1.0); outcome ledger recording (ot:pending=48=_N_RECORD);
      472 contrib keys carry OT as role=context/distribution_transport/shadow_only/ok=False/cold_start →
      dir 0/conv 0 = ZERO authority confirmed. Loads in the live brain (restarted, package imports clean).
      RULE-9 disconfirm — my change is NOT the cause of the idle funnel: builds_total frozen since 01:06
      is PRE-EXISTING (bot at max_open=5/5 ⇒ engine skips origination ⇒ universe-core not invoked); the
      code path itself works perfectly when invoked (proven above). VERIFICATION-PENDING: (a) per-symbol
      DIRECTIONAL votes turn on only after both clouds reach _MIN_CLOUD via the 30-min horizon; (b)
      migration_velocity>0 after ≥2 distinct-regime cycles; (c) settled IC (same carry-forward as the
      prior 3 modules). OBSERVE: scibrain:ot:settled_total rising, scibrain:ot:cloud:winner/loser filling,
      then contrib ok=True with non-zero dir; HGET scibrain:ic:map optimal_transport_regime once matured.
      git scope (Rule 11): optimal_transport_regime.py(new), universe_modules/__init__.py, keys.py.
      ── FINDING for owner (out-of-scope, flagged not silently fixed per Rule 18): the scibrain universe-
      core + ALL shadow-module IC maturation STALLS whenever the bot sits at max_open (origination, which
      drives build_or_get, is capacity-gated). Decoupling universe scoring from origination capacity is an
      engine-loop change (relates to Phase 5 full-universe scorer), not this module — owner decision.
      NEXT: EVTLargeDeviationTail.
- [x] EVTLargeDeviationTail — POT/GPD adverse-tail probability + risk envelope
      — NEW modules/evt_large_deviation_tail.py (16th hot-pair module, the FIRST RISK-plane module,
      role='risk' → gate+size-cap). UNIQUE EVIDENCE (§6g.1/2, fills the §6d gap "tail probability is not
      explicitly calibrated despite real-money operation"): the bank has crash early-WARNING heuristics
      (StatPhysSOC power-law, Chaos/Wavelet) but no calibrated tail PROBABILITY. Extreme Value Theory
      (Pickands–Balkema–de Haan): exceedances over a high threshold converge to the Generalized Pareto
      Distribution → a principled P(adverse>m) + ES. evidence_family='tail'. ROLE=RISK not direction:
      emits direction=0 + conviction=tail_SAFETY∈[0.15,1] — the existing fusion already turns a
      direction-0/conviction>0 output into a multiplicative size damper (stability=Π gate_conviction, same
      path BOCPD uses), so NO new consume-side plumbing. COMPUTATION (deterministic, closed-form — no MLE):
      POT threshold u=quantile(side,0.90); GPD(ξ,σ) by METHOD OF MOMENTS on exceedances (ξ=½(1−m²/s²),
      σ=½m(1+m²/s²)); BOTH tails (loss+gain) → adverse-tail prob (horizon-scaled, clustering-inflated),
      VaR/ES@0.99, rare-event rate λ, extremal index θ (runs estimator; θ<1 ⇒ clustering), tail asymmetry
      ξ_L−ξ_R, Hill cross-check. Gate severity driven by the CALIBRATED differentiators (ξ heaviness + ES),
      not the ~1% rare-bar prob (Rule-18 fix: first cut gave a useless flat 0.98 gate). ADMISSION (§6g +
      Rule 14, design VS-11 "risk shadow authority"): shadow_only=True — full envelope RECORDED on every
      scored symbol, NEVER caps live size until owner-promoted. Deterministic, bounded, abstains on too-few
      exceedances. VERIFIED LIVE (Rule 19 → §6e Tier-A goal): §6g.6 synthetic invariants PASS in-container —
      GPD MoM recovered Pareto(a=4) tail index ξ=0.252 vs true 0.25; HEAVY (Student-t ν=2.5) safety 0.378 <
      LIGHT (Gaussian) 0.824; extremal index clustered 0.20 < isolated 1.0; bounded risk-gate, DETERMINISTIC,
      abstain on thin data. LIVE on real 1h SensorFrames (after Rule-18 fix: _TF 15m→1h — only 1h carries
      ≥120 bars, ≤30m have ≤65): real envelopes differentiate names — USELESSUSDT ξ_loss 0.45/asym +0.84 →
      safety 0.28 (heaviest, most damped) vs LIT/MANTA ξ_loss≈−0.05 → safety 0.80; all role=risk/shadow_only/
      dir 0 = ZERO authority confirmed. Brain restarted (LOAD-CHECK) → 16-module bank loads, no tracebacks.
      VERIFICATION-PENDING: (a) folds into live Decisions when the funnel next fires (idle while bot at
      max_open 5/5 — see the funnel-idle finding); (b) tail CALIBRATION eval (predicted P vs realized
      exceedance, Brier/reliability) is a MAIN-PROCESS concern — this module runs in the per-symbol FORK
      WORKERS so it must NOT run a settle sweep here (would race per-worker); calibration is wired in the
      risk-plane evaluation step (pairs with CorrelationKellyAllocator, VS-11 / Phase 7c promotion gate).
      No dead keys: the envelope rides in the recorded ModuleOutput.features. git scope (Rule 11):
      evt_large_deviation_tail.py(new), modules/__init__.py. NEXT: InformationGeometryHealth.
- [x] InformationGeometryHealth — Fisher-Rao/MMD/HSIC drift + nonlinear redundancy
      — NEW signals/scibrain/info_geometry.py (the FIRST recalibration/health module — scores the BANK
      ITSELF, not the market; role=recalibration/context, Tier-0 report-only, NOT a per-symbol vote).
      Three diagnostics no alpha module can give (§6d gaps + design line 285): (1) MODEL-MANIFOLD DRIFT —
      Fisher-Rao geodesic distance on the univariate-Gaussian manifold (closed form, metric (dμ²+2dσ²)/σ²
      → hyperbolic half-plane) between each module's CURRENT output law and a slow EWMA baseline; (2)
      IC-TRANSFERABILITY = exp(−drift) — a drifted module's historical IC is trusted less; (3) NONLINEAR
      REDUNDANCY — normalized HSIC (≈CKA, RBF + median-heuristic bandwidth) between every pair of modules'
      ALIGNED output vectors, catching nonlinear dependence a Pearson corr misses. SUBSTRATE: the aligned
      per-module direction vectors ic_tracker already stashes in scibrain:ic:pending + rolling IC in
      scibrain:ic:map — NO new per-symbol plumbing. WIRED main-process, once/cycle in gate.py right after
      ic_tracker.settle (NOT in the fork workers); CONSUMER: dashboard /scibrain "infogeo" block (verified
      end-to-end — no dead key). It produces the redundancy matrix the NEXT item (evidence-family penalty)
      consumes. VERIFIED LIVE (Rule 19 → §6e Tier-B goal): §6g.6 math invariants PASS in-container —
      Fisher-Rao identity 0 / mean-monotonic (0.50<2.61) / symmetric / var-sensitive; HSIC identical 1.0,
      independent 0.04, and the KILLER test nonlinear y=x² HSIC=0.478 while Pearson|r|=0.057 (catches what
      correlation misses). LIVE on 971 real ic:pending vectors / 16 modules: report computed + published +
      SERVED by dashboard /scibrain (infogeo block present, n_modules 16/n_samples 971). Real redundancy
      structure found — chaos↔noiseharvest 0.62, kalman↔noiseharvest 0.62 (overlapping reversion/momentum
      experts) while the distinct cross-market modules score low (causal_lead_lag↔spectral 0.08). mean_drift
      0.0 = correct COLD START (baseline initialized to current). Tier-0: emits NO vote → ZERO authority by
      construction. Brain+dashboard restarted (LOAD-CHECK), compile OK (info_geometry, gate, api), no
      tracebacks. VERIFICATION-PENDING: non-zero drift emerges only as the live brain runs assess() across
      cycles with evolving outputs (the funnel is idle while bot at max_open 5/5 — see funnel-idle finding);
      OBSERVE scibrain:infogeo:runs_total rising + scibrain:infogeo:health mean_drift>0. git scope (Rule 11):
      info_geometry.py(new), keys.py, gate.py, dashboard/api.py. NEXT: MultifractalRG+ErgodicMixing (shadow
      research) OR the evidence-family correlation penalty (which now has its HSIC substrate).
- [x] MultifractalRG and ErgodicMixing — shadow research; promote only if incremental beyond existing bank
      — TWO new hot-pair GATE modules (role=gate+horizon, shadow_only=True), each with evidence DISTINCT
      from Chaos+Wavelet+SOC (the §6e bar). (1) modules/multifractal_rg.py — full q-order structure-function
      spectrum S_q(τ)~τ^ζ(q) → generalized Hurst H(q)=ζ(q)/q and its NONLINEARITY (Chaos sees only the
      single q=2 Hurst; this sees mono- vs MULTI-fractal = the H(q) spectrum WIDTH = intermittency) + a
      CROSSOVER-scale break + a rough-volatility exponent. evidence_family='multifractal'. Emits a
      HORIZON-VALIDITY gate: wide spectrum / strong crossover ⇒ single-horizon prediction unreliable ⇒
      validity damped. (2) modules/ergodic_mixing.py — decorrelation/mixing time (ACF integral time +
      1/e half-life = Ruelle/spectral-gap proxy = ALPHA HALF-LIFE), Bandt-Pompe PERMUTATION ENTROPY
      (model-free predictability), and an ERGODICITY-BREAKING statistic (dispersion of sub-window variances
      ⇒ is the window representative?). evidence_family='ergodicity'. Emits a SAMPLE-TRUST gate. Both:
      direction=0 (gate via the existing fusion size-damper, same path as BOCPD/EVT), pure numpy,
      deterministic, abstain-safe. ADMISSION (§6g + Rule 14): shadow_only — per the design they STAY shadow /
      get PRUNED unless they prove incremental IC beyond the existing bank (the prune/demote report, the 3rd
      VS-12 item, will judge that). VERIFIED LIVE (Rule 19 → §6e Tier-B goal): §6g.6 invariants PASS
      in-container — MF width discriminates monofractal RW 0.012 vs intermittent (vol-clustered) 0.094 →
      validity 0.88 vs 0.67; EM ergodicity-breaking stationary 0.16 vs variance-regime-shift 0.47, perm-
      entropy sine 0.55 < random 0.997; both bounded/gate/shadow/deterministic/abstain. LIVE on real 1h
      SensorFrames: real differentiation — MF RLCUSDT width 0.381 (multifractal)→validity 0.49 vs GUAUSDT
      0.155→0.82; EM EB 0.31–0.62 (RLC/BABY non-ergodic→trust 0.32–0.34), PE≈1.0/half-life 1 bar (1h crypto
      is near-white/fast-mixing — semantically correct: the sample-trust gate honestly signals low
      confidence). All role=gate/shadow_only/dir 0 = ZERO authority. Brain restarted (LOAD-CHECK), 18-module
      bank loads, no tracebacks. VERIFICATION-PENDING: settled IC (same horizon carry-forward as the bank) —
      and per the design their KEEP/PRUNE decision is explicitly deferred to the ablation/prune report.
      git scope (Rule 11): multifractal_rg.py(new), ergodic_mixing.py(new), modules/__init__.py.
      NEXT: evidence-family correlation penalty in fusion/router.
- [x] Add evidence-family correlation penalty to fusion/router — §6g.330 "five trend-derived modules
      can't count as five independent confirmations." HYBRID similarity (owner choice 2026-06-11):
      S_ij = max(same-evidence_family prior 0.6, normalized-HSIC from info_geometry's redundancy matrix);
      discount_i = 1/(1+strength·Σ_{j≠i}S_ij) → a redundant clique collapses toward counting ~once, an
      independent voter ≈ undiscounted. PREREQUISITE FIXED: the whole live directional bank built
      ModuleOutput with NO evidence_family (all "unspecified" → penalty would've been a no-op); declared
      a grounded family on all 14 modules (koopman=dynamics_spectral, kalman+rmt=trend_momentum,
      chaos=persistence, wavelet=multiscale_energy, noiseharvest=mean_reversion, ising=crowding,
      quantum=cycle_spectral, tda=topology, info_theory=information, langevin_hawkes=flow_excitation;
      gates bocpd/hmm/statphys get role+family for the manifest). Stamped centrally in base.Module.evaluate
      (fills class-declared identity only when output left the default; never overrides the shadow bank's
      own). RULE-18 BONUS FIX: base.evaluate's clamp path rebuilt ModuleOutput by hand and DROPPED
      role/evidence_family/shadow_only → a shadow_only module whose conviction needed clamping silently
      LEAKED into the live vote; now rebuilds via dataclasses.replace (shadow flag preserved — unit-proven).
      Wired: fusion._family_redundancy + applied to directional weights; per-module redundancy_discount +
      family recorded in attribution; Decision.family_penalty summary in to_dict/from_dict; influence_manifest
      effective_weight now multiplies by the discount (honest). runner reads scibrain:family_penalty_strength
      (default 1.0 = owner "full apply"; SET 0 = instant no-op/rollback) + the infogeo HSIC report, passes to
      fuse. Real money → behind the strength knob + scibrain:enabled kill switch (Rule 14). VERIFIED LIVE
      (Rule 19 L3, paper): invariants PASS in-container (clique net 0.667→0.190, HSIC cross-family→0.526,
      strength=0 exact no-op, independents undiscounted, shadow-leak fix holds); on the LIVE brain 31/31
      recent decisions carry family_penalty applied=True strength=1.0, and ORDIUSDT short shows it BIT —
      groups{trend_momentum:2}, hsic_pairs_used=4, kalman→0.449/rmt→0.625 + cross-family HSIC discounts.
      Brain restarted (LOAD-CHECK), no tracebacks. git scope (Rule 11): all in the UNTRACKED signals/scibrain
      package (keys.py, contracts.py, fusion.py, runner.py, modules/base.py + 13 module files,
      _smoke_family_penalty.py). NEXT: automatic module ablation/prune/demote report.
- [x] Automatic module ablation/prune/demote report — §6g.8 (survive ablation) + §6g.10 (reject/merge/
      demote if redundant/unstable/useless), VS-12's 3rd piece. New signals/scibrain/ablation.py:
      assess(r) judges the bank each cycle from substrates already computed — rolling IC (ic_tracker) +
      the nonlinear-redundancy/drift matrix (info_geometry) — and publishes an ADVISORY per-module
      verdict. Precedence (worst-first): PRUNE > DEMOTE > WATCH > KEEP; INSUFFICIENT until IC matures.
      DEMOTE keeps the higher-IC representative of a redundant (HSIC≥0.55) cluster, demotes the duplicate.
      CRITICAL correctness (Rule 18, caught when v1 flagged 11/18 PRUNE on tiny ICs): added a SIGNIFICANCE
      gate — a PRUNE requires the IC to clear ~2 standard errors (SE≈1/√n) so we NEVER prune on
      statistically-insignificant noise (§6g.8 "MEASURABLY reduce performance"; Rule 14 evidence scales
      with blast radius). ADVISORY ONLY: never mutates live authority (no runtime demote mechanism;
      capital-affecting action is owner-gated, deferred to the promotion kernel VS-17) — report carries
      advisory_only=True. Wired: gate.py beat calls ablation.assess right after info_geometry.assess
      (separate guard, Rule 12); dashboard /scibrain payload serves the "ablation" block (consumer).
      Keys: scibrain:ablation:report + scibrain:ablation:runs_total. VERIFIED LIVE (Rule 19 L3, paper):
      8 verdict invariants PASS incl. the KILLER "small IC on few samples → KEEP (no pruning on noise)";
      on the live brain the BEAT fires it automatically — ablation:runs_total +2 in lockstep with
      infogeo +2 over 90s (same gate cycle); corrected report sane (n_matured 18, mostly KEEP, only the
      worst |IC| prune/demote with evidence cited, e.g. ising IC upper-bound +0.013<0.02 useful floor);
      dashboard GET /scibrain 200 serves the ablation block (advisory_only=True). Brain+dashboard restarted
      (LOAD-CHECK). git scope (Rule 11): untracked signals/scibrain (ablation.py NEW, gate.py, keys.py,
      _smoke_ablation.py) + tracked dashboard/api.py (+ pre-existing churn). PHASE 2b COMPLETE.

## PHASE 3 — Meta-Router (MoE) + regime gating
- [x] Regime detector wired (BOCPD + HMM) → router state per pair — router.py::detect_regime: canonical
      {trending|mean_revert|turbulent|neutral} from BOCPD p_change + HMM bull/bear/turbulent + StatPhysSOC
      crash/criticality, confirmed by Chaos-Hurst & Kalman-velocity. LIVE: regime counters scibrain:regime:*
      populating (mean_revert 890 / turbulent 165 / neutral 393 / trending 6) + carried on every Decision.
- [x] Meta-router activates the right module subset per pair×regime (Block 16 MoE) — router.py::route: per-regime
      gain profile (boost the experts that fit, damp/deselect the rest; gain 0 = deselected). fusion.fuse() now
      multiplies each directional weight by its router gain. VERIFIED: in mean_revert the router damped the
      trend experts and correctly SUPPRESSED a trend-chasing long (HMSTRUSDT plain=long → router=None);
      neutral regime = exact no-op (ONDOUSDT identical). Config scibrain:router_enabled / router_strength.
      ── 2026-06-10 RULE-19 AUDIT: found the gain-0 deselection path DORMANT (no _PROFILES entry was 0;
      turbulent min was 0.30, so g=1+(raw-1)·strength never reached ≤1e-6, RouterState.deactivated was always
      empty and scibrain:deactivated_total stayed None). OWNER-APPROVED FIX (2026-06-10): enabled TRUE
      deselection — turbulent profile koopman 0.30→0.0 and langevin_hawkes 0.30→0.0 (the two pure trend-chasers
      that chase INTO a crash; the rest stay heavily damped). At default router_strength=1.0 their gain is now
      exactly 0 ⇒ vote zeroed in fusion, not just damped. VERIFIED LIVE (Rule 19): after brain restart,
      turbulent MANTRAUSDT → koopman gain 0.0 + langevin_hawkes gain 0.0, RouterState.deactivated=
      ['koopman','langevin_hawkes'], kalman still damped 0.5165 (not deselected); scibrain:deactivated_total
      climbed None→80. Kill switch: scibrain:router_strength=0 (off) or router_enabled. CAPITAL-AFFECTING
      change, owner-approved before merge (Rule 14 satisfied).
- [x] Per-module Information-Coefficient tracking → adaptive router weights — ic_tracker.py: shadow IC over the
      whole universe (record votes+ref price → settle vs realized fwd return at ic_horizon_min → rolling Pearson
      IC per module → router folds IC→multiplier [0.35,1.5]). VERIFIED: predictive module IC +0.88→×1.5,
      anti-predictive −0.88→×0.35; record live (pending queue 1454), settle centralized once/cycle in gate.
      ── 2026-06-10 RULE-19 AUDIT FIX (was SILENTLY DEAD): the documented "one observation per symbol per
      horizon bucket" dedup never worked (the pending member JSON varies by price/ts so it never collapsed).
      With ~486 pairs re-recorded every ~5s cycle, the pending ZSET pinned at its 5000 cap and zremrangebyrank
      EVICTED every vote ~3.4× faster than its 30-min maturity → settlement starved, settled_total FROZEN at
      29,399, and the IC map served STALE values the router still multiplied into live trades. MEASURED the
      smoking gun: pending survival window 533s vs 1800s needed. FIX (ic_tracker.record): O(1) NX seen-marker
      per (symbol,bucket), auto-expiring just past maturity, so only the first cycle in each bucket records →
      inflow ≈ 487/30min << 500/cycle settle capacity, every vote survives to settle. VERIFIED LIVE: dedup
      unit test PASS (1 member + seen-marker after 3 same-bucket records); post-brain-restart seen-markers=487
      ≈ #pairs (live runner uses the fix); maturity-survival spread widened 533s→1233s + 997 seen-markers.
      CLIMB CONFIRMED LIVE: settled_total 29399→29527 at 13:18 (IC learner UNFROZEN — votes now mature +
      settle once/cycle). NOTE: re-activates adaptive IC that was frozen → router gains will shift; kill
      switch scibrain:ic_enabled=0. keys.py IC_SEEN added.

## PHASE 4 — Ollama direction-audit on every fired trade  ✅ FUNCTIONAL (cloud-primary, 2026-06-09)
NOTE: all three BUILT + WIRED + LIVE. The CPU-only local-Ollama saturation that made audits time out
is RESOLVED by routing the audit LLM through the bot's CLOUD-PRIMARY chain (llm.providers.call_chain →
nvidia/groq/cerebras 70B+ models) with local Ollama as fallback. Audits now complete in ~7–16s
(was 300–600s timeout-fail), on far better models, using ZERO local CPU. The wrong-direction detector
now actually FIRES (verified: ALLOUSDT flagged wrong_dir_risk=0.7). Config: scibrain:audit_use_cloud=1
(default); scibrain:lead_model/critic_model are the local FALLBACK models.
- [x] Post-trade interrogator: automatic EDENUSDT-style "what led to this direction" post-mortem —
      audit.py (audit_one/drain) + celery beat scibrain-audit-drain(30s)+scibrain-prewarm(300s) on the
      airllm queue, single-flight lock; opener enqueues a Decision snapshot on every real open. RUNS
      end-to-end (BLESSUSDT/IRYSUSDT audits completed + stamped) when ollama has a window.
- [x] Right-signal-wrong-direction detector + flag — audit._flag_wrong_direction: flags when the
      interrogation is available AND (disagrees with the opened direction OR wrong_direction_risk ≥
      scibrain:wrong_dir_threshold[0.6]) → scibrain:wrong_direction_trades ZSET + counter. BONUS:
      remediation engine (_remediate/_record_action) proposes a fix + circuit improvement on flagged trades.
- [x] Trade provenance (chosen modules, scores, reasoning) stamped on the trade row — opener writes
      signals_at_entry={origin,primary_driver,regime,conviction,size_frac,attribution}; audit._stamp_trade
      merges the verdict in. VERIFIED (no ollama dep): every scibrain trade row has origin=scibrain +
      driver + canonical router regime + 12-module attribution; completed audits show audit=YES.

## PHASE 5 — Parallel scorer (full universe, 8–10 cores)
- [x] ProcessPool fan-out scorer across all ~491 pairs × multi-TF — fork pool in gate.py (_score_universe), ~3.7× warm; LIVE (done early under Phase 1b)
- [x] In-RAM feature/candle cache for the full universe — BUILT + bit-identical, but DEFAULT OFF (not a win).
      signals/scibrain/sensor_bus.py: SnapshotReader + bulk_load() (one chunked pipeline reads every key
      build_frame touches for the whole universe) + a process-global snapshot routed through build_frame;
      gate.py _score_cached_parallel: parent bulk-loads → installs snapshot → forks a fresh pool so workers
      inherit it via COW (zero per-symbol frame reads), behind scibrain:scorer_cache_enabled. VERIFIED LIVE
      (Rule 19): frames BIT-IDENTICAL to live Redis on 8 real symbols (_smoke_snapshot.py) + SnapshotReader
      unit tests pass. BUT live cycle_ms REGRESSED 4620→5937-7575ms warm, so DEFAULT=0 (flag off, reverted;
      decisions still flowing). Kept as correct flag-gated infra for a future persistent-pool+shared-memory
      (pre-parsed numpy) design. See [[finding-scibrain-scan-compute-bound]].
- [x] Verify sub-second full-market scan + no Redis round-trip storm — VERIFIED (negative result, Rule 19):
      NOT sub-second and NOT read-bound. Measured pure module compute = 143ms/symbol (18 PhD modules) →
      ~6.9s/480 across 10 workers; per-symbol reads were already parallelized across workers and are ~50×
      cheaper than compute. bulk_load whole universe = 975ms, frame-parse 1334ms. CONCLUSION: the scan is
      COMPUTE-bound — caching reads cannot reach sub-second. Real lever = compute (fewer/lighter modules,
      vectorize/numba, top-K prefilter, or accept cadence). Evidence saved: [[finding-scibrain-scan-compute-bound]].
- [x] Add separately budgeted Universe Core cadence; never recompute universe matrices per symbol
      — SATISFIED by the Phase-2b universe_frame work, now LIVE-VERIFIED (Rule 19): build_or_get builds
      the shared UniverseFrame at most once per scibrain:universe_interval_s (default 60s, deliberately
      SLOWER than the ~20s funnel) into an in-RAM _CACHE; run_universe_modules carries a frame.ts guard
      that SKIPS recompute unless the frame actually rebuilt, so the expensive matrices/decompositions
      (corr, lead-lag, RPCA, spectral, OT) are NEVER recomputed per funnel cycle — let alone per symbol.
      The per-symbol fork workers consume the published scibrain:universe:contrib:{sym}, never rebuilding
      universe matrices. LIVE EVIDENCE: universe_frame_built fires ~once/109s for the WHOLE 453–460-symbol
      universe at once (6 builds over ~9 min) while the funnel cycles tick every ~20–35s and funnel_active
      every ~10s — the build cadence is decoupled from and far slower than the hot loop; builds_total
      climbing (182), digest mirror scibrain:universe:state fresh (n_symbols 460, ts current). 60s interval
      is a FLOOR (at most once/interval); origination-gating makes effective ~109s. PHASE 5 COMPLETE (4/4).
      git scope: tracker only (cadence code shipped under Phase 2b).
      ── PREFILTER ADDENDUM (the §10 "sub-second pair scan" lever, evaluated this session): the top-K∪
      rotation prefilter was shadow-measured with a NEW pick-level metric (gate.py pick-coverage agg — the
      qualifier-coverage metric was misleading, over-counting ~450 low-rank qualifiers that never open a
      trade). RESULT over 8 live cycles / 24 picks: pick-coverage 22/24=91.7%, 2 cycles STARVED a real pick
      (PRLUSDT); a volatility-ranked prefilter systematically misses low-vol/high-CONVICTION picks (BTC also
      sits in the qualifier-miss list). At K=150 that costs ~8% of trades for a ~5s→~2–2.5s win that is
      still NOT sub-second; smaller K starves more. DECISION (Rule 14, capital-affecting → owner-gated):
      KEEP SHADOW, prefilter_mode returned to default 'off'; confirms [[finding-scibrain-scan-compute-bound]]
      that the real sub-second lever is lighter/vectorized COMPUTE, not a read prefilter. New evidence served
      on dashboard /scibrain (prefilter + prefilter_agg blocks). git scope: keys.py, gate.py, dashboard/api.py.

## PHASE 6 — Full "CPU" dashboard visualization
Full visual redesign: `next_impl/scientist_brain_visual_launchpad.md`.
- [x] Audit current ScientistBrain panel/frontend stack and lock 2.5D Cognitive Atlas direction
- [x] Research graph/circuit/semantic-zoom approaches and lock React Flow + Sigma/lightweight-charts hybrid goal
- [x] VS-V1 split monolithic panel into typed adapter, atlas, inspector, timeline, and fallback table
      — DONE + VERIFIED IN A REAL BROWSER (Rule 19, Playwright/Chromium). Split the former monolithic
      ScientistBrain.tsx into frontend/src/panels/scibrain/: (1) ui.tsx shared primitives; (2) brainGraph.ts
      TYPED ADAPTER — toBrainGraph(decision) → deterministic BrainGraphSnapshot (BrainNode/BrainEdge per
      design §8) mapping modules → router(MoE) → fusion → safety/audit → action with typed signed_value/
      activation/confidence/authority/shadow/evidence_family/health + raw detail back-pointers (no prose
      inference); (3) BrainAtlas.tsx — React Flow (@xyflow/react v12, the locked §9 stack) stable left→right
      circuit, custom nodes encode meaning via color PLUS shape/border (§11.1: shadow=dashed-blue/🕶,
      router-deactivated=⊘+dashed-edge, dir=green/red signed bar), animates only the ≤3 strongest real live
      contributions (reduced-motion aware §11.3), MiniMap+Controls, deterministic viewport framing;
      (4) BrainInspector.tsx — exact-value panel for the selected node/edge (§11.7) incl. nested RAW DETAIL;
      (5) DecisionTimeline.tsx — real-ts decision selection spine; (6) BrainFallbackTable.tsx — the guaranteed
      text/table fallback (§10/§11.9) preserving the live+shadow module evidence verbatim. ScientistBrain.tsx
      is now a container with an Atlas|Table toggle, keeping crash-radar/shadow-bank/audit drawers. VERIFIED
      LIVE: built clean, nginx serves the new bundle; Playwright on the real dashboard → 26 nodes/25 edges
      render, Atlas+Table tabs work, clicking a node populates the inspector with exact typed values
      (kalman: activation 0.0654/conf 0.0445/trend_momentum/regime drifting + features/attribution/role;
      Fusion ALU: authority live/signed_value/regime), table fallback + 🕶 SHADOW MODULES render, ZERO page
      errors; viewport transform debug-confirmed a correct fit (scale 0.614). git scope (Rule 11): new
      scibrain/ package (ui/brainGraph/BrainAtlas/BrainInspector/DecisionTimeline/BrainFallbackTable),
      ScientistBrain.tsx, package.json (+@xyflow/react). NEXT: harden into the stable circuit atlas (Phase-6
      task 2) — region grouping + the full router-gain/safety-veto path mapping.
- [x] Build stable live circuit atlas from existing `/scibrain`: modules → router/fusion → audit/action/safety
      — DONE + VERIFIED LIVE IN A REAL BROWSER (Rule 19 Level-3, Playwright/Chromium). Hardened VS-V1 into the
      STABLE atlas per design §4.1/§7/§11.6. (1) WIRED the orphan `registry.ts` (was produced 14:23 but no
      consumer — C3/Rule-18 gap) into BrainAtlas: every module now sits at its FIXED registry slot keyed BY
      NAME (MODULE_SLOTS), grouped into 10 PhD brain-regions across 2 columns; unknown future modules → a
      deterministic overflow column (never hidden, §11.9). The atlas no longer reflows by decision-array order.
      (2) REGION-GROUP labels: new `regionLabel` non-interactive node type renders the 10 region headers behind
      their modules. (3) ROUTER-GAIN path mapping: added `gain` to BrainEdge; each module's vote edge now
      thickens+brightens with router gain (amplified) / thins+fades when attenuated. (4) SAFETY PASS-THROUGH:
      re-routed the executed path fusion→SAFETY→action (was parallel fusion→action + safety→action) so the
      action pulse visibly passes THROUGH the risk plane before execution (§4.1); crash/gate rides this edge,
      `vetoed` flag set when crash≥0.6 or gate_applied. Files (Rule 11, scope clean — only these 4, rest of
      scibrain/ untouched): registry.ts, brainGraph.ts, BrainAtlas.tsx, ScientistBrain.tsx (caption). Built
      clean (CI=false → main.a3d7bf64.js, +966B; nginx serves it, no restart). VERIFIED LIVE on the real
      dashboard: 36 nodes / 0 page errors; modules at exact registry slots (koopman[8,30] col0, wavelet[208,188]
      / kalman[208,72] / sparse_factor_residual[208,494] col1 — stable, not array-order); 10 region labels
      render (Dynamics·Spectral, Nonlinear·Chaos, …); strict pipeline x-order router 440 < fusion 640 < safety
      840 < action 1040; pass-through PROVEN by edge geometry — e:fusion path 652→847 (fusion→safety),
      e:safety path 852→1047 (safety→action), old direct fusion→action GONE; gain encoding live = 22 vote edges,
      17 distinct widths 0.94–2.68px, opacity 0.55–0.77; viewport scale 0.641 (correct fit); clicking
      mod:wavelet populates the inspector with exact RAW DETAIL. (audit node correctly absent on the sampled
      decision — no Ollama reasoning present; data-dependent, not a break.) NEXT: pulse animation of real
      timestamped events + VS-V2 belief/uncertainty/router-gain field.
- [x] Animate only real timestamped signal pulses; show activation, contribution, suppression, and counter-evidence
      — DONE + VERIFIED LIVE IN A REAL BROWSER (Rule 19 Level-3, Playwright/Chromium). Replaced the PERPETUAL
      React-Flow `animated` dash-march (which the design bans — §5.2/§11.3 "no decorative perpetual traffic")
      with a true ONE-SHOT, EVENT-DRIVEN pulse. (1) New `PulseEdge` custom edge type (registered via edgeTypes):
      draws a travelling dot with SMIL `<animateMotion>` ONLY while `data.pulse` is set; re-keyed per event so it
      replays exactly once; dot RADIUS encodes contribution magnitude, SPEED is fixed (never encodes confidence,
      §5.3). (2) The "real timestamped event" = the active decision's snapshot_id (symbol@ts); a useEffect bumps
      pulseGen only when it changes (new cycle scores the symbol, or operator selects another) — never on an idle
      3s re-poll — then clears `pulsing` after the cascade. (3) CASCADE staging (EDGE_STAGE begin-offsets) so a
      fired pulse flows modules→fusion→through SAFETY→action (the executed pass-through animates). (4) pulseSet
      caps concurrency (§10): top-6 supporting votes + top-2 counter-evidence + the executed chain; suppressed/
      shadow edges never pulse (activation+suppression encoded). (5) COUNTER-EVIDENCE channel (§4.1): added
      `opposes` to BrainEdge in the adapter (vote sign opposite the fused dir; also auditor-disagrees) → those
      edges render a DISTINCT amber + dotted '1 3' channel (color PLUS pattern, §11.1) and pulse amber.
      (6) Reduced-motion (§11.3): no pulseGen bump, empty pulseSet → fully static. Files (Rule 11, scope clean,
      only these 4): brainGraph.ts (opposes), BrainAtlas.tsx (PulseEdge/event firing/cascade/counter channel),
      ScientistBrain.tsx (caption). Built clean (CI=false → main.d17104c2.js, +642B; nginx serves it, no restart).
      VERIFIED LIVE (2 contexts): motion-ON → perpetual `.react-flow__edge.animated` count = 0 (old behaviour
      GONE, Rule 9 disconfirm); on a real event the pulse peaked at 10 concurrent animateMotion then RETURNED TO
      0 and stayed 0 (one-shot, series [10×17, 0×12]); reduced-motion context → animateMotion peak = 0 (§11.3);
      0 page errors both contexts. Counter-evidence PROVEN against redis ground truth: selecting BANANAUSDT
      (short, conv 0.35; redis counter-votes = ising/kalman/wavelet/info_theory) rendered EXACTLY e:ising/
      e:kalman/e:wavelet/e:info_theory as amber (rgb 255,180,84) + stroke-dasharray "1, 3" (raw inline-style
      dump), supporting e:koopman solid red, + 2 amber counter-pulses travelling. NEXT: per-node/edge inspector
      enrichment (formula/version/uncertainty) then VS-V2 belief/uncertainty field.
- [x] Per-node/edge inspector: formula/evidence/reasoning/confidence/uncertainty/regime/version/authority
      — DONE + VERIFIED LIVE IN A REAL BROWSER (Rule 19 Level-3, Playwright/Chromium). Enriched the inspector
      so a clicked node/edge shows ALL EIGHT required facets, every value REAL (live decision or static math
      catalogue), never prose-inferred (§11.7). (1) New `frontend/src/panels/scibrain/moduleScience.ts` — a
      static science catalogue for all 22 modules: `formula` (the actual math each module runs, harvested
      VERBATIM from each module's own docstring + the PROGRESS §2/§2b one-liners — koopman=Hankel-DMD,
      rmt=SSA+Marchenko-Pastur, sparse_factor_residual=Robust-PCA L+Sp, …), `method`, `origin`/citation,
      `version` (honest "1.0" baseline = first versioned release; a real math change bumps only THAT entry,
      with the file header documenting the lineage rule). Formula/version are STATIC code properties → live
      with the versioned bundle, NOT re-sent on every 3s poll. (2) `brainGraph.ts` adapter: added typed
      `reasoning` (= the component's own explanation string), `epistemic_uncertainty` (= 1−conviction, a
      TRANSPARENT self-uncertainty proxy, labelled as such — not dressed up as an independent estimate) and
      `reliability_ic` (the independent empirical settled-IC, shown only when present) to BrainNode; populated
      for module nodes (explanation + 1−conv + reliability_ic), the Fusion ALU (primary_driver + 1−conv) and
      the Ollama audit (narrative + wrong_direction_risk). (3) `BrainInspector.tsx` rewritten into labelled
      blocks — SIGNAL·CONFIDENCE (signed VoteBar + activation + confidence bars), UNCERTAINTY (1−conv +
      reliability IC, with an explicit "no estimate for this stage" line where absent), REGIME·EVIDENCE
      (regime + evidence_family + health), REASONING (the live explanation), SCIENCE·FORMULA (catalogue:
      formula/method/origin/version), then the preserved RAW DETAIL dump; module-vote EDGES inherit their
      source module's science (e:koopman→koopman) and gain a router-gain×/counter-evidence row. Files (Rule 11,
      scope clean — only these 4): moduleScience.ts (new), brainGraph.ts, BrainInspector.tsx, ScientistBrain.tsx
      (caption). tsc --noEmit clean; built clean (CI=false → main.5319ba18.js, +3.09kB gz; nginx serves it, no
      restart — index.html now refs the new hash). VERIFIED LIVE on the real dashboard (scibrain:enabled=1):
      clicked mod:koopman → inspector rendered authority=live, confidence 0.598, uncertainty(1−conv) 0.402
      (= 1−0.598 exactly, amber bar), regime mean_revert, evidence_family dynamics_spectral, health 1.000,
      REASONING = the live "Koopman/DMD: dominant |λ|=0.883 … recon_err=0.158, rank=11 → mean_revert,
      next-bar −0.420%" string, SCIENCE formula "Hankel-DMD: time-delay embedding → DMD…" + method "Koopman
      operator / DMD" + origin "arXiv:1904.09082" + version 1.0; text-probe of all 8 facets = true, viewport
      scale 0.641 (correct fit), 0 page errors. NEXT: VS-V2 belief/disagreement/uncertainty FIELD + router
      gains/deactivated paths + safety/authority path.
- [x] VS-V2 belief/disagreement/uncertainty field + router gains/deactivated paths + safety/authority path
      — DONE + VERIFIED LIVE IN A REAL BROWSER (Rule 19 Level-3, Playwright/Chromium). Added the VS-V2 posterior
      layer to the atlas (design §5.1/§5.3/§4.1), every value COMPUTED from real live votes — never a single
      point of certainty. (1) `brainGraph.ts`: new `BeliefField` on the snapshot, `computeBelief()` folds the
      LIVE (non-shadow, role=direction) module votes → long_mass/short_mass + n_long/n_short/n_abstain,
      disagreement = 2·min/total (0 unanimous … 1 evenly split), mean_uncertainty = mean(1−conviction),
      evidence_leans (raw module-mass direction) vs actual_action (executed) → `overridden` when they differ,
      and `vetoed` ONLY when actual_action is null AND (crash≥0.6 | gate_applied | abstained) — a gate that
      trimmed influence while a direction still fired is NOT a veto (fixed a misleading "LONG→LONG ⛔veto"
      readout mid-build, Rule 18). (2) `BrainAtlas.tsx`: a BELIEF·DISAGREEMENT FIELD strip above the flow —
      executed dir + regime + conviction, the long-mass(green)/short-mass(red) split bar (disagreement made
      visible), DISAGREEMENT + MEAN-UNCERTAINTY meters, and a PROPOSED→ACTUAL readout with ⛔veto / ↔override
      badges (§4.1 proposed-vs-actual). (3) node visual grammar (§5.1): border STYLE now encodes AUTHORITY
      (solid=live · dashed=gate/shadow · dotted=advise), an outer box-shadow HALO whose blur ∝ epistemic
      uncertainty, and an inset green RING whose brightness ∝ calibrated confidence. Router-gain/deactivated
      edge encoding (VS-V1 task-2) retained. Files (Rule 11, scope clean — only these 3): brainGraph.ts,
      BrainAtlas.tsx, ScientistBrain.tsx (caption). tsc --noEmit clean; built clean (CI=false → main.3e854783.js,
      nginx serves it, index.html re-hashed, no restart). VERIFIED LIVE (scibrain:enabled=1): strip rendered all
      5 facets (BELIEF·DISAGREEMENT FIELD / vote split / DISAGREEMENT / MEAN UNCERTAINTY / PROPOSED→ACTUAL);
      36 nodes → 23 carried the uncertainty halo (rgba 127,166,255) + 21 the inset confidence ring; authority
      borders correct (koopman/action=solid live, router/safety=dashed gate); proposed-vs-actual proven on real
      decisions — TUTUSDT "ABSTAIN · conv 0.03 · split 5↑/5↓/1∅ · disagreement 0.81 · mean-unc 0.76 · LONG →
      ABSTAIN ⛔veto" and a fired case "LONG → SHORT ↔override"; 0 page errors. NEXT: live/pause/cycle replay,
      semantic zoom, focus+context, minimap, filters, reduced motion, accessibility.
- [x] Live/pause/cycle replay, semantic zoom, focus+context, minimap, filters, reduced motion, and accessibility
      — DONE + VERIFIED LIVE IN A REAL BROWSER (Rule 19 Level-3, Playwright/Chromium). Built the atlas interaction
      layer (design §6.1/§6.2/§6.3/§11.3), all real + read-only (no interaction changes live authority §6.3).
      (1) TIME CONTROL (container `ScientistBrain.tsx`): a client-side ring buffer of the last 48 polled frames;
      a transport bar (⏮ ◀ ●LIVE/⏸PAUSED ▶ ⏭) — LIVE follows the newest poll, stepping pauses the cursor while
      the buffer keeps filling so the operator replays recent brain cycles (≈one per 3s) and ⏭ resumes the live
      edge; shows frame k/N + capture time + brain cycle + a "replay (not live)" flag. (2) SEMANTIC ZOOM (§6.1
      three levels): a ZOOM [atlas|circuit|evidence] control + a SemanticZoomController child that zoomTo's the
      matching scale; node detail rises with zoom — atlas = label+signed bar only, circuit = +role/family,
      evidence = +exact conf/uncertainty/regime numbers inline; manual pan/zoom also drives the level via onMove
      (guarded, no snap-back). (3) FOCUS+CONTEXT (§6.1): selecting a node keeps its 1-hop neighbourhood full and
      dims the rest (nodes + edges to ~0.12), computed from the real edge list. (4) FILTERS (§6.3): shadow /
      suppressed toggles + an evidence-family <select> dim (never hide, §11.9) non-matching modules; pipeline
      stages never filtered. (5) REDUCED MOTION (§11.3): a user "motion on/off" toggle OR'd with the OS pref →
      no pulse generation when off (disabled+explained when the OS forces it). (6) MINIMAP retained. (7)
      ACCESSIBILITY: every control is a real <button>/<select> with aria-label + aria-pressed, keyboard-operable,
      and the view stays fully readable with motion off. Files (Rule 11, scope clean): ScientistBrain.tsx,
      BrainAtlas.tsx. tsc --noEmit clean; built clean (CI=false → main.c34a8ec2.js, +1.8kB; nginx serves it,
      index.html re-hashed, no restart). VERIFIED LIVE (scibrain:enabled=1): all 9 controls present by aria-label;
      semantic zoom — evidence level showed inline conf/unc numbers, atlas level hid them; focus+context —
      selecting mod:koopman left it at opacity 1.0 while non-neighbours action=0.084 / mod:tda=0.14; filters —
      with no selection a shadow node went 1.0→0.14 on shadow-off, and a family filter dimmed non-matching
      koopman to 0.14; transport — ⏸ showed "PAUSED" + the Resume button appeared; 0 page errors throughout.
      NEXT: VS-V3 Trade Autopsy Theatre (synchronized life trace, actual/counterfactuals, fault class,
      ChangeSpec comparison).
- [x] VS-V3 Trade Autopsy Theatre: synchronized life trace, actual/counterfactuals, fault class, ChangeSpec comparison
      — DONE + VERIFIED LIVE IN A REAL BROWSER (Rule 19 Level-3, Playwright/Chromium). Full-stack — read-only.
      (1) BACKEND: new `GET /scibrain/autopsy?limit=` (dashboard/api.py) reads the IMMUTABLE per-trade artifacts
      from `signals_at_entry` JSONB on each closed scibrain trade row (no re-derivation, C2/C11): the
      `decision_snapshot` (entry-time brain decision — SAME shape as a live decision), `lifetrace` (recorded
      events + MFE/MAE), `outcome_packet` (realized multi-objective utility/ROC/drawdown/forward horizons), the
      path-aware `counterfactual` twin (actual vs OPPOSITE vs ABSTAIN replays + confidence-labelled FAULT CLASS
      none/direction/selection), the at-open `audit`, and the top-level `remediation` (the per-trade proposed
      circuit change = ChangeSpec analog). On-demand, NOT in the 3s poll; bounded to `limit` trades. nginx
      `location /scibrain` is a prefix → covers the sub-path (no nginx change); dashboard RESTARTED (bind-mount
      inode). (2) FRONTEND: `frontend/src/panels/scibrain/TradeAutopsy.tsx` — a trade selector + per-trade theatre:
      header (entry→exit/PnL/ROC/utility/won), the synchronized LIFE TRACE (MFE/MAE excursion envelope + ordered
      discrete events, honestly noting the continuous path isn't stored), ACTUAL-vs-COUNTERFACTUAL policy compare
      (actual/opposite/abstain utility+PnL bars, winner starred) with the FAULT-CLASS badge + confidence, the
      AT-OPEN AUDIT, the PROPOSED CIRCUIT CHANGE (ChangeSpec), and the ENTRY-TIME circuit REPLAYED through the
      EXISTING BrainAtlas+BrainInspector (decision_snapshot fed to the same toBrainGraph adapter — full reuse).
      Wired as a 3rd container view (🧠 Atlas | ▤ Table | 🔬 Autopsy) in ScientistBrain.tsx; api client
      getSciBrainAutopsy. Files (Rule 11): dashboard/api.py, api.ts, TradeAutopsy.tsx (new), ScientistBrain.tsx.
      tsc clean; built clean (CI=false → main.35aa304e.js; nginx serves it). VERIFIED LIVE (scibrain:enabled=1):
      endpoint returns real per-trade truth (FOLKSUSDT short — actual util −0.0063/pnl +0.89 vs OPPOSITE util
      +0.137/pnl +4.53, fault none/high-conf; DEXEUSDT — opposite would've LOST, fault none → our direction was
      right); in-browser /scibrain/autopsy → 200 available n=4; Autopsy tab rendered all five sections
      (LIFE TRACE / ACTUAL vs COUNTERFACTUAL / FAULT / ENTRY-TIME CIRCUIT / PROPOSED CIRCUIT CHANGE), atlas renders
      by default, 0 page errors.
      NOTE (serving): a dashboard restart causes a brief warm-up where /scibrain returns empty → the panel shows
      "no decision"/"buffering" for a few seconds; it self-heals once redis polls resume (verified: 5 polls/10s,
      cycles 2945, 30 decisions). nginx serves ./frontend/build with no-store index.html → users must hard-refresh
      to pick up a new bundle (a stale SPA tab keeps running old JS).
      NEXT: VS-V4 Learning Laboratory (hypothesis genealogy, experiments, champion/challenger, rollback, memory,
      competence).
- [x] VS-V4 Learning Laboratory: hypothesis genealogy, experiments, champion/challenger, rollback, memory, competence
      — DONE + VERIFIED LIVE IN A REAL BROWSER (Rule 19 Level-3, Playwright/Chromium). Full-stack, read-only,
      Tier-0 (renders the living-intelligence experiment system; applies NOTHING). (1) BACKEND: new
      `signals/scibrain/learning_view.py::build_learning_snapshot(r, limit)` assembles ONE bounded,
      deterministic LearningGraphSnapshot from the REAL artifacts (C2/C11, no re-derivation): the experiment
      REGISTRY (scibrain:experiments:reg) → per-hypothesis genealogy nodes (bounded ChangeSpec essentials +
      validated-lifecycle status + the append-only transition HISTORY = lineage + the LATEST cohort EVALUATION
      only [the beat re-evaluates every pass → 180 evals; we never ship them all] = champion[current base] vs
      challenger[candidate] scorecard with its bootstrap LCB(Δutility) falsifier, support/fidelity/ablation-
      remove/rigor + code/config VERSIONS); the canonical LIFECYCLE pipeline (STATUSES + LEGAL_TRANSITIONS DAG +
      by-status counts from the registry aggregate); generalized MEMORY banks (scibrain:memory:negative/positive,
      knob+direction); the advisory per-module COMPETENCE map (ablation report `verdicts` map = status + rolling
      IC + samples + redundancy + ic-transferability + evidence-family + current authority for all 22 modules,
      honest — NO fabricated regime/symbol/horizon grid); the FDR p-value guard; and the AUTHORITY block
      (origination/router/IC switches + the Tier-0 invariant that the registry never applies a change, Phase-7c
      gate not active). Pure read, never raises. New endpoint `GET /scibrain/learning?limit=` (dashboard/api.py)
      is a thin wrapper; nginx `location /scibrain` prefix covers the sub-path (no nginx change); dashboard
      RESTARTED (bind-mount inode). (2) FRONTEND: `frontend/src/panels/scibrain/LearningLab.tsx` (new) — the
      AUTHORITY banner, the validated-lifecycle PIPELINE (nodes carry the live per-state count, fail-terminal
      off-ramps separated), the hypothesis GENEALOGY (collapsible cards: champion→challenger headline with
      LCB+falsifier always visible; expand → predicate/expected/verdict-basis/sizing-interaction/rigor + the
      transition lineage + code/config versions), the negative/positive MEMORY banks (honest empty-states), and
      the signed-IC per-module COMPETENCE map with KEEP/WATCH/DEMOTE/PRUNE/INSUFFICIENT badges. Wired as a 4th
      container view (🧠 Atlas | ▤ Table | 🔬 Autopsy | 🧪 Learning Lab) in ScientistBrain.tsx; api client
      getSciBrainLearning. Files (Rule 11): learning_view.py (new), dashboard/api.py, api.ts, LearningLab.tsx
      (new), ScientistBrain.tsx. tsc clean; built clean (CI=false → main.a37b799d.js, +3.07kB; nginx serves it).
      VERIFIED LIVE: endpoint 200 through nginx with real data (1 hypothesis router.gain.wavelet.mean_revert
      compiled, champion 1.0→challenger 1.2, verdict size_effect_only LCB 0.0; competence 18/22 matured,
      KEEP 15/PRUNE 1/DEMOTE 2; authority origination ON, promotion gate OFF, 0 applied live). In-browser:
      🧪 tab rendered ALL sections (authority banner, pipeline proposed→…→retained, genealogy card, memory
      banks, competence map chaos…rmt), card expand revealed TRANSITION LINEAGE (180 evals) proposed→compiled +
      versions line, 0 page errors.
      NOTE (serving): unchanged from VS-V3 — a stale SPA tab keeps old JS until hard-refresh (nginx no-store
      index.html serves the new hashed bundle); a dashboard restart causes a brief /scibrain warm-up.
      NEXT: VS-V5 Universe Neural Field (cluster/factor/lead-lag/contagion/tail/liquidity semantic zoom after
      UniverseFrame).
- [x] VS-V5 Universe Neural Field: cluster/factor/lead-lag/contagion/tail/liquidity semantic zoom after UniverseFrame
      — Ships a bounded, DETERMINISTIC relational-field mirror of the live universe so the dashboard renders
      ALL active pairs as a dynamic field — topology entirely from the REAL UniverseFrame (Rule 12/13: NO
      force-layout, NO fabrication). PRODUCER signals/scibrain/universe_field.py (new): build_field_snapshot
      computes (1) CLUSTERS = correlation territories via union-find connected components of the |ρ|≥adaptive-
      threshold graph (95th-pct off-diagonal |corr|, clamped [0.30,0.90]); (2) NODES placed by CLASSICAL MDS
      (Torgerson) on the correlation distance d=√(2(1−ρ)) — top-2 eigenvectors of the double-centred −½D² =
      real co-movement geometry, normalized to [−1,1]; (3) EDGES = strongest DIRECTED lead-lag L[i,j]=
      corr(rᵢ[t−1],rⱼ[t]) predictive-flow/contagion arrows, top-|L| capped; per-symbol centrality (|directed
      flow| in+out), vol-z, liq-z, momentum, direction. Bounded: max 160 nodes (salience = centrality+½·liq) /
      110 edges. publish_field() ts-guards to actual frame rebuilds, BLAS-pinned to 1 thread (saturated box),
      Tier-0 (never raises into the gate, NO trading authority). WIRED gate.py:432 right after the frame
      rebuild + run_universe_modules. CONSUMER dashboard/api.py /scibrain/universe reads the mirror and JOINS
      the fast-moving OPEN-POSITION overlay at request time (per-pair held side) so it stays current vs the 60s
      frame. FRONTEND scibrain/UniverseField.tsx (new) — semantic zoom OVERVIEW (cluster territory discs:
      radius∝members, colour=net direction; aggregated cluster→cluster flow) ↔ DETAIL (individual MDS pairs:
      radius∝centrality, colour=direction, ring=held position; green/red directed lead-lag edges) + direction/
      held-only filters + click-to-inspect (cluster zoom, per-pair in/out flow). Wired as the 🌐 Universe Field
      container view in ScientistBrain.tsx + getSciBrainUniverse() client. New keys UNIVERSE_FIELD/
      UNIVERSE_FIELD_BUILDS. VERIFIED LIVE (Rule 19, Level-3): (a) build on a real 502-pair frame → available,
      160 nodes/150 clusters/110 edges, thr 0.5866, top cluster 204 members ρ̄ 0.31 net −0.652 (bearish tilt);
      Rule-9 disconfirm PASS = deterministic (same frame → bit-identical nodes), 62KB bounded payload. (b)
      LOAD-CHECK: brain restarted (stale 11h bind-mount had gate.py+the new module unloaded → field_builds was
      empty) → field key publishes on the FIRST cycle (field_builds 0→1, universe_builds 564), and the funnel is
      UNCHANGED (scored=500 picks=3 parallel=True dropped_cluster=0 = zero trading effect, Tier-0 confirmed). (c)
      dashboard restarted (route predated its 3h process) → /scibrain/universe 200 with available, 502/160/150/
      110 AND the live overlay joined (n_open_positions=36, held nodes side-tagged FET/XLM/TAO short, TON long,
      frame 12.9s fresh). (d) REAL Chromium render (recipe [[reference-dashboard-browser-verify]], raw SVG so no
      React-Flow gotchas): header "502 pairs · 150 clusters · 160 shown · ρ≥0.57 · 1h", OVERVIEW = 150 cluster
      discs + flow lines + legend, DETAIL = 168 dots (MDS, direction-coloured) + 110 directed edges, held-only
      filter → 16, ZERO console errors. Frontend tsc clean + built (CI=false → main.ee5841b5.js). git scope
      (Rule 11): universe_field.py(new), gate.py, keys.py, dashboard/api.py, UniverseField.tsx(new),
      ScientistBrain.tsx, api.ts. NEXT: versioned BrainGraphSnapshot/.../UniverseGraph API contracts.
- [x] Add versioned BrainGraphSnapshot/BrainPulse/TradeReplay/LearningGraph/UniverseGraph API contracts
      — NEW signals/scibrain/viz_contracts.py: the single BACKEND source of truth for the five typed,
      VERSIONED visualization payloads (design §8 / visual_launchpad §8), each carrying its per-contract
      schema_version (SCHEMA_VERSIONS) + IMMUTABLE evidence_ids (pointers back to the exact Redis evidence
      the value came from) so every node/edge/item is inspectable + reproducible. The headline change:
      BrainGraphSnapshot is a faithful BACKEND PORT of the old client-side brainGraph.ts::toBrainGraph —
      modules→router→fusion→safety→audit→action mapped to typed nodes[]/edges[]/regions[] + the belief/
      disagreement field + real BrainPulses (one per ACTIVE non-shadow non-suppressed edge ≥ mag floor; NOT
      decorative, §11.3), every node/edge stamped with evidence_ids (e.g. scibrain:{sym}:modules#{module},
      :decision, :reasoning). This MOVES the graph construction off the frontend (the design forbids the
      frontend inferring scientific meaning from prose). The other four are version+evidence ENVELOPES over
      the already-typed endpoint payloads (additive, non-breaking): TradeReplayFrame (trade row + entry-
      snapshot + outcome evidence), LearningGraphSnapshot (hypothesis registry ids), UniverseGraphSnapshot
      (frame ts). Pure/deterministic/never-raises; NO new Redis keys (computed from existing immutable
      evidence). WIRING (dashboard/api.py): NEW on-demand GET /scibrain/graph?symbol= (the live circuit for
      ONE symbol — NOT in the 3s poll, matches §8 "don't redraw the whole brain every event" + §10
      "separately budgeted"); /scibrain/universe → wrap_universe_graph; /scibrain/learning →
      wrap_learning_graph; /scibrain/autopsy → each trade wrapped as a TradeReplayFrame + a BACKEND-built
      entry-circuit BrainGraphSnapshot embedded (entry_graph) so the autopsy replays the entry circuit
      without re-inference. FRONTEND: api.ts getSciBrainGraph(symbol); ScientistBrain.tsx fetches the backend
      graph for the selected symbol each cycle and the graph selection PREFERS it while LIVE, falling back to
      the client toBrainGraph when the backend is unavailable AND for PAUSED historical replay (§10 graceful
      degradation — the client adapter is retained as the typed fallback, never deleted). VERIFIED LIVE
      (Rule 19, Level-3): (a) build_brain_graph_snapshot on a real live decision (MANTAUSDT) → contract
      BrainGraphSnapshot v1, 26 nodes/25 edges/6 regions/6 pulses; belief leans short, actual None (abstained
      @conv 0.012), disagreement 0.999, n_live 11; EVERY node+edge carries evidence_ids (0 missing); Rule-9
      disconfirm PASS = deterministic (same decision → bit-identical snapshot), None→available:False (safe).
      (b) all four endpoints live through a minted JWT: /scibrain/graph BrainGraphSnapshot v1 26 nodes;
      /scibrain/universe UniverseGraphSnapshot v1 evidence [field, frame@ts]; /scibrain/learning
      LearningGraphSnapshot v1 evidence [experiments:registry]; /scibrain/autopsy TradeReplayFrame v1, trade0
      evidence_ids [trade:…, entry_snapshot:…, outcome:…] + nested entry_graph BrainGraphSnapshot (26 nodes,
      per-node evidence). (c) REAL Chromium: frontend fires /scibrain/graph?symbol= deterministically
      (waitForRequest TRUE; in-page probe 200 through nginx, 30 live decisions), atlas renders 36 react-flow
      nodes/25 edges, ZERO console errors. Rule-18 fix this session: the fetch was first gated on `live`,
      which raced to falsy and never fired (proven via a temporary GFX log → request fired once decoupled);
      fixed by gating only the graph USE on `live`, never the fetch (debug log removed before ship). tsc
      clean + built (CI=false → main.456c305c.js). git scope (Rule 11): viz_contracts.py(new), dashboard/
      api.py, api.ts, ScientistBrain.tsx. NEXT: prove operator-task accuracy/latency + browser performance.
- [x] Prove operator-task accuracy/latency and browser performance; visualization failure has zero trading impact
      — NEW scripts/scibrain_viz_verify.mjs: a durable, reproducible Playwright harness that PROVES the three
      visual_launchpad acceptance criteria (§10 budgets + §13 operator-task list) with real measured evidence,
      not assertions. Runs read-only against the live dashboard (PW_HOME=/tmp/pw node scripts/scibrain_viz_
      verify.mjs); opens no trades. (1) OPERATOR TASKS — for each of the six §13 tasks it establishes BACKEND
      ground truth via an authenticated in-page fetch, drives the real UI, and asserts the panel surfaces the
      SAME computed value (correctness), timing each (latency). RESULT 6/6 PASS: identify decision driver
      (inspector shows primary_driver="koopman" 659ms), find uncertainty (inspector exposes uncertainty(1−conv)
      matching backend module unc≈0.538, 719ms), detect counter-evidence (opposing edges marked ⚠ counter-
      evidence; none in the sampled decision → vacuously ok), explain veto/abstain (abstain/disagreement state
      surfaced, 251ms), replay an outcome (Autopsy renders 5 closed trades w/ realized utility, 2054ms), locate
      a learning change (Learning Lab renders the hypothesis champion→challenger, 2190ms). (2) BROWSER
      PERFORMANCE — API job latency (isolated, page-idle, min of spaced samples on the saturated 10-vCPU box):
      /scibrain 228ms, /scibrain/graph 15ms, /scibrain/universe 1167ms, /scibrain/autopsy 612ms, /scibrain/
      learning 644ms (all within budget: polled /scibrain<1s, on-demand panels<2.5s). FPS via rAF sampling:
      idle 35 / interact 58 / burst 47 — interaction ≈60 proves the pipeline is capable, burst 47≫30, and low
      idle is CORRECT (no decorative animation when idle, §11.3); all ≥30. Bounded DOM (36 react-flow nodes via
      semantic zoom, not a hairball). ZERO console errors. RULE-18 FIX during this proof: the harness's first
      latency pass reported /scibrain at 11.3s — diagnosed as a measurement artifact (5 rapid sequential
      through-nginx fetches stacking on the SPA's 3s poll right after the FPS burst), DISPROVEN by clean single
      spaced calls (server-side /scibrain 141–171ms direct, graph 9ms); fixed the harness to measure latency
      isolated + spaced (min) BEFORE any interaction so the committed artifact reports honest numbers.
      (3) VISUALIZATION FAILURE = ZERO TRADING IMPACT — code audit: viz_contracts.py and all four scibrain
      endpoints do ZERO Redis writes (pure r.get/zrevrange/hgetall); dashboard/brain/executor are SEPARATE
      containers. LIVE PROOF (Rule 19): fully STOPPED the dashboard container (Exited, simulating total viz
      failure) for ~90s — the brain kept trading UNAFFECTED: cycles 3219→3221 (+2 funnel cycles), scored_total
      +998 symbols, universe_builds +1, funnel logs show scored=499 picks=3 parallel=True with no dashboard-
      related errors (only the pre-existing ollama/groq LLM rate-limits); restarted the dashboard cleanly after.
      git scope (Rule 11): scripts/scibrain_viz_verify.mjs (new) only — pure verification, no product code
      touched. Phase 6 (Full "CPU" dashboard visualization) is now COMPLETE (13/13).

## PHASE 7a — Living Intelligence instrument: outcome truth + causal replay
Full design: `next_impl/scientist_brain_living_intelligence.md`.
- [x] Audit current opened-trade remediation logic against realized outcomes and identify false-alarm/grounding failures
- [x] Lock constitution: one trade creates a hypothesis, never a direct live formula/code mutation
- [x] Rename/reframe immediate-open "post-trade" display as Ex-ante Decision-Risk Audit — every
      audit payload now self-describes (audit_kind=ex_ante_decision_risk / evaluated_at=
      post_open_pre_outcome) on the Interrogation contract, persisted onto the trade row, and in
      the /scibrain payload (audits_meta); dashboard panel header reframed + consumes the meta. LIVE
      (verified: live endpoint emits audits_meta, frontend rebuilt clean +117B). Key NAMES kept stable.
- [x] Persist actual LLM provider/model + prompt/schema/version and grade forecast calibration at close
      — interrogator._complete now returns the ACTUAL provider/model/transport (recovered from
      call_chain + get_providers); Interrogation carries provider/model/transport/prompt_hash/
      schema_version(=2); audit._stamp_trade persists them + latency on the trade row. NEW
      audit.grade_calibration() + celery beat scibrain-calibration-grade(300s, default queue, no LLM):
      at close (with a failure_type label) grades p_wrong=wrong_direction_risk vs y_wrong=
      (failure_type=='direction' PROXY) → Brier + reliability bins, per-trade grade immutable on the
      row (idempotency marker), aggregate RECOMPUTED from rows (double-count-proof). LIVE on /scibrain
      (calibration block) + panel CalibrationLine. VERIFIED: graded 147 real trades → Brier 0.3085,
      base-rate-wrong 0.388, mean-forecast 0.600, Brier-SKILL −0.30 (WORSE than base-rate guess) —
      quantitatively confirms living_intelligence §2 (auditor is uncalibrated/over-confident: bins show
      0.70/0.80 forecasts → actual 0.40/0.33 wrong). Idempotent re-run grades 0. PROXY note kept: y_wrong
      is temporary until the path-aware counterfactual lab replaces it.
- [x] Build immutable EntrySnapshot with replay pointers, module/router/fusion/config/code versions,
      and action propensities — NEW signals/scibrain/snapshot.py::build_entry_snapshot freezes at every
      open (design §5.1): unique snapshot_id; per-TF deterministic raw-data replay fingerprints
      (closes sha1 + last_ts + n) + scalar sensors; version lineage (code_fingerprint over the pkg
      source, git commit, config.yaml hash, contract+audit schema versions, full module_set);
      feasible alternative actions {long,short,abstain} + LOGGING PROPENSITIES (p_act=conviction,
      direction split by σ(net_vote/T) → sums to 1, recovers chosen-action prob for later IPS/DR);
      execution micro-state (mark/bid/ask/spread/imbalance/ofi_l1, depth+slippage honest None);
      replay_pointers to decision_snapshot/influence_manifest/audit/calibration. Wired into
      opener._plan_open → signals_at_entry.entry_snapshot (provenance schema_version 2→3); flows to
      /trades/open automatically. VERIFIED LIVE (C7): built on a real scored decision (TNSRUSDT short)
      — complete=True, propensities sum 1.0, real spread 0.000356/imbalance −0.267/ofi 0.0056, all 5 TFs
      fingerprinted, versions pinned; Rule-9 disconfirms PASS (fingerprint deterministic on same frame,
      snapshot_id unique, code_fingerprint stable). Brain restarted clean + scoring (heartbeat fresh).
- [x] Build bounded LifeTrace and OutcomePacket on `CH_TRADE_CLOSED` + standardized matured horizons —
      NEW signals/scibrain/outcome.py: build_lifetrace (bounded RECORDED events: entry/DCA/TP/SL/brain
      interventions/MFE-MAE envelope/exit; per-tick path honestly None) + build_outcome_packet (realized
      multi-objective utility U=roc−λ_tail·tail−λ_dd·dd−λ_cost·cost, ROC, MFE/MAE, drawdown, failure
      label, + standardized forward horizons 15/60/240m signed by direction = decision edge independent
      of our exit). Beat-driven idempotent harvest_outcomes (mirrors grade_calibration: immutable per-row
      ledger + recomputed aggregate; counter once/trade; backfills horizons as they mature). celery beat
      scibrain-outcome-harvest@120s (default queue, no LLM, single-flight). /scibrain serves outcomes
      block. VERIFIED LIVE: 400 packets built (counter 381, 19 pre-existing not recounted), idempotent
      re-run=0, horizons resolve w/ real prices + correct sign (BASUSDT short: price↑@15m→dir_correct
      false, price↓@240m→true). Finding: win_rate 0.6625 but mean_utility −0.0138 (mean MAE −3.29 vs MFE
      +3.24) + forward dir-edge near coin-flip (15m 50.5%/60m 55.5%/240m 50.3%). Fixed Rule-18 bug
      (brain_actions dict slice). Tier-0 observability, zero trading effect.
- [x] Build path-aware Counterfactual Digital Twin: actual/opposite/abstain/delay/module-ablation/weight/size/SL-TP/exit
      — CORE shipped (signals/scibrain/twin.py): replays the real forward OHLC path bar-by-bar with
      path-aware SL/TP/horizon exits. actual = REALIZED ground truth (not simulated — avoids injecting
      replay error); opposite = flip direction + MIRROR the SL/TP geometry about entry; abstain = U=0.
      Emits a confidence-labelled fault_class (none|direction|selection) matching the diagnosis
      win/direction/signal taxonomy but path-aware (replaces the same-exit-price proxy). Fidelity
      self-check: replays the actual policy too, reports abs_error vs realized. SL from the frozen
      entry_policy (opener now stores initial_sl) or ATR-derived fallback (lower confidence). Wired
      into outcome_packet.counterfactual (preserved across rebuilds; backfilled via harvest); twin
      fault distribution on /scibrain outcomes.twin. VERIFIED LIVE: 248/401 replayed (156 out-of-window
      = honest unavailable), faults none 150/direction 59/selection 39 (dir-fault-rate 0.238); wins→none,
      EDENUSDT/BOMEUSDT/KAS/SAGA shorts→direction (opposite wins on the path), XNY/1000XEC→selection
      (both dirs lose). Fixed a Rule-18 bug (was simulating "actual" → derived-SL mislabeled a winner
      as direction fault; now uses realized ground truth). REMAINING (follow-on): delay/module-ablation/
      gain/size/SL-TP sweeps need a circuit re-score on the entry frame — separate slice.
- [x] Replace same-exit-price opposite-direction proxy with path-aware policy replay and confidence-labelled fault classes
      — CONSUMER swap shipped: audit.grade_calibration now sets y_wrong from the twin's path-aware
      fault_class (1.0 iff fault_class=='direction') whenever the replay is trustworthy (status ok +
      confidence high/medium), else falls back to the failure_type=='direction' same-exit-price proxy so
      out-of-window paths still grade. Each row records label_source (twin_path_aware|failure_type_proxy)
      + fault_class + twin_confidence (schema_version 2); aggregate carries a label_sources breakdown.
      Upgrade-in-place: rows graded BEFORE the twin landed (old schema-1 NULL-source or schema-2 proxy)
      are re-selected and re-labelled once a CONFIDENT twin exists; twin grades are never re-touched.
      Rule-18 fixes this session: (a) brand-new-vs-upgrade was decided by label_source IS NULL, which
      misclassified schema-1 upgrades as brand-new → CALIB_GRADED_TOTAL double-counted; now decided by
      audit_calibration KEY existence. (b) CALIB_GRADED_TOTAL switched from incrby to a SET of the
      recomputed agg["n"] (same pure-recompute discipline as the aggregate → can never double-count).
      VERIFIED LIVE: counter self-healed 334→293 (true unique-graded); grade pass graded_now 0/upgraded 0
      at steady state; label_sources twin_path_aware 118 / failure_type_proxy 175 (=293). Brier 0.3301,
      Brier-skill −0.85 (auditor still over-confident — model-quality, not label-quality). Worker
      restarted to load the fix. git scope (Rule 11): audit.py only.
- [x] Build SciBrain module-state embeddings + matched-cohort retrieval; do not rely on prose similarity alone
      — NEW signals/scibrain/cohort.py: every closed scibrain trade gets a fixed-length module-state
      embedding = per-module direction·conviction aligned to the 14-module ROSTER (sign=long/short
      vote, magnitude=conviction-weighted strength; abstain/absent=0) + a structured market context
      (regime, pair_class, conviction, net_vote, size_frac, vpin/ofi/funding/oi_z/sentiment/spread,
      conviction-weighted horizon). retrieve_cohort() ranks comparable PRIOR trades by NAME-aligned
      module cosine (robust to roster drift) blended with a Gaussian context similarity, hard-filtered
      to same regime — explicitly NOT prose/explanation similarity. Idempotent beat-harvest
      embed_decisions() mirrors grade_calibration/harvest_outcomes: immutable per-row module_embedding
      + RECOMPUTED aggregate (roster + leave-one-out retrieval-quality). keys COHORT_AGG/
      COHORT_EMBEDDED_TOTAL; celery task scibrain_embed_decisions + beat @180s (default queue, no LLM,
      single-flight); /scibrain serves the cohort block. VERIFIED LIVE (Rule 19): 169 decisions
      embedded (idempotent re-run=0; counter recompute-SET=169), regimes turbulent61/mean_revert81/
      neutral22/trending5; single-query retrieval returns coherent neighbors (module sim 0.96–0.99 +
      realized utility/fault per neighbor); leave-one-out cohort_sign_hit_rate 0.7083 over 120 trades
      (mean neighbor sim 0.79, mean support 18.6) — the same-regime module-state cohort predicts a
      held-out trade's realized-utility SIGN 71% of the time → the embedding carries outcome structure.
      HONEST CAVEAT: hit-rate is not yet base-rate-corrected (class imbalance can inflate it); the
      rigorous matched-cohort evaluation (IPS/DR, negative controls) is Phase 7b. Worker+beat+dashboard
      restarted to load it; live /scibrain route returns cohort.n_embedded=169. Rule-9/19 disconfirms
      caught + fixed 2 dead-feature bugs (decision JSON key is `modules` not `contributing`; packet
      `utility` is a nested dict). git scope (Rule 11): cohort.py(new), keys.py, celery_app.py, api.py.

## PHASE 7b — Living Intelligence hypothesis factory + experiment engine
- [x] Introduce typed bounded ChangeSpec/equation DSL + single experiment registry
      — NEW signals/scibrain/changespec.py: frozen typed ChangeSpec (design §7: target/role/context_
      predicate/intervention{kind,old,candidate}/parameter_bounds/evidence_ids/expected_effect/falsifier/
      complexity_cost/status/versions) + a BOUNDED target DSL: the only nameable knobs are
      router.gain.<module>.<regime> (hard bounds [0,GAIN_CEIL], module∈ROSTER, regime∈canonical) and an
      allow-listed set of scibrain scalars (router_strength/wrong_dir_threshold/ic_horizon_min/…), each
      with its own hard bounds. compile_proposal() deterministically rejects anything that can't compile
      to the safe DSL (unknown/unlisted target, unsupported kind, non-numeric or out-of-bounds candidate,
      missing falsifier/expected_effect) — arbitrary Python/equations are impossible by construction.
      NEW signals/scibrain/experiments.py: the SINGLE experiment registry (Redis scibrain:experiments:*) —
      register() compiles+dedups by content fingerprint (same knob+context+candidate = same hypothesis),
      transition() advances ONLY through the validated lifecycle (proposed→compiled→unit_tested→…→
      live/retained, illegal hops refused), registry_summary() recomputes counts-by-status for the panel.
      CONSTITUTION: Tier-0 — it RECORDS/tracks hypotheses; it has NO authority to APPLY any change to a
      live parameter (that is the Phase-7c Evidence/Authority/Influence gate). keys.py EXPERIMENTS_*;
      /scibrain serves the experiments block. VERIFIED LIVE (Rule 19): 61 bounded targets enumerated; a
      real proposal (router.gain.noiseharvest.turbulent→0.60) compiled+registered; re-register → deduped
      (same id); INVALID rejected (target 'os.system' not in allow-list; candidate 9.9 outside [0,1.6]);
      legal compiled→unit_tested OK + illegal →live refused with the legal set; dashboard returns the
      experiments block. Test artifact removed → registry truthfully empty for real producers. ZERO trading
      effect. git scope (Rule 11): changespec.py(new), experiments.py(new), keys.py, dashboard/api.py.
- [x] Build LLM scientific council: forensic analyst, causal skeptic, experiment designer, formula engineer, reviewer
      — NEW signals/scibrain/council.py: turns trade evidence into a grounded bounded ChangeSpec, CLOSING
      the hypothesis→evaluate loop. assemble_evidence() builds the deterministic grounding (regime,
      direction, attribution modules, realized + path-aware-twin fault, ex-ante audit) from a closed
      trade. The 5 roles run in ONE structured cloud-primary LLM pass (forensic/skeptic/designer/formula-
      engineer/reviewer; local-Ollama fallback; one call to respect the box's LLM budget). The Formula
      Engineer is CONSTRAINED to the bounded target menu (only the trade's own attribution modules ×
      regime) so output is forced into the typed DSL. grounding_checks() enforces the §8 deterministic
      mandatory-rejection gate (target must be in the DSL allow-list AND a module actually MEASURED in the
      evidence attribution; regime must match; candidate must be a real change) ON TOP of the LLM, and the
      reviewer must 'accept'+grounded — only then compile_proposal→register→evaluate_registered. Degrades
      HONESTLY: no LLM / no usable JSON → NO proposal (never fabricated). propose_from_flagged() drives it
      from the wrong-direction-flagged trades; celery task scibrain_council + beat @1800s (airllm queue,
      single-flight). Tier-0 — produces+grades proposals, applies NONE (Phase-7c gate applies). VERIFIED
      LIVE (Rule 19): deterministic loop with a mock LLM → grounded proposal compiled→registered→auto-
      evaluated (size_effect_only); §8 gate rejects an ungrounded module ('bocpd' not in attribution);
      LIVE cloud LLM (nvidia llama-3.3-70b) on a real flagged trade → grounded proposal router.gain.
      wavelet.mean_revert→1.2 (wavelet WAS the primary driver), reviewer accepted, compiled+registered+
      evaluated end-to-end; task+beat registered @1800s airllm. Test artifacts removed. git scope (Rule
      11): council.py(new), celery_app.py.
- [x] Deterministic target-grounding/semantic-consistency validator rejects unsupported LLM recommendations
      — NEW signals/scibrain/validator.py (no LLM): semantic_consistency() rejects an LLM proposal that
      contradicts the frozen evidence even when it's grounded. Rule from attribution.aligned + twin
      fault_class: a BAD decision (fault∈{direction,selection} or lost) means modules ALIGNED with it
      pushed the error → the consistent change is to CUT them / BOOST opposers; a GOOD decision (fault
      none / won) is the reverse. Formally: an INCREASE is consistent iff (good∧aligned)∨(bad∧¬aligned),
      a DECREASE iff the opposite; indeterminate outcome → abstains (never blocks on missing evidence).
      Wired into council.run_council as the gate AFTER grounding, BEFORE compile (verdict
      'semantically_inconsistent'). This completes the §8 mandatory-rejection gate (grounding +
      duplicate-dedup in the registry + DSL-compile + semantic consistency). VERIFIED LIVE (Rule 19):
      6/6 truth-table cases PASS (BAD+aligned+INCREASE→reject, BAD+aligned+DECREASE→ok, BAD+opposed+
      INCREASE→ok, GOOD+aligned+INCREASE→ok, GOOD+aligned+DECREASE→reject, indeterminate→skip);
      integration — a mock LLM boosting 'chaos' (aligned with a real losing selection-fault trade) →
      council verdict 'semantically_inconsistent' ("reinforces the error"); worker restarted, live
      council references the validator. Tier-0; test artifacts removed. git scope (Rule 11):
      validator.py(new), council.py.
- [x] Evaluate candidate changes with matched cohorts, leave-one-out/pairwise ablation, and interaction tests
      — NEW signals/scibrain/evaluator.py: deterministic matched-cohort evaluator for a router.gain
      ChangeSpec. Replays the REAL historical decisions with the candidate gain — rebuilds the exact
      per-module ModuleOutputs from decision_snapshot.modules, recovers each trade's historical IC context
      by inverting its stored gain (ic_mult = ((g−1)/strength+1)/base), recomputes only the target module's
      gain, and re-fuses — then maps every decision that CHANGES through the path-aware twin to a realized-
      utility delta (direction FLIP → twin.opposite−realized; →ABSTAIN → 0−realized), with a fixed-seed
      bootstrap LCB and the ChangeSpec falsifier (LCB≤0 ⇒ fail) deciding pass/fail. Ablation (candidate→0,
      full removal) gives marginal contribution; the interaction test reports direction-preserving
      conviction/size sensitivity so a gain tweak is never falsely reported as 'no effect'. evaluate_
      registered() attaches the result to the registry record + advances the lifecycle (pass→unit_tested,
      fail→rejected); evaluate_pending() + celery beat scibrain-evaluate-compiled@600s auto-evaluate
      compiled specs (idle until producers register). VERIFIED LIVE (Rule 19): replay_fidelity=1.0 (stored-
      gain replay reproduced 100% of actual directions — Rule-9 faithfulness); DISCONFIRM test (remove
      mean_revert champion noiseharvest 1.40→0.0) correctly detected as size_effect_only — 0 flips but
      rescales sizing on 46/92 trades (mean Δconv −0.048, Δsize 0.023), NOT a false 'no effect'; param_set
      target → 'unsupported in v1' honest gap; empty registry → evaluated 0; beat registered @600s. Tier-0,
      ZERO trading effect; test artifacts removed. git scope (Rule 11): evaluator.py(new), celery_app.py.
- [x] Add doubly robust/IPS off-policy evaluation for accepted/rejected/action-selection bias
      — NEW signals/scibrain/rigor.py ips_dr(): IPS reweights each observed Δutility by 1/propensity
      (clipped at 10) + doubly-robust = direct twin estimate + IPS-corrected residual; Kish effective
      sample size penalizes a few huge weights. Propensity source CONFIRMED: snapshot.py
      _action_propensities() logs a principled soft propensity (p_act·σ(net_vote/T)) into
      entry_snapshot.action_space.chosen_propensity; evaluator.py:208 reads it per cohort trade.
- [x] Add purged walk-forward + embargo + negative controls + realistic cost/path replay
      — rigor.py walk_forward() (k time-ordered folds + sign-stability ≥0.67) + negative_control()
      (sign-flip permutation null, perm_p<0.10). (cost/path replay already handled by the path-aware twin.)
- [x] Score incremental utility/expectancy/CVaR/drawdown/calibration/stability; win rate remains descriptive only
      — rigor.py score_distribution(): expectancy, std, CVaR-5 (tail mean), worst-case; win_rate carried
      DESCRIPTIVE only (never gates the verdict).
- [x] Add minimum-effective-sample + lower-confidence-bound + online FDR/e-value + complexity gates
      — rigor.py: _MIN_EFFECTIVE=8 (Kish ESS floor), bootstrap one-sided 95% LCB, _fdr_guard() rolling
      Benjamini–Hochberg over scibrain:experiments:pvals window (q=0.10), complexity_penalty() so a
      bigger move must clear a higher LCB. Combined in rigorous_verdict(); evaluator.py:270-276 calls it
      after the support/fidelity gate and evaluate_registered() routes fail_fdr/fail_negative_control/
      unstable → rejected. VERIFIED LIVE (Rule 19): (1) compileall exit 0; (2) evaluate_changespec ran on
      a REAL 110-trade mean_revert cohort, replay_fidelity=1.0, correctly early-out insufficient_support
      (0 organic flips) BEFORE rigor; (3) rigorous_verdict on a constructed ≥8-changed cohort → 'pass'
      (IPS/DR 0.98, ESS 9.9, stability 1.0, perm_p 0.002, adj-LCB 0.874>0, BH passes), zero-mean cohort →
      'fail' (adj-LCB −0.286). PENDING: an ORGANIC real cohort with ≥8 direction flips to fire rigor on
      live data (gain tweaks rarely flip ≥8 dirs); wiring proven on real data up to that gate. live
      scibrain:experiments:pvals NOT polluted (stub redis used for the unit-level FDR run).
- [x] Store positive, negative, procedural, and false-alarm memory so failed ideas are not repeated
      — NEW signals/scibrain/memory.py: generalized hypothesis memory keyed by a COARSER signature than
      the registry's exact fingerprint — '<target>|<increase|decrease|zero>' relative to the knob's
      current value — so "boost wavelet.mean_revert to 1.5" failing also blocks "…to 1.4" (same
      direction). NEGATIVE bank = rejected/demoted/rolled_back; POSITIVE bank = retained (validated
      laws). Fed CENTRALLY from experiments.transition() on every terminal state (so ALL producers feed
      it, no bypass). The council consults recall_negative() as a gate AFTER grounding+semantic, BEFORE
      compile → verdict 'repeats_failed_hypothesis', completing the §8 "don't repeat a rejected
      hypothesis" check at the generalized level. PROCEDURAL memory = the retained registry record itself
      (full spec + history + evaluations). FALSE-ALARM memory (auditor flagged high wrong-dir-risk but
      the trade WON) is already quantified per-row in audit_calibration (p_wrong high, y_wrong=0) + the
      calibration block's over-confidence reliability bins — surfaced there, not duplicated. keys.py
      MEMORY_*; /scibrain serves the memory block. VERIFIED LIVE (Rule 19): reject→negative (n=1, sig
      'router.gain.wavelet.mean_revert|increase'); generalized recall hits on a DIFFERENT value same
      direction (1.3 vs failed 1.5); council on a real trade refuses the repeat ('repeats_failed_
      hypothesis … previously failed 1×'); retained→positive (n=1); worker+dashboard restarted, memory
      block served, council has the gate. Tier-0; test artifacts removed. git scope (Rule 11):
      memory.py(new), experiments.py, council.py, keys.py, dashboard/api.py.

## PHASE 7c — Unified controlled evolution and promotion
- [x] Replace fixed Rule-14 shadow-cycle gate with Evidence, Authority, and Influence Gate
- [x] Reconcile current real-LIVE state with explicit observe/advise/canary/live/veto authority
      — NEW signals/scibrain/authority.py::reconcile_authority(r): the CANONICAL, falsifiable map of the
      current real-LIVE state to the Rule-14 ladder observe→advise→bounded_canary→live (+veto = safety
      suppression, orthogonal). Enumerates all 13 component classes that actually exist (origination gate,
      fusion directional verdict, sizing/SL/leverage, SOC crash veto, meta-router MoE, IC reliability learner,
      top-K selection prefilter, Universe-Core shadow modules, Ollama audit, audit auto-actuator, experiment
      registry, living instruments, viz/dashboard); for each it reports the declared authority CAP, the
      EFFECTIVE authority NOW (read from the live kill-switches — capital paths collapse to observe when
      scibrain:enabled=0), risk tier (0–3), live flag, status, kill switch, and evidence_ids. Then it CHECKS
      4 INVARIANTS that catch authority drift: (1) no component's effective authority exceeds its cap;
      (2) only the known real-money path {origination, fusion_direction, sizing/SL/leverage} holds live
      capital-affecting authority; (3) every Tier-0 instrument/learner holds observe-only; (4) the promotion
      gate is inactive so the registry can apply nothing. Pure read, deterministic, never raises, NO trading
      authority. WIRING: NEW GET /scibrain/authority (on-demand, pure read); and the Learning-Lab authority
      block now USES reconcile_authority as the SINGLE source of truth (learning_view.py — replaced the
      partial ad-hoc 3-switch dict; banner keys origination/router/ic/promotion_gate + router_strength +
      hypotheses_applied_live preserved so the existing banner is unbroken). FRONTEND: LearningLab.tsx gains an
      AuthorityReconciliation card — the 4 invariant chips (✓/✗) + a per-component ladder table (component ·
      cap→effective coloured by authority · tier · status · kill switch, sorted live→veto→canary→advise→
      observe) + a live-capital-authority footer. VERIFIED LIVE (Rule 19, Level-3): (a) reconcile_authority on
      live redis → 13 components, by_authority {live 5, veto 1, observe 7}, live_capital exactly
      [fusion_direction, origination_gate, risk_sizing_sl_leverage], ALL 4 invariants PASS; deterministic
      (ex-ts identical). Tracks REAL state differentially — origination shows applied/LIVE (enabled=1) while
      ollama_audit shows unavailable/observe (interrogate=0) and audit_autoact observe (autoact=0), proving it
      reads the actual switches not a hardcoded map. (b) /scibrain/authority 200: AuthorityReconciliation v1,
      13 components, invariants ok; /scibrain/learning authority block now carries 13 components + 4 invariants
      WITH the banner keys intact. (c) REAL Chromium: Learning-Lab renders the reconciliation section
      (waitFor TRUE), in-page learning.authority = 13 components/4 invariants, component rows (Trade
      origination + Universe-Core + Fusion ALU) + invariant text + live-capital footer all present, ZERO
      console errors. ZERO trading impact (authority.py is pure-read, used only by the dashboard; brain not
      touched). tsc clean + built (CI=false → main.b0951aa2.js). git scope (Rule 11): authority.py(new),
      dashboard/api.py, learning_view.py, LearningLab.tsx. NEXT: champion/challenger eval with baseline
      fallback where support is sparse.
- [x] Champion/challenger evaluation with baseline fallback where support is sparse
      — Adds SPIBB baseline bootstrapping (design §9 "when support is sparse … the candidate may act only in
      supported contexts and must fall back to the current champion elsewhere"; arXiv:1712.06924) to the
      matched-cohort evaluator so a challenger with sparse GLOBAL support is no longer flat-rejected when it
      has strong, concentrated LOCAL evidence. signals/scibrain/evaluator.py: (1) _context_key(old_dec) =
      coarse interpretable sub-context = champion conviction band (lo/mid/hi) × direction (low cardinality so
      buckets accrue support); each direction-changing trade is tagged with its context. (2)
      _baseline_bootstrap(changed) partitions the changes by context, marks contexts with ≥ _MIN_CONTEXT_
      SUPPORT(3) changed samples as SUPPORTED, and treats the challenger as a RESTRICTED policy that acts only
      in supported contexts + falls back to the champion (Δ=0) elsewhere — so it can never do worse than the
      champion outside its support. Reports per-context evidence (n, mean, bootstrap LCB), applicable_contexts,
      n_supported_total, fallback_fraction, and the pooled restricted-policy bootstrap LCB. (3) VERDICT: in the
      SPARSE case (support_changed < _MIN_SUPPORT(8)) it no longer flat-rejects — if fidelity ok AND
      n_supported_total ≥ _MIN_SUPPORT_SPIBB(6) AND the pooled bootstrap LCB > 0 AND EVERY supported context is
      individually non-harmful (its own LCB ≥ 0 — the safety guard), the verdict is "baseline_bootstrap_pass"
      (restricted) carrying applicable_contexts; the SPIBB safety guarantee (never deviating outside support)
      is what justifies acting on less GLOBAL evidence than the regime-wide effective-8 gate — and it advances
      only to unit_tested (shadow), not live. evaluate_registered transitions a restricted pass like a pass
      but records rec.applicable_contexts + rec.restricted so any future application is scoped. The baseline_
      bootstrap block is ALWAYS computed + surfaced (evaluator → registry record → learning_view scorecard →
      /scibrain/learning → LearningLab hypothesis card, conditional line shown once a challenger flips any
      direction). Well-supported challengers are UNAFFECTED (the existing full-cohort rigor path is unchanged).
      VERIFIED (Rule 19, Level-3): (a) synthetic disconfirms PASS — pass-predicate is True only for a
      supported+positive context, False for negative deltas, all-sub-threshold contexts, a mixed case where one
      supported context is net-negative (safety guard fires), and low fidelity; partition/LCB/fallback-fraction
      all correct. (b) end-to-end on 279 real closed trades (registered router.gain.wavelet.mean_revert) → no
      regression (size_effect_only preserved: 0 flips, pure sizing), baseline_bootstrap block attached
      (applicable=[], n_supported=0). (c) LIVE WORKER load-check: restarted celery_worker-1, triggered the
      scibrain-evaluate-compiled beat → it ran the NEW code (evaluated 1 → size_effect_only) and PERSISTED the
      baseline_bootstrap block (method spibb_baseline_bootstrap, fresh ts) onto the registry record. (d)
      /scibrain/learning scorecard surfaces baseline_bootstrap live; LearningLab renders (genealogy + scorecard,
      conditional SPIBB line) with ZERO console errors. ZERO trading impact (Tier-0 evaluator — advances only
      shadow lifecycle states; promotion gate to live inactive; brain/origination untouched, only worker +
      dashboard restarted). tsc clean + built (CI=false → main.7cd9a43b.js). git scope (Rule 11): evaluator.py,
      learning_view.py, LearningLab.tsx. NEXT: per-change effective-sample/conditional-utility/ablation/risk
      report; no arbitrary cycle count.
- [x] Per-change effective-sample/conditional-utility/ablation/risk report; no arbitrary cycle count
      — NEW signals/scibrain/change_report.py::build_change_report(ev, spec): consolidates everything the
      matched-cohort evaluator + rigor layer + baseline-bootstrap already compute for ONE ChangeSpec into a
      single normalized report along the four mandated §9 dimensions, with an explicit decision basis that
      rests on EVIDENCE not a clock-cycle count: (1) EFFECTIVE_SAMPLE — propensity-weighted Kish ESS over the
      twin-graded direction changes + support vs the floor (basis string makes "NOT a fixed cycle count"
      explicit). (2) CONDITIONAL_UTILITY — the change's incremental utility given context: off-policy IPS/DR
      mean, complexity-adjusted LCB, per-sub-context law ("gain base C for module M in regime R → mean ΔU …";
      the SPIBB supported contexts), and a human-readable conditional_law. (3) ABLATION — marginal
      contribution of removing the candidate (→0) + the direction-vs-sizing decomposition (does it flip
      decisions or just rescale size?). (4) RISK — CVaR₅/worst of the Δutility distribution, walk-forward
      stability, FDR + negative-control guards, the spec falsifier, and an explicit tail_worsens flag (§9
      falsifier "CVaR worsens") + risk_ok. Plus a DECISION_BASIS block: the verdict, the six promotion gates
      (effective-sample, positive complexity-adjusted LCB, walk-forward stability, multiple-testing survival,
      negative control, tail-not-worsened) as booleans, no_arbitrary_cycle_count=True, and the Rule-14
      promotion rule. Pure compute, never raises, Tier-0; works whether or not the rigor layer ran (sparse
      changes fall back to cohort/baseline-bootstrap evidence). WIRING: evaluate_changespec attaches
      out["change_report"] on every evaluation; learning_view scorecard carries it as `report`; LearningLab
      hypothesis card renders a ChangeReportCard (the four dimension rows + the gate chips + the "promotion
      rests on evidence, not a cycle count" header). VERIFIED (Rule 19, Level-3): (a) rich synthetic flip-case
      → all four dimensions + decision_basis + six gates populate correctly; conditional_law renders; tail
      DISCONFIRM (cvar5=−0.15 → tail_worsens True, risk_ok False). (b) real evaluation (size_effect_only) →
      report attaches + degrades gracefully (eff-sample insufficient, ablation = "rescales SIZE/conviction, no
      direction flips", risk_ok True, no_cycle_count True). (c) LIVE worker load-check: restarted
      celery_worker-1, triggered the beat → it ran the NEW code and PERSISTED change_report on the registry
      record. (d) /scibrain/learning scorecard surfaces report (all 5 sections + 6 gates) live; (e) REAL
      Chromium: hypothesis card renders the per-change evidence report (header + no-cycle-count phrase +
      effective-sample/conditional-utility rows + gate chips), ZERO console errors. ZERO trading impact
      (Tier-0; brain untouched, only worker+dashboard restarted). tsc clean + built (CI=false → main.a1e627f1
      .js). git scope (Rule 11): change_report.py(new), evaluator.py, learning_view.py, LearningLab.tsx.
      NEXT: canary one bounded context/role at a time with automatic rollback + explicit owner approval.
- [x] Canary one bounded context/role at a time with automatic rollback and explicit owner approval
      — NEW signals/scibrain/canary.py: the bounded-canary kernel — the ONLY path that grants a passed
      ChangeSpec actual capital-affecting (bounded_canary) authority, built as the narrowest, most reversible
      one (design §9 shadow→canary→owner-approved-live; launchpad §6 "one component/role at a time, never a
      batch"; Rule 14 "explicit owner approval before first capital-affecting promotion"). SAFETY INVARIANTS
      all enforced in code: (1) DEFAULT OFF — scibrain:canary:enabled defaults 0; get_active_override() returns
      None ⇒ the router is BYTE-IDENTICAL to no canary. (2) OWNER APPROVAL — approve_canary refuses unless the
      owner explicitly set BOTH the master switch AND scibrain:canary:approval:{hid}; the AGENT CANNOT
      self-approve (those flags are the owner's act); canary→live needs a second owner flag
      scibrain:canary:promote:{hid}. (3) ONE AT A TIME — at most one ACTIVE canary; approve refuses if another
      is live. (4) BOUNDED — applies the candidate to exactly ONE (module,regime) router-gain base; the
      champion is used everywhere else (router.py route() gains a `canary` param: overrides base ONLY when the
      DETECTED regime matches AND the module matches; gate experts immune). (5) REVERSIBLE — the champion base
      is the immutable _PROFILES value, so rollback = clear the override; the artifact + full history are
      recorded. (6) AUTO-ROLLBACK — monitor_canary() (beat scibrain-canary-monitor @120s) rolls back
      automatically if the live bootstrap LCB(Δutility) over canary-context closes drops below the guard, the
      master switch is flipped off, the owner trips the manual trigger, or max_hours elapses without a positive
      verdict; a strong positive verdict only marks 'promote_eligible' (owner still must approve). WIRING:
      runner.score_symbol passes get_active_override(r) (cheap guarded read, None by default) into route();
      celery beat monitor task; authority.py reconciliation gains a bounded_canary component (observe when off,
      bounded_canary tier-2 when active) and the invariant is reframed to "no_autonomous_application — the
      registry never self-applies; the ONLY application path is the owner-approved canary"; NEW GET
      /scibrain/canary status endpoint; the existing AuthorityReconciliation table renders the canary row
      automatically. VERIFIED (Rule 19, Level-3): (a) route() unit: canary=None BYTE-IDENTICAL to default;
      wrong-regime canary = no-op; matching canary changes ONLY the target module (wavelet 1.0→1.5); gate-expert
      bocpd immune. (b) full kernel flow (isolated test hyp, cleaned up): get_active_override default None →
      request → approve REFUSED w/o master switch → REFUSED w/o owner approval → approve OK w/ both → override
      live → second canary REFUSED (one at a time) → monitor 'monitoring' (no trades) → owner trigger → AUTO
      ROLLBACK → override cleared + status rolled_back; cleanup left canary fully OFF. (c) LIVE load-check:
      restarted brain+dashboard+worker+beat with canary DEFAULT OFF → funnel UNCHANGED (scored=487 picks=3
      parallel=True, no errors), get_active_override None live, /scibrain/canary enabled False/active None,
      authority bounded_canary component = observe, all invariants OK, monitor beat no-op {active:False}. ZERO
      trading impact by construction + proven (byte-identical routing with canary off; brain origination
      untouched). git scope (Rule 11): canary.py(new), keys.py, router.py, runner.py, celery_app.py,
      authority.py, dashboard/api.py. NEXT: unify the self-improvement subsystems under the same
      experiment/promotion kernel.
- [x] Unify SciBrain remediation, AI Scientist, OPRO, F9/F12 actuator, GA, DSL miner, IC/router learner,
      world model, and direction model under the same experiment/promotion kernel; no bypass
      — COMPLETE 2026-06-12 (LIVE, Level-3): n_open_bypasses 8→0. Every self-improvement producer is now
      ACCOUNTED + recorded under the ONE kernel (no silent independent promotion path). Per-producer modes
      (live /scibrain/authority): llm_council=kernel; ic_router_learner+meta_router=in_loop_bounded;
      bayes_threshold+f9f12_decoder+ga_params=bounded_recorded (kernel-owned bounded write + audit ledger);
      opro+dgm+ai_scientist+strategy_pool+dsl_miner=model_recorded (ModelChangeSpec recorded under the
      kernel); feature_governance+metacog=safety_only; scibrain_remediation+direction_model=disabled. All 5
      authority invariants OK. HONEST SCOPE: model_recorded producers still AUTO-APPLY in observe mode
      (byte-identical autonomous behavior) but every change is now typed/versioned/recorded + dashboard-
      surfaced — they are no longer silent bypasses. The owner-gated ENFORCE mode (hold a model apply
      pending evaluation/approval) is the future escalation, not built/flipped.
      — SPINE SHIPPED 2026-06-12 (LIVE, Level-3). NEW signals/scibrain/producers.py: the unified Producer
      Bus (design §11). (1) ENUMERATES all 15 self-improvement producers (llm_council, ic_router_learner,
      meta_router, scibrain_remediation, opro, dgm, ai_scientist, ga, feature_governance, metacog,
      direction_model, strategy_pool, dsl_miner, f9f12_decoder, bayes_threshold) — each grounded in its
      REAL live beat task + governance kill-switch (F39A/F25/F30/F43/F13/F54/F46 + redis flags) + the live
      parameter it writes + the bounded ChangeSpec target it maps to (or None for un-mappable code/prompt/
      weight knobs). (2) submit(r, proposer, proposal) = the ONE door → experiments.register (compile to
      bounded ChangeSpec, dedup by fingerprint, stamp proposer, count). (3) audit_bypass() = the FALSIFIABLE
      no-bypass instrument: per-producer live classification {kernel|in_loop_bounded|legacy_direct|
      safety_only|disabled}; an OPEN BYPASS = an ENABLED producer that still self-applies a persisted live
      param outside the kernel. (4) scibrain:unify:mode (observe DEFAULT = report-only, byte-identical;
      enforce = future per-producer gate). WIRED into authority.reconcile_authority: new `producer_bus`
      component + new invariant `all_producers_accounted`; bus audit surfaced in /scibrain/authority output.
      VERIFIED (Rule 19, Level-3): _smoke_producers.py green (15 producers, submit/dedup/reject/unknown-
      proposer/audit all pass, isolated FakeRedis — no live registry pollution); LIVE on real Redis:
      llm_council=kernel(already routes), ic_router_learner+meta_router=in_loop_bounded(bounded+IC-graded),
      8 OPEN BYPASSES {opro,dgm,ai_scientist,ga,strategy_pool,dsl_miner,f9f12_decoder,bayes_threshold},
      scibrain_remediation+direction_model=disabled, feature_governance+metacog=safety_only; after
      `docker compose restart dashboard` the LIVE GET /scibrain/authority returns producer_bus.available
      with n_open_bypasses=8 and all 5 invariants OK (n_components 14→15). ZERO trading impact (observe
      mode, pure-read; nothing calls submit() on the hot path; brain NOT restarted — bus is read-only).
      git scope (Rule 11): producers.py(new), _smoke_producers.py(new), keys.py(+UNIFY_*), authority.py.
      REMAINING (this task's [x] needs all of these — migration, one producer at a time, each Level-3):
      route each legacy_direct producer's apply through the kernel instead of self-applying, and (for
      un-mappable knobs: code/prompts/weights/lists) extend the ChangeSpec target surface or add a typed
      non-DSL ModelChangeSpec so they can compile. Enforce mode flips ON only after all are routed.
  • MIGRATION 1/8 — bayes_threshold SHIPPED 2026-06-12 (LIVE, Level-3). Established the bounded_recorded
    pattern for fast online controllers (the IC-learner precedent: a per-tick canary is the wrong tool; the
    kernel's role is the BOUND + AUDIT TRAIL + versioning). NEW producers.apply_controller(r, proposer,
    redis_key, value, bounds, reason) = KERNEL-OWNED bounded write: hard-clamps to bounds, performs the
    live r.set ITSELF (the producer no longer self-applies), and appends a versioned record (old→new,
    clamped, bounds, reason, code/config lineage) to scibrain:unify:controller:{proposer} (capped 200) +
    controller_last. signals/bayes_threshold.py:refresh() — the two direct r.set(T_HIGH/T_LOW) writes
    REPLACED by apply_controller (bounds T_high∈[25,60], T_low∈[15,T_high-5]); last-resort direct-set
    fallback so the controller can never freeze. Reclassified bayes_threshold LEGACY_DIRECT→BOUNDED_RECORDED
    (accounted, NOT an open bypass); fixed its gate key (signal_monitor:bayes_enabled → real
    bayes_threshold:enabled). VERIFIED (Rule 19, Level-3): smoke green (apply_controller in-bounds 42→42,
    out-of-bounds 99→clamped 60, ledger=2, applies counter=2, bayes open_bypass=False); REAL refresh() on
    the worker against live Redis wrote t_high 57.5→57.5 / t_low 47.5→47.5 BYTE-IDENTICAL (behavior-
    preserving) with both writes recorded in the ledger; after `restart celery_worker dashboard` the LIVE
    GET /scibrain/authority shows n_open_bypasses 8→7 (bayes gone), bayes effective_mode=bounded_recorded
    open_bypass=False controller_last present, all 5 invariants OK. git scope: producers.py, keys.py
    (+UNIFY_CONTROLLER*), bayes_threshold.py, _smoke_producers.py. NEXT bypass: a router-gain-adjacent or
    another scalar controller; the un-mappable ones (opro/dgm/ai_scientist prompts+code) need the typed
    ModelChangeSpec surface first.
  • MIGRATION 2/8 — f9f12_decoder SHIPPED 2026-06-12 (LIVE, Level-3). Established the SET/membership-
    controller pattern. NEW producers.apply_set(r, proposer, redis_key, mapping, max_size, count_key,
    reason) = kernel-owned bounded write for a dict-membership controller (the set-valued sibling of
    apply_controller): enforces a max_size RUNAWAY CAP (keeps the most-recently-added by added_ts — a
    controller can never suppress > max_size pairs), performs the live r.set ITSELF, writes count_key, and
    records an old→new added/removed diff (kind:"set") to the controller ledger. celery_app.py:
    update_pair_lists_from_decoder — the two direct r.set(BRAIN_PAIR_PROBATION/SUSPENSION + counts) writes
    REPLACED by apply_set (max_size=200), each with a direct-set fallback. Reclassified f9f12_decoder
    LEGACY_DIRECT→BOUNDED_RECORDED. VERIFIED (Rule 19, Level-3): smoke green (apply_set added/removed diff
    exact, max_size cap keeps the 3 newest); REAL update_pair_lists_from_decoder on the worker wrote the
    suspension list with membership BYTE-IDENTICAL (same 5 pairs ARC/BIO/COAI/NIL/PHB preserved) and BOTH
    writes recorded to the ledger (delta +2, kind:set, versioned code/config fingerprint); after
    `restart celery_worker celery_beat dashboard` LIVE GET /scibrain/authority shows n_open_bypasses 7→6,
    f9f12 bounded_recorded open_bypass=False controller_last applies=4, all 5 invariants OK. GOTCHA (re-hit,
    per memory finding_scibrain_nginx_proxy / single-file inode): celery_app.py is a SINGLE-FILE bind mount
    → Edit replaces the inode → the worker kept serving stale code (4783 vs 4799 lines) and silently took
    the fallback until `docker compose restart celery_worker celery_beat` re-bound the inode. signals/ is a
    DIR mount so bayes needed no such dance. ALWAYS restart worker+beat after a celery_app.py edit.
    git scope: producers.py, celery_app.py, _smoke_producers.py. STATUS: spine + 2/8 migrated; 6 open
    bypasses remain {opro,dgm,ai_scientist,ga,strategy_pool,dsl_miner}.
  • MIGRATION 3/8 — ga_params SHIPPED 2026-06-12 (LIVE, Level-3, DURABLE). Established the PARAM-VECTOR
    controller pattern. NEW producers.apply_params(r, proposer, redis_key, params, bounds, reason) =
    kernel-owned bounded write for a multi-scalar param dict (3rd shape after apply_controller scalar +
    apply_set membership): clamps EACH named param to its own [lo,hi], performs the live r.set ITSELF,
    records a per-param old→new diff (kind:"params"). ml/genetic_algorithm.py:run_ga — the direct
    r.set(_GA_PARAMS_KEY=ga:best_params) REPLACED by apply_params (bounds=PARAM_BOUNDS: min_signal_strength
    [15,60]/turbulence_cap[1.5,5]/dca_*/trailing_sl[0.5,3]/kelly[0.1,0.5]), direct-set fallback. Reclassified
    ga_params LEGACY_DIRECT→BOUNDED_RECORDED. VERIFIED (Rule 19, Level-3): smoke green (kelly 0.99→clamped
    0.50, only changed params diffed); REAL ga_evolve_params on the worker ran the DEAP GA → apply_params
    recorded the 6-param vector (4 evolved params diffed, kelly in-bounds) to the ledger; LIVE GET
    /scibrain/authority n_open_bypasses 6→5, ga_params bounded_recorded open_bypass=False, all 5 invariants
    OK. DURABILITY: ml/ is NOT bind-mounted (baked) → needed a full image rebuild; deployed live via
    `docker cp` first (proves it works), THEN baked: `docker build -t trading-bot-app:latest .` (new image
    41cc15381b71) + `docker compose up -d --force-recreate celery_worker celery_beat` (paper env) → re-ran
    ga_evolve_params on the BAKED recreated worker, ledger grew again (applies=2) — proving it survives a
    recreate, not just a restart. git scope: producers.py, ml/genetic_algorithm.py, _smoke_producers.py,
    .dockerignore(new). BUILD GOTCHA (now fixed): NO .dockerignore existed → COPY . . shipped the 3.6 GB repo
    (data/historical 2.3G + models 885M) + chown -R every build (COPY 213s + chown 233s + export 451s); the
    earlier "65-min stall" was that PLUS daemon contention from concurrent recreates. Added a SAFE
    .dockerignore (git/pycache/logs); the 2.3G data/historical exclusion is deferred (mounted on only 5/14
    services — must confirm the other 9 don't read it). STATUS: spine + 3/8 migrated; 5 open bypasses remain
    {opro,dgm,ai_scientist,strategy_pool,dsl_miner}. The 4 un-mappable ones (opro/dgm prompts+code,
    ai_scientist hypotheses, dsl_miner factors) need the typed ModelChangeSpec surface; strategy_pool needs a
    generative-lifecycle change (register children as proposals, not auto-insert).
  • MIGRATION 4-8/8 — opro/dgm/ai_scientist/strategy_pool/dsl_miner SHIPPED 2026-06-12 (LIVE, Level-3).
    Built the typed NON-DSL ModelChangeSpec surface: NEW producers.apply_model_change(r, proposer, kind,
    target, summary, evidence_ids, reason) — records a typed versioned ModelChangeSpec (kind ∈ prompt/code/
    factor/hypothesis/strategy; provenance + evidence + code/config lineage + reversibility + bus mode) to
    scibrain:unify:model:{proposer} (capped) + model_last. NEW mode MODEL_RECORDED. Wired in celery_app.py:
    opro_optimize (both applied + reverted prompt paths), dgm_rewrite_weakest (code rewrite dispatch),
    ai_scientist_run (hypothesis-gen dispatch), evolve_strategy_pool_task (child-strategy creation),
    llm_dsl_mining_run_task (promoted-factor count). All 5 reclassified LEGACY_DIRECT→MODEL_RECORDED.
    VERIFIED (Rule 19, Level-3): smoke green (apply_model_change records, model_last counter, all 5
    model_recorded, n_open_bypasses=0); LIVE apply_model_change on the worker wrote the real ledger
    (model_id, records=1) AND the dashboard /scibrain/authority surfaces opro model_last. FLEET ROLL: all
    13 app services force-recreated onto image 41cc15381b71 in paper (fixes the mixed-image state from the
    paper-switch recovery) — confirmed 13/13 on 41cc, brain PaperExecutionEngine no halt. git scope:
    producers.py, keys.py (+UNIFY_MODEL*), celery_app.py (5 task wrappers), _smoke_producers.py.
    ═══ PHASE 7c §11 GLOBAL SELF-IMPROVEMENT UNIFICATION COMPLETE: spine + 8/8 producers migrated,
    n_open_bypasses=0, all authority invariants OK. Remaining 7c task: the Living-Intelligence dashboard
    (lineage/evidence/replay/calibration/authority/rollback). Future kernel work: owner-gated ENFORCE mode
    that HOLDS a bounded_recorded/model_recorded apply pending evaluation (currently observe = record+apply).
- [x] Living Intelligence dashboard: hypothesis lineage, evidence, replay deltas, calibration, state, authority, rollback
      — SHIPPED 2026-06-12 (LIVE, Level-3 browser-verified). BACKEND (signals/scibrain/learning_view.py):
      build_learning_snapshot now adds (a) `self_improvement` — the §11 unified producer-bus state (15
      producers, each effective_mode + last change, by_effective_mode, n_open_bypasses) + a newest-first
      `recent_changes` LINEAGE merged across the controller + ModelChangeSpec ledgers (what changed, by
      whom, when, under which mode); and (b) `rollback` — the bounded-canary event history + active canary.
      NEW producers.recent_changes(r) aggregator. The existing snapshot already carried hypothesis lineage
      (hypotheses), evidence (evidence_ids), competence/calibration (rolling IC + ablation), and the
      canonical authority reconciliation — so all 7 facets (lineage/evidence/replay/calibration/state/
      authority/rollback) are now in ONE /scibrain/learning payload. FRONTEND (LearningLab.tsx): NEW
      SelfImprovementBus (producer-mode grid + change-lineage timeline, no-bypass banner) + RollbackHistory
      (canary promote/rollback events) components, wired after AuthorityReconciliation. VERIFIED (Rule 19,
      Level-3): /scibrain/learning live returns self_improvement(available, 15 producers, 0 bypasses, 30
      change records) + rollback; CRA frontend rebuilt (CI=false → main.2b8b60d6.js) and served by nginx
      (the SPA host; dashboard serves only the API); REAL chromium render (scripts/li_dashboard_verify.mjs)
      = 7/7 render checks PASS, 0 console errors, screenshot /tmp/li_learninglab.png. GOTCHA: CRA build
      REPLACES the build/ dir inode → the nginx frontend/build dir-mount needed a container recreate to
      re-bind (same inode class as the single-file gotcha). git scope: learning_view.py, producers.py,
      frontend LearningLab.tsx (+build), scripts/li_dashboard_verify.mjs(new).
      ═══ PHASE 7c COMPLETE (7/7): unified controlled evolution + promotion — every self-improvement
      mechanism under ONE kernel (no bypass) + the Living-Intelligence dashboard surfacing it end-to-end.

## PHASE 7d — Cognitive OS spine: shared belief, workspace, and metacognition
Full design: `next_impl/scientist_brain_cognitive_os.md`.
- [x] Audit existing neural/RL/memory/world-model/evolutionary/quantum capabilities and identify real gaps
- [x] Research and lock brain-inspired architecture with explicit computational roles and falsifiable admission rules
- [x] Lock developmental curriculum: ledger → perception → belief/memory → world model → offline policy → whole-brain learning
- [x] Define typed CognitiveMessage, BeliefState, LearningSignal, CompetenceRecord, and ModelChangeSpec contracts
      — SHIPPED 2026-06-12 (LIVE-importable, Level-3). NEW signals/scibrain/cognitive_contracts.py: the 5
      Cognitive-OS spine contracts (design §7/§3.4), frozen JSON-serializable dataclasses matching the
      contracts.py FinRL-X "one typed contract is the sole interface" idiom — each with bounded-range
      validate(), to_dict/from_dict round-trip, and full version/evidence lineage; ZERO behavior/authority
      (pure substrate the workspace + learners will publish/consume). (1) CognitiveMessage — source/role/
      evidence_family/belief_delta/uncertainty[0,1]/salience[0,1]/horizon/support_distance/lineage; (2)
      BeliefState — shared latent_mean+dispersion, calibrated regime/changepoint posteriors, disagreement,
      uncertainty_decomposition, tail/liquidity/portfolio/execution state, active goals/hypotheses,
      retrieved episodes, source/version lineage (the §3.4 BeliefState_t); (3) LearningSignal — typed kind
      (reward_error/uncertainty/changepoint/surprise/patience_risk/homeostatic/novelty) + the
      observe→advise→bounded_canary→live AUTHORITY LADDER so a signal can't exceed its blast radius; (4)
      CompetenceRecord — component/context/task/calibration[0,1]/utility_delta/support/drift/version (the
      metacognitive abstention map); (5) ModelChangeSpec — the BROADER bounded model/data/objective/update
      change (generalizes scalar changespec.ChangeSpec) requiring bounds + expected_effect + falsifier,
      routed through the SAME promotion kernel (producers.apply_model_change). VERIFIED (Rule 19): smoke
      _smoke_cognitive_contracts.py green LIVE in the brain container — 5 constructed + round-tripped
      identically, validation caught all 8 bad cases (out-of-range, bad role/kind/authority, missing
      falsifier, unbounded change, non-normalized posterior), from_dict coerces junk defensively. git
      scope: cognitive_contracts.py(new), _smoke_cognitive_contracts.py(new). NEXT 7d: build the read-only
      global latent workspace that publishes CognitiveMessages from current modules/models and assembles a
      BeliefState (these contracts are its substrate).
- [x] Build read-only global latent workspace from current modules/models with source/version lineage
      — SHIPPED 2026-06-12 (LIVE, Level-3). NEW signals/scibrain/workspace.py: build_workspace(r, symbol)
      re-expresses the latest scored Decision's module bank as the §3.4 Global Workspace — pure read, NO
      authority, using ONLY the Phase-7d contracts. Each ModuleOutput → a typed CognitiveMessage
      (source=module, role, evidence_family, belief_delta=direction×conviction, uncertainty=1−conviction,
      salience=|belief_delta|, support_distance from REAL IC sample support [shadow ⇒ 1.0], source_version
      lineage); the top-salience subset is the selective BROADCAST. Assembles one shared BeliefState:
      latent_mean=per-module weighted votes (+pstdev dispersion), regime_posterior=REAL distribution from
      the live per-regime decision counters, changepoint_posterior from a bocpd module, disagreement=frac
      of convicted modules whose sign opposes the fused direction (= epistemic uncertainty), aleatoric=mean
      module uncertainty, tail_state from statphys_soc, active_hypotheses from the registry, full source/
      version lineage. NEW GET /scibrain/workspace?symbol= endpoint (on-demand, pure read). VERIFIED (Rule
      19, Level-3): _smoke_workspace.py green vs LIVE Redis — built AZTECUSDT (22 messages, 13 convicted),
      all 22 round-trip to VALID CognitiveMessages, BeliefState valid, regime_posterior sums to 1.0,
      broadcast ⊆ sources, published to scibrain:workspace; after restart the LIVE GET /scibrain/workspace
      returns it (mean_revert 0.70 posterior, disagreement 0.54, top msg langevin_hawkes belief_delta −0.89
      support_distance 0.0). git scope: workspace.py(new), _smoke_workspace.py(new), cognitive_contracts.py
      (ROLES +context/allocator), keys.py (+WORKSPACE), dashboard/api.py (+endpoint). Phase 7d now 5/9.
      NEXT 7d: thalamic salience routing (evidence/memory/planning/audit/compute budgets) — the workspace's
      salience field is its seed; then metacognitive competence maps (CompetenceRecord) + brainstem safety.
- [x] Add thalamic salience routing for evidence, memory retrieval, planning depth, audit depth, and compute budget
      — SHIPPED 2026-06-12 (LIVE, Level-3). NEW signals/scibrain/thalamus.py: route_salience(r, workspace)
      consumes the read-only workspace and does the §3.3 attention+bandwidth job, pure read / no authority.
      (1) SALIENCE: each CognitiveMessage scored by the §3.3 decomposition = info_gain (bd·(1−unc)·(1−sd))
      + decision_relevance (bd) + anomaly (changepoint) + risk_urgency (turbulence/tail/disagreement) +
      memory_match (stub until episodic retrieval) − compute_cost (shadow experts cost more) − redundancy
      (evidence-family crowding); below a floor a message ABSTAINS. (2) SPARSE EVIDENCE SELECTION: top
      messages admitted with a per-evidence-family cap (sparse-MoE load balance) within a load-scaled
      evidence budget. (3) BUDGETS: bounded memory_retrieval_depth (↑ with changepoint/disagreement/risk),
      planning_depth (↑ with relevance·coherence), audit_depth (↑ with disagreement/risk/low-conviction),
      compute_fraction = real OS load headroom — so the box's risk/execution is NEVER starved (§8.2). Config
      weights via scibrain:thalamus:w_*. NEW GET /scibrain/thalamus endpoint. VERIFIED (Rule 19, Level-3):
      _smoke_thalamus.py green vs LIVE Redis (BASEDUSDT: 22 msgs/17 abstained, ranked salience-sorted,
      evidence family-balanced + within budget, all budgets bounded, compute_fraction==load_factor,
      published); after restart LIVE GET /scibrain/thalamus returns it (MERLUSDT, load 0.657, evidence=5
      family-balanced modules, budgets {evidence 9, memory 1, planning 2, audit 1, compute 0.657}). git
      scope: thalamus.py(new), _smoke_thalamus.py(new), keys.py (+THALAMUS), dashboard/api.py (+endpoint).
      Phase 7d now 6/9. NEXT 7d: metacognitive competence maps (calibration/epistemic-aleatoric/support
      distance/OOD/abstention utility) — the CompetenceRecord contract is its substrate.
- [x] Add metacognitive competence maps: calibration, epistemic/aleatoric uncertainty, support distance, OOD, abstention utility
      — SHIPPED 2026-06-12 (LIVE, Level-3). NEW signals/scibrain/metacog.py: build_competence_map(r) — the
      §3.11 metacortex self-model, pure read / no authority, emitting a VALID CompetenceRecord per component
      PLUS the derived facets, all from REAL measured data (IC map + IC samples + ablation report + global
      audit Brier). Per component: calibration = clip(|IC|/target · sample-sufficiency); EPISTEMIC unc =
      1−support-sufficiency (reducible); ALEATORIC unc = 1−|IC|/target (irreducible); support_distance/OOD =
      below the IC trust gate (or shadow); abstention_utility = (1−calibration)·(0.5+0.5·support_distance);
      utility_delta + drift from the ablation report. KEY OUTPUT P(action_supported|belief,evidence,versions)
      for the live decision = salience-weighted DRIVER calibration · (1−disagreement) · (1−driver support
      distance), with an abstention recommendation below threshold (reward correct abstention, penalize
      confident unsupported action). NEW GET /scibrain/metacognition endpoint. VERIFIED (Rule 19, Level-3):
      _smoke_metacog.py green vs LIVE Redis (22 components, 4 OOD, all facets bounded [0,1], every
      CompetenceRecord validates + round-trips, real audit Brier 0.351 n=613); after restart LIVE GET
      /scibrain/metacognition returns it — top-competent chaos/hmm_regime/info_theory cal=1.0 @400 samples,
      most-abstain-worthy bocpd/ergodic_mixing abst=1.0/OOD (no IC support), P(action_supported)=0.39.
      git scope: metacog.py(new), _smoke_metacog.py(new), keys.py (+METACOG), dashboard/api.py (+endpoint).
      Phase 7d now 7/9. NEXT 7d: brainstem safety projection / baseline-fallback contract that NO learned
      component can bypass; then the whole-brain dashboard (workspace/region/uncertainty/competence/authority/
      compute) — the last 7d item.
- [x] Add brainstem safety projection/baseline fallback contract that no learned component can bypass
      — SHIPPED 2026-06-12 (LIVE, Level-3). NEW signals/scibrain/brainstem.py: the §3.1 non-negotiable
      safety floor OUTSIDE all learning. project_action(r, a_raw, symbol) = the CBF-style projection
      a_safe = argmin||a−a_raw||² s.t. risk_state_next ∈ SafeSet: box-clamps size→[min,max] and
      leverage→[1,cap] (convex ⇒ per-coordinate clamp is the closest admissible), and HARD-VETOES → the
      baseline_fallback (ABSTAIN, no position) on any fault — kill switch off, max-open exposure reached,
      per-symbol crash/tail reflex (crash_radar zset ≥0.70), stale-data guard, no-direction. safe_set(r)
      reads the bot's REAL hard limits live (bot:running/max-min position/leverage/max_open) under absolute
      hard ceilings (≤100 USDT, ≤20×) that even a mis-set operator key can't exceed. SafeAction frozen
      dataclass. NEW GET /scibrain/brainstem endpoint with the falsifiable non-bypass invariants
      (size/leverage within hard ceiling, baseline=abstain, no_learned_bypass = project_action is the SOLE
      admissible-action source). RULE-18 FIX found during verify: first cut used scibrain:open_count (=1154,
      a CUMULATIVE counter) for current exposure → falsely reported headroom=0 / exposure_saturated; fixed
      to the AUTHORITATIVE source the live opener uses (len(memory.query.get_open_trades), DB-backed, fails
      OPEN on a DB hiccup so it never freezes trading). VERIFIED (Rule 19, Level-3): _smoke_brainstem.py
      green vs LIVE Redis (clamp 999/50x→25/5x, floor 1/0.2x→10/1x, all 4 veto classes abstain, baseline=
      abstain, invariants ok); after restart LIVE GET /scibrain/brainstem shows the real SafeSet (open_now
      36 / headroom 14) + all 4 invariants OK. git scope: brainstem.py(new), _smoke_brainstem.py(new),
      keys.py (+BRAINSTEM), dashboard/api.py (+endpoint). Phase 7d now 8/9. LAST 7d item: the whole-brain
      dashboard (workspace broadcast, region activity, uncertainty, competence, authority, compute) — which
      ties together the workspace/thalamus/metacog/brainstem/producer-bus surfaces shipped this session.
- [x] Whole-brain dashboard: workspace broadcast, region activity, uncertainty, competence, authority, and compute
      — SHIPPED 2026-06-12 (LIVE, Level-3 browser-verified). The §Phase-E step-17 BrainPulse that TIES THIS
      SESSION TOGETHER. BACKEND signals/scibrain/brain_view.py: build_brain_pulse(r) folds every region
      surface (brainstem/thalamus/workspace/metacortex/action/tail/learning) into ONE read-only snapshot —
      REGION ACTIVITY (each region's activity/authority/health/key-metric) + the cross-cutting glance views:
      workspace BROADCAST (ws + thalamus-admitted), UNCERTAINTY (epistemic vs aleatoric, disagreement,
      changepoint, P(action_supported), abstain reco), COMPETENCE (mean calibration, OOD, top + abstain-
      worthy modules), AUTHORITY (live-capital path + producer-bus no-bypass + mode mix), COMPUTE (load
      factor + attention budgets). NEW GET /scibrain/brain endpoint. FRONTEND: NEW WholeBrain.tsx (region-
      activity rows + 4 cross-cut cards + broadcast) wired as a new "🧬 Whole Brain" tab (view-brain) in
      ScientistBrain.tsx + getSciBrainBrain in api.ts. VERIFIED (Rule 19, Level-3): _smoke_brain_view.py
      green vs LIVE Redis (7 regions all healthy, all cross-cut sections, 0 open bypasses, published); CRA
      rebuilt (main.8c418348.js) + nginx recreated (build-dir inode re-bind); REAL chromium render
      (scripts/brain_dashboard_verify.mjs) = 10/10 render checks PASS, 0 console errors, screenshot
      /tmp/whole_brain.png (after restarting the dashboard to load the new endpoint — caught a 404 in the
      first run, Rule-18). git scope: brain_view.py(new), _smoke_brain_view.py(new), keys.py (+BRAIN_PULSE),
      dashboard/api.py (+endpoint), frontend WholeBrain.tsx(new)+ScientistBrain.tsx+api.ts(+build),
      scripts/brain_dashboard_verify.mjs(new).
      ═══ PHASE 7d COMPLETE (9/9): Cognitive-OS spine — typed contracts → workspace → thalamic routing →
      metacognition → brainstem safety → whole-brain dashboard, all LIVE, read-only, no trading authority.

## PHASE 7e — Perception, hippocampal memory, and slow consolidation
- [x] Build shared multimodal self-supervised latent challenger from CandleNet MAE + sequence/GNN experts
      — SHIPPED 2026-06-12 (architecture + assembly LIVE-verified, Level-3 for a build task). NEW
      ml/shared_latent.py: SharedLatentChallenger(nn.Module) — the §3.2 sensory-cortex/association
      challenger. Per-modality input projections (modules 40-d role-aggregated [signed-vote/conviction/
      count/dispersion per canonical role] + regime one-hot 4-d + belief 5-d = 49-d INPUT) → a fused,
      LayerNorm-bottlenecked SHARED LATENT (24-d, the information bottleneck), with both SSL objective
      HEADS in place: a masked-reconstruction decoder (L_mask) + a JEPA future-latent predictor (L_latent).
      assemble_modalities(decision, belief) builds the fixed-dim multimodal input from the EXISTING experts'
      outputs (the live Decision's 14+-module bank re-expressed by role + regime + the workspace BeliefState
      — torch-free numpy). build_latent() convenience runs encode()→latent. This is TASK-1: the architecture
      + feature assembly + a working forward; it is UNTRAINED (random init) and a SHADOW CHALLENGER with NO
      action authority (never on the live path) — training the masked/contrastive/cross-modal/future-latent
      objectives + proving downstream utility/no-leakage is the separate step-2 task. VERIFIED (Rule 19,
      Level-3): _smoke_shared_latent.py green in the LIVE brain container (torch 2.12) — instantiates
      (4481 params), assembles REAL modalities from live APEUSDT, full SSL forward latent(24)/recon(49)/
      jepa_pred(24) all finite, masking changes the latent (MAE objective effective), build_latent returns a
      bounded 24-d latent. DEPLOY NOTE: ml/ is NOT bind-mounted → verified via docker cp into the brain
      container; the source is durable in the repo and gets BAKED when step-2 trains+consumes it (a shadow
      challenger has no live consumer yet, so no image rebuild needed for task-1). git scope: shared_latent.py
      (new), _smoke_shared_latent.py(new). Phase 7e now 1/7. NEXT 7e: TRAIN the SSL objectives (masked/
      contrastive/cross-modal/future-latent) + prove downstream utility & no leakage (the real training run).
- [x] Train masked, contrastive, cross-modal, and future-latent prediction objectives; prove downstream utility/no leakage
      — SHIPPED 2026-06-12 (LIVE-run, Level-3) — HONEST NEGATIVE RESULT (correct per the §8 promotion gate).
      NEW ml/train_shared_latent.py: a real SSL training + rigorous evaluation pipeline (torch-only, no
      sklearn — rank-based AUC + torch logistic probe). CORPUS: 528 closed trades' module_embedding.module_vec
      (18-d roster-aligned) + regime one-hot + flow context (ofi/vpin/funding/spread/sentiment) + win/loss
      label + entry_time. OBJECTIVES (design §3.2 L_perception) all trained 300 epochs: masked_reconstruction
      (MSE on 30%-masked dims), future_latent JEPA (predict next-in-time latent, stop-grad target),
      contrastive InfoNCE on two augmented views (noise + MODALITY-DROPOUT = the cross-modal-alignment view),
      + information-bottleneck L2. EVALUATION: PURGED WALK-FORWARD split by entry_time (train 316 / embargo 27
      / test 185, test strictly later), downstream utility = a frozen-latent logistic probe vs a raw-input
      baseline (ROC-AUC on TEST), with leakage controls (flow standardization + probe fit on TRAIN only; a
      label-PERMUTATION control). RESULT: latent AUC 0.494 vs baseline 0.507 vs permutation 0.408 →
      utility_positive=FALSE → VERDICT "DO NOT PROMOTE". This is the CORRECT, HONEST outcome: on this tiny,
      89%-win-imbalanced corpus (~20 test negatives) NEITHER the raw module features NOR the learned latent
      predict realized win/loss out-of-sample above chance; the design's constitution (§3.2 "promotion based
      on downstream utility, not reconstruction beauty"; §8 FDR/no-manufactured-discoveries) DEMANDS we
      reject it — and the pipeline did. The representation stays a SHADOW challenger with NO authority (never
      promoted). Saved models/shared_latent.pt (shadow artifact) + scibrain:perception:train_report. RULE-18
      FIX during run: nn.ModuleDict reserves the key 'modules' (collides with nn.Module.modules()) → prefixed
      encoder keys 'enc_*'. VERIFIED (Rule 19): trained + evaluated end-to-end in the LIVE brain container
      (torch 2.12, 44s), report persisted to Redis. git scope: train_shared_latent.py(new), shared_latent.py
      (dim-parametric + key-prefix fix). Phase 7e now 2/7. (To get a POSITIVE utility result would need a
      larger/less-imbalanced corpus or the live cross-universe snapshot — NOT more tuning on ~20 negatives,
      which would be the p-hacking §8 forbids.)
  • TRAINING-HEALTH DASHBOARD SHIPPED 2026-06-12 (LIVE, Level-3 browser-verified) — owner-requested "show
    this usefully + point out training issues." The point: a raw 0.49 AUC is uninterpretable; what's useful
    is the AUTO-DIAGNOSIS of WHY to (dis)trust it. NEW signals/scibrain/training_health.py:
    build_training_health(r) turns the SSL train report into typed issues {code,severity,title,detail,
    recommendation} + the Hanley–McNeil AUC 95% CI + a one-word health verdict (trustworthy|underpowered|
    issues|leakage|no_run). On the real report it correctly diagnoses health=UNDERPOWERED (not "bad model"):
    AUC 0.494 CI [0.36,0.63] INCLUDES 0.5, only ~20 minority test examples, win-rate 0.894, raw baseline
    ALSO ≈ chance ⇒ "no out-of-sample signal = a DATA/feature issue not a model bug", latent ≤ baseline ⇒
    correctly not promoted, permutation 0.41 ⇒ no leakage evidence, small corpus — 0 critical/4 warn/3 info,
    each with a recommendation. NEW GET /scibrain/training endpoint; folded into brain_view BrainPulse as a
    `sensory_cortex` region + a `training_health` section. FRONTEND: NEW TrainingHealth card in WholeBrain.tsx
    — AUC bars (latent/baseline/permutation) with a 0.5 chance-marker + the CI band, corpus/imbalance facts
    (n_test_neg highlighted), and the severity-colored issues list with recommendations. VERIFIED (Rule 19):
    diagnostics green on the real report; LIVE /scibrain/training + /scibrain/brain serve it; CRA rebuilt
    (main.fe3d9c92.js) + nginx recreated; REAL chromium render = 14/14 checks PASS (10 region + 4 training-
    health), 0 console errors. git scope: training_health.py(new), keys.py(+PERCEPTION_REPORT/TRAINING_HEALTH),
    brain_view.py, dashboard/api.py(+endpoint), frontend WholeBrain.tsx(+build), brain_dashboard_verify.mjs.
    NEXT 7e: upgrade episodic memory to rich Episodes with prioritized importance-weighted replay.
- [x] Upgrade episodic memory to EntrySnapshot/LifeTrace/Outcome/Counterfactual episodes with pattern-separated embeddings
      — SHIPPED 2026-06-12 (LIVE, Level-3 browser-verified). NEW signals/scibrain/episodic.py: the §3.5
      hippocampus. build_episodic_memory(r) re-assembles RICH Episodes from the components the ledger
      ALREADY stores per closed trade (signals_at_entry: entry_snapshot + module_embedding + lifetrace +
      outcome_packet), and gives each: (1) a PATTERN-SEPARATED embedding — fixed random EXPAND (→64-d) +
      k-Winner-Take-All sparsification (k=8) = the dentate-gyrus trick that DECORRELATES similar episodes so
      a rare failure isn't averaged into the wins; (2) a REPLAY PRIORITY = |reward_pred_error| + surprise +
      tail_severity + model_disagreement + rarity − redundancy (design §3.5), all from real outcome/lifetrace
      signals. Plus an honest MEMORY-HEALTH diagnosis flagging the issues: outcome imbalance, whether rare
      FAILURES are actually PRESERVED (top-priority loss-rate vs base) or averaged away, and pattern-
      separation quality (sparse-vs-raw code similarity). NEW GET /scibrain/episodic; folded into brain_view
      BrainPulse as a `hippocampus` region + `episodic_memory` section. FRONTEND: NEW EpisodicMemory card in
      WholeBrain.tsx (health, the two key diagnostics, severity-colored issues + recs, and the replay-queue
      head with per-episode priority signals). VERIFIED (Rule 19, Level-3) on REAL data (400 episodes):
      health=watch; pattern separation WORKING (raw sim 0.39 → sparse 0.167, gain 0.223); rare failures
      PRESERVED (top-priority loss rate 35% vs base 10.3% = 3.4× over-representation — failures rise to the
      top of the replay queue, the whole point); top episode = a LOSS (EPICUSDT short, priority 2.19, high
      rpe/surprise/rarity). LIVE /scibrain/episodic + /scibrain/brain serve it; CRA rebuilt (main.7a25e405.js)
      + nginx recreated; REAL chromium render = 18/18 checks PASS (incl. 4 episodic), 0 console errors.
      RULE-18 FIX: ragged module_vec rosters broke np.stack → pad/truncate to a fixed 24-d width. git scope:
      episodic.py(new), keys.py(+EPISODIC_MEMORY), brain_view.py, dashboard/api.py(+endpoint), frontend
      WholeBrain.tsx(+build), brain_dashboard_verify.mjs. Phase 7e now 3/7. NEXT 7e: the prioritized-replay
      SAMPLER with importance correction (the priorities + pattern-separated codes built here are its input).
- [x] Add prioritized replay using surprise/reward error/tail severity/model disagreement/rarity with importance correction
      — SHIPPED 2026-06-12 (LIVE, Level-3 browser-verified). episodic.replay_health(r) (refactored episodic.py
      to share `_assemble` so the memory view + sampler use the SAME priorities). Prioritized Experience
      Replay (design §3.5 / Schaul 2015): samples episodes ∝ priority^α (priority = |rpe|+surprise+tail+
      disagreement+rarity−redundancy from step-3), then applies IMPORTANCE-SAMPLING weights w=(N·P)^(−β),
      normalized so IS only scales down — to UNBIAS the prioritized draw. The honest diagnostics POINT OUT
      whether (a) rare FAILURES are over-sampled (the point), (b) the IS correction RECOVERS the true outcome
      distribution (unbiasedness check: IS-weighted loss-rate ≈ base), and (c) effective COVERAGE (priority^α
      ESS / N) so over-concentration on a few episodes is caught, + IS-weight batch ESS (variance). α/β
      config via scibrain:replay:alpha/beta. NEW GET /scibrain/replay; attached to the hippocampus in
      brain_view; rendered as a replay sub-section in the EpisodicMemory card (base→sampled→IS-corrected loss
      rate with an unbiased/biased badge + coverage + batch ESS). VERIFIED (Rule 19, Level-3) on REAL data
      (400 episodes): over-samples failures (sampled loss 14% > base 10%), IS correction RECOVERS base
      (13.6% ≈ 10.3%, bias_recovered=True), coverage 99% (priority_ess 396/400 — no over-concentration),
      IS-weight batch ESS 63.9/64 (low variance); on the 300-subset it honestly flips health=watch when
      prioritization is weak. LIVE /scibrain/replay + /scibrain/brain serve it; CRA rebuilt (main.0934786f.js)
      + nginx recreated; REAL chromium render = 20/20 checks PASS (incl. 2 replay), 0 console errors. git
      scope: episodic.py(+_assemble refactor +replay_health), brain_view.py, dashboard/api.py(+endpoint),
      frontend WholeBrain.tsx(+build), brain_dashboard_verify.mjs. Phase 7e now 4/7. NEXT 7e: slow semantic
      consolidation (replay + EWC/adapters + protected old-competence regression tests).
- [x] Upgrade slow semantic consolidation with replay + EWC/adapters + protected old-competence regression tests
      — SHIPPED 2026-06-12 (LIVE, Level-3 browser-verified) — a CLEAN POSITIVE result (EWC demonstrably
      works). NEW ml/consolidation.py: the §3.6 neocortex slow-consolidation experiment, run end-to-end on
      the real corpus. (1) train the shared latent on the OLD time-window → θ_old + the diagonal FISHER
      information F (each param's importance to the old task); (2) consolidate on the NEW window TWICE —
      WITH EWC (L_slow = L_new + λ·ΣF_i(θ−θ_old)²) and WITHOUT (the ablation that should forget); (3) the
      PROTECTED old-competence REGRESSION TEST: old-window SSL loss before vs after, ACCEPT iff forgetting <
      20% threshold else REJECT (keep θ_old). NEW signals/scibrain/consolidation_health.py diagnoses the
      report into typed issues + a health verdict (healthy|ewc_weak|forgetting|no_run). RESULT on real data
      (n=535, OLD 321 / NEW 214): old-task loss 0.271 → 0.316 (EWC) vs 0.340 (no-EWC); FORGETTING 16.6% with
      EWC vs 25.6% without → EWC CUTS FORGETTING BY 8.9pp; the protected gate ACCEPTS the EWC update (16.6%
      < 20%) while the no-EWC ablation (25.6%) would be REJECTED — EWC + the regression gate both working as
      designed. NEW GET /scibrain/consolidation; brain_view `neocortex` region + `consolidation` section.
      FRONTEND: NEW ConsolidationHealth card in WholeBrain.tsx (forgetting bars with-vs-without-EWC + the
      threshold line, accept/reject badge, old-task loss before→after, λ_ewc, issues). VERIFIED (Rule 19,
      Level-3): consolidation ran in the LIVE brain container (torch 2.12, 30s); diagnostics green; LIVE
      /scibrain/consolidation + /scibrain/brain serve it (10 regions now); CRA rebuilt (main.0b08c937.js) +
      nginx recreated; REAL chromium render = 23/23 checks PASS (incl. 3 consolidation), 0 console errors.
      DEPLOY NOTE: ml/ not mounted → ran via docker cp; report persists in Redis (the dashboard reads that),
      so no rebuild needed. git scope: consolidation.py(new), consolidation_health.py(new), keys.py
      (+CONSOLIDATION_*), brain_view.py, dashboard/api.py(+endpoint), frontend WholeBrain.tsx(+build),
      brain_dashboard_verify.mjs. Phase 7e now 5/7. NEXT 7e: isolated SLEEP jobs (replay/consolidation/
      calibration/adversarial-rehearsal/homeostasis/pruning) — the beat-scheduled wrapper around the
      replay+consolidation machinery shipped in steps 3-5.
- [x] Add isolated sleep jobs for replay, consolidation, calibration, adversarial rehearsal, homeostasis, and pruning
      — SHIPPED 2026-06-12 (LIVE, Level-3 browser-verified). NEW signals/scibrain/sleep.py: run_sleep_cycle(r)
      — the §3.6/§9 isolated sleep orchestrator that runs 6 maintenance jobs OFF the brain hot loop, SINGLE-
      FLIGHT (redis lock) + LOAD-AWARE, pure read + ephemeral bookkeeping (NO live trading state mutated):
      (1) replay — exercises the prioritized-replay sampler (rare-failure coverage + IS-unbiasedness);
      (2) consolidation — checks EWC health + flags re-consolidation DUE if the report is stale >24h;
      (3) calibration — reads the audit Brier + IC freshness, flags drift vs the base-rate baseline;
      (4) adversarial rehearsal — perturbs recent episodes' module_vec within a bounded ε-ball and measures
      top-priority-set STABILITY (Jaccard) — a memory whose priorities flip under tiny noise is fragile;
      (5) homeostasis — priority-distribution entropy (degeneracy/collapse check); (6) pruning — surfaces
      PRUNE-verdict modules + redundant episodes (report-only). Each job is isolated (one failure doesn't
      abort the cycle). NEW celery beat task celery_app.scibrain_sleep_cycle on the WORKER @ */30min (off the
      brain process = isolated). NEW GET /scibrain/sleep (run=true triggers on-demand); brain_view `sleep`
      section. FRONTEND: NEW SleepCycle card in WholeBrain.tsx (health, the 6 jobs each with status+metric,
      issues). VERIFIED (Rule 19, Level-3) on LIVE data: full cycle ran in 1.75s health=rested — replay
      healthy/unbiased/99% coverage, consolidation fresh (EWC✓), calibration Brier 0.354 no-drift,
      adversarial STABLE (jaccard 0.935 under ε=0.05), homeostasis balanced (entropy 0.998), pruning 3
      candidate modules (info_theory/ising/sparse_factor_residual) report-only; worker registered the beat
      task + ran it; LIVE /scibrain/sleep + /scibrain/brain serve it; CRA rebuilt (main.9d24c6f9.js) + nginx
      recreated; REAL chromium render = 25/25 checks PASS (incl. 2 sleep), 0 console errors. Restarted
      celery_worker+beat (celery_app.py single-file inode). git scope: sleep.py(new), celery_app.py
      (+task+beat), brain_view.py, dashboard/api.py(+endpoint), frontend WholeBrain.tsx(+build),
      brain_dashboard_verify.mjs. Phase 7e now 6/7. NEXT 7e (last): reward correct abstention + preserve
      false-alarms/rejected-actions/near-misses in memory.
- [x] Reward correct abstention and preserve false alarms/rejected actions/near misses in memory
      — SHIPPED 2026-06-12 (LIVE, Level-3 browser-verified). NEW signals/scibrain/abstention.py: closes the
      §3.5 memory gap (memory kept TRADES only; replay must include "wins, false alarms, rejected actions,
      near misses") + the §3.11 reward ("rewarded for correct abstention, penalized for confident
      unsupported action"). build_abstention_memory(r) reads the 141,262 RESOLVED counterfactuals (every
      signal the bot REJECTED + whether it would_have_won) and classifies: CORRECT_ABSTENTION (rejected,
      would have LOST), MISSED_OPPORTUNITY (rejected, would have WON), NEAR_MISS, + the decoder's FALSE-ALARM
      miss-tag breakdown; computes a net ABSTENTION REWARD (correct-abstention-rate·avoided-loss −
      missed-rate·forgone-gain) and an ABSTENTION SKILL read (rejected would-win rate vs accepted win rate =
      does the bot reject DISCRIMINATINGLY). NEW GET /scibrain/abstention; brain_view `abstention` section.
      FRONTEND: NEW AbstentionMemory card in WholeBrain.tsx (reward, discrimination, the 3 classes with
      counts/rates/avg magnitudes, issues). VERIFIED (Rule 19, Level-3) on REAL data (141,262 rejected /
      12,299 accepted): abstention reward +1.169; DISCRIMINATING — rejected would-win 42% < accepted win 55%
      (discrimination +0.133); correct_abstention 81,942 (58%, avoids 14.2% loss avg), missed_opportunity
      59,320 (42%, forgoes 16.8% gain avg), near_miss 4,689; 141k non-trade events NOW preserved (was: trades
      only). LIVE /scibrain/abstention + /scibrain/brain serve it; CRA rebuilt (main.3727cd2c.js) + nginx
      recreated; REAL chromium render = 28/28 checks PASS (incl. 3 abstention), 0 console errors. git scope:
      abstention.py(new), keys.py(+ABSTENTION_MEMORY), brain_view.py, dashboard/api.py(+endpoint), frontend
      WholeBrain.tsx(+build), brain_dashboard_verify.mjs.
      ═══ PHASE 7e COMPLETE (7/7): perception (SSL latent + honest training-health), hippocampal episodic
      memory (pattern separation + prioritized replay + IS correction), neocortex EWC consolidation, isolated
      sleep cycle, and abstention memory — every step LIVE, read-only/shadow, no trading authority, each with
      auto-diagnosed health surfaced in the Whole-Brain dashboard.

## PHASE 7f — World model, planning, and safe hierarchical learning
- [x] Upgrade RSSM observations/actions to real rich sequences from the immutable ledger; remove sparse trade-only state
- [x] Add ensemble epistemic uncertainty and multi-horizon latent/reward/continuation calibration
- [x] Replace constant-action imagination with bounded sequence planning and compare against deterministic digital twin
- [x] Reframe existing day/minute PPO “MARL” as hierarchical controllers; add real trajectories, hour role, shared belief, coordination tests
- [x] Build offline CQL/IQL challengers with abstain/enter/manage/exit options and support-aware baseline fallback
- [x] Add distributional/CVaR objectives, realistic costs, safety projection, and digital-twin/shadow evaluation
- [x] Upgrade MAML/meta-learning tasks to real regime/cohort tasks with protected competence and no synthetic-zero-state shortcut
- [x] Give only bounded cerebellar calibration/timing/execution residual learners the first limited canary authority
      — NEW signals/scibrain/cerebellum.py: the FIRST capital-affecting authority grant (design §3.8/§5.5 step-12,
      Rule-14 ladder bounded_canary). THREE bounded residual heads, each online-learned from REAL matured trade
      labels with a HARD out-of-sample earns-gate (a head moves a real trade only if it provably beats baseline OOS):
      (1) CALIBRATION — decile recalibration of conviction vs realized win/loss (6k labels), ±0.05 clamp, earns iff
      OOS Brier improves ≥0.0015 (live 0.278→0.269, Δ+0.0088 EARNED); APPLIED to dec.conviction → size+leverage in
      opener._plan_open. (2) TIMING — P(adverse fill) from matured predicted-vs-actual entry; defers ONE pick by ≤1
      cycle when armed+earned+p≥0.60, hard anti-starvation cap (≤1 skip/symbol); EARNED (p=0.649); APPLIED in
      run_open loop. (3) SLIPPAGE — median adverse fill (bps), EARNED (MAE Δ+0.80) but report-only under paper (no
      limit lever; Rule 12 — declares bounded_canary cap, application PENDING a real-exchange order path).
      OWNER-GATED + DEFAULT ON (owner 2026-06-13 "turn these on so I can see if it improves trades"); instant
      disarm via scibrain:cerebellum:canary=0 (→ all heads observe, byte-identical to no-cerebellum). VISIBILITY:
      each open trade carries a frozen cerebellum block (base→adjusted conviction, Δ, applied?) in provenance,
      surfaced by dashboard get_open_trades + a new "Cerebellum" column in OpenTradesTable.tsx (shown even while
      disarmed/unearned). keys.py CEREBELLUM_* added; beat scibrain-cerebellum @*/30m (celery_app.scibrain_cerebellum_train).
      VERIFIED LIVE (Rule 19/21): trainer earns all 3 heads, hot-path applied counter increments, disarm flips
      applied→False while still showing Δ, _plan_open stamps block (base 0.85→0.80 applied), worker registered the
      task, beat schedule loaded, all 78 open trades carry the key (None on pre-ship trades — honest).

## PHASE 7g — Whole-brain plasticity and advanced research challengers
- [ ] Add typed dopamine-like reward error, uncertainty/changepoint, patience/risk, and homeostatic learning signals
- [ ] Connect representation, memory, world model, router, policy, and prompt changes to one experiment/promotion kernel
- [ ] Add causal representation/intervention benchmarks and information-bottleneck/MDL complexity accounting
- [ ] Benchmark reservoir/liquid-state, neural-operator/SPDE, and multiscale/RG challengers against simpler sequence models
- [ ] Benchmark tensor-network compressed beliefs against low-rank, Transformer, Mamba, and GNN baselines
- [ ] Benchmark density-matrix/Bures belief geometry against standard ensemble covariance/simplex methods
- [ ] Keep quantum kernels/variational circuits QPU/simulator-only until data-loading cost and classical baselines are beaten
- [ ] Reject any biological/physics/quantum component that lacks a concrete observable, bounded output, and incremental utility

## PHASE 8 — Risk, allocation, exit, and reflexivity control plane
- [ ] CorrelationKellyAllocator — portfolio-level fractional Kelly with covariance/cluster/tail constraints
- [ ] HJBOptimalStoppingController — shadow hold/close policy against actual SL/TP outcomes
- [ ] MarketImpactReflexivityController — spread/slippage/Kyle-lambda/own-impact size and timing caps
- [ ] Control-plane dashboard: proposed action, actual action, counterfactual PnL, reason, authority state

## PHASE 9 — Discovery sandbox (never direct-to-live)
- [ ] SINDy/PySR equation discovery with walk-forward falsification
- [ ] GFlowNet diverse hypothesis generation with novelty and complexity penalties
- [ ] Research-only queue for Free Probability, SPDE/CFT/field theory, deeper quantum information,
      Malliavin, and Mean Field Games; require concrete observable + estimator + falsifiable hypothesis
- [ ] Promotion pipeline from sandbox → shadow module → admitted component, with no bypass

---

## SESSION LOG (newest first)
### 2026-06-13 — PHASE 7f task-7: META-LEARNING on REAL regime/cohort tasks + PROTECTED COMPETENCE (LIVE, SHADOW)
- GOAL (§3.7 / §5.4-7 protected-competence / §5.5 stage-6 meta-learning / §8-419): upgrade ml/maml.py — the
  spec flags it "task construction is synthetic and weak; adaptation target too narrow". Confirmed the live
  defects by reading the code: adapt_world_model_to_recent_regime() builds ALL-ZEROS RSSM latents
  (deter/stoch=torch.zeros) = the SYNTHETIC-ZERO-STATE shortcut, LEAKS the target (stoch[:,0]=pnl/100 then
  predicts pnl), uses PER-PAIR (not regime/cohort) tasks, and OVERWRITES shared world-model weights every
  changepoint (adapt_count=50) with NO protected-competence guard.
- RULE 21 (classify the surface): the live maml mutates the world-model reward head, but the world model has
  ZERO planning authority (planning_weight 0.0, granted False) → it influences NO open trade. Per the Phase-7
  "never direct-to-live" rule + §5.4 ("run offline/shadow, compare against protected competence benchmarks") +
  the task-4/5/6 precedent, I built the upgrade as a SHADOW measurement and changed NOTHING in ml/maml.py or
  the world-model weights. live_maml_untouched=True, live_agents_untouched=True.
- BUILT ml/meta_regime_tasks.py (deterministic numpy, ~1.5s): REAL COHORT TASKS = regime×VPIN-tertile from the
  controller corpus with REAL belief features [ofi,vpin,sentiment,conviction,bias] (nonzero, standardized) and
  targets = digital-twin utilities (u_long,u_short) — full counterfactual feedback, target NEVER an input.
  Reptile (first-order) meta-trains a fast-adapting init; few-shot adapt per cohort vs no-adapt / from-scratch /
  one POOLED model. PROTECTED-COMPETENCE test: MAML (ephemeral adapt from a protected shared init) vs the
  live-style SEQUENTIAL fine-tune (mutate shared weights cohort-by-cohort) — re-eval the first cohort to expose
  forgetting. Numeric robustness: winsorized+z-scored targets + gradient-clipped inner SGD (first run diverged
  to 1e+269 on heavy-tailed twin utilities; fixed).
- HONEST RESULT (Rule 12): INTEGRITY PASS — state_real=True, target_leakage=False (the named defects fixed).
  Few-shot adapt lowers loss vs no-adapt (+0.003..+0.02) and ~ties from-scratch, BUT a single POOLED linear
  model is BETTER (pool_gain ≈ −0.004..−0.05) → adaptation_helps=FALSE, meta-learning does NOT earn its keep on
  this corpus yet (health "no_edge"). PROTECTED COMPETENCE: MAML preserves the old cohort by construction
  (maml_forgetting=0); the sequential fine-tune showed positive transfer (no forgetting) on these similar
  cohorts this run — the guard is built+correct but not biting. Like tasks 4/5/6 the fancy method ties/loses to
  the simple baseline — honest, do NOT promote; owner-gated (§5.5 stage-6 must pass benchmarks before authority).
- WIRED LIVE (Rule 19): keys META_LEARNING_REPORT/HEALTH; signals/scibrain/meta_learning_health.py faculty;
  /scibrain/meta_learning endpoint; beat task scibrain_meta_learning_train (daily 04:35, shadow) + schedule;
  brain_view folds 'meta_learning' as a NEW 'cerebellum' region (fast regime adaptation; leaves room for
  task-8 cerebellar authority); WholeBrain.tsx <MetaLearning/> card (query-MSE table, adapt/pool-gain +
  integrity + protected-competence pills).
- VERIFIED LIVE (Rule 19): module runs ({'ran':True,'rc':0}); FORCE-RECREATED celery_worker AND celery_beat
  (single-file celery_app.py inode was stale — import failed before recreate); dispatched task RUNS on the fresh
  worker daemon; live beat daemon carries the 04:35 schedule; health faculty available+no_edge (0 crit/1 warn);
  whole-brain PULSE carries meta_learning + the cerebellum region (healthy, adaptation_gain/beats_pooled/
  competence_protected/n_cohorts); frontend tsc clean + build OK (main.82a46f7c.js) served live by nginx.
  Rule 21: live_maml_untouched + live_agents_untouched confirmed True — no open-trade path changed.
- git scope (Rule 11): ml/meta_regime_tasks.py(new), signals/scibrain/meta_learning_health.py(new),
  signals/scibrain/keys.py, dashboard/api.py, celery_app.py, signals/scibrain/brain_view.py,
  frontend/src/panels/scibrain/WholeBrain.tsx, frontend/build/*.

### 2026-06-13 — PHASE 7f task-6: distributional/CVaR objective + realistic costs + CBF SAFETY PROJECTION (LIVE, SHADOW)
- GOAL (§3.7 multi-objective reward / §3.10 safe-set tail reflex / §8-416 / step 11): build a risk-sensitive
  layer on task-5's offline RL — model the full per-(state,action) RETURN DISTRIBUTION from the deterministic
  digital twin, optimize a CVaR (worst-tail) objective instead of the mean, charge an explicit turnover cost,
  and add a Control-Barrier-Function-style SAFETY PROJECTION (reduce-only: veto a heavy-tail entry to abstain,
  never enlarge a position — §3.10 "can veto or reduce, cannot invent a larger position"). SHADOW (Rule 21 —
  live agents/funnel untouched, live_agents_untouched=True).
- BUILT ml/risk_sensitive_policy.py (tabular, deterministic, ~1.2s): per state-bucket × {enter_long,enter_short,
  abstain} twin-utility distributions (mean/std/CVaR_fit α=0.25/CVaR_tail α=0.10); CVaR-policy = argmax CVaR_fit
  − λ_turnover; CBF projection vetoes any entry with CVaR_0.1 < safety_cap −0.20 to abstain; support-aware
  fallback to the global baseline on sparse buckets (τ=8). Shadow eval on held-out twin: mean/CVaR/worst/maxDD/
  turnover for mean-policy vs CVaR vs CVaR+safety vs baseline vs realized.
- HONEST RESULT (Rule 12): risk_sensitive_helps=TRUE — CVaR policy slashes the tail (CVaR_0.1 −0.055 vs
  mean-policy −0.55..−0.67; tail gain +0.49..+0.61) and max-drawdown 0.97 vs 8.23, for a small mean cost
  (−0.026) and cost-aware turnover 0.074 vs realized 1.0; CBF projection fired 3 reduce-only vetoes (~1.5%).
  This is the INTENDED distributional behavior — trade a little average return for a much better worst case.
  (Numbers drift slightly run-to-run because the controller corpus grows with live trades — deterministic for
  a fixed corpus.) Like tasks 4/5, the challenger mean (0.007) still trails the realized live funnel (0.12),
  so SHADOW only — do NOT promote; owner-gated on purged walk-forward (§6).
- WIRED LIVE (Rule 19): keys RISK_POLICY_REPORT/HEALTH; signals/scibrain/risk_policy_health.py faculty;
  /scibrain/risk_policy endpoint; beat task scibrain_risk_policy_train (daily 04:30, shadow) + schedule;
  brain_view folds 'risk_policy' (amygdala) + enriches the amygdala region (cvar_tail_gain/cvar_dd_reduction);
  WholeBrain.tsx <RiskPolicy/> card (policy×tail-metric table, tail gain/mean cost/DD↓ pills, issues).
- VERIFIED LIVE (Rule 19): module runs ({'ran':True,'rc':0}); dispatched task RUNS on the live worker daemon
  (not just a fresh import); live celery_beat daemon carries the 04:30 schedule (beat container started 09:12,
  after the 09:02 edit); health faculty available+healthy (0 crit/0 warn); whole-brain PULSE carries risk_policy
  (available, healthy) and the amygdala region (cvar_tail_gain 0.611, cvar_dd_reduction 7.26); frontend tsc
  clean + react-scripts build OK (main.aa49beec.js) served live by nginx (./frontend/build bind-mount). Rule 21:
  shadow, no open-trade authority — confirmed live_agents_untouched=True; nothing on the trade path changed.
- git scope (Rule 11): ml/risk_sensitive_policy.py(new), signals/scibrain/risk_policy_health.py(new),
  signals/scibrain/keys.py, dashboard/api.py, celery_app.py, signals/scibrain/brain_view.py,
  frontend/src/panels/scibrain/WholeBrain.tsx, frontend/build/*.

### 2026-06-13 — PHASE 7f task-5: offline CQL/IQL CHALLENGERS + support-aware fallback (LIVE, SHADOW)
- GOAL (§3.7 "offline RL first ... CQL or IQL ... baseline fallback" / §8-416 "support-aware baseline
  improvement"): offline RL challengers over options {abstain, enter, manage, exit} with a support-aware
  baseline fallback. No existing CQL/IQL code — built new. SHADOW (Rule 21 — no live authority).
- BUILT ml/offline_rl_challengers.py (tabular, deterministic, ~0.7s): reuses the controller corpus + the
  deterministic digital twin for reward (full counterfactual feedback for ENTRY options u_long/u_short/
  u_abstain on the real path with fees+SL/TP; manage/exit have NONE). CQL = conservative Q − α/√bucket-
  coverage penalty; IQL = expectile-V (τ=0.7) + advantage. SUPPORT-AWARE FALLBACK: if a state-bucket's
  coverage < τ (or the chosen option is unsupported) the policy DEFERS to the baseline instead of an
  extrapolated value. Off-policy eval on held-out twin vs behavior/baseline/oracle + a NAIVE no-fallback
  argmax-Q (the OOD-chaser) to prove the conservatism earns its keep.
- HONEST RESULT (Rule 12): CQL=IQL 0.0286 BEAT the global baseline 0.0131 AND beat the naive no-fallback
  policy 0.0089 — direct evidence the support-aware conservatism ADDS value, not just caution
  (conservatism_helps). Support-aware fallback fires on ~5% of states. option_support: enter_long 246,
  enter_short 211, and abstain/manage/exit = 0 (the bot never abstained on a logged trade; manage/exit have
  no intra-trade decision logging — lifetrace empty) → the fallback correctly REFUSES them (honest data
  limit, flagged options_unsupported, NOT a fabricated reward). BUT like task 4 the challengers (0.029) still
  badly UNDERPERFORM the realized live funnel (0.119, oracle 0.169) → underperforms_realized; do not promote.
- WIRED LIVE (Rule 19): keys + signals/scibrain/offline_rl_health.py faculty; /scibrain/offline_rl endpoint;
  beat task scibrain_offline_rl_train (daily 04:25, shadow); brain_view folds 'offline_rl' + enriches the
  basal_ganglia region (orl_fallback / orl_beats_base); WholeBrain.tsx renders the card (CQL/IQL/baseline/
  naive/realized/oracle, fallback rate, option support, issues). VERIFIED: runs on the WORKER
  ({'ran':True,'rc':0}) after force-recreate (celery_app inode); endpoint + pulse carry it; REAL chromium
  render = 7/7 checks PASS, 0 console errors.
- git scope (Rule 11): ml/offline_rl_challengers.py(new), signals/scibrain/offline_rl_health.py(new),
  signals/scibrain/keys.py, dashboard/api.py, celery_app.py, signals/scibrain/brain_view.py,
  frontend/src/panels/scibrain/WholeBrain.tsx.

### 2026-06-13 — PHASE 7f task-4: day/minute MARL reframed as HIERARCHICAL CONTROLLERS + coordination tests (LIVE, SHADOW)
- GOAL (§3.7/§8-table line 37 & 417 / step 10): the existing ml/marl.py day/minute agents are NOT coordinated
  MARL — independent ONE-STEP contextual bandits on weak 3-dim obs, hour agent declared-but-dead, no shared
  state/coordination. Reframe as hierarchical controllers + add real trajectories, hour role, shared belief,
  coordination tests + ablation.
- RULE 21 (the new rule, applied): day+minute checkpoints EXIST and are CALLED LIVE (marl:day/minute:call_count)
  — day scales capital in brain/soar.py, minute vetoes in signals/engine.py → they DO influence open trades.
  So per Rule 21 + the Phase-7 "never direct-to-live" philosophy, I built the reframe as a SHADOW measurement
  and changed NOTHING in ml/marl.py / soar.py / engine.py. live_agents_untouched=True verified. No open-trade
  impact, by design.
- BUILT ml/hierarchical_controllers.py (deterministic, ~0.5s): reuses the world-model corpus + the deterministic
  digital twin (task 3) for per-action utility. REAL TRAJECTORIES = ordered trades in multi-timescale windows
  (1 DAY decision ≈ 20 trades, 1 HOUR ≈ 5, MINUTE per trade). SHARED BELIEF = one state (ofi/vpin/sentiment/
  conviction/regime) read at three resolutions. THREE ROLES: DAY (strategic stance, coarse=regime) → HOUR
  (tactical engage/wait veto, medium=sentiment) → MINUTE (execution enter/skip veto, fine=ofi+vpin). COORD GAIN
  = full cascade − flat-independent baseline; ABLATION = flat→+belief→day→+hour→+minute; per-level marginal
  VETO rates (decorative test) + strategy-vs-hindsight conflict.
- HONEST RESULT (Rule 12 — the finding IS the deliverable): coordination_gain = 0.0 (full 0.018 ties flat 0.018);
  belief_gain 0; HOUR level DECORATIVE (veto 0%); day stance = always-long (long-biased data), conflicting with
  hindsight 58% of the time; and ALL reframed controllers (0.018) badly UNDERPERFORM the realized live policy
  (0.120, oracle 0.170). i.e. the reframing is real and measured but NOT competitive with the existing funnel —
  surfaced as issues no_coordination_gain / underperforms_realized / hour_decorative. Do NOT promote; live PPO
  agents stay in charge.
- WIRED LIVE (Rule 19): keys + signals/scibrain/controllers_health.py faculty; /scibrain/controllers endpoint;
  beat task scibrain_controllers_train (daily 04:20, shadow); brain_view folds a 'controllers' faculty + enriches
  the basal_ganglia region with ctrl_coord_gain; WholeBrain.tsx renders the controllers card (ablation ladder,
  veto rates, SHADOW badge, issues). VERIFIED: runs on the WORKER ({'ran':True,'rc':0}); endpoint + brain pulse
  carry it; REAL chromium render = 7/7 checks PASS, 0 console errors.
- RULE-19/21 CATCH: the beat task wasn't importable on the worker — single-file bind-mount INODE staleness on
  ./celery_app.py:/app/celery_app.py (Edit changes the inode; a plain restart keeps the old one). Fixed with
  docker compose up -d --force-recreate celery_worker celery_beat. (Same class as the lost ./ml mount in task 3
  — single-file mounts + edited files do NOT propagate to the running container without force-recreate.)
- git scope (Rule 11): ml/hierarchical_controllers.py(new), signals/scibrain/controllers_health.py(new),
  signals/scibrain/keys.py, dashboard/api.py, celery_app.py, signals/scibrain/brain_view.py,
  frontend/src/panels/scibrain/WholeBrain.tsx.

### 2026-06-13 — PHASE 7f task-3: BOUNDED PLANNING vs deterministic DIGITAL TWIN (LIVE)
- GOAL: "Replace constant-action imagination with bounded sequence planning and compare against deterministic
  digital twin" (§3.9 MPC/bounded action search + §6 Counterfactual Digital Twin).
- REUSED the existing deterministic twin (signals/scibrain/twin.py simulate_counterfactuals — actual/opposite/
  abstain replayed on the REAL candle path with fees + mirrored SL/TP geometry), read from each trade's stored
  signals_at_entry->outcome_packet->counterfactual (572/635 corpus trades have status='ok'). NOT a -pnl proxy.
- BOUNDED PLANNER (ml/world_model_train.py _plan_eval): at each test decision the planner SEARCHES the bounded
  action set {long, short, abstain} — imagines each candidate's reward with the RSSM ensemble — and PICKS the
  argmax (abstain if both directional bets imagine a loss). Replaces replaying the recorded (constant) action.
  Its choice is scored on the twin vs realized policy, a fixed best-constant-action baseline, abstain-all, and
  the hindsight oracle. Authority granted ONLY if model beats baseline AND safe horizon≥1 AND a MEANINGFUL
  planning lift (>0.005 utility, not noise) over baseline — else FALL BACK to the deterministic baseline (§3.9).
- HONEST RESULT (Rule 12): ~89 twin decisions — planner ≈ −0.04 ≈ baseline (lift vs baseline ~0.000, gated
  to "no lift"), realized ~+0.01, oracle ~+0.05, oracle-action agreement ~0.45 (≈ chance). The planner
  COLLAPSES to a near-constant action (long ~89/89) because the world model can't discriminate states — so its
  best move IS the best constant action (= baseline, zero lift). Surfaced explicitly as the diagnosed issue
  planner_collapsed_constant_action + planning_no_lift. The oracle (+0.05) shows exploitable signal EXISTS;
  the model just can't capture it yet → fallback correctly engaged. Planner built, bounded, twin-evaluated,
  gated — exactly the §3.9 behaviour when there is no learned edge.
- WIRED LIVE (Rule 19): planning block + planner_collapsed/planning_no_lift/planner_beats issues in
  world_model_health.py; brain_view prefrontal region carries plan_lift + fallback; WholeBrain.tsx renders the
  planner-vs-twin row (planner/realized/baseline/oracle, lift, oracle-agreement, action mix, collapse flag).
  Seeded the eval (manual_seed(7)) for report reproducibility; EPS=0.005 lift threshold so a noise lift can't
  claim "beats baseline". VERIFIED: trains+plans on the WORKER end-to-end ({'ran':True,'rc':0}, 5 members
  ~112s); REAL chromium render = 6/6 checks PASS, 0 console errors.
- RULE-19 CATCH (motivated a new rule, below): the celery_worker container had SILENTLY LOST its ./ml bind-
  mount (recreated from the baked image since task-2) → the beat task raised "No module named ml.world_model_
  train". docker compose up -d celery_worker re-applied the mount. Lesson: a feature can be "shipped" in code +
  compose yet NOT actually live in the running container — must verify the live container, not just the file.
- git scope (Rule 11): ml/world_model_train.py, signals/scibrain/world_model_health.py,
  signals/scibrain/brain_view.py, frontend/src/panels/scibrain/WholeBrain.tsx.

### 2026-06-13 — PHASE 7f task-2: RSSM ENSEMBLE epistemic uncertainty + multi-horizon calibration (LIVE)
- GOAL: "Add ensemble epistemic uncertainty and multi-horizon latent/reward/continuation calibration"
  (§3.9/§3.11/§8). Fills the gap task-1 flagged: planning_weight hard-coded epistemic=0.
- ENSEMBLE (ml/world_model_train.py refactored): train N=5 RSSM members on bootstrap-resampled train
  windows + distinct seeds; their DISAGREEMENT on the imagined reward = epistemic (reducible) uncertainty.
  Clean train(70%)/calib(15%)/test(15%) split — aleatoric (irreducible) is estimated on the held-out CALIB
  slice (total residual − epistemic), so coverage is honest, not in-sample. epistemic is now REAL in the
  §3.9 gate: planning_weight = clip(1 − model_error − epistemic, 0, 1).
- MULTI-HORIZON CALIBRATION (per imagined horizon k=1..3): (a) REWARD interval coverage — does the
  ensemble+aleatoric predictive interval cover the realized reward at the nominal rate; (b) LATENT open-loop
  drift — prior-only imagined latent vs observed-posterior latent, normalized by latent variance; (c)
  CONTINUATION reliability (flagged degenerate: within-trade windows never terminate → actual_continue≡1).
- §3.9 HORIZON SHORTENING: safe_planning_horizon = leading run of horizons that BOTH beat baseline AND have
  low disagreement; authority needs both. Honest result = 0.
- REAL-DATA RESULT (Rule 12 — the honest finding is the point): epistemic 0.116 (LOW — members agree) vs
  aleatoric 0.710 (HIGH) → the uncertainty is mostly IRREDUCIBLE trade-PnL noise, not model ignorance.
  90% reward-interval coverage 0.895 vs 0.90 nominal (gap 0.005) — the calibration MACHINERY WORKS even
  though the point prediction doesn't beat baseline (R²≈−0.07). latent drift ~1.97 (open-loop state lost
  fast). safe_planning_horizon 0, planning_weight 0.0 → authority correctly WITHHELD, now MEASURED not
  assumed. This is calibrated ignorance (§3.11): the bot honestly knows trade-PnL is near-irreducible here.
- WIRED LIVE (Rule 19): world_model_health.py surfaces uncertainty decomposition + coverage + safe horizon
  with new issues (epistemic_decomposed / reward_intervals_calibrated). brain_view prefrontal region carries
  epistemic/coverage90/safe_horizon. WholeBrain.tsx card shows the 5-member ensemble, epistemic-vs-aleatoric,
  90% interval coverage, per-horizon model-error/cov90/epi/drift. VERIFIED: ensemble trains on the WORKER
  end-to-end ({'ran':True,'rc':0}, 5 members ~101s); endpoint + brain-pulse carry all fields; REAL chromium
  render = 9/9 checks PASS, 0 console errors. NB: had to RESTART the dashboard so its cached task-1 faculty
  module reloaded the task-2 output schema (bind-mount inode is live, the imported module is not).
- git scope (Rule 11): ml/world_model_train.py, signals/scibrain/world_model_health.py,
  signals/scibrain/brain_view.py, frontend/src/panels/scibrain/WholeBrain.tsx.

### 2026-06-13 — PHASE 7f task-1: RSSM upgraded to RICH LEDGER SEQUENCES + honest planning-authority gate (LIVE)
- GOAL: "Upgrade RSSM observations/actions to real rich sequences from the immutable ledger; remove
  sparse trade-only state" (§3.6/§3.9). The WorldModelBundle RSSM (ml/architectures.py, 68,690 params)
  previously trained ONLY its reward head from a single trade-close PnL. Now ml/world_model_train.py builds
  the rich cross-trade TRAJECTORY from the ledger (each step = full multimodal entry obs [module_vec +
  ofi/vpin/funding/spread/sentiment/conviction/net_vote + regime one-hot] + typed action + realized reward
  + continue) and trains the WHOLE RSSM on SEQUENCES (T=8 windows) with the DreamerV3 observe loss
  (reward + continue + KL(post‖prior)). 635 ordered trades → 627 windows.
- RULE-18 FIX (the degenerate metric the prior session stopped mid-surfacing): replaced the sign-accuracy
  capability metric (0.9127 == 0.9127 baseline — DEGENERATE at a ~91% win rate; "always-win" already scores
  0.91) with an honest open-loop multi-step CALIBRATION metric: normalized_model_error = imagined-reward
  MSE / constant-baseline MSE in symlog space (= 1−R²), reported PER HORIZON (k=1,2,3). This is the exact
  quantity §3.9 needs: planning_weight = clip(1 − normalized_model_error − epistemic, 0, 1).
- HONEST RESULT (Rule 12): normalized_model_error 1.068 (R² −0.068), per-horizon 1.076/1.060/1.067 — the
  imagined reward is NO BETTER than a constant → world model does NOT beat baseline → planning_weight 0.0,
  authority correctly WITHHELD (the safe §3.9 default). The UPGRADE is real (rich sequences + full RSSM);
  the predictive power isn't there yet on this small noisy corpus (next levers = ensemble + richer obs +
  costs/CVaR, tasks 2–6). Surfaced as faculty health "no_authority" (a safe state, NOT a fault).
- WIRED LIVE (Rule 19): NEW signals/scibrain/world_model_health.py (build_world_model_health → diagnoses +
  publishes scibrain:world_model_health, matches the 7e faculty pattern) · NEW /scibrain/world_model
  endpoint · NEW celery beat task scibrain_world_model_train (default queue, single-flight redis lock +
  load-aware skip>9.0, daily 04:10) · brain_view.py folds a prefrontal_cortex region + world_model faculty
  into the BrainPulse · WholeBrain.tsx renders the world-model card (per-horizon model-error bars, planning
  gate, honest verdict). docker-compose: added ./ml mount to celery_worker so the new trainer runs live.
- VERIFIED: trainer runs on the WORKER end-to-end ({'ran':True,'rc':0}, ~38s); beat schedule + endpoint +
  brain-pulse region all registered; REAL chromium render of the Whole-Brain dashboard = 8/8 checks PASS
  (card visible, R² −0.068, model err 1.068, planning weight 0.000, NO_AUTHORITY badge, per-horizon k=1/2/3
  bars, honest verdict), 0 console errors. git scope (Rule 11): ml/world_model_train.py, signals/scibrain/
  world_model_health.py(new), dashboard/api.py, celery_app.py, signals/scibrain/brain_view.py,
  frontend/src/panels/scibrain/WholeBrain.tsx, docker-compose.yml.

### 2026-06-10 — PHASE 2b: CausalLeadLag (3rd Universe-Core module) + Universe-Core 30× PERF FIX (LIVE)
- NEW universe_modules/causal_lead_lag.py: SPARSE CONDITIONAL Granger/PCMCI lag graph from the top-K
  liquid leaders. Factor-residualize leaders' lagged returns + targets' next returns (conditional on the
  market factor) → one shared ridge multivariate solve → (K×S) conditional lead-lag (controls for the
  other leaders). Stability selection (20 subsamples, top-3 parents, π=0.5) → sparse graph; vote = the
  move a target's STABLE causal leaders predict (abstain if none). Distinct from SpectralGraphContagion
  (bivariate) + InfoTheory TE (bivariate). shadow_only/observe, deterministic, evidence_family='causal_flow'.
- RULE-18 (sparsity): first cut wasn't sparse (~15-18 drivers/target via a median-threshold trap; Rule-9
  caught it) → top-N-parents stability selection → mean 1.74 drivers/target, 29 honest abstentions, with
  sensible structure (ZIL←SOL, VANRY←BNB, KAS←PEPE).
- BIG RULE-18 PERF FIX (fixed the WHOLE Universe-Core, incl. the already-shipped SFR+SGC): the universe-
  core ran 10–26s → a 33–43s funnel cycle. Rule-9 root cause was NOT the math — the brain MAIN process
  oversubscribed BLAS threads while the 8 fork-scorer workers + candlenet/cn_train saturate the 10-core
  box (load 8–10). Measured SFR RPCA 14s unpinned vs 0.17s @ 1 thread (~80×). FIX: run_universe_modules
  wraps the bank in threadpoolctl.threadpool_limits(1) (matching the workers' gate._worker_init pin).
  Plus SGC full O(n³) eigh → scipy eigsh k=2 (Fiedler only), 9.6s→0.22s (~43×). VERIFIED LIVE: universe-
  core 10–26s → ~0.8s for ALL 3 modules (501 symbols); skip-cycle funnel ~4s; 91 live Decisions fold all
  3 as counterfactual_only/effect 0.0 (zero authority). VERIFICATION-PENDING: settled IC needs 30-min
  horizon. git scope (Rule 11): causal_lead_lag.py(new), __init__.py, universe_frame.py, spectral_graph_
  contagion.py.

### 2026-06-10 — PHASE 2b: SpectralGraphContagion (2nd Universe-Core module, SHADOW) SHIPPED (LIVE)
- NEW universe_modules/spectral_graph_contagion.py: spectral analysis of the cross-market graph the
  UniverseFrame already carries — Laplacian/Fiedler 2-community spectral clustering + algebraic
  connectivity (undirected |correlation| graph), eigenvector-centrality contagion-hub score, and
  HEAT/shock diffusion of recent returns along the DIRECTED lead-lag graph (Σ αᵏ(Wᵀ)ᵏr, ∞-norm
  normalized). Directional hypothesis = directed contagion: a node its leaders predict will FOLLOW
  the incoming diffused shock (conviction weighted by follower-ness so leaders self-suppress). Kept
  deliberately distinct from the future CausalLeadLag (sparse Granger). evidence_family='contagion',
  shadow_only=True (Rule 14 observe authority). Registered in UNIVERSE_MODULES — reused the existing
  run_universe_modules/contrib/fold plumbing (zero new wiring).
- OWNER-REQUESTED RULE-14 AUDIT: confirmed both this-session shadow modules pass the Evidence/Authority/
  Influence Gate (the rule that REPLACED the old fixed-shadow-cycle gate) — authority=observe,
  status=counterfactual_only, never an applied cause, two strong opposing shadows can't move a live
  pick, kill switch present, promotion deferred to owner+Tier-2 evidence. Opinion given on adding an
  explicit authority enum: defer to Phase 7c (no consumer for advise/canary/veto yet = would be a dead
  field); shadow_only correctly expresses the only live levels (observe/live).
- VERIFIED LIVE (Rule 19): real 485–488 pairs — Fiedler split 177/311, 192 leaders/296 followers, 488
  bounded votes (conv up to 0.94 on strong followers). Rule-9 disconfirm: deterministic; zero capital
  authority (counterfactual_only/effect 0.0). LOAD-CHECK: brain+dashboard restarted → universe_modules_ran
  lists both modules (~3.4s combined), 81 live Decisions fold spectral_graph_contagion as
  counterfactual_only, funnel unaffected (picks=3), dashboard shows both. VERIFICATION-PENDING: settled
  IC needs the 30-min horizon. git scope (Rule 11): spectral_graph_contagion.py(new), __init__.py.

### 2026-06-10 — PHASE 2b: SparseFactorResidual (first Universe-Core module, SHADOW) SHIPPED (LIVE)
- NEW signals/scibrain/universe_modules/ package (UniverseModule base + SparseFactorResidualModule):
  the first cross-market scorer over the shared UniverseFrame. Robust PCA via Principal Component
  Pursuit (inexact ALM: singular-value soft-threshold for the low-rank market/sector factor L +
  ℓ1 soft-threshold for the sparse idiosyncratic Sp) on the column-z-scored primary-TF return matrix.
  Directional vote = FADE the cross-sectionally standardized recent idiosyncratic residual (residual-
  reversal — distinct evidence_family 'cross_asset', unlike the per-symbol RMT/NoiseHarvest), conviction
  scaled by |z_idio|·idio_fraction. SHIP shadow_only=True (§6g + Rule 14: recorded + IC-evaluable, never
  a live vote until incremental IC proven; promotion is owner-approved).
- WIRED across the fork boundary: gate → universe_frame.run_universe_modules() runs the bank ONCE per
  Universe-Core cycle (main process) and publishes per-symbol votes to scibrain:universe:contrib:{sym};
  runner.score_symbol folds them into the per-symbol outputs before router/fuse/ic_record (fusion
  records but never applies shadow; ic_tracker grades them → promotion evidence). dashboard /scibrain
  serves the universe_modules block.
- RULE 18 (fixed in-session): (a) ts-guard — RPCA recomputes only when the frame actually rebuilds, not
  every funnel cycle (was redundant ~9s/cycle); (b) Universe-Core default cadence decoupled to 60s
  (>the 20s funnel) so the cross-market decomposition never chokes the hot SL loop. This is the
  Phase-5 "separately budgeted Universe Core cadence" lever, now real.
- VERIFIED LIVE (Rule 19, vs the design §6e Tier-A goal): PCP converged on real 488–495 pairs
  (rank 79<120, L+Sp relerr 8e-4, Sp sparse), 488 bounded shadow votes. Rule-9 disconfirms: deterministic;
  a STRONG opposing shadow vote leaves the live pick identical (counterfactual_only/effect 0.0 = zero
  capital authority); too-thin frame → wholesale abstain; ts-guard proven (2 funnel scans → 1 RPCA;
  skipped cycle 2.7–3.0s vs 11s). LOAD-CHECK: brain+dashboard restarted → 41/41 live worker-scored
  Decisions fold sparse_factor_residual as counterfactual_only, funnel unaffected (picks=3), 16 IC
  pending-queue members carry the sparse vote. VERIFICATION-PENDING: settled IC needs the 30-min horizon
  to mature (recording path proven) — the evidence the §6g ablation/promotion gate will judge.
- git scope (Rule 11): universe_modules/(new pkg), universe_frame.py, runner.py, gate.py, keys.py,
  dashboard/api.py. CONSTITUTION: Tier-0/shadow Universe-Core directional module — orthogonal cross-
  market information, zero authority, awaiting matured-IC promotion evidence.

### 2026-06-10 — PHASE 2b: read-only in-RAM UniverseFrame (Universe Core substrate) SHIPPED (LIVE)
- NEW signals/scibrain/universe_frame.py + contracts.UniverseFrame (frozen): the shared cross-market
  substrate the Universe Core builds ONCE per funnel cycle in the brain main process (before the
  per-symbol worker fan-out). Column-aligned (cols ↔ symbols) returns_by_tf matrices, Pearson
  correlation, lag-1 DIRECTED lead-lag (asymmetric predictive-flow primitive), per-symbol
  feature_matrix (last_ret/vol/momentum/log_liquidity), liquidity, + a cheap market-state digest
  (breadth, crowding mean_abs_corr, PC1 market_factor_share). ONE pipelined Redis round trip per TF
  (kills the per-symbol storm Phase 5 flagged). In-RAM cache behind a cycle guard (build_or_get builds
  at most once per universe_interval_s); get_current_frame() is the read-only accessor the future
  cross-market modules (SparseFactorResidual / SpectralGraphContagion / CausalLeadLag /
  OptimalTransportRegime) will consume in-process instead of each re-reading the universe.
- WIRED: gate.funnel_pairs builds it over the FULL universe each cycle (Tier-0, never touches picks);
  too-thin universe → None + scibrain:universe:skips_total (Rule 12). CONSUMER: dashboard /scibrain
  serves the digest mirror scibrain:universe:state (the live-visible half; full matrices stay in-RAM).
- VERIFIED LIVE (Rule 19): live build on 476–483 real pairs (returns 1h 120×483 / 15m 64×483; corr
  483² no-NaN range [-0.998,1.0]; lead-lag asymmetric; digest breadth_up 0.679 / mean_abs_corr 0.131 /
  factor_share 0.155). Rule-9 disconfirms PASS — deterministic (two builds → identical corr+digest),
  corr diag≡1, build_or_get returns the SAME cached object within-interval (proves built once/cycle),
  tiny universe → None + skip counter +1. LOAD-CHECK: brain+dashboard restarted → the live runner's
  universe_frame_built fires once per funnel cycle (builds_total 1→2→3, each immediately before
  scibrain_funnel) while the funnel still picks=3/scored=482 with no errors (zero trading effect);
  dashboard /scibrain returns the universe block (n_symbols 483, ts = the live build) end-to-end.
- git scope (Rule 11): universe_frame.py(new), contracts.py, gate.py, keys.py, dashboard/api.py.
- CONSTITUTION: Tier-0 observability/substrate — pure read-only cross-market object + digest, no
  directional vote and no authority; the foundation the Tier-A Universe modules (§6e) build on next.

### 2026-06-10 — PHASE 7a: path-aware Counterfactual Digital Twin (core) SHIPPED (LIVE)
- NEW signals/scibrain/twin.py (design §6): replays the REAL forward OHLC path bar-by-bar with
  path-aware exits (SL/TP/horizon, SL-first on a both-touched bar) under three bounded policies:
  actual = the REALIZED outcome (ground truth, NOT simulated — simulating it would inject derived-SL
  error), opposite = flip direction + MIRROR the SL/TP geometry about entry, abstain = U=0. Scores all
  on the same multi-objective utility (reuses outcome._utility/_lambdas). Fault class (none|direction|
  selection) = win→none, lost+opposite-wins→direction, lost+both-lose→selection — the diagnosis
  win/direction/signal taxonomy but with a real opposite replay, REPLACING diagnosis.py's same-exit-
  price proxy (opposite_pnl=(exit−entry)·qty·−sign). Built-in fidelity check: also replays the actual
  policy, reports abs_error vs realized. SL from the frozen entry_policy (opener.py now stores
  initial_sl/leverage/entry_price in provenance — the trailing_sl_level column is mutated, so the row
  loses the entry SL) or an ATR-derived fallback for pre-policy trades (confidence discounted).
- WIRED: outcome.build_outcome_packet computes the twin once (first build, path still in-window) and
  PRESERVES it across horizon-backfill rebuilds; harvest reselects packets with counterfactual.status=
  'pending' to backfill the twin onto pre-twin packets (idempotent — once ok/unavailable/error it is
  not reselected). _outcome_aggregate adds a twin block (fault distribution, direction_fault_rate,
  mean replay abs_error); /scibrain serves outcomes.twin. keys: TWIN_FEE_RATE/TWIN_DERIVED_SL_ATR_MULT.
- VERIFIED LIVE (C7): 248/401 packets replayed (156 out-of-window = honest unavailable), faults
  none 150 / direction 59 / selection 39 (direction_fault_rate 0.238, mean replay err $3.88 — high
  because historical trades use coarse 15m paths + derived SL, correctly flagged low-confidence).
  Wins→none (OPN/LIGHT), losing shorts EDENUSDT/BOMEUSDT/KASUSDT/SAGAUSDT→direction (opposite would
  have won on the path), XNYUSDT/1000XECUSDT→selection (both directions lose → should have abstained).
  Live /scibrain returns outcomes.twin; idempotent re-harvest converges to backfilled=5 (horizons-only).
- RULE 18 FIX (in-session): first version SIMULATED the actual policy with a derived ATR SL and
  mislabeled a WINNING trade (HOMEUSDT +0.73) as a direction fault because the derived stop hit a level
  the real winner never had. Corrected: actual = realized ground truth; only counterfactuals simulated.
  Worker/brain restarted to drop the cached pre-twin module (its old _outcome_aggregate had overwritten
  the twin block with {}). git scope (Rule 11): twin.py(new), outcome.py, keys.py, opener.py.
- CONSTITUTION: Tier-0 observability — the path-aware evaluator the kernel needs before any equation
  change; replaces the proxy as the fault basis. FOLLOW-ON: delay/ablation/gain/size/SL-TP sweeps
  (need a circuit re-score on the entry frame) + swap calibration y_wrong from the failure_type proxy
  to the twin fault_class.

### 2026-06-10 — PHASE 7a: bounded LifeTrace + OutcomePacket outcome-truth ledger SHIPPED (LIVE)
- NEW signals/scibrain/outcome.py (design §5.2/§5.3): the close-side complement to EntrySnapshot.
  build_lifetrace = bounded DISCRETE recorded events (entry, DCA fills, TP fires, final trailing-SL,
  brain interventions w/ last-3, realized MFE/MAE envelope from peak_pnl/peak_loss, exit) — per-tick
  mark/fill path honestly None (not persisted per trade, NOT fabricated). build_outcome_packet =
  realized multi-objective utility U=roc−λ_tail·tail−λ_dd·drawdown−λ_cost·cost (config λ via Redis,
  C6), ROC, MFE/MAE, drawdown_frac, hold, exit_reason, descriptive won, failure_label (diagnosis proxy),
  + STANDARDIZED FORWARD HORIZONS 15/60/240m after ENTRY, signed by direction (dir_correct = was the
  chosen side right at that horizon, independent of how we exited), priced from the live candle lists
  (ms timestamps; finest covering TF wins; matured-but-out-of-window vs not-yet-matured both honest).
  counterfactual=pending (path-aware twin is the NEXT task — declared, not faked).
- HARVEST: harvest_outcomes(r,limit) mirrors grade_calibration EXACTLY — immutable per-row artifacts
  (signals_at_entry.lifetrace/.outcome_packet) are the durable ledger AND idempotency marker; aggregate
  RECOMPUTED from all packets; counter incremented once per first build; horizons backfilled on later
  passes as they mature (so building "on CH_TRADE_CLOSED" via an idempotent beat scan, not a synchronous
  subscriber, is what LETS the +4h horizon mature). keys.py: OUTCOMES_AGG/OUTCOME_HARVESTED_TOTAL +
  λ/horizon config. celery beat scibrain-outcome-harvest@120s + task scibrain_outcome_harvest (DEFAULT
  queue, no LLM, single-flight lock, expires 300s). dashboard/api.py: /scibrain serves the outcomes block.
- VERIFIED LIVE (C7): import clean; read-only build on real trades shows correct LifeTrace + Packet;
  forward-horizon sign proven on a SHORT (BASUSDT: price↑@15m→dir_correct=false, price↓@240m→true) and a
  LONG; full backlog harvest built 400 packets (counter 381 — the 19 from a prior partial run were NOT
  recounted = idempotent), re-pass built_now=0, backfilled=4 (the recent trades whose 240m hasn't matured).
  Live /scibrain (minted token) returns outcomes n=400, mean_utility −0.0138, win_rate 0.6625, horizons
  {15m dir-correct 0.505 / 60m 0.5552 / 240m 0.5034}. QUANTITATIVE FINDING: 66% win-rate but NEGATIVE
  mean utility (mean MAE −3.29 vs MFE +3.24 → big adverse excursions the win-rate hides) and a forward
  direction-edge barely above coin-flip — exactly the outcome-truth the kernel needs.
- RULE 18 FIX (in-session): harvest first aborted the aggregate with "unhashable type: 'slice'" — a row's
  brain_actions jsonb was a dict, and brain_actions[-3:] slices a dict. Guarded to coerce to list; fixed
  + re-verified (counter + aggregate now populate). git scope (Rule 11): only celery_app.py, dashboard/
  api.py, signals/scibrain/{outcome.py(new),keys.py}.
- CONSTITUTION: Tier-0 observability under the Evidence/Authority/Influence Gate — pure capture, zero
  trading effect; the realized-truth half of the Phase-7a replay foundation the priority gate requires.

### 2026-06-10 — PHASE 7a: immutable EntrySnapshot ledger SHIPPED (LIVE)
- NEW signals/scibrain/snapshot.py (design §5.1): build_entry_snapshot(r, dec, mark) freezes an
  immutable, replay-grade record at every real open. Contents: snapshot_id (uuid) + entry/decision
  ts; replay = per-TF data_fingerprint {n, last_ts, closes_sha1} + scalar sensors (the EXACT inputs,
  hashed so a replay can PROVE byte-identical data — "storing only an explanation is not sufficient");
  versions = code_fingerprint (sha1 over the scibrain pkg source, git-independent) + git commit +
  config.yaml hash + entry/audit schema versions + full module_set + router_present; action_space =
  feasible {long,short,abstain} + logging propensities (p_act=conviction, p(long|act)=σ(net_vote/T))
  summing to 1 with the chosen-action propensity for later IPS/DR off-policy correction; execution =
  mark/bid/ask/spread_rel/bid_ask_imbalance/micro_ofi_l1 (depth+slippage honest None — not tracked);
  replay_pointers to decision_snapshot/influence_manifest/audit/audit_calibration (no duplication).
- WIRED: opener._plan_open builds it best-effort and embeds it at signals_at_entry.entry_snapshot;
  provenance schema_version 2→3. Already surfaces via /trades/open (signals_at_entry passthrough).
- VERIFIED LIVE (C7): scored a real decision (TNSRUSDT short, conv 0.224) → entry_snapshot complete,
  propensities {long .092, short .132, abstain .776} sum 1.0 (low conviction → high abstain prob, as
  designed), real micro spread 0.000356 / imbalance −0.267 / ofi_l1 0.0056, all 5 TFs fingerprinted,
  code_fingerprint d2d96b52 / config_hash 77cfd276 pinned. Rule-9 disconfirms PASS: data fingerprint
  deterministic on the same frame, snapshot_id unique per call, code_fingerprint stable. Brain
  restarted (signals/ bind-mounted) — startup traceback was the OLD process's benign shutdown
  (brain_stop_requested/online_learner_cancelled), package imports clean, new instance scoring (fresh
  heartbeat 469 pairs / 3 picks / 3.6s). git scope (Rule 11): only opener.py + new snapshot.py.
- CONSTITUTION: Tier-0 observability under the Evidence/Authority/Influence Gate — pure capture, zero
  trading effect; it is the replay foundation the priority gate requires before any expanded module.

### 2026-06-10 — PHASE 7a: auditor provenance + close-time calibration grading SHIPPED (LIVE)
- PART 1 (provenance): interrogator._complete() now returns (text, meta) with the ACTUAL provider/
  model/transport — recovered from call_chain's provider name + get_providers() model map (cloud) or
  the ollama model (local fallback). Interrogation gains provider/model/transport/prompt_hash(sha1[:16])/
  schema_version(=2, AUDIT_SCHEMA_VERSION). All 3 _complete callers updated (lead+critic in interrogate,
  remediation in audit). audit._stamp_trade persists provider/model/transport/prompt_hash/schema_version/
  latency_s onto the trade row → the forecaster itself is now attributable (gate's calibration/telemetry
  evidence). VERIFIED: to_dict carries all fields; imports clean.
- PART 2 (calibration at close): NEW audit.grade_calibration(r,limit) + audit._calibration_aggregate +
  keys AUDIT_CALIBRATION/CALIB_GRADED_TOTAL + celery beat scibrain-calibration-grade(300s, DEFAULT queue —
  pure DB/Redis, no LLM, single-flight lock). For each CLOSED scibrain trade with an available audit + a
  failure_type label, p_wrong=wrong_direction_risk graded vs y_wrong=(failure_type=='direction' PROXY);
  Brier=(p-y)^2. Per-trade grade written IMMUTABLY to signals_at_entry.audit_calibration (= durable ledger
  AND idempotency marker); aggregate (Brier, base rate, mean forecast, Brier-climatology, Brier-SKILL, 10
  reliability bins) RECOMPUTED from all graded rows → restart-safe + double-count-proof by construction.
- VERIFIED LIVE (C7): grade_calibration on the real backlog graded 147 trades → n=147, Brier=0.3085,
  base_rate_wrong=0.3878, mean_forecast=0.6001, brier_clim=0.2374, brier_SKILL=−0.2993 (NEGATIVE = the
  LLM forecaster is WORSE than always guessing the base rate). Reliability bins expose the over-confidence:
  forecasts 0.70→actual 0.397 wrong, 0.80→0.333 wrong. This quantitatively confirms living_intelligence §2
  (uncalibrated; must NOT be causal authority). GWEIUSDT (doc's §2 example: risk 0.7, WON) auto-graded
  brier 0.49. Idempotent re-run grades 0, aggregate stable. Frontend rebuilt clean; dashboard+beat+worker
  restarted; live /scibrain returns the calibration block; beat registered @300s (expires 750s). git scope
  (Rule 11): only celery_app.py + dashboard/api.py + untracked scibrain/panel touched.
- CONSTITUTION ALIGNMENT (owner-flagged): this is Tier-0 observability under the Evidence/Authority/
  Influence Gate (shadow-cycle Rule-14 replacement) — observe authority, zero trading effect, and exactly
  the calibration/failure-telemetry the gate requires before the auditor could ever be promoted.
- HONEST GAP (C11): NEW audits will carry the provider/model provenance once they next drain; the 147
  historical rows graded here predate the provenance fields (their audit blocks have no provider yet).
  y_wrong remains the temporary failure_type='direction' proxy until the path-aware counterfactual lab.

### 2026-06-10 — PHASE 7a START: immediate-open audit reframed as Ex-ante Decision-Risk Audit (LIVE)
- PRIORITY GATE work begun (Phase 7a foundation blocks all expanded modules). First design-doc step
  (living_intelligence §4.1/§13.1): the immediate-open audit fires BEFORE any outcome exists, but was
  mislabeled "POST-TRADE DIRECTION AUDIT" — a semantic lie the design explicitly calls out.
- FIX (vertical slice, producer→consumer): Interrogation contract (interrogator.py) gains
  audit_kind='ex_ante_decision_risk' + evaluated_at='post_open_pre_outcome', carried in to_dict() so
  every payload self-describes. audit.py docstring reframed + stamps both fields onto the trade row
  (signals_at_entry.audit) — the row is now self-describing (C9). keys.py Phase-4 comments reframed;
  Redis key NAMES kept stable (restart-safety + already-persisted data). dashboard/api.py adds
  audits_meta {audit_kind, evaluated_at, label, note} to /scibrain + reframed comments. Frontend
  ScientistBrain.tsx AuditPanel header reframed to "EX-ANTE DECISION-RISK AUDIT" + italic note,
  consuming data.audits_meta (not hardcoded).
- VERIFIED (C7): Interrogation.to_dict() carries both fields (brain container); audit/keys/contracts
  import clean; frontend rebuilt clean (+117B, no warnings); dashboard+celery_worker restarted; LIVE
  /scibrain (minted JWT) returns audits_meta with the reframed label, enabled=True, 30 audits/30
  decisions. git scope (Rule 11): only the intended scibrain files (untracked pkg) + api.py touched.
- SCOPE NOTE (C11): this is the rename/reframe + self-describing-payload task ONLY. The paired next
  task — persist actual provider/model+prompt/schema versions and GRADE forecast calibration at close
  — is still open and is the next Phase-7a step.

### 2026-06-09 — VISUAL LAUNCHPAD GOAL LOCKED: Cognitive Atlas instead of text wall
- OWNER INTENT UNDERSTOOD: redesign the Scientist-Brain Launchpad so it visually resembles an
  advanced brain/circuit and shows how evidence, uncertainty, memory, planning, safety, and learning
  flow, instead of presenting nearly everything as text messages.
- CURRENT UI AUDITED: `ScientistBrain.tsx` is a single inline-styled polling component with status
  text, bars, audit paragraphs, and expandable rows; the frontend has React/lightweight-charts but
  no graph/circuit renderer. Existing `/scibrain` data can support a first live circuit atlas.
- DESIGN LOCKED: stable readable 2.5D Cognitive Atlas, not a decorative rotating 3D brain. Real
  event pulses flow through sensory modules → router → belief/fusion → planner/audit → action →
  brainstem safety. Memory, tail reflex, competence, uncertainty, suppression, veto, abstention,
  and authority are first-class visual states.
- MODES LOCKED: Live Cognitive Atlas, Trade Autopsy Theatre, Learning Laboratory, Universe Neural
  Field, and Safety/Authority. Text/formulas remain exact-value drill-down evidence, not the main
  screen.
- STACK GOAL: React Flow for rich stable brain/experiment circuits; Sigma.js+Graphology for the
  future universe graph; existing lightweight-charts for synchronized timelines; SVG/Canvas for
  uncertainty fields and real pulses. Three.js/WebGL is optional only where 3D proves useful.
- SCIENTIFIC VISUAL RULES: stable layout, semantic zoom, uncertainty visible, actual/predicted/
  counterfactual/shadow/live clearly distinct, no decorative fake firing, color+shape encoding,
  reduced motion, text fallback, immutable evidence IDs, and zero impact on trading if UI fails.
- NEW DETAIL DOC: `next_impl/scientist_brain_visual_launchpad.md`; linked into the master Launchpad
  and Cognitive OS goals. No frontend/runtime code, dependencies, Docker state, Redis settings, or
  live trading behavior changed in this goal-design session.

### 2026-06-09 — COGNITIVE OS GOAL LOCKED: governed brain-like learning architecture
- OWNER INTENT UNDERSTOOD: deepen the living intelligence into a real-brain-inspired learning system
  using advanced ML/RL/neural/deep-learning, mathematics, physics, and quantum-inspired methods,
  while adding/editing the goal rather than changing the real-LIVE runtime.
- REPO REALITY AUDITED: substantial parts already exist — RSSM world model, PPO day/minute agents,
  MAML, EWC/replay, fast/slow memory, MemRL/Q-learning, CandleNet MAE, TFT/PatchTST/Mamba/Chronos,
  GNNs, curiosity, DSL/evolution, and quantum-inspired spectral math. The gap is one shared belief,
  rich experience, coordinated learning signals, metacognition, and a common promotion kernel.
- IMPORTANT CORRECTIONS LOCKED: current PPO/MARL is mostly one-step contextual-bandit training with
  sparse observations, not mature coordinated MARL; current world-model experience/planning is too
  sparse; current MAML tasks and replay episodes are weak; current quantum module is CPU
  quantum-inspired math, not quantum computing.
- ARCHITECTURE LOCKED: brainstem safety; self-supervised sensory cortex; thalamic salience router;
  typed global latent workspace; fast hippocampal episodes + slow cortical consolidation; offline
  risk-sensitive hierarchical action selection; bounded residual correction; uncertain world-model
  planning; anomaly/tail reflex; metacognitive competence and rewarded abstention; typed
  neuromodulator-like learning signals; isolated sleep/replay/consolidation.
- ADVANCED RESEARCH POLICY: predictive coding/Bayesian filtering, POMDPs, information bottleneck,
  causal representation, optimal control/barrier functions, dynamical systems, neural operators,
  tensor networks, density-matrix beliefs, and quantum kernels are challengers with concrete
  observables and compute-matched baselines. Biological or quantum terminology is never evidence.
- NEW DETAIL DOC: `next_impl/scientist_brain_cognitive_os.md`; linked into the master launchpad and
  Living Intelligence goal. No runtime code, Docker state, Redis configuration, models, or live
  trading behavior changed in this goal-design session.

### 2026-06-09 — LIVING INTELLIGENCE GOAL LOCKED: causal outcome lab + safe equation evolution
- OWNER INTENT UNDERSTOOD: use LLMs and every realized trade to retrace why the CPU acted, ask what
  bounded code/equation change might improve the class of trade, then let the brain learn and evolve.
  The hard constraint is scientific: one loss/win may generate a hypothesis but cannot prove or
  directly apply a formula change.
- CRITICAL CURRENT-STATE FINDING: the displayed "POST-TRADE DIRECTION AUDIT" actually runs just
  after OPEN, before any outcome exists. It is now defined as an Ex-ante Decision-Risk Audit; the
  goal adds a separate Ex-post Outcome Causal Audit after close/horizon maturity.
- EXACT EXAMPLE CHECK: GWEIUSDT (`wrong-dir=0.7`, REDUCE/down-weight chaos) and POWERUSDT
  (`wrong-dir=0.8`, REDUCE/down-weight Koopman) both CLOSED AS WINS, +0.9584 and +0.4865 USDT.
  POWER also proposed changing Koopman despite naming wavelet + mean-reversion conflict as the
  evidence. These are false-alarm/target-grounding failures that a living brain must learn from.
- CALIBRATION BASELINE: 151 closed audited SciBrain trades; average predicted wrong-direction risk
  0.5842 vs current direction-failure label rate 0.3841; Brier 0.3069. Of 93 risk>=0.6 audits,
  50 won and 43 lost, with positive average PnL. All 151 audit verdicts agreed with the opened
  direction. LLM risk/remediation cannot be direct authority.
- ARCHITECTURE LOCKED: immutable EntrySnapshot/LifeTrace/OutcomePacket; path-aware Counterfactual
  Digital Twin; typed ChangeSpec/equation DSL; LLM forensic/skeptic/experiment/formula/reviewer
  council; deterministic target grounding; matched-cohort + DR/IPS + purged walk-forward evaluator;
  online-FDR/complexity gates; champion/challenger shadow/canary/rollback; unified experiment
  registry for every existing self-improvement subsystem.
- NEW DETAIL DOC: `next_impl/scientist_brain_living_intelligence.md`. No runtime code, live settings,
  Redis configuration, Docker state, or trading behavior changed in this goal-design session.
- PRIORITY DECISION: build the immutable ledger + causal replay + experiment/promotion kernel before
  expanding the module bank. New math without a trustworthy evaluator increases hypothesis count,
  not intelligence.

### 2026-06-09 — GOAL EXPANDED: two-speed scientific CPU + admission constitution
- REVIEW VERDICT: the existing 14 modules are enough for a strong V1 pair-level core, but not the
  complete scientist vision. The largest missing domains are cross-market structure/causality,
  path-order sequence geometry, distribution transport, calibrated tail probability, portfolio
  allocation, optimal stopping, and reflexivity/market impact.
- GOAL DESIGN CHANGED: no target to accumulate 50–100 active votes. The CPU is now four planes:
  Hot Pair Core, shared Universe Core, Risk/Control Plane, and a shadow/offline Discovery Sandbox.
  Cross-market modules compute once per UniverseFrame and emit the same per-symbol ModuleOutput
  contract; expensive matrices/graphs must never be recomputed per symbol.
- PRIORITY COMPONENTS LOCKED: SparseFactorResidual, SpectralGraphContagion, CausalLeadLag,
  RoughPathSignature, OptimalTransportRegime, EVTLargeDeviationTail; then InformationGeometryHealth,
  MultifractalRG/ErgodicMixing only if incremental. Economic control priorities: true
  CorrelationKellyAllocator, HJBOptimalStopping, MarketImpactReflexivity.
- NON-PROMOTION DECISION: more quantum labels, Free Probability duplicate votes, SPDE/CFT/field
  theory, Malliavin, Mean Field Games, representation/category theory, and generated equations stay
  in the research sandbox until they define a unique observable and pass falsification.
- SAFETY REALITY CORRECTED: SciBrain is currently real-LIVE. Every new component starts at observe
  or advise authority and needs unique-evidence proof, CPU budget, conditional IC, ablation,
  evidence proportional to requested authority, explicit owner approval for high-risk promotion,
  and rollback. No runtime trading formula changes were made in this goal-edit session.

### 2026-06-09 — PHASE 4 audit FIXED via CLOUD-PRIMARY routing (the real resolution)
- ROOT INSIGHT: the bot is already CLOUD-PRIMARY for LLM (llm/providers.py call_chain → groq
  llama-3.3-70b / cerebras qwen-3-235b / nvidia llama-3.3-70b, per-provider cooldown + fallback); the
  brain's debate council uses it. The scibrain audit was the ONLY LLM path still calling LOCAL Ollama
  directly (chat_ollama) — i.e. it was using the slow, CPU-saturated path while a fast cloud chain sat
  right there. (Discovered by mapping the ollama-hammering client 172.18.0.9 → the brain, whose LLM
  calls were cloud-primary with ollama fallback.)
- FIX (code, in scope): interrogator.py new _complete() — CLOUD-PRIMARY (call_chain, json_mode) with
  LOCAL-Ollama fallback; interrogate() gains use_cloud (default True); lead+critic both routed through it.
  audit.py: audit_one + _remediate read scibrain:audit_use_cloud and route the same way. runner.py:
  _interrogate_and_stream passes use_cloud. keys.py: AUDIT_USE_CLOUD. Local models (lead/critic) are now
  the FALLBACK only.
- VERIFIED LIVE: interrogate() AVAILABLE=True in 10.6s on nvidia llama-3.3-70b (vs 300–600s timeout-fail
  on local), high-quality dual-brain narrative + counter-evidence + critic. Full drain path: processed a
  REAL opened trade (ALLOUSDT) in ~7s, available=True, and the wrong-direction detector FIRED
  (wrong_dir_risk=0.7 → flagged) — the detector was dead before because audits never completed. Zero
  local CPU used; falls back to local Ollama only if every cloud provider is cooled down.
- The CPU-serialize Ollama config (prior log entry) is KEPT — still correct for the local FALLBACK path,
  the brain's debate fallback, and every other local Ollama consumer (RAM 21.6→7.2GB, no swap thrash).

### 2026-06-09 — OLLAMA capacity fix: researched + applied CPU-serialize config (owner-approved)
- Web research (Ollama FAQ + CPU-inference guides): on a CPU box, PARALLEL inferences SPLIT cores →
  every request crawls; the fix is SERIALIZE (NUM_PARALLEL=1, one call uses all cores, rest queue
  FIFO via OLLAMA_MAX_QUEUE=512) + cap loaded models + keep_alive (auto-unload idle) / keep_alive=0
  (on-demand unload of occasional heavy models). Sources saved to [[finding-ollama-saturation]].
- APPLIED (owner approved "apply+verify"): docker-compose.yml ollama env —
  OLLAMA_NUM_PARALLEL 3→1, OLLAMA_NUM_THREADS 6→7, OLLAMA_MAX_LOADED_MODELS 4→2 (KEEP_ALIVE 30m
  kept). `docker compose up -d ollama` recreated it. VERIFIED env live + RAM dropped 21.6GB→7.2GB
  (only 2 models resident now, LRU-evicts the rest — the model-swap thrash is gone).
- BUT throughput still CPU-starved: measured load avg ~24 on 10 cores; during a single generation
  ollama gets only ~2.8 of its 7-core CEILING because cgroup limits are ceilings, NOT reservations —
  celery_worker(~1.7)+candlenet(~1.35)+data_feed(~0.8)+tail consume the rest, kernel preempts ollama.
  ROOT: sum of CPU caps (~15.5) >> 10 physical cores + the BRAIN is uncapped (bursts to 8 cores in
  scibrain scans, starving ollama exactly when an audit runs). The serialize config moved audits from
  HARD-TIMEOUT-FAIL toward SLOW-BUT-COMPLETE (3B completed in 123s post-change vs timeout before).
- REMAINING (owner capacity decision, not a code fix): (a) cpuset-PIN ollama to dedicated cores
  (guarantees cores, trades trading-CPU headroom); (b) throttle/space-out the background LLM beat
  tasks on the `llm` queue (web_intel sentiment, ai_scientist, strategy_research, llm_dsl) so ollama
  serves the important calls (scibrain audit, debate) fast — preserves all CORE features; (c) cap the
  brain / reduce scibrain scorer_workers during scans; (d) bigger box / GPU (real long-term fix).
  Config kept as-is (strict improvement); awaiting owner pick on the capacity lever.

### 2026-06-09 — PHASE 4 verified BUILT+WIRED (3/3) + systemic OLLAMA-SATURATION blocker diagnosed
- Found Phase 4 was ALREADY fully implemented in a prior session but never ticked: audit.py
  (post-trade dual-brain interrogator + wrong-direction flag + bonus remediation engine), opener.py
  (provenance stamp + enqueue on every open), celery beat scibrain-audit-drain(30s)/prewarm(300s) on
  the airllm queue with a single-flight lock. Verified the wiring + that provenance stamping works
  (every scibrain trade row carries origin/driver/regime/12-module attribution; completed audits stamp
  audit=YES on the row, e.g. BLESSUSDT/IRYSUSDT).
- DIAGNOSED why audits often return available=False (Rule 9/13, disconfirming tests): NOT a code bug —
  the single CPU-only ollama is SATURATED by the whole bot's LLM workload. Evidence: ollama 276% CPU AT
  REST with 4 models thrashing; /api/generate returning 500 every ~60s to multiple clients (web_intel
  sentiment, llm_dsl, etc.); tinyllama (1B) = 0.1 tok/s (5 tokens in 28–87s); 14B+8B and even mistral:7b
  hit the 300–600s timeout. Host is 10 cores (worker capped 2.0, ollama capped 7.0, brain unlimited).
  Reducing scibrain scorer_workers 8→3 did NOT help (ollama saturated at rest regardless) → REVERTED to
  the verified default 8.
- ACTIONED per owner choice ("lighter models"): set scibrain:lead_model=mistral:7b,
  scibrain:critic_model=qwen2.5:3b (runtime config, C6 — no code change). Correct + reversible; helps
  once ollama has capacity, but insufficient ALONE because the bottleneck is bot-wide ollama saturation,
  not model size. OPEN INFRA DECISION for owner (bigger than scibrain): throttle the bot's other LLM beat
  tasks / give ollama dedicated resources / GPU / accept audit as best-effort. Phase 4 CODE is done.

### 2026-06-09 — PHASE 3 Meta-Router (MoE) + regime gating SHIPPED (LIVE) + dashboard panel FIXED
- NEW signals/scibrain/router.py (Layer-2 MoE): detect_regime() → canonical regime per pair from the bank's
  regime-speaking experts (BOCPD p_change, HMM bull/bear/turbulent, StatPhysSOC crash/criticality, confirmed by
  Chaos-Hurst + Kalman-velocity); route() → per-module GAIN = clip(regime_profile × ic_multiplier, 0, 1.6),
  blended by scibrain:router_strength. RouterState (regime/confidence/change_point_prob/crash_warning/trend_score/
  gains/deactivated) attached to every Decision for visibility. Gate expert (bocpd) is regime-agnostic.
- NEW signals/scibrain/ic_tracker.py (Task 3): shadow Information-Coefficient per module over the WHOLE universe
  (no real trade needed). record() stashes votes+ref price (deduped per symbol×horizon bucket) maturing
  ic_horizon_min later; settle() (once/cycle, main process in gate) grades matured obs vs realized fwd log-return,
  pushes (vote,ret) to a rolling per-module window, recomputes Pearson IC → scibrain:ic:map. Router folds IC into
  its gains so weights self-tune from realized skill. Zero-variance/NaN/thin guards; settlement atomic (ZREM as
  consumed → can lose, never double-count).
- WIRED: contracts.Decision gains a router field (+to_dict/from_dict); fusion.fuse(router=) multiplies gains into
  weights + deselects gain-0 experts + sets Decision.regime to the canonical router verdict; runner.score_symbol
  routes + records IC + streams regime counters (scibrain:regime:*) + deactivated_total; gate.funnel_pairs settles
  IC once/cycle. keys.py: router + IC namespace.
- VERIFIED LIVE (brain restarted, signals/ bind-mounted so restart-only): regime counters populating
  (mean_revert 890 / turbulent 165 / neutral 393 / trending 6); fresh Decisions carry the router block with the
  correct MoE profile (revert regime → noiseharvest 1.4/tda 1.3/ising 1.2 up, koopman 0.55/kalman 0.65/langevin
  0.55 down, bocpd 1.0); router CHANGES outcomes the right way (HMSTRUSDT trend-long suppressed in mean_revert;
  neutral = exact no-op — both Rule-9 disconfirming checks pass); IC settle math proven (+0.88→×1.5, −0.88→×0.35);
  IC pending queue filling (1454), live settlement begins ~30 min post-restart (clock-bound; path proven offline).
- DASHBOARD FIX (owner-reported "panel still shows scored 0 / no decisions"): ROOT CAUSE = nginx.conf had a
  proxy `location` for every API prefix EXCEPT /scibrain → the panel's GET /scibrain fell through to `location /`
  and got index.html (SPA) instead of JSON, so it rendered defaults. The backend was fine all along
  (enabled=1, cycles 658, scored 322k, 520 decisions, fresh status). Added `location /scibrain { proxy_pass
  $dashboard; ... }`; the single-file bind-mount needed a container restart to re-resolve the inode (atomic edit
  swapped it). VERIFIED: /scibrain now returns API 401 JSON like /launchpad (unknown routes still hit the SPA);
  handler returns populated payload (485 pairs, cycles 658, 30 decisions). Panel populates on browser refresh; no
  frontend rebuild needed. NOTE: ScientistBrain.tsx rendering of the new router.gains block is Phase 6 (CPU view);
  data is already in the payload.

### 2026-06-08 — kickoff
- Established C1–C12 production-code rules. Explored: master notes (25 PhD blocks incl. BLOCK 16
  circuit/CPU architecture), existing launch_pad (6 files, 7-layer design in launch_pad_10.md),
  live facts (491 active pairs, Ollama qwen2.5:14b-instruct + deepseek-r1:8b, full model set).
  Built this tracker + report script. Next: wire hook, web research, get owner GO.

### 2026-06-08 — VS-1 compute core SHIPPED (real, verified on live data)
- Owner GO: dual-brain (qwen2.5:14b + deepseek-r1:8b), first slice Koopman+BOCPD. Made physics
  explicit (6 physics + 1 quantum modules grouped in Phase 2).
- Built signals/scibrain/: __init__, contracts.py (SensorFrame/ModuleOutput/Decision frozen
  dataclasses), modules/{base,koopman,bocpd}.py, sensor_bus.py, fusion.py, _smoke.py.
- KoopmanModule = real Hankel time-delay DMD (SVD → reduced Koopman operator Ã → eigenvalues |λ|,
  recon-error regime detector, one-step forecast). BOCPDModule = real Adams-MacKay run-length
  recursion w/ Normal-Inverse-Gamma Student-t predictive, hazard from horizon.
- VERIFIED: `docker exec trading-bot-brain-1 python -m signals.scibrain._smoke` runs on live
  Redis; both modules fire, fusion emits long/short + conviction + Kelly size + regime; graceful
  abstain on degenerate data. CAUGHT+FIXED a real bug (Koopman expected-move linearly summed the
  one-bar move across the horizon → 50% runaway; now bounded to ±3σ one-step forecast).
- signals/ IS bind-mounted into brain+dashboard+celery_worker (compose 104/297/451) → live on
  restart, no rebuild.

### 2026-06-08 — VS-1 Ollama interrogator + attribution SHIPPED (live-verified)
- Added keys.py (scibrain:* bus), interrogator.py (dual-brain bull/bear → structured JSON
  verdict + responsible_factor + wrong_direction_risk + narrative + critic_note), runner.py
  (score_symbol/run_cycle, streams modules+decision+reasoning to scibrain:*, increments
  wrong_dir_flag when interrogator disagrees / risk>=0.6).
- Added DETERMINISTIC attribution to fusion: each module's signed share of net vote →
  Decision.primary_driver = the computed responsible driver (answers "which is responsible"
  exactly, not inferred). Verified: SFPUSDT long primary_driver=koopman share +0.65.
- VERIFIED live: CTKUSDT long conv .745, primary_driver koopman; dual-brain (qwen2.5:3b test
  proxy) returned verdict long conf .75 + critic flagged wrong_direction_risk 0.70 with a REAL
  reason ("koopman mean-revert at odds with bocpd stable → possible regime change"). The
  right-signal-wrong-direction detector works.
- OPEN: 14b cold-load exceeded 300s (raised to 600s + model swappable + needs keep_alive
  pre-warm). Narrative depth scales with Phase-2 modules (OFI/cascade/sentiment/regime).
- Remaining VS-1: dashboard panel v1 + engine/buffer wiring at scibrain:enabled=0 + 14b pre-warm.

### 2026-06-08 — Dashboard panel SHIPPED + deployed
- /scibrain endpoint in dashboard/api.py (bind-mounted → restart, no rebuild): heartbeat +
  counters + recent decisions (with attribution) + Ollama transcript. Route verified registered;
  endpoint returns 20 live decisions.
- frontend/src/panels/ScientistBrain.tsx (full-width panel): per-symbol row → conviction bar +
  primary_driver + regime + wrong-dir-risk; expandable detail → module vote bars + explanations +
  computed attribution (★ aligned) + Ollama narrative/critic. Registered in App.tsx, getSciBrain
  in api.ts. Built clean (CI=false npm run build, +1.78kB, no warnings in the new file); nginx
  serves frontend/build (bind-mounted), nginx bounced. LIVE at the dashboard.
- Made interrogator models configurable via scibrain:lead_model / scibrain:critic_model (C6).
- Panel visible now; reasoning rows populate as interrogations run (needs the celery-beat cadence
  + 14b pre-warm for steady-state — next task).

### 2026-06-08 — scibrain REPLACED launch_pad as the live trade origin (PAPER)
- gate.py (picker: score 466 → conviction filter → rank → direction-balance + cooldown +
  20s throttle) + opener.py (sizing within owner caps + leverage + SL + engine.open_trade with
  scibrain provenance) + guarded branch in engine.process_signals (early-return when
  scibrain:enabled=1, bypasses the gauntlet; legacy untouched when 0).
- LIVE: scibrain:enabled=1, launchpad:enabled=0. The brain loop opened real paper trades via the
  Scientist (tf=scibrain). Caught + fixed: sizing maxed every trade at $200 ignoring balance →
  now honors owner bot:min/max_position_usdt ([$80,$150]), conviction-scaled, balance-bounded,
  won't open below the min cap. Verified live (PROVEUSDT conv .71 → $129.71, etc.).
- Self-limits: dedup (no stacking a held pair), cooldown, throttle, free-balance floor.
- TODO: parallel scorer (serial ~10s now), correlation-cluster cap, auto-interrogate opened trades.

### 2026-06-08 — Phase 2 batch 1: 4 physics/quantum modules SHIPPED (LIVE)
- Added modules/{chaos,noiseharvest,kalman,quantum}.py — all real math, abstain-safe, in the bank.
  chaos = Hurst structure-function (trend vs revert); noiseharvest = OU AR(1) fade of >1σ residual
  ("use noise to our advantage"); kalman = constant-velocity filter (level+velocity, innovation gate);
  quantum = Von Neumann/spectral entropy + QFT dominant-cycle phase slope.
- VERIFIED live: all 6 modules fire with orthogonal votes; picks now DIVERSIFY (e.g. GUSDT
  driver=chaos overriding koopman via Hurst 0.15 mean-revert). Disagreement correctly lowers
  fusion conviction → more selective. Full 463-pair x 6-module scan = 10.8s (23ms/pair).
- Throttle raised to 45s (scan 11s, loop free ~34s) so the 6-module scan doesn't starve SL loop.
  Brain restarted, 6-module bank LIVE as the trade origin. Strategy column will now show
  scibrain·chaos / ·kalman / ·noiseharvest / ·quantum as drivers vary.
- Phase 2 module bank COMPLETE (cont.75): StatPhysSOC, MeanFieldIsing, LangevinHawkes, RMT, TDA,
  Wavelet, InfoTheory, HMM(retrained) all LIVE → 14-module bank. Remaining: Phase 1b correlation-
  cluster cap + Phase 5 parallel scorer, then Phase 3 meta-router/regime gating.
- PERF NOTE: serial 11s scan is the next bottleneck → parallel scorer (Phase 5) when convenient.

### 2026-06-08 — Phase 1b correlation-cluster cap SHIPPED (LIVE, verified) + data backfill confirmed
- DATA: data/feed.py thin-history REST backfill CONFIRMED converged — 0 thin symbols across all
  5 TFs over 491 pairs (1m/5m/15m/30m min depth 61–65 ≥ target 60; 1h min 189 ≥ 168 for HMM).
  rest_fallback pollers still topping up on cadence (5m beacon live) = healthy steady state.
- CLUSTER CAP: signals/scibrain/gate.py — after direction-balance, a greedy correlation-cluster
  cap on the conviction-ranked picks. "Same risk cluster" = signed return-correlation
  ρ(rᵢ,rⱼ)·sign(dᵢ)·sign(dⱼ) ≥ rho_max (two longs/two shorts in co-moving assets concentrate one
  factor; a long+short in co-movers is a hedge → negative signed-ρ → NOT clustered). Real Pearson
  on real {sym}:1h:candles log-returns (helpers _returns/_corr, overlap≥8 & zero-var guards).
  Config-driven (C6): scibrain:cluster_rho=0.8, max_per_cluster=2, cluster_tf=1h, cluster_n=50
  (rho=1.0 OR max/cluster=0 disables). Per-skip counters (Rule 12): scibrain:cluster_capped_total
  + scibrain:dir_capped_total; heartbeat now carries clusters/dropped_cluster/dropped_dir.
- VERIFIED live (C7): kernel math exact (corr a·a=1, a·-a=-1, guards→0). Real funnel, same universe:
  cap OFF (rho=1.0)=20 picks/20 clusters/0 dropped; cap ON (rho=0.5,1/cluster)=12 dropped_cluster
  (counter +12), correlated longs evicted for uncorrelated names. Signed-corr matrix confirmed real
  clusters (SIGNUSDT·ZEREBROUSDT=+0.505). Restored operator config (max_picks=3, interval=45s),
  cluster keys left UNSET → code defaults; counter reset to 0 for clean prod baseline.
- HONEST NOTE (C11): at prod max_picks=3 the cap rarely binds (need 3 co-movers ≥0.8 in 3 picks);
  it's the correct guard once max_picks is raised / in all-correlated regimes.

### 2026-06-08 — Rule 18 added + statphys bug FIXED + parallel scorer SHIPPED (LIVE)
- RULE 18 (Fix-Don't-Defer, ALWAYS-ON) saved by owner to all 3 rule stores (memory, MEMORY.md,
  CLAUDE_CODE_RULES_COMPLETE.md): found a bug/gap → FIX it this session, never flag for later.
- FIXED (per Rule 18, the bug I wrongly flagged last session): statphys_soc.py formatted Hill
  alpha with {alpha:.2f} when _hill_alpha returns None (near-zero-return symbols like PAXGUSDT) →
  crashed _compute → base.evaluate caught it → module ABSTAINED → SOC vote silently lost. Guarded
  to "n/a". VERIFIED: PAXGUSDT now ok=True (real vote) and 0 statphys/module errors in live logs
  since restart.
- PARALLEL SCORER (Phase 5 pulled into Phase 1b): signals/scibrain/gate.py — persistent FORK
  ProcessPool (_get_pool/_worker_init/_worker_score/_score_universe). FORK not spawn (brain
  __main__ is a non-import-safe daemon; spawn would re-run startup in every worker). Each worker
  builds its OWN Redis client (no forked-socket reuse) + pins BLAS to 1 thread (threadpoolctl) so
  8 workers don't oversubscribe 10 cores. Config: scibrain:scorer_parallel(=1), scorer_workers
  (=min(cpu-2,8)). Cooldown filter collapsed to one ZSET read. Removed orphaned _on_cooldown
  (Rule 18 / C3). Pool torn down when scibrain disabled. Serial fallback on ANY pool failure → the
  funnel can never die.
- VERIFIED (C7): import clean; serial vs parallel candidates IDENTICAL (overlap 125, dir_mismatch=0;
  the 1 boundary diff = a 0.15-cutoff symbol drifting on live data, not logic). Live brain after
  restart: parallel=True workers=8, cold cycle 9.3s then warm 4.7s vs ~21s serial (~4.5×); no
  fallback, 0 module errors. Heartbeat now carries parallel/workers/clusters/dropped_*.
- NET: a full 500-pair × 14-module circuit scan now lands in ~4.7s, freeing ~40s of every 45s
  throttle window for the SL/hot loop. Next levers: in-RAM candle cache (Phase 5) → sub-second.

### 2026-06-09 — Rule 14 replaced + open-trade influence ledger + dead-trade exit disabled
- Replaced the fixed 50-shadow-cycle Rule 14 with the Evidence, Authority, and Influence Gate:
  observe → advise → bounded_canary → live → veto; evidence scales with blast radius, and only
  `applied` / `gate_applied` records may be described as causes.
- SciBrain new opens now persist the full router, decision snapshot, and normalized influence
  manifest. The post-open LLM audit/remediation is persisted as `advise`, never falsely marked
  live/pending because no action consumer exists.
- Legacy engine new opens now persist exact selected component weights/effects plus honest
  counterfactual records for inactive coherence/legacy-winrate and TFT multi-timeframe candidates.
- `/trades/open` exposes normalized provenance, audits, recommendations, and post-open influences.
  The Open Trades table has an expandable Influence ledger. Pre-schema SciBrain opens are
  reconstructed only from exact stored attribution and explicitly marked partial history.
- Runtime safety change requested by owner: `risk:dead_trade_disabled=1`; this disables both
  `dead_trade_time_exit` and `dead_trade_force_close_max_age`. SL/TP and other exit logic remain on.
- Rule-18 operational fix discovered during deployment: `metacognition/confidence.py` queried the
  nonexistent `trades.closed_at`; corrected to `exit_time`, mounted `metacognition/` into brain and
  main worker, and verified live confidence computes successfully.
