// UniverseField — Phase-6 VS-V5 Universe Neural Field (design §4.4 / §VS-V5). Renders all active pairs
// as a dynamic relational field from the REAL UniverseFrame topology (/scibrain/universe): correlation-
// territory clusters, classical-MDS node positions (co-movement geometry, NOT force-layout), directed
// lead-lag edges (predictive flow / contagion), and per-pair centrality / volatility / liquidity / held
// position. Semantic zoom: OVERVIEW aggregates to cluster territories + cluster-flow (no hairball);
// DETAIL reveals individual pairs + directed links. Filters: direction, held-only. Topology is the data's.
import React, { useEffect, useMemo, useState } from 'react';
import { getSciBrainUniverse } from '../../api';
import { COL_LONG, COL_SHORT, COL_MUTE, COL_WARN } from './ui';

const W = 720, H = 460, PAD = 28;       // SVG canvas; data coords are in [-1,1]
const RX = (W - 2 * PAD) / 2, RY = (H - 2 * PAD) / 2, CX = W / 2, CY = H / 2;
const px = (x: number) => CX + x * RX;
const py = (y: number) => CY - y * RY;   // flip y for screen
const dirCol = (d: number) => (d > 0 ? COL_LONG : d < 0 ? COL_SHORT : COL_MUTE);
const fmt = (v: any, d = 3) => (v == null || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(d));

// a cluster's net_direction (−1..+1) → territory colour
const clusterCol = (nd: number) => (nd > 0.1 ? COL_LONG : nd < -0.1 ? COL_SHORT : COL_MUTE);

type Node = { symbol: string; x: number; y: number; cluster_id: number; direction: number;
  momentum: number; volatility: number; vol_z: number; liquidity_z: number; centrality: number;
  held?: boolean; held_side?: string };
type Edge = { source: string; target: string; value: number; magnitude: number };
type Cluster = { cluster_id: number; members: number; cx: number; cy: number; mean_intra_corr: number;
  net_direction: number; mean_momentum: number; mean_vol: number; mean_log_liquidity: number };

