// Scientist-Brain Launchpad — Phase-6 VS-V1 container. Splits the former monolithic panel into a typed
// data adapter (scibrain/brainGraph) + a live circuit ATLAS (React Flow) + INSPECTOR + decision TIMELINE
// + a guaranteed text/table FALLBACK. Polls /scibrain every 3s. Read-only / shadow. The crash radar,
// shadow bank, and ex-ante audit stay as compact drawers above the view.
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { getSciBrain, getSciBrainGraph } from '../api';
import { card, title } from './shared';
import { COL_LONG, COL_SHORT, COL_MUTE, COL_WARN, COL_SHADOW, dirColor, Bar } from './scibrain/ui';
import { toBrainGraph } from './scibrain/brainGraph';
import BrainAtlas from './scibrain/BrainAtlas';
import BrainInspector from './scibrain/BrainInspector';
import DecisionTimeline from './scibrain/DecisionTimeline';
import BrainFallbackTable from './scibrain/BrainFallbackTable';
import TradeAutopsy from './scibrain/TradeAutopsy';
import LearningLab from './scibrain/LearningLab';
import UniverseField from './scibrain/UniverseField';
import WholeBrain from './scibrain/WholeBrain';

// Crash Radar — StatPhysSOC self-organized-criticality early-warning strip.
const CrashRadar: React.FC<{ items: any[] }> = ({ items }) => {
  if (!items || items.length === 0) return null;
  const sev = (v: number) => v >= 0.6 ? COL_SHORT : v >= 0.4 ? COL_WARN : COL_MUTE;
  return (
    <div style={{ margin: '4px 0 10px', padding: '6px 8px', background: 'rgba(255,84,112,0.05)',
                  border: '1px solid #2a1f2a', borderRadius: 6 }}>
      <div style={{ fontSize: 11, color: COL_WARN, marginBottom: 4 }}>
        ⚠ CRASH RADAR — self-organized-criticality early-warning (StatPhysSOC), most-critical first
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        {items.map((it: any, i: number) => (
          <span key={i}
            title={`criticality ${it.criticality} · regime ${it.regime} · Hill α ${it.hill_alpha} · CSD ${it.csd} · skew ${it.skew}`}
            style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11,
                     padding: '2px 6px', background: '#141a26', borderRadius: 4,
                     border: `1px solid ${sev(it.crash_warning)}` }}>
            <b style={{ color: '#cdd6e6' }}>{it.symbol}</b>
            <Bar v={it.crash_warning} color={sev(it.crash_warning)} w={48} />
            <span style={{ color: sev(it.crash_warning) }}>{(it.crash_warning ?? 0).toFixed(2)}</span>
            {it.fused_direction && (
              <span style={{ color: dirColor(it.fused_direction) }}>
                {it.fused_direction === 'short' ? '↓' : '↑'}</span>)}
          </span>
        ))}
      </div>
    </div>
  );
};

const ACTION_COLOR: Record<string, string> = {
  HOLD: COL_MUTE, REDUCE: COL_WARN, TIGHTEN_SL: COL_WARN,
  CLOSE: COL_SHORT, REVERSE_BIAS: COL_SHORT,
};

const CalibrationLine: React.FC<{ cal?: any }> = ({ cal }) => {
  if (!cal || !cal.n) return null;
  const skill = cal.brier_skill;
  const skillColor = skill == null ? COL_MUTE : (skill > 0 ? COL_LONG : COL_SHORT);
  return (
    <div style={{ fontSize: 10, color: COL_MUTE, marginBottom: 4 }}>
      📈 calibration (n={cal.n}, graded at close):{' '}
      Brier <b style={{ color: '#cdd6e6' }}>{cal.brier}</b>
      {' '}vs base-rate {cal.brier_climatology}{' · '}
      skill <b style={{ color: skillColor }}>{skill == null ? 'n/a' : skill}</b>
      {' · '}mean forecast {cal.mean_forecast}{' '}vs actual wrong-rate {cal.base_rate_wrong}
      <span style={{ fontStyle: 'italic' }}> · proxy: {cal.label_proxy}</span>
    </div>
  );
};

