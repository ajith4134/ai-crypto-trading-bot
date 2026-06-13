// BrainInspector — Phase-6 task 4: per-node/edge inspector with formula / evidence / reasoning /
// confidence / uncertainty / regime / version / authority (design §11.7, §10 fallback rule). Every field
// is a REAL typed value from the live decision or the static module-science catalog — no prose inference,
// no fabricated numbers; the raw computed detail dict is always preserved underneath.
import React from 'react';
import { BrainGraphSnapshot, BrainNode, BrainEdge } from './brainGraph';
import { COL_LONG, COL_MUTE, COL_SHADOW, COL_WARN, COL_SHORT, signColor, Bar, VoteBar } from './ui';
import { moduleScience } from './moduleScience';

const fmt = (v: any): string => {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(4);
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (Array.isArray(v)) return v.length ? v.join(', ') : '∅';
  if (typeof v === 'object') return '';
  return String(v);
};

const Row: React.FC<{ k: string; v: any }> = ({ k, v }) => {
  if (v && typeof v === 'object' && !Array.isArray(v)) {
    return (
      <div style={{ marginTop: 2 }}>
        <span style={{ color: COL_MUTE }}>{k}</span>
        <div style={{ marginLeft: 8, borderLeft: '1px solid #223', paddingLeft: 6 }}>
          {Object.entries(v).map(([kk, vv]) => <Row key={kk} k={kk} v={vv} />)}
        </div>
      </div>
    );
  }
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, fontSize: 10.5, padding: '1px 0' }}>
      <span style={{ color: COL_MUTE, whiteSpace: 'nowrap' }}>{k}</span>
      <span style={{ color: '#cdd6e6', textAlign: 'right', wordBreak: 'break-word' }}>{fmt(v)}</span>
    </div>
  );
};

// Section header so the eight required facets read as distinct blocks (§11.7).
const Hdr: React.FC<{ t: string }> = ({ t }) => (
  <div style={{ color: COL_SHADOW, fontSize: 9, letterSpacing: 0.6, marginTop: 8, marginBottom: 2 }}>{t}</div>
);

// A 0..1 metric with its inline bar so confidence/uncertainty/reliability are visible, not just numeric.
const Metric: React.FC<{ k: string; v?: number; color?: string; suffix?: string }> = ({ k, v, color, suffix }) => {
  if (v === undefined || v === null || Number.isNaN(v)) return null;
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 6, padding: '1px 0' }}>
      <span style={{ color: COL_MUTE, fontSize: 10.5, whiteSpace: 'nowrap' }}>{k}</span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <Bar v={Math.abs(v)} color={color || COL_SHADOW} w={64} />
        <span style={{ color: '#cdd6e6', fontSize: 10.5, width: 48, textAlign: 'right' }}>
          {v.toFixed(3)}{suffix || ''}</span>
      </span>
    </div>
  );
};

const wrap: React.CSSProperties = {
  width: 270, minWidth: 270, height: 460, overflowY: 'auto',
  background: '#0c111b', border: '1px solid #1a2233', borderRadius: 8, padding: '8px 10px', fontSize: 11,
};

// SCIENCE block — the formula/method/version/origin for a module, from the static catalog (§11.7 keep
// formulas + versions in the inspector). Only modules have catalogued math; pipeline stages don't.
const ScienceBlock: React.FC<{ name?: string }> = ({ name }) => {
  const sci = moduleScience(name);
  if (!sci) return null;
  return (
    <>
      <Hdr t="SCIENCE · FORMULA" />
      <div style={{ color: '#cbd5ea', fontSize: 10.5, lineHeight: 1.35, marginBottom: 3 }}>{sci.formula}</div>
      <Row k="method" v={sci.method} />
      <Row k="origin" v={sci.origin} />
      <Row k="version" v={sci.version} />
    </>
  );
};

