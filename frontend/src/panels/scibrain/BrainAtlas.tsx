// BrainAtlas — Phase-6 VS-V1 live circuit atlas (React Flow). Renders the typed BrainGraphSnapshot as a
// stable left→right circuit: modules → router/fusion → safety/audit → action. Encodes meaning with
// color PLUS shape/border (§11.1): shadow = dashed blue border (recorded, never applied), suppressed =
// dimmed + dashed edge, direction = green/red signed bar. Pulses are ONE-SHOT per real decision event
// (no perpetual traffic — §5.2/§11.3): a fired event cascades modules → fusion → through safety → action;
// counter-evidence rides a distinct amber channel; capped + reduced-motion aware (§10/§11.3). Click to inspect.
import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  ReactFlow, Background, Controls, MiniMap, Handle, Position, BaseEdge, getBezierPath,
  useNodesInitialized, useReactFlow, useNodesState, useEdgesState,
  type Node, type Edge, type NodeProps, type EdgeProps,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { BrainGraphSnapshot, BrainNode, BeliefField, Region } from './brainGraph';
import { COL_LONG, COL_SHORT, COL_MUTE, COL_WARN, COL_SHADOW, signColor, dirColor } from './ui';
import { MODULE_SLOTS, SKELETON_SLOTS, REGION_LABELS, overflowSlot, NODE_W, SKEL_W } from './registry';

const regionColor: Record<Region, string> = {
  module: '#5b8def', router: '#b08cff', fusion: '#37d0c0',
  safety: COL_WARN, audit: '#6cf', action: '#cdd6e6',
};

// STABLE layout (design §11.6): every module sits at its FIXED registry slot keyed by name, so the atlas
// never reflows when a different set of modules fires — the operator learns where each function lives.
// Skeleton stages (router/fusion/safety/audit/action) sit at fixed pipeline slots. Unknown future modules
// fall into a deterministic overflow column (never hidden, §11.9).
function layout(snap: BrainGraphSnapshot): Node[] {
  let overflow = 0;
  const mk = (n: BrainNode): Node | null => {
    let p: { x: number; y: number } | undefined;
    if (n.region === 'module') p = MODULE_SLOTS[n.label] || overflowSlot(overflow++);
    else p = SKELETON_SLOTS[n.id];
    if (!p) return null;
    // explicit width/height so React Flow can compute bounds before DOM measurement settles.
    return {
      id: n.id, type: 'brain', position: { x: p.x, y: p.y }, data: { node: n } as any,
      width: n.region === 'module' ? NODE_W - 6 : SKEL_W, height: 40,
    };
  };
  return snap.nodes.map(mk).filter((x): x is Node => x !== null);
}

// static region-group labels — drawn once, behind the modules they head (the "region grouping" view).
function regionLabelNodes(): Node[] {
  return REGION_LABELS.map(r => ({
    id: r.id, type: 'regionLabel', position: { x: r.x, y: r.y },
    data: { label: r.label, color: r.color } as any,
    width: NODE_W, height: 18, selectable: false, draggable: false, focusable: false,
  }));
}

// border STYLE encodes authority (§5.1): solid = live, dashed = gate/shadow, dotted = advise; shadow
// keeps the distinct blue dashed channel. State (suppressed) still dims + dashes mute.
function authorityBorder(n: BrainNode, accent: string): string {
  if (n.shadow) return `1.5px dashed ${COL_SHADOW}`;          // observe / shadow — never applied
  if (n.state === 'suppressed') return `1px dashed ${COL_MUTE}`;
  if (n.authority === 'advise') return `1.5px dotted ${accent}`;   // advisory (Ollama audit)
  if (n.authority === 'gate') return `1.5px dashed ${accent}`;     // gating (router/safety)
  return `1.5px solid ${accent}`;                                  // live
}

