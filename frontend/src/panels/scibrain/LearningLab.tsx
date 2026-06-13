// LearningLab — Phase-6 VS-V4 Learning Laboratory (design §4.3 / §VS-V4). Renders the living-
// intelligence experiment system from the immutable registry artifacts (/scibrain/learning):
//   • the AUTHORITY banner (always visible) — the live switches + the Tier-0 invariant;
//   • the validated-lifecycle PIPELINE with the real by-status placement of every hypothesis;
//   • the hypothesis GENEALOGY — each bounded ChangeSpec, its champion→challenger scorecard with
//     its bootstrap lower-confidence bound, its transition lineage, and its code/config versions;
//   • the generalized MEMORY banks (negative = must-not-repeat, positive = retained laws);
//   • the advisory per-module COMPETENCE map (rolling IC + ablation verdict, never auto-acting).
// No "learning" animation implies a live change: nothing here applies a knob (Phase-7c gate owns that).
import React, { useEffect, useMemo, useState } from 'react';
import { getSciBrainLearning } from '../../api';
import { COL_LONG, COL_SHORT, COL_MUTE, COL_WARN, COL_SHADOW, Bar } from './ui';

const fmt = (v: any, d = 4) => (v == null || Number.isNaN(Number(v)) ? '—' : Number(v).toFixed(d));
const fmtTs = (ts: any) => (ts ? new Date(Number(ts) * 1000).toLocaleString() : '—');

// lifecycle status → colour. proposed/compiled = neutral, mid-pipeline = warming, applied = green,
// terminal failures = red. Mirrors the real STATUSES order from the backend.
const STATUS_COL: Record<string, string> = {
  proposed: COL_MUTE, compiled: COL_SHADOW, unit_tested: '#7fa6ff', historical_replay: '#7fa6ff',
  walk_forward: COL_WARN, shadow: COL_WARN, canary: '#ffd479', live: COL_LONG, retained: COL_LONG,
  demoted: COL_SHORT, rolled_back: COL_SHORT, rejected: COL_SHORT,
};
const statusCol = (s?: string) => (s && STATUS_COL[s]) || COL_MUTE;

// IC sign → colour (a module's directional skill: positive = informative, negative = anti-signal).
const icCol = (v: any) => (v == null ? COL_MUTE : v > 0.02 ? COL_LONG : v < -0.02 ? COL_SHORT : COL_MUTE);
const VERDICT_COL: Record<string, string> = {
  KEEP: COL_LONG, WATCH: COL_WARN, DEMOTE: COL_SHORT, PRUNE: COL_SHORT, INSUFFICIENT: COL_MUTE,
};

const card: React.CSSProperties = {
  background: '#10162a', border: '1px solid #243049', borderRadius: 6, padding: '8px 10px', marginBottom: 8,
};
const sectionTitle: React.CSSProperties = {
  fontSize: 9, color: COL_MUTE, letterSpacing: 0.4, marginBottom: 5, textTransform: 'uppercase',
};
const pill = (col: string): React.CSSProperties => ({
  padding: '1px 6px', borderRadius: 4, color: col, border: `1px solid ${col}`,
  background: '#0c1322', fontWeight: 700, fontSize: 9.5,
});

// AUTHORITY banner — design §4.3 demands authority state is always visible. Surfaces the live
// origination/router/IC switches and the Tier-0 invariant that the registry never applies a change.
const AuthorityBanner: React.FC<{ a: any }> = ({ a }) => {
  if (!a) return null;
  const sw = (label: string, on: boolean) => (
    <span style={pill(on ? COL_LONG : COL_MUTE)}>{label} {on ? 'ON' : 'OFF'}</span>
  );
  return (
    <div style={{ ...card, borderColor: '#2c3a18', background: '#141a0f' }}>
      <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
        <span style={pill(COL_WARN)}>TIER-0 · RECORDS ONLY</span>
        {sw('origination', !!a.origination_enabled)}
        {sw('router', !!a.router_enabled)}
        <span style={{ fontSize: 10, color: COL_MUTE }}>router strength {fmt(a.router_strength, 2)}</span>
        {sw('IC adapt', !!a.ic_enabled)}
        <span style={pill(a.promotion_gate_active ? COL_LONG : COL_MUTE)}>
          promotion gate {a.promotion_gate_active ? 'ACTIVE' : 'OFF (Phase-7c)'}</span>
        <span style={{ fontSize: 10, color: a.hypotheses_applied_live ? COL_LONG : COL_MUTE }}>
          {a.hypotheses_applied_live || 0} applied live</span>
      </div>
      <div style={{ fontSize: 9.5, color: COL_MUTE, marginTop: 5 }}>{a.note}</div>
    </div>
  );
};

