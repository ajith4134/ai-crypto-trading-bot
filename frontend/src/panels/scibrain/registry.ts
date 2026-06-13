// Canonical brain-region registry — Phase-6 VS-V1 task 2 (STABLE circuit atlas). The atlas must NOT
// reflow when the decision changes (§11.6: movement falsely implies structural change). So every module
// has a FIXED slot here, grouped into brain regions; the layout reads these coordinates, never the
// decision's array order. Unknown future modules fall into a deterministic overflow column (never hidden).

export interface RegionDef { id: string; label: string; color: string; col: 0 | 1; modules: string[]; }

// Two macro-columns of regions on the left; the pipeline skeleton sits to their right.
export const REGIONS: RegionDef[] = [
  { id: 'dynamics',    label: 'Dynamics · Spectral',   color: '#5b8def', col: 0, modules: ['koopman', 'rmt'] },
  { id: 'nonlinear',   label: 'Nonlinear · Chaos',     color: '#7c6cff', col: 0, modules: ['chaos', 'langevin_hawkes'] },
  { id: 'multifractal',label: 'Multifractal · Ergodic',color: '#9d7bff', col: 0, modules: ['multifractal_rg', 'ergodic_mixing'] },
  { id: 'criticality', label: 'Criticality · Tail',    color: '#ffb454', col: 0, modules: ['statphys_soc', 'evt_large_deviation_tail'] },
  { id: 'flow',        label: 'Crowding · Flow',       color: '#ff8f6c', col: 0, modules: ['ising', 'rough_path_signature'] },
  { id: 'reversion',   label: 'Reversion · Trend',     color: '#37d0c0', col: 1, modules: ['noiseharvest', 'kalman'] },
  { id: 'cycle',       label: 'Cycle · Wavelet',       color: '#46c6ff', col: 1, modules: ['quantum', 'wavelet'] },
  { id: 'structure',   label: 'Topology · Info',       color: '#6cf', col: 1, modules: ['tda', 'info_theory'] },
  { id: 'regime',      label: 'Regime · Change',       color: '#c9a227', col: 1, modules: ['hmm_regime', 'bocpd'] },
  { id: 'crossmarket', label: 'Cross-Market (universe)', color: '#7fa6ff', col: 1, modules: ['sparse_factor_residual', 'spectral_graph_contagion', 'causal_lead_lag', 'optimal_transport_regime'] },
];

export const MODULE_REGION: Record<string, string> = {};
export const REGION_COLOR: Record<string, string> = {};
for (const r of REGIONS) { REGION_COLOR[r.id] = r.color; for (const m of r.modules) MODULE_REGION[m] = r.id; }

export const NODE_W = 184, SKEL_W = 150;
// ROW_MOD must exceed the rendered node height (~37px: label+role+signed-bar+padding) or stacked
// modules overlap; keep a few px of breathing room.
const COL_W = 200, ROW_LABEL = 22, ROW_MOD = 42, GAP = 10, PAD_TOP = 8, NODE_H = 38;

export interface Slot { x: number; y: number; }
export interface RegionLabel { id: string; label: string; color: string; x: number; y: number; }

// Built ONCE (module-load). Deterministic → stable positions for the lifetime of the app.
function build() {
  const modulePos: Record<string, Slot> = {};
  const labels: RegionLabel[] = [];
  const colY: Record<number, number> = { 0: PAD_TOP, 1: PAD_TOP };
  const colX: Record<number, number> = { 0: 0, 1: COL_W };
  for (const r of REGIONS) {
    const c = r.col;
    labels.push({ id: 'region:' + r.id, label: r.label, color: r.color, x: colX[c], y: colY[c] });
    colY[c] += ROW_LABEL;
    for (const m of r.modules) { modulePos[m] = { x: colX[c] + 8, y: colY[c] }; colY[c] += ROW_MOD; }
    colY[c] += GAP;
  }
  const modulesHeight = Math.max(colY[0], colY[1]);
  const midY = modulesHeight / 2;
  // pipeline skeleton — fixed slots to the right, always present (§11.9 never hide a stage).
  // Left→right flow is the EXECUTED path: modules → router-gated fusion → SAFETY projection → action.
  // The action pulse visibly passes THROUGH the safety/risk plane before execution (design §4.1); the
  // Ollama audit sits above as an advisory input into the final action, not on the executed line.
  const BASE = 2 * COL_W + 40;
  const skeleton: Record<string, Slot> = {
    router: { x: BASE,         y: midY - 19 },
    fusion: { x: BASE + 200,   y: midY - 19 },
    safety: { x: BASE + 400,   y: midY - 19 },
    action: { x: BASE + 600,   y: midY - 19 },
    audit:  { x: BASE + 400,   y: midY - 170 },
  };
  return { modulePos, labels, skeleton, modulesHeight };
}

const BUILT = build();
export const MODULE_SLOTS = BUILT.modulePos;
export const REGION_LABELS = BUILT.labels;
export const SKELETON_SLOTS = BUILT.skeleton;
export const MODULES_HEIGHT = BUILT.modulesHeight;

// Deterministic overflow slot for any module not in the registry (future modules): a column far left,
// stacked by a stable index so it never overlaps the canonical grid and is never hidden.
export function overflowSlot(index: number): Slot {
  return { x: -COL_W, y: PAD_TOP + index * ROW_MOD };
}

export const ATLAS_NODE_H = NODE_H;