// custom node: compact box, semantics via color + authority border + a signed activation bar, PLUS the
// VS-V2 uncertainty halo (outer box-shadow blur ∝ epistemic uncertainty, §5.1) and a confidence inner
// ring (inset ring brightness ∝ calibrated confidence, §5.1). Both are real typed node values.
type Detail = 'atlas' | 'circuit' | 'evidence';
const BrainNodeView: React.FC<NodeProps> = ({ data, selected }) => {
  const n: BrainNode = (data as any).node;
  const detail: Detail = (data as any).detail || 'circuit';   // semantic-zoom level (§6.1)
  const dim: boolean = !!(data as any).dim;                    // focus+context / filter dimming
  const accent = n.region === 'module' || n.region === 'fusion' || n.region === 'action'
    ? signColor(n.signed_value) : regionColor[n.region];
  const border = authorityBorder(n, accent);
  const stateOp = n.state === 'suppressed' ? 0.5 : n.state === 'abstain' ? 0.6 : 1;
  const opacity = dim ? stateOp * 0.14 : stateOp;             // dimmed but never fully hidden (§11.9)
  const unc = n.epistemic_uncertainty;       // 0..1 outer halo
  const conf = n.confidence;                 // 0..1 inner ring
  const shadows = [
    selected ? `0 0 0 2px ${accent}` : '',
    (unc != null && unc > 0.02) ? `0 0 ${4 + unc * 13}px ${1 + unc * 3}px rgba(127,166,255,${(0.12 + unc * 0.33).toFixed(3)})` : '',
    (conf != null && conf > 0.02) ? `inset 0 0 0 1.5px rgba(33,208,122,${(0.15 + conf * 0.5).toFixed(3)})` : '',
  ].filter(Boolean).join(', ');
  // semantic zoom: ATLAS = label + signed bar only (overview); CIRCUIT = + role/family; EVIDENCE = + exact
  // confidence/uncertainty numbers inline. Detail rises with zoom (§6.1 three levels).
  const showMeta = detail !== 'atlas';
  const showNumbers = detail === 'evidence';
  return (
    <div aria-label={`${n.label} · ${n.role} · authority ${n.authority}`}
         style={{ width: n.region === 'module' ? 178 : 150, opacity,
                  background: selected ? '#1b2540' : '#10162400', borderRadius: 6,
                  border, padding: '3px 6px', fontSize: 10, color: '#cdd6e6',
                  boxShadow: shadows || 'none' }}>
      <Handle type="target" position={Position.Left} style={{ background: accent, width: 5, height: 5 }} />
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 4 }}>
        <b style={{ color: accent, fontSize: 10, whiteSpace: 'nowrap', overflow: 'hidden',
                    textOverflow: 'ellipsis' }}>{n.label}</b>
        {n.shadow && <span style={{ fontSize: 8, color: COL_SHADOW }}>🕶</span>}
        {n.state === 'suppressed' && <span style={{ fontSize: 8, color: COL_MUTE }}>⊘</span>}
      </div>
      {showMeta && (
        <div style={{ fontSize: 8, color: COL_MUTE, marginTop: 1 }}>
          {n.role}{n.evidence_family ? ` · ${n.evidence_family}` : ''}
        </div>
      )}
      {/* signed activation bar */}
      <div style={{ position: 'relative', height: 5, marginTop: 2, background: '#1c2333', borderRadius: 3 }}>
        <span style={{ position: 'absolute', left: '50%', top: 0, bottom: 0, width: 1, background: '#3a4a63' }} />
        <span style={{ position: 'absolute', top: 0, height: '100%',
          left: n.signed_value >= 0 ? '50%' : `${50 - Math.min(50, Math.abs(n.signed_value) * 50)}%`,
          width: `${Math.min(50, Math.abs(n.signed_value) * 50)}%`,
          background: n.signed_value >= 0 ? COL_LONG : COL_SHORT }} />
      </div>
      {showNumbers && (
        <div style={{ fontSize: 7.5, color: COL_MUTE, marginTop: 1, display: 'flex', gap: 6 }}>
          <span>conf {conf != null ? conf.toFixed(2) : '—'}</span>
          <span>unc {unc != null ? unc.toFixed(2) : '—'}</span>
          {n.regime && <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{n.regime}</span>}
        </div>
      )}
      <Handle type="source" position={Position.Right} style={{ background: accent, width: 5, height: 5 }} />
    </div>
  );
};

// non-interactive region-group header (the "region grouping" overlay; no handles, no selection).
const RegionLabelView: React.FC<NodeProps> = ({ data }) => {
  const d = data as any;
  return (
    <div style={{ width: NODE_W, fontSize: 9, fontWeight: 700, letterSpacing: 0.3,
                  color: d.color, textTransform: 'uppercase', opacity: 0.85,
                  borderBottom: `1px solid ${d.color}33`, paddingBottom: 1, pointerEvents: 'none' }}>
      {d.label}
    </div>
  );
};