// AUTHORITY RECONCILIATION (Phase-7c) — the canonical map of the CURRENT live state to the explicit
// observe→advise→bounded_canary→live(+veto) ladder, with the invariant checks that catch authority drift.
const authColor = (auth: string) =>
  auth === 'live' ? COL_LONG : auth === 'veto' ? COL_WARN
    : auth === 'bounded_canary' ? '#d9a23b' : auth === 'advise' ? '#5a86c8' : COL_MUTE;
const AUTH_ORDER: Record<string, number> = { live: 0, veto: 1, bounded_canary: 2, advise: 3, observe: 4 };

const AuthorityReconciliation: React.FC<{ a: any }> = ({ a }) => {
  if (!a || !a.components) return null;
  const comps = [...a.components].sort((x, y) =>
    (AUTH_ORDER[x.effective_authority] - AUTH_ORDER[y.effective_authority]) || (y.risk_tier - x.risk_tier));
  const invs: any[] = a.invariants || [];
  const allOk = a.summary?.all_invariants_ok;
  return (
    <div style={{ ...card, borderColor: allOk ? '#2c3a18' : '#5a1d1d' }}>
      <div style={sectionTitle}>
        authority reconciliation — current live state · observe → advise → canary → live (+veto)</div>
      {/* invariant checks (the falsifiable part) */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 7 }}>
        {invs.map(i => (
          <span key={i.name} title={i.detail} style={pill(i.ok ? COL_LONG : COL_WARN)}>
            {i.ok ? '✓' : '✗'} {i.name.replace(/_/g, ' ')}</span>
        ))}
      </div>
      {/* per-component ladder */}
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1.6fr) auto auto auto auto', gap: '2px 8px',
                    fontSize: 10, alignItems: 'center' }}>
        <span style={{ fontSize: 8.5, color: COL_MUTE }}>COMPONENT</span>
        <span style={{ fontSize: 8.5, color: COL_MUTE }}>CAP→EFFECTIVE</span>
        <span style={{ fontSize: 8.5, color: COL_MUTE }}>TIER</span>
        <span style={{ fontSize: 8.5, color: COL_MUTE }}>STATUS</span>
        <span style={{ fontSize: 8.5, color: COL_MUTE }}>KILL</span>
        {comps.map(c => (
          <React.Fragment key={c.component}>
            <span title={c.note} style={{ color: '#cdd6e6', whiteSpace: 'nowrap', overflow: 'hidden',
              textOverflow: 'ellipsis' }}>{c.label}</span>
            <span style={{ whiteSpace: 'nowrap' }}>
              <span style={{ color: COL_MUTE }}>{c.declared_cap}</span>
              <span style={{ color: COL_MUTE }}> → </span>
              <b style={{ color: authColor(c.effective_authority) }}>{c.effective_authority}</b>
            </span>
            <span style={{ color: c.risk_tier >= 2 ? COL_WARN : COL_MUTE, textAlign: 'center' }}>{c.risk_tier}</span>
            <span style={{ color: c.live ? authColor(c.effective_authority) : COL_MUTE, fontSize: 9 }}>{c.status}</span>
            <span style={{ color: COL_MUTE, fontSize: 8.5, whiteSpace: 'nowrap', overflow: 'hidden',
              textOverflow: 'ellipsis' }}>{c.kill_switch ? c.kill_switch.replace('scibrain:', '') : '—'}</span>
          </React.Fragment>
        ))}
      </div>
      <div style={{ fontSize: 9, color: COL_MUTE, marginTop: 6 }}>
        live capital-affecting authority: <b style={{ color: COL_LONG }}>
          {(a.summary?.live_capital_components || []).join(', ') || 'none'}</b> — the only paths that can move
        capital right now. Tier-0 instruments/learning hold no live authority.</div>
    </div>
  );
};