const AuditPanel: React.FC<{ audits: any[]; meta?: any; calibration?: any }> =
    ({ audits, meta, calibration }) => {
  if (!audits || audits.length === 0) return null;
  const label = meta?.label || 'Ex-ante Decision-Risk Audit';
  const note = meta?.note ||
    'Judged at OPEN, before any realized outcome — a forecast of how likely the direction is wrong, not a post-result verdict.';
  return (
    <div style={{ margin: '4px 0 10px', padding: '6px 8px', background: 'rgba(91,141,239,0.05)',
                  border: '1px solid #1f2a3a', borderRadius: 6 }}>
      <div style={{ fontSize: 11, color: '#6cf', marginBottom: 1 }}>
        🔬 {label.toUpperCase()} — every opened trade interrogated at open; the agent proposes a fix when it flags one
      </div>
      <div style={{ fontSize: 10, color: COL_MUTE, marginBottom: 4, fontStyle: 'italic' }}>{note}</div>
      <CalibrationLine cal={calibration} />
      {audits.map((a: any, i: number) => {
        const rem = a.remediation || {};
        const rec = a.recommendation || {};
        const flagged = !a.agrees_with_fusion || (a.wrong_direction_risk ?? 0) >= 0.6;
        const act = rec.recommended_action || rem.recommended_action;
        return (
          <div key={i} style={{ fontSize: 11, padding: '4px 6px', marginBottom: 4,
                                background: '#121826', borderRadius: 4,
                                borderLeft: `2px solid ${flagged ? COL_SHORT : COL_LONG}` }}>
            <div>
              <b style={{ color: '#cdd6e6' }}>{a.symbol}</b>
              <span style={{ color: COL_MUTE }}> #{String(a.trade_id).slice(0, 8)} · </span>
              verdict <b style={{ color: dirColor(a.verdict_direction) }}>{a.verdict_direction}</b>
              <span style={{ color: a.agrees_with_fusion ? COL_LONG : COL_SHORT }}>
                {' '}· {a.agrees_with_fusion ? 'agrees' : '⚠ DISAGREES'}</span>
              <span style={{ color: (a.wrong_direction_risk ?? 0) >= 0.6 ? COL_SHORT : COL_MUTE }}>
                {' '}· wrong-dir {a.wrong_direction_risk}</span>
              {act && (
                <span style={{ marginLeft: 8, padding: '0 6px', borderRadius: 3,
                               background: '#0d1320', color: ACTION_COLOR[act] || COL_MUTE,
                               border: `1px solid ${ACTION_COLOR[act] || COL_MUTE}` }}>
                  → {act}{rec.mode ? ` (${rec.mode})` : ''}</span>)}
            </div>
            {a.narrative && <div style={{ color: '#9aa7bd', marginTop: 2 }}>{a.narrative}</div>}
            {rem.available && (
              <div style={{ color: '#cdd6e6', marginTop: 2 }}>
                🛠 improve: <b>{rem.module_to_adjust}</b> — {rem.circuit_improvement}
                {rem.reason && <span style={{ color: COL_MUTE }}> · {rem.reason}</span>}</div>)}
          </div>
        );
      })}
    </div>
  );
};

// Panel-level visibility of the SHADOW components running outside the per-decision rows.
const ShadowBank: React.FC<{ um?: any; uni?: any; pf?: any; pfa?: any }> = ({ um, uni, pf, pfa }) => {
  const umList: string[] = (um && um.modules) || [];
  const pfMode = (pf && pf.mode) || 'off';
  if (umList.length === 0 && pfMode === 'off' && !(pfa && pfa.pick_coverage != null)) return null;
  return (
    <div style={{ margin: '4px 0 10px', padding: '6px 8px', background: 'rgba(127,166,255,0.05)',
                  border: '1px solid #243049', borderRadius: 6, fontSize: 11 }}>
      <div style={{ color: COL_SHADOW, marginBottom: 3 }}>
        🕶 SHADOW BANK — components recorded + IC-graded, NEVER applied to capital</div>
      {umList.length > 0 && (
        <div style={{ color: '#9aa7bd' }}>
          universe-core: <b style={{ color: '#cdd6e6' }}>{umList.length}</b> modules
          <span style={{ color: COL_MUTE }}> ({umList.join(', ')})</span>
          {um.n_symbols != null && <span style={{ color: COL_MUTE }}> · over {um.n_symbols} symbols · {um.ms}ms</span>}
          {uni && uni.breadth_up != null && (
            <span style={{ color: COL_MUTE }}> · breadth {Number(uni.breadth_up).toFixed(2)} · crowding {Number(uni.mean_abs_corr ?? 0).toFixed(2)}</span>)}
        </div>
      )}
      <div style={{ color: '#9aa7bd' }}>
        prefilter (Phase-5 sub-second lever): <b style={{ color: pfMode === 'off' ? COL_MUTE : COL_WARN }}>{pfMode}</b>
        {pfMode !== 'off' && pf && (
          <span style={{ color: COL_MUTE }}> · K={pf.k}∪{pf.rotate} · qualifier-cov {pf.coverage}</span>)}
        {pfa && pfa.pick_coverage != null && (
          <span>
            <span style={{ color: COL_MUTE }}> · pick-cov </span>
            <b style={{ color: (pfa.starve_cycles ?? 0) > 0 ? COL_WARN : COL_LONG }}>
              {(pfa.pick_coverage * 100).toFixed(1)}%</b>
            <span style={{ color: COL_MUTE }}> ({pfa.picks_captured}/{pfa.picks_total}, {pfa.starve_cycles ?? 0} starve cycles
              {pfa.last_missed && pfa.last_missed.length ? `, missed ${pfa.last_missed.join('/')}` : ''})</span>
          </span>)}
      </div>
    </div>
  );
};