const nodeTypes = { brain: BrainNodeView, regionLabel: RegionLabelView };

// VS-V2 BELIEF / DISAGREEMENT FIELD — the brain's posterior over the symbol, shown as a field rather than
// one point of certainty (§5.3). The split bar shows long-mass (green) vs short-mass (red) of the live
// votes → its balance IS the disagreement; a separate "proposed → actual" readout makes a router/safety
// override or veto explicit (§4.1 proposed-versus-actual action). All values computed from real votes.
const Meter: React.FC<{ label: string; v: number; color: string }> = ({ label, v, color }) => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 78 }}>
    <span style={{ color: COL_MUTE, fontSize: 8.5, letterSpacing: 0.3 }}>{label}</span>
    <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
      <span style={{ display: 'inline-block', width: 46, height: 5, background: '#1c2333', borderRadius: 3 }}>
        <span style={{ display: 'block', height: '100%', borderRadius: 3,
                       width: `${Math.max(0, Math.min(1, v)) * 100}%`, background: color }} />
      </span>
      <span style={{ color: '#cdd6e6', fontSize: 9 }}>{v.toFixed(2)}</span>
    </div>
  </div>
);

const BeliefStrip: React.FC<{ b: BeliefField }> = ({ b }) => {
  const total = b.long_mass + b.short_mass;
  const longPct = total > 1e-9 ? (b.long_mass / total) * 100 : 50;
  const shortPct = total > 1e-9 ? (b.short_mass / total) * 100 : 50;
  const actCol = dirColor(b.actual_action);
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '5px 10px',
                  borderBottom: '1px solid #1a2233', background: '#0b1018', flexWrap: 'wrap' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 150 }}>
        <span style={{ color: COL_SHADOW, fontSize: 8.5, letterSpacing: 0.4 }}>BELIEF · DISAGREEMENT FIELD</span>
        <span style={{ fontSize: 10 }}>
          <b style={{ color: actCol }}>{(b.actual_action || 'ABSTAIN').toUpperCase()}</b>
          <span style={{ color: COL_MUTE }}> · {b.regime || '—'} · conv </span>
          <b style={{ color: '#cdd6e6' }}>{b.conviction.toFixed(2)}</b>
        </span>
      </div>

      {/* long-mass vs short-mass split — the disagreement made visible */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 200, flex: 1 }}>
        <span style={{ color: COL_MUTE, fontSize: 8.5 }}>
          vote split — {b.n_long}↑ / {b.n_short}↓ / {b.n_abstain}∅ &nbsp;(of {b.n_live} live)
        </span>
        <div style={{ display: 'flex', height: 9, borderRadius: 4, overflow: 'hidden', background: '#1c2333' }}>
          <span style={{ width: `${longPct}%`, background: COL_LONG }} title={`long mass ${b.long_mass.toFixed(2)}`} />
          <span style={{ width: `${shortPct}%`, background: COL_SHORT }} title={`short mass ${b.short_mass.toFixed(2)}`} />
        </div>
      </div>

      <Meter label="DISAGREEMENT" v={b.disagreement} color={COL_WARN} />
      <Meter label="MEAN UNCERTAINTY" v={b.mean_uncertainty} color={COL_SHADOW} />

      {/* proposed (evidence leaning) → actual (executed); badge the override/veto */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 132 }}>
        <span style={{ color: COL_MUTE, fontSize: 8.5, letterSpacing: 0.3 }}>PROPOSED → ACTUAL</span>
        <span style={{ fontSize: 10 }}>
          <b style={{ color: dirColor(b.evidence_leans) }}>{(b.evidence_leans || '—').toUpperCase()}</b>
          <span style={{ color: COL_MUTE }}> → </span>
          <b style={{ color: actCol }}>{(b.actual_action || 'ABSTAIN').toUpperCase()}</b>
          {b.vetoed && <span style={{ color: COL_WARN, fontSize: 8.5 }}> ⛔veto</span>}
          {!b.vetoed && b.overridden && <span style={{ color: COL_WARN, fontSize: 8.5 }}> ↔override</span>}
        </span>
      </div>
    </div>
  );
};

