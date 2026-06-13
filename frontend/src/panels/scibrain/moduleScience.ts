// Module science catalog — Phase-6 task 4 (per-node/edge inspector: formula/version/origin).
// formula/method/origin are STATIC properties of each module's fixed math (pure documentation of the
// code that actually runs), so they live with the frontend bundle — which is versioned together with
// that code — rather than being re-sent on every 3s /scibrain poll. The text is harvested VERBATIM from
// each module's own docstring + the build tracker's authoritative one-liners (signals/scibrain/modules/*,
// universe_modules/* and next_impl/scientist_brain_PROGRESS.md §Phase-2/2b) — nothing invented here.
//
// `version` is the initial shipped baseline ("1.0") for every module: there is no prior released version
// to show rollback lineage from yet, so 1.0 is the truthful current stamp. A real code change to a
// module's math bumps ITS entry (and the inspector then shows the new version) — never a blanket bump.
// The inspector degrades gracefully (§11.9): a module name absent here simply shows no science block.

export interface ModuleScience {
  formula: string;   // the actual computation/equation the module runs
  method: string;    // short method label (column/region context)
  origin: string;    // field of origin / citation
  version: string;   // shipped code version of this module's math
}

export const MODULE_SCIENCE: Record<string, ModuleScience> = {
  // ── Dynamics · Spectral ──
  koopman: {
    formula: 'Hankel-DMD: time-delay embedding → Dynamic Mode Decomposition. Reduced-operator '
      + 'eigenvalues λ ⇒ |λ|>1 trending / |λ|<1 mean-reverting; one-step Koopman push → next-bar forecast.',
    method: 'Koopman operator / DMD', origin: 'Fluid dynamics; arXiv:1904.09082', version: '1.0',
  },
  rmt: {
    formula: 'SSA trajectory-matrix eigenspectrum + iteratively-fitted Marchenko–Pastur noise edge → '
      + 'keep signal eigenmodes above the noise bulk → denoised-trend direction.',
    method: 'Random Matrix Theory', origin: 'Nuclear physics; Wigner / Marchenko–Pastur', version: '1.0',
  },
  // ── Nonlinear · Chaos ──
  chaos: {
    formula: 'Hurst exponent via structure-function scaling: H>0.5 ⇒ persistent/trend, H<0.5 ⇒ '
      + 'anti-persistent/revert; blended with short-lag autocorrelation.',
    method: 'Chaos / nonlinear dynamics', origin: 'Dynamical systems (Domain 5)', version: '1.0',
  },
  langevin_hawkes: {
    formula: 'AR(1) Ornstein–Uhlenbeck drift/diffusion (revert-to-fair) blended with a Hawkes '
      + 'self-excitation intensity (continuation when events cluster).',
    method: 'Langevin/Fokker–Planck + Hawkes', origin: 'Stat. physics + point processes', version: '1.0',
  },
  // ── Multifractal · Ergodic ──
  multifractal_rg: {
    formula: 'Multifractal scaling / renormalization-group view of the price path: generalized Hurst '
      + 'spectrum width as a turbulence/roughness measure.',
    method: 'Multifractal / RG', origin: 'Multifractal analysis (shadow research)', version: '1.0',
  },
  ergodic_mixing: {
    formula: 'Mixing-time / ergodicity estimate of the return process → an alpha-half-life '
      + '(how fast an edge decays).',
    method: 'Ergodic theory / mixing', origin: 'Dynamical systems (shadow research)', version: '1.0',
  },
  // ── Criticality · Tail ──
  statphys_soc: {
    formula: 'Hill tail-index + critical-slowing-down (rising lag-1 autocorr) + vol-of-vol + skew → '
      + 'self-organized-criticality score / crash_warning.',
    method: 'Self-Organized Criticality', origin: 'Statistical mechanics (Bak)', version: '1.0',
  },
  evt_large_deviation_tail: {
    formula: 'Peaks-Over-Threshold Generalized-Pareto fit of the loss tail → adverse-tail probability '
      + '+ a large-deviation risk envelope.',
    method: 'Extreme Value Theory (POT/GPD)', origin: 'Extreme value statistics', version: '1.0',
  },
  // ── Crowding · Flow ──
  ising: {
    formula: 'Mean-field magnetization m from OFI/funding/OI/momentum "spins" → crowding; '
      + 'contrarian when |m| is extreme (herding exhausted).',
    method: 'Ising / mean-field games', origin: 'Statistical mechanics', version: '1.0',
  },
  rough_path_signature: {
    formula: 'Level-2/3 log-signature of the [return, volume, OFI, OI] path → iterated integrals that '
      + 'encode event ORDER and interaction, not just magnitude.',
    method: 'Rough-path signature', origin: 'Rough-path theory (Lyons)', version: '1.0',
  },
  // ── Reversion · Trend ──
  noiseharvest: {
    formula: 'Ornstein–Uhlenbeck (AR1-fit) mean-reversion: fade a residual stretched >1σ from its '
      + 'estimated fair level — turn noise into edge.',
    method: 'OU mean-reversion', origin: 'Stochastic processes (Domain 19)', version: '1.0',
  },
  kalman: {
    formula: 'Constant-velocity Kalman filter → fair-value + velocity (clean momentum), with an '
      + 'innovation gate that abstains when the measurement surprise is too large.',
    method: 'Kalman / Bayesian filtering', origin: 'Optimal filtering (Domain 2)', version: '1.0',
  },
  // ── Cycle · Wavelet ──
  quantum: {
    formula: 'Von Neumann / spectral entropy of the return density-matrix + QFT dominant-cycle phase → '
      + 'a cyclic directional vote (quantum-inspired, runs on CPU).',
    method: 'Quantum information (inspired)', origin: 'Quantum info & QML (Domain 8)', version: '1.0',
  },
  wavelet: {
    formula: 'Haar DWT multi-scale energy + Donoho universal noise floor (SNR) → denoised-trend slope '
      + 'on the scales that carry real signal.',
    method: 'Wavelet multi-scale', origin: 'Wavelet signal analysis', version: '1.0',
  },
  // ── Topology · Info ──
  tda: {
    formula: 'H0 sublevel-set persistent homology (union-find) of the price path → persistence entropy '
      + '+ the dominant persistent range → structure-aware reversion.',
    method: 'Persistent homology (TDA)', origin: 'Topological data analysis', version: '1.0',
  },
  info_theory: {
    formula: 'Bandt–Pompe permutation entropy + Shannon sign-entropy + volume→price transfer entropy → '
      + 'predictability-gated momentum (act only when the series is predictable).',
    method: 'Information theory', origin: 'Shannon / Schreiber TE', version: '1.0',
  },
  // ── Regime · Change ──
  hmm_regime: {
    formula: 'Hidden Markov Model on daily-scale returns → predict_proba over bear/bull/turbulent '
      + 'states, health-gated (abstains if the fitted model is degenerate).',
    method: 'Hidden Markov Model', origin: 'models/hmm_regime.pkl', version: '1.0',
  },
  bocpd: {
    formula: 'Bayesian Online Changepoint Detection (Adams–MacKay, Student-t predictive) → run-length '
      + 'posterior and change-point probability; stability damper for fusion.',
    method: 'BOCPD', origin: 'Adams & MacKay 2007', version: '1.0',
  },
  // ── Cross-Market (universe core) ──
  sparse_factor_residual: {
    formula: 'Robust PCA (Principal Component Pursuit): standardized return matrix M = L (low-rank '
      + 'market/sector factor flow) + Sp (sparse idiosyncratic shock) → trade the residual reversal.',
    method: 'Robust PCA (PCP/ALM)', origin: 'Universe Core §6e Tier-A', version: '1.0',
  },
  spectral_graph_contagion: {
    formula: 'Signed-correlation graph Laplacian L=D−A: Fiedler value/vector (cohesion + 2-community '
      + 'split) + centrality + directed heat-diffusion → leader→follower contagion follow.',
    method: 'Spectral graph theory', origin: 'Universe Core §6e Tier-A', version: '1.0',
  },
  causal_lead_lag: {
    formula: 'Sparse CONDITIONAL Granger: factor-residualized multivariate regression + top-N-parents '
      + 'stability selection → causal flow from a target’s stable liquid drivers.',
    method: 'Conditional Granger / PCMCI', origin: 'Universe Core §6e Tier-A', version: '1.0',
  },
  optimal_transport_regime: {
    formula: 'Sliced-Wasserstein / Sinkhorn distance of the cross-sectional feature cloud + an '
      + 'outcome-learned winner/loser prototype classifier → distribution-migration regime.',
    method: 'Optimal transport', origin: 'Universe Core §6e Tier-A', version: '1.0',
  },
};

// Look up science for a module node label (the atlas labels module nodes by raw module name).
export function moduleScience(name?: string): ModuleScience | undefined {
  if (!name) return undefined;
  return MODULE_SCIENCE[name];
}