const UniverseField: React.FC = () => {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [mode, setMode] = useState<'overview' | 'detail'>('overview');
  const [dirFilter, setDirFilter] = useState<'all' | 'long' | 'short'>('all');
  const [heldOnly, setHeldOnly] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const [focusCluster, setFocusCluster] = useState<number | null>(null);

  useEffect(() => {
    getSciBrainUniverse().then(d => { setData(d); if (!d.available) setErr(d.error || 'unavailable'); })
      .catch(e => setErr(String(e?.message || e)));
  }, []);

  const nodes: Node[] = useMemo(() => data?.nodes || [], [data]);
  const edges: Edge[] = useMemo(() => data?.edges || [], [data]);
  const clusters: Cluster[] = useMemo(() => data?.clusters || [], [data]);
  const posById = useMemo(() => {
    const m: Record<string, Node> = {}; nodes.forEach(n => { m[n.symbol] = n; }); return m;
  }, [nodes]);

  // detail-view node filter (direction / held / focused cluster)
  const visNodes = useMemo(() => nodes.filter(n =>
    (dirFilter === 'all' || (dirFilter === 'long' ? n.direction > 0 : n.direction < 0)) &&
    (!heldOnly || n.held) &&
    (focusCluster == null || n.cluster_id === focusCluster)), [nodes, dirFilter, heldOnly, focusCluster]);
  const visSet = useMemo(() => new Set(visNodes.map(n => n.symbol)), [visNodes]);
  const visEdges = useMemo(() => edges.filter(e => visSet.has(e.source) && visSet.has(e.target)), [edges, visSet]);

  // overview: aggregate directed edges to cluster→cluster flow (sum magnitude), top 28
  const clusterEdges = useMemo(() => {
    const agg: Record<string, { a: number; b: number; mag: number }> = {};
    edges.forEach(e => {
      const sa = posById[e.source]?.cluster_id, sb = posById[e.target]?.cluster_id;
      if (sa == null || sb == null || sa === sb) return;
      const k = `${sa}>${sb}`;
      (agg[k] ||= { a: sa, b: sb, mag: 0 }).mag += e.magnitude;
    });
    return Object.values(agg).sort((x, y) => y.mag - x.mag).slice(0, 28);
  }, [edges, posById]);
  const clusterById = useMemo(() => {
    const m: Record<number, Cluster> = {}; clusters.forEach(c => { m[c.cluster_id] = c; }); return m;
  }, [clusters]);
  const maxMembers = useMemo(() => Math.max(1, ...clusters.map(c => c.members)), [clusters]);

  if (err) return <div style={{ color: COL_WARN, fontSize: 12, padding: 12 }}>Universe field unavailable: {err}</div>;
  if (!data) return <div style={{ color: COL_MUTE, fontSize: 12, padding: 12 }}>Loading universe neural field…</div>;

  const selNode = sel ? posById[sel] : null;
  const tab = (on: boolean): React.CSSProperties => ({
    padding: '2px 9px', borderRadius: 5, fontSize: 10.5, cursor: 'pointer',
    border: `1px solid ${on ? '#3f5a86' : '#243049'}`, color: on ? '#cdd6e6' : COL_MUTE,
    background: on ? '#16213a' : '#0c1322',
  });

  return (
    <div>
      {/* controls */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6, flexWrap: 'wrap' }}>
        <span data-testid="uf-overview" style={tab(mode === 'overview')}
          onClick={() => { setMode('overview'); setFocusCluster(null); }}>◉ Overview (territories)</span>
        <span data-testid="uf-detail" style={tab(mode === 'detail')} onClick={() => setMode('detail')}>⦿ Detail (pairs)</span>
        <span style={{ width: 1, height: 16, background: '#243049' }} />
        <span style={tab(dirFilter === 'all')} onClick={() => setDirFilter('all')}>all</span>
        <span style={{ ...tab(dirFilter === 'long'), color: dirFilter === 'long' ? COL_LONG : COL_MUTE }}
          onClick={() => setDirFilter('long')}>▲ long</span>
        <span style={{ ...tab(dirFilter === 'short'), color: dirFilter === 'short' ? COL_SHORT : COL_MUTE }}
          onClick={() => setDirFilter('short')}>▼ short</span>
        <span style={tab(heldOnly)} onClick={() => setHeldOnly(!heldOnly)}>● held only ({data.n_open_positions ?? 0})</span>
        {focusCluster != null && (
          <span style={{ ...tab(true), color: COL_WARN }} onClick={() => setFocusCluster(null)}>✕ cluster {focusCluster}</span>)}
        <span style={{ fontSize: 9.5, color: COL_MUTE, marginLeft: 'auto' }}>
          {data.n_symbols_total} pairs · {data.n_clusters} clusters · {nodes.length} shown · ρ≥{fmt(data.corr_threshold, 2)} · {data.primary_tf}</span>
      </div>

      <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
        <svg width={W} height={H} style={{ background: '#0a0f1c', border: '1px solid #1c2742', borderRadius: 6, flexShrink: 0 }}>
          <defs>
            <marker id="uf-arr" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto">
              <path d="M0,0 L6,3 L0,6 Z" fill="#56708f" />
            </marker>
          </defs>

          {mode === 'overview' ? (
            <>
              {/* cluster→cluster aggregated flow */}
              {clusterEdges.map((e, i) => {
                const a = clusterById[e.a], b = clusterById[e.b];
                if (!a || !b) return null;
                const w = Math.min(3, 0.4 + e.mag);
                return <line key={i} x1={px(a.cx)} y1={py(a.cy)} x2={px(b.cx)} y2={py(b.cy)}
                  stroke="#33455f" strokeWidth={w} markerEnd="url(#uf-arr)" opacity={0.7} />;
              })}
              {/* cluster territories: radius ~ member count, colour ~ net direction */}
              {clusters.map(c => {
                const r = 5 + 22 * Math.sqrt(c.members / maxMembers);
                const on = focusCluster == null || focusCluster === c.cluster_id;
                return (
                  <g key={c.cluster_id} style={{ cursor: 'pointer' }}
                     onClick={() => { setFocusCluster(c.cluster_id); setMode('detail'); }}>
                    <circle cx={px(c.cx)} cy={py(c.cy)} r={r} fill={clusterCol(c.net_direction)}
                      fillOpacity={on ? 0.18 : 0.06} stroke={clusterCol(c.net_direction)}
                      strokeOpacity={on ? 0.8 : 0.3} strokeWidth={1.2} />
                    {c.members >= 3 && <text x={px(c.cx)} y={py(c.cy) + 3} fontSize={9} textAnchor="middle"
                      fill="#9fb0c8">{c.members}</text>}
                  </g>
                );
              })}
            </>
          ) : (
            <>
              {/* directed lead-lag edges among visible pairs */}
              {visEdges.map((e, i) => {
                const a = posById[e.source], b = posById[e.target];
                if (!a || !b) return null;
                const hot = sel && (e.source === sel || e.target === sel);
                return <line key={i} x1={px(a.x)} y1={py(a.y)} x2={px(b.x)} y2={py(b.y)}
                  stroke={e.value >= 0 ? '#2f6f4f' : '#7a3a44'} strokeWidth={hot ? 1.8 : Math.min(2, 0.3 + e.magnitude * 1.5)}
                  markerEnd="url(#uf-arr)" opacity={hot ? 0.95 : 0.4} />;
              })}
              {/* pairs: radius ~ centrality, colour ~ direction, ring ~ held */}
              {visNodes.map(n => {
                const r = 2.5 + 5 * Math.abs(n.centrality);
                const isSel = n.symbol === sel;
                return (
                  <g key={n.symbol} style={{ cursor: 'pointer' }} onClick={() => setSel(n.symbol)}>
                    {n.held && <circle cx={px(n.x)} cy={py(n.y)} r={r + 3} fill="none"
                      stroke={n.held_side === 'long' ? COL_LONG : COL_SHORT} strokeWidth={1.5} />}
                    <circle cx={px(n.x)} cy={py(n.y)} r={r} fill={dirCol(n.direction)}
                      fillOpacity={isSel ? 1 : 0.72} stroke={isSel ? '#fff' : 'none'} strokeWidth={isSel ? 1.4 : 0} />
                    {(isSel || n.centrality > 0.55) && <text x={px(n.x) + r + 2} y={py(n.y) + 3} fontSize={8.5}
                      fill={isSel ? '#fff' : '#7f8da3'}>{n.symbol.replace('USDT', '')}</text>}
                  </g>
                );
              })}
            </>
          )}
        </svg>

        {/* side panel: legend + selected-pair / focused-cluster detail */}
        <div style={{ flex: 1, minWidth: 0, fontSize: 10 }}>
          {mode === 'overview' ? (
            <div style={{ color: COL_MUTE, lineHeight: 1.6 }}>
              <div style={{ fontSize: 9, letterSpacing: 0.3, marginBottom: 4 }}>OVERVIEW — CORRELATION TERRITORIES</div>
              Each disc = a correlation cluster (connected component of the ρ≥{fmt(data.corr_threshold, 2)} graph),
              radius ∝ member count, colour = net direction (<span style={{ color: COL_LONG }}>long</span>/
              <span style={{ color: COL_SHORT }}>short</span> tilt). Grey arrows = aggregated directed lead-lag
              flow between territories. <b>Click a territory</b> to zoom into its pairs.
              <div style={{ marginTop: 8 }}>top territories:</div>
              {clusters.slice(0, 6).map(c => (
                <div key={c.cluster_id} style={{ display: 'flex', gap: 6, alignItems: 'center', padding: '1px 0' }}>
                  <span style={{ width: 9, height: 9, borderRadius: 5, background: clusterCol(c.net_direction) }} />
                  <span style={{ color: '#cdd6e6', cursor: 'pointer' }}
                    onClick={() => { setFocusCluster(c.cluster_id); setMode('detail'); }}>#{c.cluster_id}</span>
                  <span>{c.members} pairs · ρ̄ {fmt(c.mean_intra_corr, 2)} · net {fmt(c.net_direction, 2)}
                    · liq {fmt(c.mean_log_liquidity, 1)}</span>
                </div>
              ))}
            </div>
          ) : selNode ? (
            <div style={{ color: COL_MUTE, lineHeight: 1.7 }}>
              <div style={{ fontSize: 12, color: dirCol(selNode.direction), fontWeight: 700 }}>
                {selNode.symbol} {selNode.held ? `· HELD ${selNode.held_side?.toUpperCase()}` : ''}</div>
              cluster <b style={{ color: '#cdd6e6' }}>#{selNode.cluster_id}</b>
              ({clusterById[selNode.cluster_id]?.members ?? '—'} pairs)<br />
              direction <b style={{ color: dirCol(selNode.direction) }}>
                {selNode.direction > 0 ? 'up' : selNode.direction < 0 ? 'down' : 'flat'}</b>
              · momentum {fmt(selNode.momentum, 4)}<br />
              centrality <b style={{ color: '#cdd6e6' }}>{fmt(selNode.centrality, 2)}</b> (lead-lag throughput)<br />
              volatility {fmt(selNode.volatility, 5)} (z {fmt(selNode.vol_z, 2)})
              · liquidity z {fmt(selNode.liquidity_z, 2)}<br />
              <div style={{ marginTop: 6, fontSize: 9 }}>
                in-flow / out-flow (directed lead-lag):
                {visEdges.filter(e => e.target === selNode.symbol).slice(0, 4).map((e, i) =>
                  <div key={'i' + i}>← {e.source.replace('USDT', '')} {fmt(e.value, 2)}</div>)}
                {visEdges.filter(e => e.source === selNode.symbol).slice(0, 4).map((e, i) =>
                  <div key={'o' + i}>→ {e.target.replace('USDT', '')} {fmt(e.value, 2)}</div>)}
              </div>
            </div>
          ) : (
            <div style={{ color: COL_MUTE, lineHeight: 1.6 }}>
              <div style={{ fontSize: 9, letterSpacing: 0.3, marginBottom: 4 }}>DETAIL — INDIVIDUAL PAIRS</div>
              Each dot = a pair, placed by classical MDS on the correlation distance (close = co-moving),
              radius ∝ lead-lag centrality, colour = direction. <span style={{ color: '#2f6f4f' }}>green</span>/
              <span style={{ color: '#7a3a44' }}>red</span> arrows = directed predictive flow (i leads j).
              A coloured ring = a current open position. <b>Click a pair</b> for its flows.
            </div>
          )}
          <div style={{ fontSize: 8.5, color: '#566', marginTop: 10, lineHeight: 1.5 }}>{data.note}</div>
        </div>
      </div>
    </div>
  );
};
export default UniverseField;