// PulseEdge — a one-shot signal pulse along a real edge. NO perpetual dash-march (design §5.2/§11.3:
// "moving pulse = a real timestamped event; no decorative perpetual traffic"). A travelling dot is
// rendered ONLY while `data.pulse` is true (the host fires it on a real new decision event, then clears
// it after the cascade). The dot's RADIUS encodes contribution magnitude; SPEED is fixed (never encodes
// confidence — §5.3). Counter-evidence pulses ride a distinct amber channel. Re-keyed per event so it
// replays exactly once. Reduced motion → the host never sets `data.pulse`, so this draws a static edge only.
const PulseEdge: React.FC<EdgeProps> = (p) => {
  const { id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, style, markerEnd } = p;
  const d = (p.data || {}) as any;
  const [path] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition });
  return (
    <>
      <BaseEdge id={id} path={path} style={style} markerEnd={markerEnd} />
      {d.pulse && (
        <circle key={d.pulseKey} r={d.r} fill={d.pulseColor} opacity={0}>
          {/* fade in at begin, out at end so no stray dot sits at the path origin before/after */}
          <animate attributeName="opacity" values="0;0.95;0.95;0" keyTimes="0;0.06;0.9;1"
                   begin={`${d.begin}ms`} dur={`${d.dur}ms`} repeatCount="1" fill="remove" />
          <animateMotion begin={`${d.begin}ms`} dur={`${d.dur}ms`} path={path} repeatCount="1" fill="remove" />
        </circle>
      )}
    </>
  );
};

const edgeTypes = { pulse: PulseEdge };

// pipeline stage of each non-vote edge → begin offset, so a fired pulse visibly CASCADES
// modules → fusion → through safety → action (the executed path passes through the risk plane).
const EDGE_STAGE: Record<string, number> = { 'e:fusion': 1, 'e:audit': 1, 'e:safety': 2 };
const PULSE_DUR = 650; // ms per hop — FIXED (speed must not encode confidence, §5.3)

// Frame the circuit deterministically from the KNOWN layout coordinates (fitView depends on DOM
// measurement that races the 3s re-render and misframes). We set the viewport directly when the circuit
// (symbol) changes; the user can still pan/zoom freely afterwards and the poll never yanks it back.
const ViewportController: React.FC<{ symbolKey: string; vp: { x: number; y: number; zoom: number } }> =
    ({ symbolKey, vp }) => {
  const { setViewport } = useReactFlow();
  const initialized = useNodesInitialized();
  const lastKey = useRef<string>('');
  useEffect(() => {
    if (lastKey.current !== symbolKey) {
      lastKey.current = symbolKey;
      setViewport(vp, { duration: 200 });
    }
  }, [symbolKey, vp, setViewport, initialized]);
  return null;
};

// Semantic zoom (§6.1): a level button bumps `cmd.n`; this child (inside the RF provider) zooms to the
// matching scale so the operator jumps between Atlas / Circuit / Evidence. It reacts ONLY to the command
// counter, never to free pan/zoom, so manual zooming updates detail (via onMove) without snapping back.
const ZOOM_FOR: Record<string, number> = { atlas: 0.42, circuit: 0.72, evidence: 1.25 };
const SemanticZoomController: React.FC<{ cmd: { level: string; n: number } }> = ({ cmd }) => {
  const { zoomTo } = useReactFlow();
  const last = useRef(0);
  useEffect(() => {
    if (cmd.n !== last.current) { last.current = cmd.n; zoomTo(ZOOM_FOR[cmd.level] ?? 0.72, { duration: 250 }); }
  }, [cmd, zoomTo]);
  return null;
};
// derive the detail level from a live zoom value (manual pan/zoom also changes detail).
const detailForZoom = (z: number): 'atlas' | 'circuit' | 'evidence' =>
  z < 0.55 ? 'atlas' : z < 1.0 ? 'circuit' : 'evidence';