// Live/pause/cycle-replay transport (design §6.2). Accessible: real <button>s with aria-labels; the
// live/pause control reports aria-pressed. Stepping pauses; ⏭ resumes the live edge.
const tBtn: React.CSSProperties = {
  cursor: 'pointer', fontSize: 11, lineHeight: '16px', padding: '1px 7px', borderRadius: 4,
  background: '#10162a', color: '#cdd6e6', border: '1px solid #1f2a3a',
};
const TimeControl: React.FC<{
  live: boolean; cursor: number; nFrames: number; captured: number; cycles?: number;
  onFirst: () => void; onPrev: () => void; onNext: () => void; onLatest: () => void; onToggle: () => void;
}> = ({ live, cursor, nFrames, captured, cycles, onFirst, onPrev, onNext, onLatest, onToggle }) => {
  const atStart = cursor <= 0, atEnd = cursor >= nFrames - 1;
  const t = captured ? new Date(captured).toLocaleTimeString() : '—';
  const dim = (d: boolean): React.CSSProperties => d ? { opacity: 0.4, cursor: 'default' } : {};
  return (
    <div role="group" aria-label="Time control: live, pause, and cycle replay"
         style={{ display: 'flex', alignItems: 'center', gap: 6, margin: '2px 0 6px', flexWrap: 'wrap' }}>
      <button style={{ ...tBtn, ...dim(atStart) }} onClick={onFirst} disabled={atStart}
              aria-label="Jump to oldest buffered cycle">⏮</button>
      <button style={{ ...tBtn, ...dim(atStart) }} onClick={onPrev} disabled={atStart}
              aria-label="Step to previous cycle">◀</button>
      <button style={{ ...tBtn, background: live ? '#13351f' : '#3a2a13',
                       color: live ? COL_LONG : COL_WARN, borderColor: live ? '#1f5a36' : '#5a4420' }}
              onClick={onToggle} aria-pressed={!live}
              aria-label={live ? 'Pause live updates' : 'Resume live updates'}>
        {live ? '● LIVE' : '⏸ PAUSED'}</button>
      <button style={{ ...tBtn, ...dim(atEnd) }} onClick={onNext} disabled={atEnd}
              aria-label="Step to next cycle">▶</button>
      <button style={{ ...tBtn, ...dim(atEnd && live) }} onClick={onLatest}
              aria-label="Jump to live edge">⏭</button>
      <span style={{ fontSize: 10, color: COL_MUTE }}>
        {nFrames ? `frame ${cursor + 1}/${nFrames}` : 'buffering…'} · captured {t}
        {cycles != null ? ` · brain cycle ${cycles}` : ''}
        {!live && <span style={{ color: COL_WARN }}> · replay (not live)</span>}
      </span>
    </div>
  );
};

const EMPTY_FRAME = { decisions: [], counters: {}, status: {}, enabled: false, _captured: 0 };