// The validated-lifecycle PIPELINE: the canonical promotion path (proposed → … → retained) with the
// live count of hypotheses sitting at each state. Terminal failure states shown separately as off-ramps.
const LifecyclePipeline: React.FC<{ lc: any }> = ({ lc }) => {
  const states: string[] = lc?.states || [];
  const terminal: string[] = lc?.terminal || [];
  const byStatus: Record<string, number> = lc?.by_status || {};
  const promotion = states.filter(s => !terminal.includes(s));
  const failTerminal = terminal.filter(s => s !== 'retained');
  const Node = (s: string) => {
    const n = byStatus[s] || 0;
    const col = statusCol(s);
    return (
      <span key={s} style={{ display: 'inline-flex', flexDirection: 'column', alignItems: 'center',
                             minWidth: 0, opacity: n ? 1 : 0.5 }}>
        <span style={{ width: 22, height: 22, borderRadius: 11, border: `2px solid ${col}`,
                       display: 'flex', alignItems: 'center', justifyContent: 'center',
                       background: n ? col : '#0c1322', color: n ? '#08111f' : col, fontWeight: 700,
                       fontSize: 10 }}>{n}</span>
        <span style={{ fontSize: 8, color: col, marginTop: 2, whiteSpace: 'nowrap' }}>{s}</span>
      </span>
    );
  };
  return (
    <div style={card}>
      <div style={sectionTitle}>validated lifecycle — {lc?.n || 0} hypotheses on the promotion pipeline</div>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 2, flexWrap: 'wrap' }}>
        {promotion.map((s, i) => (
          <React.Fragment key={s}>
            {Node(s)}
            {i < promotion.length - 1 && <span style={{ color: '#3a4a63', alignSelf: 'center',
              padding: '0 1px', fontSize: 11 }}>→</span>}
          </React.Fragment>
        ))}
      </div>
      {failTerminal.some(s => byStatus[s]) && (
        <div style={{ display: 'flex', gap: 8, marginTop: 6, alignItems: 'center' }}>
          <span style={{ fontSize: 8.5, color: COL_MUTE }}>off-ramps:</span>
          {failTerminal.map(s => Node(s))}
        </div>
      )}
    </div>
  );
};

// Phase-7c per-change EVIDENCE REPORT — the four mandated evaluation dimensions (effective sample /
// conditional utility / ablation / risk) consolidated, with the gate chips that make explicit that
// promotion rests on EVIDENCE, not a fixed cycle count.
const ChangeReportCard: React.FC<{ rep: any }> = ({ rep }) => {
  const es = rep.effective_sample || {}, cu = rep.conditional_utility || {};
  const ab = rep.ablation || {}, rk = rep.risk || {}, db = rep.decision_basis || {};
  const gates: Record<string, boolean> = db.gates || {};
  return (
    <div style={{ ...card, borderColor: '#1c3340', background: '#0d1622', marginTop: 6 }}>
      <div style={sectionTitle}>per-change evidence report · promotion rests on evidence, not a cycle count</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '2px 8px', fontSize: 9.5 }}>
        <span style={{ color: '#5a86c8' }}>effective sample</span>
        <span style={{ color: COL_MUTE }}>ESS <b style={{ color: es.sufficient ? COL_LONG : COL_WARN }}>
          {fmt(es.effective_sample_size, 1)}</b> / min {es.min_required} · {es.n_direction_changed ?? '—'} dir-changes
          of {es.n_cohort ?? '—'} cohort · fidelity {fmt(es.replay_fidelity, 2)}</span>
        <span style={{ color: '#5a86c8' }}>conditional utility</span>
        <span style={{ color: COL_MUTE }}>meanΔU <b style={{ color: icCol(cu.mean_delta_utility) }}>
          {fmt(cu.mean_delta_utility)}</b> · DR {fmt(cu.off_policy_dr_mean)} · adjLCB {fmt(cu.complexity_adjusted_lcb)}
          {cu.conditional_law ? <span style={{ color: '#7f8da3' }}> · {cu.conditional_law}</span> : null}</span>
        <span style={{ color: '#5a86c8' }}>ablation</span>
        <span style={{ color: COL_MUTE }}>remove→0 ΔU {fmt(ab.remove_candidate_to_zero?.mean_delta_utility)} ·
          {' '}{ab.interpretation}</span>
        <span style={{ color: '#5a86c8' }}>risk</span>
        <span style={{ color: COL_MUTE }}>CVaR₅ <b style={{ color: rk.tail_worsens ? COL_WARN : COL_MUTE }}>
          {fmt(rk.cvar5_delta_utility)}</b> · worst {fmt(rk.worst_delta)} · wf-stability {fmt(rk.walk_forward_stability, 2)}
          {' '}· <b style={{ color: rk.risk_ok ? COL_LONG : COL_WARN }}>{rk.risk_ok ? 'tail ok' : 'tail risk'}</b></span>
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, marginTop: 6 }}>
        {Object.entries(gates).map(([k, v]) => (
          <span key={k} style={pill(v ? COL_LONG : COL_MUTE)}>{v ? '✓' : '·'} {k.replace(/_/g, ' ')}</span>
        ))}
      </div>
    </div>
  );
};