// container the atlas is laid out for; viewport math frames the full circuit into it with padding.
const ATLAS_W = 1080, ATLAS_H = 388, ATLAS_PAD = 26; // 460 container − ~46px belief strip − ~26px toolbar
function frameViewport(ns: Node[]): { x: number; y: number; zoom: number } {
  if (!ns.length) return { x: 0, y: 0, zoom: 0.8 };
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const n of ns) {
    const w = (n.width as number) || 150, h = (n.height as number) || 46;
    minX = Math.min(minX, n.position.x); minY = Math.min(minY, n.position.y);
    maxX = Math.max(maxX, n.position.x + w); maxY = Math.max(maxY, n.position.y + h);
  }
  const cw = Math.max(1, maxX - minX), ch = Math.max(1, maxY - minY);
  const zoom = Math.min((ATLAS_W - 2 * ATLAS_PAD) / cw, (ATLAS_H - 2 * ATLAS_PAD) / ch, 1.1);
  const x = ATLAS_PAD - minX * zoom + ((ATLAS_W - 2 * ATLAS_PAD) - cw * zoom) / 2;
  const y = ATLAS_PAD - minY * zoom + ((ATLAS_H - 2 * ATLAS_PAD) - ch * zoom) / 2;
  return { x, y, zoom };
}