const ScientistBrain: React.FC = () => {
  const [view, setView] = useState<'atlas' | 'table' | 'autopsy' | 'lab' | 'field' | 'brain'>('atlas');
  const [selSym, setSelSym] = useState<string | null>(null);
  const [sel, setSel] = useState<{ id: string | null; kind: 'node' | 'edge' }>({ id: null, kind: 'node' });

  // ── live/pause/cycle replay (design §6.2) ── a client-side ring buffer of polled frames. LIVE follows
  // the newest poll; PAUSE freezes the cursor while the buffer keeps filling, so the operator can step
  // back/forward through recent brain cycles (each frame ≈ one 3s poll) and resume to the live edge.
  const [frames, setFrames] = useState<any[]>([]);
  const [cursor, setCursor] = useState(0);
  const [live, setLive] = useState(true);
  const liveRef = useRef(true); liveRef.current = live;

  useEffect(() => {
    const load = () => getSciBrain().then((d: any) => {
      setFrames(prev => {
        const next = [...prev, { ...d, _captured: Date.now() }].slice(-48);
        if (liveRef.current) setCursor(next.length - 1);
        return next;
      });
    }).catch(() => {});
    load();
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, []);

  const nFrames = frames.length;
  const safeCursor = nFrames ? Math.min(cursor, nFrames - 1) : 0;
  const data: any = frames[safeCursor] || EMPTY_FRAME;
  const goLive = () => { setLive(true); setCursor(Math.max(0, nFrames - 1)); };
  const pauseAt = (i: number) => { setLive(false); setCursor(Math.max(0, Math.min(nFrames - 1, i))); };

  const decisions: any[] = useMemo(() => data.decisions || [], [data.decisions]);
  // keep a valid selection: fall back to the first decision when the chosen symbol drops out.
  const activeSym = useMemo(() => {
    if (selSym && decisions.some(d => d.symbol === selSym)) return selSym;
    return decisions[0]?.symbol ?? null;
  }, [selSym, decisions]);
  const activeDecision = decisions.find(d => d.symbol === activeSym) || null;
  // Fetch the BACKEND-built versioned BrainGraphSnapshot (typed nodes/edges + immutable evidence_ids,
  // design §8) for the selected symbol on each cycle — the frontend no longer infers the circuit from
  // prose. The graph selection below PREFERS it while LIVE, falls back to the client adapter when the
  // backend is unavailable, and ALWAYS uses the client adapter when PAUSED (historical-frame replay
  // reconstructs the buffered decision, not the live one) (§10).
  const [backendGraph, setBackendGraph] = useState<any>(null);
  useEffect(() => {
    if (!activeSym) { setBackendGraph(null); return; }
    let cancelled = false;
    getSciBrainGraph(activeSym)
      .then((g: any) => { if (!cancelled) setBackendGraph(g && g.available ? g : null); })
      .catch(() => { if (!cancelled) setBackendGraph(null); });
    return () => { cancelled = true; };
  }, [activeSym, data._captured]);
  const graph = useMemo(() => {
    if (live && backendGraph && backendGraph.available && backendGraph.symbol === activeSym)
      return backendGraph;
    return toBrainGraph(activeDecision);
  }, [live, backendGraph, activeDecision, activeSym]);

  const st = data.status || {};
  const c = data.counters || {};
  const statusColor = data.enabled ? COL_LONG : COL_WARN;
  const tabStyle = (on: boolean): React.CSSProperties => ({
    cursor: 'pointer', fontSize: 11, padding: '2px 10px', borderRadius: 4,
    background: on ? '#1b2540' : '#10162a', color: on ? '#cdd6e6' : COL_MUTE,
    border: `1px solid ${on ? '#3a4a63' : '#1f2a3a'}`,
  });

  return (
    <div style={{ ...card, gridColumn: '1 / -1' }}>
      <h3 style={title}>
        🧠 Scientist-Brain Launchpad — PhD math/physics/quantum circuit
        <span style={{ marginLeft: 12, color: statusColor, fontSize: 11 }}>
          {data.enabled ? '● LIVE FUNNEL' : '○ shadow-only'}</span>
        <span style={{ marginLeft: 12, color: COL_MUTE, fontSize: 11 }}>
          scanned {st.pairs ?? '—'} · {st.cycle_ms ?? '—'}ms/cycle · {st.actionable ?? '—'} actionable
          {data.interrogate ? ' · 🤖 interrogator ON' : ''}</span>
        <span style={{ marginLeft: 12, color: COL_MUTE, fontSize: 11 }}>
          cycles {c.cycles ?? 0} · scored {c.scored ?? 0} · interrogated {c.interrogated ?? 0}
          {' '}· <span style={{ color: (c.wrong_dir_flags ?? 0) > 0 ? COL_SHORT : COL_MUTE }}>
            wrong-dir flags {c.wrong_dir_flags ?? 0}</span></span>
        <span style={{ marginLeft: 12, color: COL_MUTE, fontSize: 11 }}>
          audited {c.audited ?? 0}
          {' '}· <span style={{ color: (c.flagged_trades ?? 0) > 0 ? COL_SHORT : COL_MUTE }}>
            flagged {c.flagged_trades ?? 0}</span>
          {' '}· queue {c.audit_queue ?? 0}
          {' '}· <span style={{ color: data.autoact ? COL_SHORT : COL_MUTE }}>
            auto-act {data.autoact ? 'ON' : 'shadow'}</span></span>
      </h3>

      <CrashRadar items={data.crash_radar || []} />
      <ShadowBank um={data.universe_modules} uni={data.universe}
                  pf={data.prefilter} pfa={data.prefilter_agg} />
      <AuditPanel audits={data.audits || []} meta={data.audits_meta} calibration={data.calibration} />

      <TimeControl live={live} cursor={safeCursor} nFrames={nFrames}
        captured={data._captured} cycles={c.cycles}
        onFirst={() => pauseAt(0)} onPrev={() => pauseAt(safeCursor - 1)}
        onNext={() => pauseAt(safeCursor + 1)} onLatest={goLive}
        onToggle={() => (live ? pauseAt(safeCursor) : goLive())} />

      <DecisionTimeline decisions={decisions} selected={activeSym} onSelect={setSelSym} />

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '8px 0 6px' }}>
        <span data-testid="view-atlas" style={tabStyle(view === 'atlas')} onClick={() => setView('atlas')}>🧠 Atlas</span>
        <span data-testid="view-table" style={tabStyle(view === 'table')} onClick={() => setView('table')}>▤ Table</span>
        <span data-testid="view-autopsy" style={tabStyle(view === 'autopsy')} onClick={() => setView('autopsy')}>🔬 Autopsy</span>
        <span data-testid="view-lab" style={tabStyle(view === 'lab')} onClick={() => setView('lab')}>🧪 Learning Lab</span>
        <span data-testid="view-field" style={tabStyle(view === 'field')} onClick={() => setView('field')}>🌐 Universe Field</span>
        <span data-testid="view-brain" style={tabStyle(view === 'brain')} onClick={() => setView('brain')}>🧬 Whole Brain</span>
        {activeDecision && (
          <span style={{ fontSize: 11, color: COL_MUTE }}>
            circuit for <b style={{ color: dirColor(activeDecision.direction) }}>{activeSym}</b>
            {' '}· {activeDecision.direction || 'abstain'} · conv {(activeDecision.conviction ?? 0).toFixed(2)}
            {' '}· {activeDecision.regime || '—'}</span>)}
      </div>

      {view === 'atlas' ? (
        <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <BrainAtlas snap={graph} selectedId={sel.id}
              onSelect={(id, kind) => setSel({ id, kind })} />
          </div>
          <BrainInspector snap={graph} selectedId={sel.id} selectedKind={sel.kind} />
        </div>
      ) : view === 'autopsy' ? (
        <TradeAutopsy />
      ) : view === 'lab' ? (
        <LearningLab />
      ) : view === 'field' ? (
        <UniverseField />
      ) : view === 'brain' ? (
        <WholeBrain />
      ) : (
        <BrainFallbackTable decisions={decisions} selected={activeSym} onSelect={setSelSym} />
      )}

      <div style={{ fontSize: 10, color: COL_MUTE, marginTop: 6 }}>
        Top strip = the belief/disagreement field: live vote split (long-mass vs short-mass), disagreement +
        mean uncertainty, and proposed (evidence-leaning) → actual (executed) with veto/override badge. Atlas:
        modules (grouped by PhD region, fixed positions) → router-gated fusion → through the safety plane →
        action; Ollama audit advises. Node border = authority (solid live · dashed gate/shadow · dotted
        advise); outer halo = epistemic uncertainty, inner green ring = confidence. Vote edges thicken/brighten
        with router gain, fade when attenuated. A one-shot pulse fires on each real decision event and cascades
        through to the action (no perpetual motion); counter-evidence rides a distinct amber channel. 🕶 dashed
        = shadow (recorded, never applied); ⊘ = suppressed/deactivated. Click a node/edge for exact values.
        Table is the full text fallback. Shadow-only until scibrain:enabled=1.
      </div>
    </div>
  );
};
export default ScientistBrain;