// One hypothesis = a genealogy node. Shows the bounded ChangeSpec, the champion(current)→challenger
// (candidate) scorecard with its lower-confidence bound + verdict, the transition lineage, and versions.
const HypothesisCard: React.FC<{ h: any; open: boolean; onToggle: () => void }> = ({ h, open, onToggle }) => {
  const sc = h.scorecard || {};
  const lcb = sc.lcb_delta_utility;
  const verdict = sc.verdict;
  const vCol = verdict === 'pass' ? COL_LONG
    : (verdict && /fail|reject|unstable/.test(verdict)) ? COL_SHORT
    : COL_WARN;
  return (
    <div style={{ ...card, borderColor: statusCol(h.status) }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }} onClick={onToggle}>
        <span style={pill(statusCol(h.status))}>{h.status}</span>
        <b style={{ fontSize: 11.5, color: '#cdd6e6' }}>{h.target}</b>
        <span style={{ fontSize: 10, color: COL_MUTE }}>
          {fmt(h.old, 2)} → <b style={{ color: COL_WARN }}>{fmt(h.candidate, 2)}</b></span>
        <span style={{ fontSize: 9.5, color: COL_MUTE }}>· {h.proposer}</span>
        {verdict && <span style={{ ...pill(vCol), marginLeft: 'auto' }}>{verdict}</span>}
        <span style={{ fontSize: 11, color: COL_MUTE }}>{open ? '▾' : '▸'}</span>
      </div>
      {/* champion vs challenger headline */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 6, fontSize: 9.5,
                    color: COL_MUTE, flexWrap: 'wrap' }}>
        <span>cohort <b style={{ color: '#cdd6e6' }}>{sc.n_cohort ?? '—'}</b></span>
        <span>support(flip) <b style={{ color: sc.support_changed ? COL_LONG : COL_MUTE }}>
          {sc.support_changed ?? '—'}</b></span>
        <span>fidelity <b style={{ color: '#cdd6e6' }}>{sc.replay_fidelity != null
          ? `${(sc.replay_fidelity * 100).toFixed(0)}%` : '—'}</b></span>
        <span>ΔU <b style={{ color: icCol(sc.mean_delta_utility) }}>{fmt(sc.mean_delta_utility)}</b></span>
        <span>LCB(ΔU) <b style={{ color: icCol(lcb) }}>{fmt(lcb)}</b>
          <span style={{ color: '#566' }}> {' '}(falsifier: {h.falsifier || 'LCB≤0'})</span></span>
      </div>
      {open && (
        <div style={{ marginTop: 7, borderTop: '1px solid #1c2742', paddingTop: 7 }}>
          <div style={{ fontSize: 9.5, color: COL_MUTE, marginBottom: 5 }}>
            <b style={{ color: '#9fb0c8' }}>predicate:</b> {h.context_predicate || '—'} ·{' '}
            <b style={{ color: '#9fb0c8' }}>kind:</b> {h.kind} · bounds {JSON.stringify(h.parameter_bounds)} ·{' '}
            complexity {h.complexity_cost ?? '—'}</div>
          {h.expected_effect && (
            <div style={{ fontSize: 9.5, color: '#9aa7bd', marginBottom: 6, lineHeight: 1.4 }}>
              <b style={{ color: '#9fb0c8' }}>expected:</b> {h.expected_effect}</div>)}
          {sc.reason && (
            <div style={{ fontSize: 9.5, color: COL_WARN, marginBottom: 6 }}>
              <b>verdict basis:</b> {sc.reason}</div>)}
          {/* sizing interaction — a gain tweak usually rescales size rather than flipping direction */}
          {sc.n_conviction_shifted != null && (
            <div style={{ fontSize: 9, color: COL_MUTE, marginBottom: 6 }}>
              sizing interaction: {sc.n_conviction_shifted} trades re-sized (mean Δconviction{' '}
              {fmt(sc.mean_conviction_delta)}) · ablation(remove→0) LCB {fmt(sc.ablation_remove?.lcb_delta_utility)}</div>)}
          {sc.rigor && (
            <div style={{ fontSize: 9, color: COL_MUTE, marginBottom: 6 }}>
              rigor: <b style={{ color: statusCol(sc.rigor.verdict) }}>{sc.rigor.verdict || '—'}</b>
              {Array.isArray(sc.rigor.reasons) && sc.rigor.reasons.length ? ` · ${sc.rigor.reasons.join('; ')}` : ''}</div>)}
          {/* SPIBB baseline bootstrapping — shown when the challenger flips any direction (so there is a
              policy to restrict). Surfaces the supported contexts, restricted-policy LCB, and fallback frac. */}
          {sc.baseline_bootstrap && sc.baseline_bootstrap.n_changed_total > 0 && (
            <div style={{ fontSize: 9, color: COL_MUTE, marginBottom: 6 }}>
              baseline-bootstrap: <b style={{ color: (sc.baseline_bootstrap.bootstrapped_lcb ?? 0) > 0 ? COL_LONG : COL_MUTE }}>
                {(sc.baseline_bootstrap.applicable_contexts || []).length} supported context(s)</b>
              {' '}· restricted LCB <b style={{ color: icCol(sc.baseline_bootstrap.bootstrapped_lcb) }}>
                {fmt(sc.baseline_bootstrap.bootstrapped_lcb)}</b>
              {' '}· champion fallback {fmt((sc.baseline_bootstrap.fallback_fraction ?? 0) * 100, 0)}%
              {(sc.applicable_contexts && sc.applicable_contexts.length) ?
                <span style={{ color: COL_LONG }}> · acts only in [{sc.applicable_contexts.join(', ')}]</span> : null}
              {(sc.baseline_bootstrap.per_context || []).filter((c: any) => c.supported).map((c: any) => (
                <span key={c.context} style={{ marginLeft: 6, color: '#7f8da3' }}>
                  {c.context}:{c.n}(lcb {fmt(c.lcb)})</span>))}
            </div>)}
          {/* Phase-7c per-change EVIDENCE REPORT — the four mandated dimensions + the explicit
              "promotion rests on evidence, NOT an arbitrary cycle count" decision basis. */}
          {sc.report && <ChangeReportCard rep={sc.report} />}
          {/* transition lineage = the genealogy edges */}
          <div style={sectionTitle}>transition lineage ({h.n_evaluations} evaluations)</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, alignItems: 'center', marginBottom: 6 }}>
            {(h.history || []).map((ev: any, i: number) => (
              <span key={i} style={{ fontSize: 9, color: COL_MUTE }}>
                <span style={{ color: statusCol(ev.from) }}>{ev.from}</span>
                <span style={{ color: '#3a4a63' }}> → </span>
                <span style={{ color: statusCol(ev.to) }}>{ev.to}</span>
                <span style={{ color: '#566' }}> ({fmtTs(ev.ts)})</span>
                {i < h.history.length - 1 && <span style={{ color: '#2c3a52' }}>{'  |  '}</span>}
              </span>
            ))}
            {!(h.history || []).length && <span style={{ fontSize: 9, color: COL_MUTE }}>no transitions yet</span>}
          </div>
          {h.versions && (
            <div style={{ fontSize: 8.5, color: '#566' }}>
              code {h.versions.code_fingerprint} · commit {h.versions.code_commit} ·{' '}
              config {h.versions.config_hash} · schema {h.versions.changespec_schema}</div>)}
        </div>
      )}
    </div>
  );
};

