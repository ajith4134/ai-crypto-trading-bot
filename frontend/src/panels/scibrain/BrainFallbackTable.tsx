// BrainFallbackTable — Phase-6 VS-V1. The guaranteed text/table fallback (§10.324, §11.9): every visual
// state has an exact-value table that never hides shadow/suppressed/degraded components. This is the
// existing rich per-decision evidence view (live + shadow modules, attribution, Ollama interrogation),
// preserved verbatim so the atlas can never become the only way to read the brain.
import React, { useState } from 'react';
import { COL_LONG, COL_SHORT, COL_MUTE, COL_WARN, COL_SHADOW, dirColor, pct, Bar, VoteBar } from './ui';

// One module's evidence row. Shadow modules are dimmed + badged so it stays visible which votes are
// RECORDED-ONLY (counterfactual / IC-graded, never applied to capital) vs live.
const ModuleRow: React.FC<{ m: any }> = ({ m }) => (
  <div style={{ fontSize: 11, marginBottom: 3, opacity: m.ok ? (m.shadow_only ? 0.8 : 1) : 0.4 }}>
    <span style={{ display: 'inline-block', width: 78,
                   color: m.shadow_only ? '#8aa' : '#bcd' }}>{m.module}</span>
    <VoteBar v={m.direction} />
    <span style={{ marginLeft: 8, color: COL_MUTE }}>conv </span>
    <Bar v={m.conviction} color={m.shadow_only ? '#6a7da8' : '#5b8def'} w={50} />
    <span style={{ marginLeft: 8, color: COL_WARN }}>{m.regime_tag || '—'}</span>
    {m.shadow_only && (
      <span title={`role ${m.role || '—'} · evidence ${m.evidence_family || '—'} · recorded + IC-graded, NEVER applied to a live pick`}
            style={{ marginLeft: 8, fontSize: 9, padding: '0 5px', borderRadius: 3,
                     background: '#1a2236', color: COL_SHADOW, border: '1px solid #2c3a5c' }}>
        SHADOW · {m.role || 'obs'}
      </span>)}
    <div style={{ marginLeft: 78, color: '#9aa7bd', fontSize: 10 }}>{m.explanation}</div>
  </div>
);

const Detail: React.FC<{ d: any }> = ({ d }) => {
  const modules = d.modules || [];
  const liveMods = modules.filter((m: any) => !m.shadow_only);
  const shadowMods = modules.filter((m: any) => m.shadow_only);
  const attribution = d.attribution || [];
  const rsn = d.reasoning;
  return (
    <div style={{ padding: '8px 12px', background: 'rgba(255,255,255,0.02)',
                  borderLeft: `2px solid ${dirColor(d.direction)}`, margin: '2px 0 8px' }}>
      <div style={{ fontSize: 11, color: COL_MUTE, marginBottom: 4 }}>
        MODULE EVIDENCE — LIVE (applied to the verdict)</div>
      {liveMods.map((m: any, i: number) => <ModuleRow key={i} m={m} />)}
      {shadowMods.length > 0 && (
        <div style={{ marginTop: 8, paddingTop: 6, borderTop: '1px dashed #2c3a5c' }}>
          <div style={{ fontSize: 11, color: COL_SHADOW, marginBottom: 4 }}>
            🕶 SHADOW MODULES ({shadowMods.length}) — recorded + IC-graded, NEVER applied to capital (counterfactual)
          </div>
          {shadowMods.map((m: any, i: number) => <ModuleRow key={i} m={m} />)}
        </div>
      )}
      {attribution.length > 0 && (
        <div style={{ fontSize: 11, marginTop: 6 }}>
          <span style={{ color: COL_MUTE }}>RESPONSIBLE (computed share of the verdict): </span>
          {attribution.map((a: any, i: number) => (
            <span key={i} style={{ marginRight: 10,
              color: a.aligned ? dirColor(d.direction) : COL_MUTE,
              fontWeight: d.primary_driver === a.module ? 700 : 400 }}>
              {a.module} {a.share >= 0 ? '+' : ''}{a.share}{a.aligned ? '★' : ''}
            </span>
          ))}
        </div>
      )}
      {rsn && (
        <div style={{ marginTop: 8, fontSize: 11, borderTop: '1px solid #222a3a', paddingTop: 6 }}>
          <div style={{ color: COL_MUTE, marginBottom: 2 }}>
            🤖 OLLAMA INTERROGATION {rsn.available
              ? <span style={{ color: '#6cf' }}>({rsn.lead_model} + {rsn.critic_model}, {rsn.elapsed_s}s)</span>
              : <span style={{ color: COL_WARN }}>(unavailable)</span>}
          </div>
          {rsn.available && (
            <>
              <div>
                verdict <b style={{ color: dirColor(rsn.verdict_direction) }}>{rsn.verdict_direction}</b>
                <span style={{ color: COL_MUTE }}> · conf {rsn.confidence} · </span>
                <span style={{ color: rsn.agrees_with_fusion ? COL_LONG : COL_SHORT }}>
                  {rsn.agrees_with_fusion ? 'agrees with circuit' : '⚠ DISAGREES with circuit'}
                </span>
                <span style={{ color: rsn.wrong_direction_risk >= 0.6 ? COL_SHORT : COL_MUTE }}>
                  {' '}· wrong-dir risk {rsn.wrong_direction_risk}
                </span>
              </div>
              {rsn.responsible_factor && (
                <div style={{ color: '#bcd' }}>responsible factor: <b>{rsn.responsible_factor}</b></div>)}
              <div style={{ color: '#cdd6e6', marginTop: 2 }}>{rsn.narrative}</div>
              {rsn.critic_note && (
                <div style={{ color: COL_WARN, marginTop: 2 }}>🔴 critic: {rsn.critic_note}</div>)}
            </>
          )}
        </div>
      )}
    </div>
  );
};

