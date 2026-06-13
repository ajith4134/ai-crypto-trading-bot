// Typed Brain-Graph adapter — Phase-6 VS-V1 (design §8). Transforms ONE raw /scibrain decision into a
// deterministic BrainGraphSnapshot that the atlas, inspector, and fallback table all read. The frontend
// never infers scientific meaning from prose: every node/edge carries typed values + the raw detail it
// came from, so the inspector can always show exact numbers/formulas/evidence (§11.7, §10 fallback rule).

export type Region = 'module' | 'router' | 'fusion' | 'safety' | 'audit' | 'action';
export type Authority = 'live' | 'observe' | 'gate' | 'advise';
export type NodeState = 'active' | 'suppressed' | 'shadow' | 'abstain' | 'ok';

export interface BrainNode {
  id: string;
  region: Region;
  role: string;                 // direction | gate | risk | context | recalibration | meta-router | …
  label: string;
  state: NodeState;
  activation: number;           // 0..1 salience/magnitude
  signed_value: number;         // -1..1 (direction); 0 for non-directional
  confidence: number;           // 0..1
  shadow: boolean;              // recorded + IC-graded, NEVER applied to capital
  authority: Authority;
  evidence_family?: string;
  regime?: string;
  health: number;               // 0..1 (1 = ok)
  reasoning?: string;           // the component's own explanation string (why this vote/verdict)
  epistemic_uncertainty?: number; // 0..1 self-reported uncertainty = 1 − conviction (transparent proxy)
  reliability_ic?: number;      // empirical settled-IC reliability (independent of self-confidence)
  detail: Record<string, any>;  // raw values for the inspector
}

export interface BrainEdge {
  id: string;
  source: string;
  target: string;
  message_kind: 'vote' | 'gate' | 'fuse' | 'audit' | 'safety';
  signed_value: number;         // signed contribution toward the verdict
  magnitude: number;            // 0..1 thickness
  gain: number;                 // router multiplicative weight (1 = neutral; >1 amplified, <1 attenuated)
  shadow: boolean;              // hypothetical / counterfactual
  suppressed: boolean;          // gain≈0 / deactivated / disagreeing
  opposes: boolean;             // counter-evidence: this message opposes the fused direction (distinct channel §4.1)
  evidence_family?: string;
  detail: Record<string, any>;
}

// VS-V2 belief field — the brain's POSTERIOR over the symbol, computed from the live module votes
// (not a single point of certainty, §5.3). disagreement = how split the bank is; evidence_leans vs
// actual_action makes the proposed-versus-actual gap explicit when the router/safety/fusion overrode
// the raw evidence (a veto or an abstain).
export interface BeliefField {
  direction: string | null;       // executed/fused direction
  regime: string | null;
  conviction: number;             // 0..1 fused conviction
  long_mass: number;              // Σ conviction of live modules voting long
  short_mass: number;             // Σ conviction of live modules voting short
  n_long: number; n_short: number; n_abstain: number; n_live: number;
  disagreement: number;           // 0..1: 0 = unanimous, 1 = evenly split (= 2·min/total)
  mean_uncertainty: number;       // 0..1 mean (1−conviction) over voting modules
  evidence_leans: string | null;  // 'long'|'short'|null — what the raw module mass pointed to
  actual_action: string | null;   // the executed decision (null = no trade / abstained)
  overridden: boolean;            // evidence_leans ≠ actual_action (router/safety/fusion changed it)
  vetoed: boolean;                // safety/gate suppressed the action
  crash_warning: number;          // 0..1 SOC crash pressure on the safety plane
}

export interface BrainGraphSnapshot {
  snapshot_id: string;
  ts: number;
  symbol: string | null;
  direction: string | null;
  conviction: number;
  regime: string | null;
  nodes: BrainNode[];
  edges: BrainEdge[];
  belief: BeliefField;          // VS-V2 posterior/disagreement field
  summary: Record<string, any>; // influence_manifest.summary
  available: boolean;
}