const BrainInspector: React.FC<{
  snap: BrainGraphSnapshot;
  selectedId?: string | null;
  selectedKind?: 'node' | 'edge';
}> = ({ snap, selectedId, selectedKind }) => {
  if (!selectedId) {
    return <div style={wrap}>
      <div style={{ color: COL_SHADOW, fontSize: 11, marginBottom: 4 }}>INSPECTOR</div>
      <div style={{ color: COL_MUTE, fontSize: 11 }}>
        Click any node or edge in the atlas to see its formula, evidence, reasoning, confidence,
        uncertainty, regime, version, authority — and the raw computed detail.</div>
    </div>;
  }

  if (selectedKind === 'edge') {
    const e = snap.edges.find(x => x.id === selectedId) as BrainEdge | undefined;
    if (!e) return <div style={wrap}><span style={{ color: COL_MUTE }}>edge gone</span></div>;
    const col = e.shadow ? COL_SHADOW : e.opposes ? COL_WARN : signColor(e.signed_value);
    // a module-vote edge inherits the source module's catalogued science (e.g. e:koopman → koopman).
    const srcMod = e.source.startsWith('mod:') ? e.source.slice(4) : undefined;
    return (
      <div style={wrap}>
        <div style={{ color: col, fontSize: 11, marginBottom: 2 }}>
          EDGE · {e.message_kind.toUpperCase()}{e.shadow ? ' · 🕶 SHADOW' : ''}
          {e.opposes ? ' · ⚠ counter-evidence' : ''}{e.suppressed ? ' · ⊘ suppressed' : ''}
        </div>
        <div style={{ color: COL_MUTE, fontSize: 10, marginBottom: 6 }}>{e.source} → {e.target}</div>
        <Hdr t="SIGNAL" />
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '1px 0' }}>
          <span style={{ color: COL_MUTE, fontSize: 10.5 }}>signed contribution</span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <VoteBar v={e.signed_value} />
            <span style={{ color: '#cdd6e6', fontSize: 10.5, width: 48, textAlign: 'right' }}>
              {e.signed_value.toFixed(3)}</span>
          </span>
        </div>
        <Metric k="magnitude" v={e.magnitude} color={col} />
        <Metric k="router gain ×" v={e.gain} color={COL_WARN} />
        <Hdr t="STATE · AUTHORITY" />
        {e.evidence_family && <Row k="evidence_family" v={e.evidence_family} />}
        <Row k="shadow (not applied)" v={e.shadow} />
        <Row k="suppressed / deactivated" v={e.suppressed} />
        <Row k="opposes fused direction" v={e.opposes} />
        <ScienceBlock name={srcMod} />
        <Hdr t="RAW DETAIL" />
        {Object.entries(e.detail).map(([k, v]) => <Row key={k} k={k} v={v} />)}
      </div>
    );
  }

  const n = snap.nodes.find(x => x.id === selectedId) as BrainNode | undefined;
  if (!n) return <div style={wrap}><span style={{ color: COL_MUTE }}>node gone</span></div>;
  const col = n.region === 'module' || n.region === 'fusion' || n.region === 'action'
    ? signColor(n.signed_value) : COL_WARN;
  const stateColor = n.shadow ? COL_SHADOW : n.state === 'suppressed' ? COL_MUTE
    : n.state === 'active' ? COL_LONG : COL_WARN;
  // module nodes are labelled by raw module name → science catalogue lookup.
  const sciName = n.region === 'module' ? n.label : undefined;
  return (
    <div style={wrap}>
      <div style={{ color: col, fontSize: 12, fontWeight: 700, marginBottom: 1 }}>{n.label}</div>
      <div style={{ fontSize: 10, marginBottom: 2 }}>
        <span style={{ color: COL_MUTE }}>{n.region} · {n.role} · </span>
        <span style={{ color: stateColor }}>{n.state}</span>
        <span style={{ color: COL_MUTE }}> · authority </span>
        <b style={{ color: n.shadow ? COL_SHADOW : '#cdd6e6' }}>{n.authority}</b>
        {n.shadow && <span style={{ color: COL_SHADOW }}> · 🕶 NEVER applied to capital</span>}
      </div>

      <Hdr t="SIGNAL · CONFIDENCE" />
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '1px 0' }}>
        <span style={{ color: COL_MUTE, fontSize: 10.5 }}>direction (signed)</span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <VoteBar v={n.signed_value} />
          <span style={{ color: '#cdd6e6', fontSize: 10.5, width: 48, textAlign: 'right' }}>
            {n.signed_value.toFixed(3)}</span>
        </span>
      </div>
      <Metric k="activation" v={n.activation} color={col} />
      <Metric k="confidence" v={n.confidence} color={COL_LONG} />

      <Hdr t="UNCERTAINTY" />
      <Metric k="uncertainty (1−conv)" v={n.epistemic_uncertainty} color={COL_WARN} />
      <Metric k="reliability IC (settled)" v={n.reliability_ic} color={COL_SHADOW} />
      {n.epistemic_uncertainty === undefined && n.reliability_ic === undefined &&
        <div style={{ color: COL_MUTE, fontSize: 10 }}>— no uncertainty estimate for this stage —</div>}

      <Hdr t="REGIME · EVIDENCE" />
      {n.regime && <Row k="regime" v={n.regime} />}
      {n.evidence_family && <Row k="evidence_family" v={n.evidence_family} />}
      <Metric k="health" v={n.health} color={n.health >= 0.9 ? COL_LONG : COL_SHORT} />

      {n.reasoning && <>
        <Hdr t="REASONING" />
        <div style={{ color: '#cbd5ea', fontSize: 10.5, lineHeight: 1.35, whiteSpace: 'pre-wrap' }}>
          {n.reasoning}</div>
      </>}

      <ScienceBlock name={sciName} />

      <Hdr t="RAW DETAIL" />
      {Object.entries(n.detail).map(([k, v]) => <Row key={k} k={k} v={v} />)}
    </div>
  );
};

export default BrainInspector;