const BrainFallbackTable: React.FC<{
  decisions: any[];
  selected?: string | null;
  onSelect?: (sym: string) => void;
}> = ({ decisions, selected, onSelect }) => {
  const [open, setOpen] = useState<Record<string, boolean>>({});
  if (!decisions || decisions.length === 0) {
    return <div style={{ color: COL_MUTE, fontSize: 12, padding: 8 }}>
      No decisions yet — run a SciBrain cycle to populate the table.</div>;
  }
  return (
    <div>
      {decisions.map((d: any) => {
        const sym = d.symbol;
        const isOpen = !!open[sym] || sym === selected;
        const wdr = d.reasoning?.wrong_direction_risk ?? null;
        return (
          <div key={sym} style={{ borderBottom: '1px solid #1a2030',
                                  background: sym === selected ? 'rgba(91,141,239,0.04)' : undefined }}>
            <div onClick={() => { setOpen({ ...open, [sym]: !open[sym] }); onSelect && onSelect(sym); }}
                 style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '4px 6px',
                          cursor: 'pointer', fontSize: 12 }}>
              <span style={{ width: 12, color: COL_MUTE }}>{isOpen ? '▾' : '▸'}</span>
              <span style={{ width: 96, fontWeight: 600, color: dirColor(d.direction) }}>{sym}</span>
              <span style={{ width: 48, color: dirColor(d.direction) }}>
                {d.direction ? d.direction.toUpperCase() : '—'}</span>
              <Bar v={d.conviction} color={dirColor(d.direction)} />
              <span style={{ width: 44, color: COL_MUTE }}>{(d.conviction ?? 0).toFixed(2)}</span>
              <span style={{ width: 150, color: '#bcd' }}>driver: <b>{d.primary_driver || '—'}</b></span>
              <span style={{ width: 92, color: COL_WARN }}>{d.regime || '—'}</span>
              <span style={{ width: 78, color: COL_MUTE }}>move {pct(d.expected_move_pct, 2)}</span>
              <span style={{ width: 70, color: COL_MUTE }}>size {(d.size_frac ?? 0).toFixed(3)}</span>
              {wdr !== null && (
                <span style={{ color: wdr >= 0.6 ? COL_SHORT : COL_MUTE }}>
                  {wdr >= 0.6 ? '⚠ ' : ''}wd-risk {wdr}</span>)}
            </div>
            {isOpen && <Detail d={d} />}
          </div>
        );
      })}
    </div>
  );
};

export default BrainFallbackTable;