const clamp01 = (v: number) => Math.max(0, Math.min(1, v || 0));
const aabs = (v: number) => Math.abs(v || 0);
export const dirSign = (d: any): number => d === 'long' ? 1 : d === 'short' ? -1 : 0;

const EMPTY_BELIEF: BeliefField = {
  direction: null, regime: null, conviction: 0, long_mass: 0, short_mass: 0,
  n_long: 0, n_short: 0, n_abstain: 0, n_live: 0, disagreement: 0, mean_uncertainty: 0,
  evidence_leans: null, actual_action: null, overridden: false, vetoed: false, crash_warning: 0,
};

export function emptyGraph(): BrainGraphSnapshot {
  return { snapshot_id: '', ts: 0, symbol: null, direction: null, conviction: 0,
           regime: null, nodes: [], edges: [], belief: EMPTY_BELIEF, summary: {}, available: false };
}

// Compute the belief/disagreement field from the LIVE (non-shadow) directional module votes.
function computeBelief(decision: any, modules: any[], summary: Record<string, any>): BeliefField {
  let long_mass = 0, short_mass = 0, n_long = 0, n_short = 0, n_abstain = 0, n_live = 0, unc = 0, uncN = 0;
  for (const m of modules) {
    if (m.shadow_only) continue;                    // shadow modules never enter the live belief
    if (m.role && m.role !== 'direction') continue; // gates/risk/context don't cast a directional vote
    n_live++;
    const d = Number(m.direction || 0), c = clamp01(m.conviction);
    if (c <= 1e-6 || Math.abs(d) <= 1e-6) { n_abstain++; continue; }
    unc += 1 - c; uncN++;
    if (d > 0) { long_mass += c; n_long++; } else { short_mass += c; n_short++; }
  }
  const total = long_mass + short_mass;
  const disagreement = total > 1e-9 ? (2 * Math.min(long_mass, short_mass)) / total : 0;
  const evidence_leans = total <= 1e-9 ? null : (long_mass >= short_mass ? 'long' : 'short');
  const crash = Number((decision.router || {}).crash_warning || 0);
  const actual_action = decision.direction || null;
  // a VETO only matters when no trade resulted: the safety/gate plane suppressed the action entirely.
  // (a gate that merely trimmed influence while a direction still fired is NOT a veto — that would make
  // a "LONG → LONG ⛔veto" readout, which is misleading.)
  const vetoed = actual_action == null && (crash >= 0.6 || !!summary.gate_applied || !!summary.abstained);
  const overridden = evidence_leans != null && actual_action != null && evidence_leans !== actual_action;
  return {
    direction: decision.direction || null, regime: decision.regime || null,
    conviction: clamp01(decision.conviction), long_mass, short_mass,
    n_long, n_short, n_abstain, n_live, disagreement,
    mean_uncertainty: uncN ? unc / uncN : 0,
    evidence_leans, actual_action, overridden, vetoed, crash_warning: crash,
  };
}

const FUSION = 'fusion', ACTION = 'action', ROUTER = 'router', SAFETY = 'safety', AUDIT = 'audit';