// Generalized hypothesis memory — knob+direction changes the council remembers so it won't re-propose
// a previously-failed direction (negative) and recalls validated laws (positive).
const MemoryBanks: React.FC<{ mem: any }> = ({ mem }) => {
  const neg: any[] = mem?.negative || [];
  const pos: any[] = mem?.positive || [];
  const Row = (m: any, col: string) => (
    <div key={m.signature} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 9.5,
                                    padding: '2px 0' }}>
      <span style={pill(col)}>×{m.n}</span>
      <span style={{ color: '#cdd6e6' }}>{m.signature}</span>
      {m.last_status && <span style={{ color: COL_MUTE }}>· {m.last_status}</span>}
      {m.reasons?.[0] && <span style={{ color: COL_MUTE, fontSize: 9 }}>· {m.reasons[0]}</span>}
    </div>
  );
  return (
    <div style={card}>
      <div style={sectionTitle}>hypothesis memory — generalized knob+direction (council never repeats a failed change)</div>
      <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
        <div style={{ flex: 1, minWidth: 220 }}>
          <div style={{ fontSize: 9, color: COL_SHORT, marginBottom: 3 }}>NEGATIVE — must not repeat ({neg.length})</div>
          {neg.length ? neg.map(m => Row(m, COL_SHORT))
            : <div style={{ fontSize: 9.5, color: COL_MUTE }}>none yet — no change has been rejected/demoted/rolled-back.</div>}
        </div>
        <div style={{ flex: 1, minWidth: 220 }}>
          <div style={{ fontSize: 9, color: COL_LONG, marginBottom: 3 }}>POSITIVE — retained laws ({pos.length})</div>
          {pos.length ? pos.map(m => Row(m, COL_LONG))
            : <div style={{ fontSize: 9.5, color: COL_MUTE }}>none yet — no change has been retained as a law.</div>}
        </div>
      </div>
    </div>
  );
};