const reducedMotion = typeof window !== 'undefined' &&
  window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const BrainAtlas: React.FC<{
  snap: BrainGraphSnapshot;
  selectedId?: string | null;
  onSelect: (id: string | null, kind: 'node' | 'edge') => void;
}> = ({ snap, selectedId, onSelect }) => {
  // ── interaction state (§6.1/§6.3) ── semantic-zoom detail, zoom command, filters, user reduced-motion.
  const [detail, setDetail] = useState<'atlas' | 'circuit' | 'evidence'>('circuit');
  const [zoomCmd, setZoomCmd] = useState<{ level: string; n: number }>({ level: 'circuit', n: 0 });
  const [showShadow, setShowShadow] = useState(true);
  const [showSuppressed, setShowSuppressed] = useState(true);
  const [family, setFamily] = useState<string>('all');
  const [userReduce, setUserReduce] = useState(false);
  const rm = reducedMotion || userReduce;     // effective reduced motion (OS pref OR user toggle, §11.3)
  const goZoom = (level: 'atlas' | 'circuit' | 'evidence') => {
    setDetail(level); setZoomCmd(c => ({ level, n: c.n + 1 }));
  };

  // ── event-driven pulses (NOT perpetual, §5.2/§11.3) ── fire ONE cascade per real new decision event.
  // The "real timestamped event" is the active decision's snapshot_id (symbol@ts): it changes only when a
  // new cycle scores this symbol or the operator selects another symbol — never on an idle 3s re-poll.
  const [pulseGen, setPulseGen] = useState(0);
  const [pulsing, setPulsing] = useState(false);
  const lastEvent = useRef<string>('');
  useEffect(() => {
    if (!snap.available || rm) return;
    if (lastEvent.current !== snap.snapshot_id) {
      lastEvent.current = snap.snapshot_id;
      setPulseGen(g => g + 1);
    }
  }, [snap.snapshot_id, snap.available, rm]);
  useEffect(() => {
    if (pulseGen === 0 || rm) return;
    setPulsing(true);
    const t = setTimeout(() => setPulsing(false), PULSE_DUR * 3 + 350); // safety stage-2 cascade + tail
    return () => clearTimeout(t);
  }, [pulseGen, rm]);

  // which edges carry a pulse this event: top supporting votes + top counter-evidence + the executed chain
  // (capped — §10 cap concurrent pulses; counter-evidence always shown when present — §4.1).
  const pulseSet = useMemo(() => {
    if (rm) return new Set<string>();
    const votes = snap.edges.filter(e => e.message_kind === 'vote' && !e.shadow && !e.suppressed);
    const support = votes.filter(e => !e.opposes).sort((a, b) => b.magnitude - a.magnitude).slice(0, 6).map(e => e.id);
    const counter = votes.filter(e => e.opposes).sort((a, b) => b.magnitude - a.magnitude).slice(0, 2).map(e => e.id);
    const chain = ['e:fusion', 'e:safety', 'e:audit'].filter(id => snap.edges.some(e => e.id === id));
    return new Set<string>([...support, ...counter, ...chain]);
  }, [snap, rm]);

  // ── focus+context (§6.1) ── when a node is selected, its 1-hop neighbourhood stays full while the rest
  // dims. Computed from the real edge list (and region-label membership for module headers).
  const focusSet = useMemo<Set<string> | null>(() => {
    if (!selectedId) return null;
    const keep = new Set<string>([selectedId]);
    for (const e of snap.edges) {
      if (e.source === selectedId) keep.add(e.target);
      if (e.target === selectedId) keep.add(e.source);
    }
    return keep;
  }, [selectedId, snap.edges]);

  // ── filter surface (§6.3) ── dim (never hide, §11.9) modules excluded by the shadow/suppressed/family
  // filters. Pipeline stages (router/fusion/safety/audit/action) and region labels are never filtered.
  const filteredOut = (n: BrainNode): boolean => {
    if (n.region !== 'module') return false;
    if (!showShadow && n.shadow) return true;
    if (!showSuppressed && n.state === 'suppressed') return true;
    if (family !== 'all' && (n.evidence_family || 'unspecified') !== family) return true;
    return false;
  };
  const families = useMemo(() => {
    const s = new Set<string>();
    for (const n of snap.nodes) if (n.region === 'module' && n.evidence_family) s.add(n.evidence_family);
    return ['all', ...Array.from(s).sort()];
  }, [snap.nodes]);

  // region-group labels (static) drawn first so they sit behind the module nodes they head.
  const regionLabels = useMemo(() => regionLabelNodes(), []);
  const circuitNodes = useMemo(() => layout(snap), [snap]);
  const baseNodes = useMemo(() => [...regionLabels, ...circuitNodes], [regionLabels, circuitNodes]);
  const vp = useMemo(() => frameViewport(circuitNodes), [circuitNodes]);
  const baseEdges = useMemo<Edge[]>(() => snap.edges.map(e => {
    const counter = e.opposes;
    const color = e.shadow ? COL_SHADOW
      : counter ? COL_WARN                                              // counter-evidence channel (§4.1)
      : e.message_kind === 'gate' || e.message_kind === 'safety' ? COL_WARN
      : signColor(e.signed_value);
    // router-gain path mapping (design §4.1): gain>1 → bolder + opaque; gain<1 → thinner + faded.
    const g = Math.min(1.5, Math.max(0, e.gain ?? 1));
    const gainW = e.message_kind === 'vote' ? (0.7 + 0.4 * g) : 1;
    const gainOpacity = e.message_kind === 'vote' && !e.suppressed && !e.shadow
      ? 0.4 + 0.4 * (g / 1.5) : null;
    return {
      id: e.id, source: e.source, target: e.target, type: 'pulse',
      data: {
        pulse: pulsing && pulseSet.has(e.id),
        pulseKey: `${pulseGen}:${e.id}`, begin: (EDGE_STAGE[e.id] ?? 0) * PULSE_DUR, dur: PULSE_DUR,
        r: 2.5 + e.magnitude * 3, pulseColor: counter ? COL_WARN : signColor(e.signed_value),
      },
      style: { stroke: color, strokeWidth: (1 + e.magnitude * 4) * gainW,
               strokeDasharray: e.shadow ? '4 3' : e.suppressed ? '2 4' : counter ? '1 3' : undefined,
               opacity: e.suppressed ? 0.3 : e.shadow ? 0.55 : (gainOpacity ?? 0.9) },
    } as Edge;
  }), [snap, pulseSet, pulsing, pulseGen]);

  // useNodesState/useEdgesState so React Flow can write back measured node dimensions (required for
  // fitView + useNodesInitialized to work — a controlled `nodes` prop without onNodesChange never does).
  const [nodes, setNodes, onNodesChange] = useNodesState([] as Node[]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([] as Edge[]);
  useEffect(() => {
    setNodes(baseNodes.map(nd => {
      const n: BrainNode | undefined = (nd.data as any)?.node;
      const dim = !!n && ((focusSet != null && !focusSet.has(nd.id)) || filteredOut(n));
      return { ...nd, selected: nd.id === selectedId,
               data: { ...(nd.data as any), detail, dim } };
    }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseNodes, selectedId, setNodes, detail, focusSet, showShadow, showSuppressed, family]);
  useEffect(() => {
    setEdges(baseEdges.map(e => {
      // dim an edge when focus is active and neither endpoint is in the focused neighbourhood.
      const dimByFocus = focusSet != null && !focusSet.has(e.source) && !focusSet.has(e.target);
      const baseOp = (e.style?.opacity as number) ?? 0.9;
      return { ...e, selected: e.id === selectedId,
               style: { ...e.style, opacity: dimByFocus ? baseOp * 0.12 : baseOp } };
    }));
  }, [baseEdges, selectedId, setEdges, focusSet]);

  if (!snap.available) {
    return <div style={{ color: COL_MUTE, fontSize: 12, padding: 16 }}>
      No decision selected — pick a symbol from the timeline or table to render its circuit.</div>;
  }

  const tgl = (on: boolean): React.CSSProperties => ({
    cursor: 'pointer', fontSize: 9.5, lineHeight: '15px', padding: '0 6px', borderRadius: 3,
    background: on ? '#1b2540' : '#10162a', color: on ? '#cdd6e6' : COL_MUTE,
    border: `1px solid ${on ? '#3a4a63' : '#1f2a3a'}`,
  });
  const zoomLevels: Array<'atlas' | 'circuit' | 'evidence'> = ['atlas', 'circuit', 'evidence'];

  return (
    <div style={{ height: 460, background: '#0c111b', borderRadius: 8, border: '1px solid #1a2233',
                  display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      <BeliefStrip b={snap.belief} />
      {/* interaction toolbar (§6.1/§6.3): semantic zoom, filters, reduced-motion — all keyboard/ARIA */}
      <div role="toolbar" aria-label="Atlas view controls"
           style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '4px 8px',
                    borderBottom: '1px solid #1a2233', flexWrap: 'wrap' }}>
        <span style={{ fontSize: 8.5, color: COL_MUTE, letterSpacing: 0.3 }}>ZOOM</span>
        {zoomLevels.map(lv => (
          <button key={lv} style={tgl(detail === lv)} onClick={() => goZoom(lv)}
                  aria-pressed={detail === lv} aria-label={`Semantic zoom: ${lv} level`}>
            {lv}</button>
        ))}
        <span style={{ width: 1, height: 14, background: '#243049', margin: '0 2px' }} />
        <span style={{ fontSize: 8.5, color: COL_MUTE, letterSpacing: 0.3 }}>FILTER</span>
        <button style={tgl(showShadow)} onClick={() => setShowShadow(s => !s)}
                aria-pressed={showShadow} aria-label="Toggle shadow modules">🕶 shadow</button>
        <button style={tgl(showSuppressed)} onClick={() => setShowSuppressed(s => !s)}
                aria-pressed={showSuppressed} aria-label="Toggle suppressed modules">⊘ suppressed</button>
        <select value={family} onChange={e => setFamily(e.target.value)} aria-label="Filter by evidence family"
                style={{ ...tgl(family !== 'all'), padding: '0 4px' }}>
          {families.map(f => <option key={f} value={f}>{f === 'all' ? 'all families' : f}</option>)}
        </select>
        <span style={{ width: 1, height: 14, background: '#243049', margin: '0 2px' }} />
        <button style={tgl(!rm)} onClick={() => setUserReduce(u => !u)} aria-pressed={rm}
                aria-label="Toggle motion (pulse animations)" disabled={reducedMotion}
                title={reducedMotion ? 'Reduced motion forced by your OS setting' : ''}>
          {rm ? 'motion off' : 'motion on'}</button>
        {focusSet && <span style={{ fontSize: 8.5, color: COL_SHADOW }}>● focus: neighbourhood of selection</span>}
      </div>
      <div style={{ flex: 1, minHeight: 0 }}>
      <ReactFlow
        nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes}
        onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
        defaultViewport={vp} minZoom={0.25} maxZoom={1.6}
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false} nodesConnectable={false} elementsSelectable
        onMove={(_, v) => { const d = detailForZoom(v.zoom); setDetail(prev => prev === d ? prev : d); }}
        onNodeClick={(_, nd) => onSelect(nd.id, 'node')}
        onEdgeClick={(_, ed) => onSelect(ed.id, 'edge')}
        onPaneClick={() => onSelect(null, 'node')}>
        <ViewportController symbolKey={snap.symbol || ''} vp={vp} />
        <SemanticZoomController cmd={zoomCmd} />
        <Background color="#1a2233" gap={18} />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable nodeColor={(nd: any) => {
          const n: BrainNode = nd.data?.node; if (!n) return COL_MUTE;
          return n.shadow ? COL_SHADOW : signColor(n.signed_value);
        }} style={{ background: '#0c111b' }} />
      </ReactFlow>
      </div>
    </div>
  );
};

export default BrainAtlas;