export function toBrainGraph(decision: any): BrainGraphSnapshot {
  if (!decision || !decision.symbol) return emptyGraph();
  const modules: any[] = decision.modules || [];
  const router = decision.router || {};
  const gains: Record<string, number> = router.gains || {};
  const deact = new Set<string>(router.deactivated || []);
  const attr: Record<string, any> = {};
  for (const a of (decision.attribution || [])) attr[a.module] = a;
  const summary = (decision.influence_manifest || {}).summary || {};
  const fp = decision.family_penalty || {};
  const rsn = decision.reasoning;
  const convF = clamp01(decision.conviction);
  const dSign = dirSign(decision.direction);

  const nodes: BrainNode[] = [];
  const edges: BrainEdge[] = [];

  // ── module nodes + their contribution edges into the fusion ALU ──
  for (const m of modules) {
    const id = 'mod:' + m.module;
    const gain = (m.module in gains) ? Number(gains[m.module]) : 1;
    const suppressed = deact.has(m.module) || gain <= 1e-6;
    const a = attr[m.module] || {};
    const isShadow = !!m.shadow_only;
    nodes.push({
      id, region: 'module', role: m.role || 'direction', label: m.module,
      state: isShadow ? 'shadow' : (suppressed ? 'suppressed' : (m.ok ? 'active' : 'abstain')),
      activation: clamp01(m.conviction),
      signed_value: m.direction || 0,
      confidence: clamp01(m.conviction),
      shadow: isShadow,
      authority: isShadow ? 'observe' : ((m.role === 'risk' || m.role === 'gate') ? 'gate' : 'live'),
      evidence_family: m.evidence_family,
      regime: m.regime_tag,
      health: m.ok ? 1 : 0.3,
      reasoning: m.explanation,
      epistemic_uncertainty: 1 - clamp01(m.conviction),
      reliability_ic: (m.reliability_ic == null ? undefined : Number(m.reliability_ic)),
      detail: { explanation: m.explanation, features: m.features, gain,
                attribution: a, reliability_ic: m.reliability_ic, horizon_min: m.horizon_min,
                expected_move_pct: m.expected_move_pct, role: m.role, evidence_family: m.evidence_family },
    });
    const share = (a.share != null) ? Number(a.share) : (m.direction || 0) * clamp01(m.conviction) * gain;
    const opposes = !isShadow && !suppressed && dSign !== 0 &&
                    aabs(share) > 1e-6 && Math.sign(share) === -dSign;
    edges.push({
      id: 'e:' + m.module, source: id, target: FUSION, message_kind: 'vote',
      signed_value: share, magnitude: clamp01(aabs(share) * 2), gain,
      shadow: isShadow, suppressed, opposes,
      evidence_family: m.evidence_family,
      detail: { vote: m.direction, conviction: m.conviction, gain,
                redundancy_discount: a.redundancy_discount, aligned: a.aligned, share },
    });
  }

  // ── meta-router (MoE) → gates the fusion ──
  nodes.push({
    id: ROUTER, region: 'router', role: 'meta-router', label: 'Router · MoE',
    state: deact.size > 0 ? 'suppressed' : 'active', activation: clamp01(router.confidence),
    signed_value: 0, confidence: clamp01(router.confidence), shadow: false, authority: 'gate',
    regime: router.regime, health: 1,
    detail: { regime: router.regime, confidence: router.confidence, crash_warning: router.crash_warning,
              change_point_prob: router.change_point_prob, trend_score: router.trend_score,
              hmm_regime: router.hmm_regime, strength: router.strength,
              deactivated: router.deactivated, gains },
  });
  edges.push({
    id: 'e:router', source: ROUTER, target: FUSION, message_kind: 'gate',
    signed_value: 0, magnitude: clamp01(router.confidence), gain: 1, shadow: false,
    suppressed: false, opposes: false,
    detail: { deactivated: router.deactivated, regime: router.regime, gains },
  });

  // ── safety / risk plane → action ──
  const crash = Number(router.crash_warning || 0);
  nodes.push({
    id: SAFETY, region: 'safety', role: 'risk', label: 'Safety · Risk',
    state: crash >= 0.4 ? 'active' : 'ok', activation: clamp01(crash), signed_value: 0,
    confidence: clamp01(crash), shadow: false, authority: 'gate', health: 1,
    detail: { crash_warning: crash, change_point_prob: router.change_point_prob,
              gate_applied: summary.gate_applied, suppressed: summary.suppressed, abstained: summary.abstained },
  });
  // EXECUTED pass-through: the fused verdict is projected by the safety plane onto the final action.
  // The action thus visibly flows fusion → safety → action (design §4.1), and the crash/veto pressure
  // rides on this edge (color shifts toward the warning channel as crash rises).
  const vetoed = crash >= 0.6 || !!summary.gate_applied;
  edges.push({
    id: 'e:safety', source: SAFETY, target: ACTION, message_kind: 'safety',
    signed_value: dSign * convF, magnitude: convF, gain: 1, shadow: false,
    suppressed: vetoed, opposes: false,
    detail: { crash_warning: crash, projected_direction: decision.direction,
              gate_applied: summary.gate_applied, vetoed } as Record<string, any>,
  });

  // ── fusion ALU → action ──
  nodes.push({
    id: FUSION, region: 'fusion', role: 'fusion', label: 'Fusion ALU',
    state: 'active', activation: convF, signed_value: dSign * convF,
    confidence: convF, shadow: false, authority: 'live', regime: decision.regime, health: 1,
    reasoning: decision.primary_driver ? ('primary driver: ' + decision.primary_driver) : undefined,
    epistemic_uncertainty: 1 - convF,
    detail: { direction: decision.direction, conviction: decision.conviction,
              primary_driver: decision.primary_driver, family_penalty: fp, summary,
              expected_move_pct: decision.expected_move_pct },
  });
  edges.push({
    id: 'e:fusion', source: FUSION, target: SAFETY, message_kind: 'fuse',
    signed_value: dSign * convF, magnitude: convF, gain: 1, shadow: false, suppressed: false,
    opposes: false,
    detail: { primary_driver: decision.primary_driver, direction: decision.direction,
              conviction: decision.conviction },
  });

  // ── Ollama audit (advisory) → action, when present ──
  if (rsn && rsn.available) {
    const aSign = dirSign(rsn.verdict_direction);
    nodes.push({
      id: AUDIT, region: 'audit', role: 'audit', label: 'Ollama Audit',
      state: rsn.agrees_with_fusion ? 'active' : 'suppressed',
      activation: clamp01(rsn.confidence), signed_value: aSign * clamp01(rsn.confidence),
      confidence: clamp01(rsn.confidence), shadow: false, authority: 'advise', health: 1,
      reasoning: rsn.narrative,
      epistemic_uncertainty: (rsn.wrong_direction_risk == null ? undefined : clamp01(rsn.wrong_direction_risk)),
      detail: { verdict: rsn.verdict_direction, agrees: rsn.agrees_with_fusion,
                wrong_direction_risk: rsn.wrong_direction_risk, narrative: rsn.narrative,
                responsible_factor: rsn.responsible_factor, lead_model: rsn.lead_model,
                critic_model: rsn.critic_model, critic_note: rsn.critic_note },
    });
    edges.push({
      id: 'e:audit', source: AUDIT, target: ACTION, message_kind: 'audit',
      signed_value: aSign * clamp01(rsn.confidence), magnitude: clamp01(rsn.confidence), gain: 1,
      shadow: false, suppressed: !rsn.agrees_with_fusion, opposes: !rsn.agrees_with_fusion,
      detail: { wrong_direction_risk: rsn.wrong_direction_risk, agrees: rsn.agrees_with_fusion },
    });
  }

  // ── final action ──
  nodes.push({
    id: ACTION, region: 'action', role: 'action',
    label: (decision.direction || 'abstain').toUpperCase(),
    state: decision.direction ? 'active' : 'abstain', activation: convF,
    signed_value: dSign * convF, confidence: convF, shadow: false, authority: 'live',
    regime: decision.regime, health: 1,
    detail: { direction: decision.direction, conviction: decision.conviction,
              size_frac: decision.size_frac, expected_move_pct: decision.expected_move_pct,
              regime: decision.regime, primary_driver: decision.primary_driver },
  });

  return {
    snapshot_id: decision.symbol + '@' + decision.ts, ts: decision.ts, symbol: decision.symbol,
    direction: decision.direction, conviction: decision.conviction || 0, regime: decision.regime,
    nodes, edges, belief: computeBelief(decision, modules, summary), summary, available: true,
  };
}