// Advisory per-module competence map: rolling IC (directional skill) + the ablation verdict + redundancy
// + current authority. Honest — no fabricated regime/symbol/horizon grid (the backend says so too).
const CompetenceMap: React.FC<{ comp: any }> = ({ comp }) => {
  const mods: any[] = comp?.modules || [];
  const maxIc = Math.max(0.05, ...mods.map(m => Math.abs(m.ic || 0)));
  return (
    <div style={card}>
      <div style={sectionTitle}>module competence — rolling IC + advisory ablation verdict (never auto-acts) ·{' '}
        {comp?.n_matured ?? '—'}/{comp?.n_modules ?? '—'} matured</div>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 6 }}>
        {Object.entries(comp?.summary || {}).map(([k, v]) => (
          <span key={k} style={pill(VERDICT_COL[k] || COL_MUTE)}>{k} {v as number}</span>))}
      </div>
      {mods.map(m => {
        const ic = m.ic;
        const half = ic == null ? 0 : Math.min(1, Math.abs(ic) / maxIc) * 0.5;
        return (
          <div key={m.module} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 9.5,
                                       padding: '1.5px 0' }}>
            <span style={{ width: 150, color: '#cdd6e6', whiteSpace: 'nowrap', overflow: 'hidden',
                           textOverflow: 'ellipsis' }}>{m.module}</span>
            <span style={{ ...pill(VERDICT_COL[m.status] || COL_MUTE), width: 78, textAlign: 'center' }}>
              {m.status || '—'}</span>
            {/* centred signed-IC bar: left = anti-signal, right = informative */}
            <span style={{ position: 'relative', width: 120, height: 9, background: '#1c2333',
                           borderRadius: 3, flexShrink: 0 }}>
              <span style={{ position: 'absolute', left: '50%', top: 0, bottom: 0, width: 1, background: '#3a4a63' }} />
              {ic != null && <span style={{ position: 'absolute', top: 0, height: '100%',
                background: icCol(ic), borderRadius: 2,
                ...(ic >= 0 ? { left: '50%', width: `${half * 100}%` }
                            : { right: '50%', width: `${half * 100}%` }) }} />}
            </span>
            <span style={{ width: 64, textAlign: 'right', color: icCol(ic) }}>IC {fmt(ic, 3)}</span>
            <span style={{ width: 42, textAlign: 'right', color: COL_MUTE }}>n {m.samples ?? 0}</span>
            <span style={{ flex: 1, color: COL_MUTE, fontSize: 8.5, whiteSpace: 'nowrap', overflow: 'hidden',
                           textOverflow: 'ellipsis' }}>
              {m.evidence_family && m.evidence_family !== 'unspecified' ? `${m.evidence_family} · ` : ''}
              {m.current_authority || ''}{m.reasons?.[0] ? ` · ${m.reasons[0]}` : ''}</span>
          </div>
        );
      })}
    </div>
  );
};

// Self-improvement unification (design §11) — every autonomous mechanism under ONE kernel, its kernel
// mode, and the newest-first LINEAGE of actual kernel-routed changes (bounded controller writes +
// ModelChangeSpec records). open_bypasses=0 means none self-applies outside the kernel.
const MODE_COL: Record<string, string> = {
  kernel: COL_LONG, bounded_recorded: '#5a86c8', model_recorded: '#d9a23b',
  in_loop_bounded: '#5a86c8', safety_only: COL_MUTE, disabled: COL_MUTE, legacy_direct: COL_SHORT,
};
const SelfImprovementBus: React.FC<{ si: any }> = ({ si }) => {
  if (!si || !si.available) return null;
  const producers: any[] = si.producers || [];
  const changes: any[] = si.recent_changes || [];
  const noBypass = (si.n_open_bypasses ?? 0) === 0;
  return (
    <div style={{ ...card, borderColor: noBypass ? '#2c3a18' : '#5a1d1d' }}>
      <div style={sectionTitle}>
        self-improvement unification — {si.n_producers} producers under one kernel ·{' '}
        <b style={{ color: noBypass ? COL_LONG : COL_SHORT }}>
          {noBypass ? 'no bypass' : `${si.n_open_bypasses} OPEN BYPASS`}</b> · mode {si.mode}</div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, marginBottom: 7 }}>
        {Object.entries(si.by_effective_mode || {}).map(([m, n]) => (
          <span key={m} style={pill(MODE_COL[m] || COL_MUTE)}>{m.replace(/_/g, ' ')} {n as number}</span>))}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1.3fr) auto 1fr', gap: '1px 8px',
                    fontSize: 9.5, marginBottom: 7 }}>
        {producers.map(p => (
          <React.Fragment key={p.id}>
            <span title={p.applies_to} style={{ color: '#cdd6e6', whiteSpace: 'nowrap', overflow: 'hidden',
              textOverflow: 'ellipsis' }}>{p.label}</span>
            <span style={{ color: MODE_COL[p.effective_mode] || COL_MUTE, fontWeight: 700,
              whiteSpace: 'nowrap' }}>{p.effective_mode}</span>
            <span style={{ color: COL_MUTE, fontSize: 8.5 }}>{p.last ? `last ${fmtTs(p.last.ts)}` : '—'}</span>
          </React.Fragment>
        ))}
      </div>
      <div style={sectionTitle}>change lineage — actual kernel-routed changes (newest first)</div>
      <div style={{ maxHeight: 160, overflowY: 'auto' }}>
        {changes.length ? changes.map((c, i) => (
          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 9, padding: '1px 0' }}>
            <span style={{ color: '#566', width: 110, flexShrink: 0 }}>{fmtTs(c.ts)}</span>
            <span style={{ color: c.change_type === 'model' ? '#d9a23b' : '#5a86c8', width: 66,
              flexShrink: 0 }}>{c.kind || c.change_type}</span>
            <span style={{ color: '#cdd6e6', width: 116, flexShrink: 0, whiteSpace: 'nowrap',
              overflow: 'hidden', textOverflow: 'ellipsis' }}>{c.proposer}</span>
            <span style={{ color: COL_MUTE, flex: 1, whiteSpace: 'nowrap', overflow: 'hidden',
              textOverflow: 'ellipsis' }}>
              {c.detail?.summary || c.target}
              {c.detail?.clamped?.length ? ` · clamped ${c.detail.clamped.join(',')}` : ''}
              {c.detail?.added?.length ? ` · +${c.detail.added.length}` : ''}
              {c.detail?.removed?.length ? ` · -${c.detail.removed.length}` : ''}</span>
          </div>
        )) : <div style={{ fontSize: 9.5, color: COL_MUTE }}>no kernel-routed changes recorded yet.</div>}
      </div>
      <div style={{ fontSize: 9, color: COL_MUTE, marginTop: 5 }}>{si.note}</div>
    </div>
  );
};

// Promotion / rollback lineage — the bounded-canary event history (the ONLY capital-affecting path).
const RollbackHistory: React.FC<{ rb: any }> = ({ rb }) => {
  if (!rb) return null;
  const events: any[] = rb.events || [];
  const evName = (e: any) => String(e.event || e.action || e.type || 'event');
  return (
    <div style={card}>
      <div style={sectionTitle}>promotion / rollback lineage — bounded canary (only capital-affecting path) ·{' '}
        {rb.active_canary ? <b style={{ color: '#d9a23b' }}>1 ACTIVE</b> : 'none active'}</div>
      {events.length ? events.map((e, i) => (
        <div key={i} style={{ display: 'flex', gap: 8, fontSize: 9, padding: '1px 0', alignItems: 'center' }}>
          <span style={{ color: '#566', width: 110, flexShrink: 0 }}>{fmtTs(e.ts)}</span>
          <span style={pill(/rollback|demote/.test(evName(e)) ? COL_SHORT
            : /promote|approve/.test(evName(e)) ? COL_LONG : COL_MUTE)}>{evName(e)}</span>
          <span style={{ color: COL_MUTE, flex: 1, whiteSpace: 'nowrap', overflow: 'hidden',
            textOverflow: 'ellipsis' }}>{e.hid || e.hypothesis_id || ''} {e.reason || ''}</span>
        </div>
      )) : <div style={{ fontSize: 9.5, color: COL_MUTE }}>
        no canary promotion/rollback events yet — the gate is owner-armed, default off.</div>}
    </div>
  );
};

const LearningLab: React.FC = () => {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);

  useEffect(() => {
    getSciBrainLearning(80).then(d => { setData(d); if (!d.available) setErr(d.error || 'unavailable'); })
      .catch(e => setErr(String(e?.message || e)));
  }, []);

  const hyps: any[] = useMemo(() => data?.hypotheses || [], [data]);

  if (err) return <div style={{ color: COL_WARN, fontSize: 12, padding: 12 }}>Learning Lab unavailable: {err}</div>;
  if (!data) return <div style={{ color: COL_MUTE, fontSize: 12, padding: 12 }}>Loading learning laboratory…</div>;

  return (
    <div style={{ maxHeight: 640, overflowY: 'auto', paddingRight: 4 }}>
      <AuthorityBanner a={data.authority} />
      <AuthorityReconciliation a={data.authority} />
      <SelfImprovementBus si={data.self_improvement} />
      <RollbackHistory rb={data.rollback} />
      <LifecyclePipeline lc={data.lifecycle} />
      <div style={sectionTitle}>hypothesis genealogy — champion (current base) vs challenger (candidate), grounded on the matched cohort</div>
      {hyps.length ? hyps.map(h => (
        <HypothesisCard key={h.hypothesis_id} h={h} open={openId === h.hypothesis_id}
          onToggle={() => setOpenId(openId === h.hypothesis_id ? null : h.hypothesis_id)} />
      )) : <div style={{ ...card, fontSize: 10, color: COL_MUTE }}>
        No hypotheses registered yet — the council/learners propose bounded ChangeSpecs as evidence accrues.</div>}
      <MemoryBanks mem={data.memory} />
      <CompetenceMap comp={data.competence} />
      <div style={{ fontSize: 9, color: COL_MUTE, marginTop: 4, lineHeight: 1.5 }}>
        {data.competence?.note} {data.fdr?.n_pvals ? `· ${data.fdr.n_pvals} bootstrap p-values under the online BH/FDR guard.` : ''}
      </div>
    </div>
  );
};
export default LearningLab;
