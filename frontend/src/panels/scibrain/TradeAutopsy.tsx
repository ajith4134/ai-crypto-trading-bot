// TradeAutopsy — Phase-6 VS-V3 Trade Autopsy Theatre (design §VS-V3 / §6.2). For one CLOSED scibrain
// trade it reconstructs, from the immutable per-trade artifacts (/scibrain/autopsy), the synchronized
// trade-life trace, the actual-vs-counterfactual policy comparison with its confidence-labelled FAULT
// CLASS, the at-open audit, the proposed circuit change (ChangeSpec analog), and replays the ENTRY-time
// brain circuit using the SAME atlas adapter as the live view (decision_snapshot ≡ a live decision).
import React, { useEffect, useMemo, useState } from 'react';
import { getSciBrainAutopsy } from '../../api';
import { COL_LONG, COL_SHORT, COL_MUTE, COL_WARN, COL_SHADOW, dirColor, Bar } from './ui';
import { toBrainGraph } from './brainGraph';
import BrainAtlas from './BrainAtlas';
import BrainInspector from './BrainInspector';

// utility is emitted as a nested {terms, lambdas, utility} object (or occasionally a bare number).
const utilNum = (u: any): number | null =>
  typeof u === 'number' ? u : (u && typeof u.utility === 'number' ? u.utility : null);
const fmtUsd = (v: any) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${Number(v).toFixed(2)}`);
const fmtPct = (v: any) => (v == null ? '—' : `${(Number(v) * 100).toFixed(2)}%`);

// FAULT CLASS — the twin's confidence-labelled verdict (none / direction / selection).
const FAULT_META: Record<string, { color: string; label: string }> = {
  none: { color: COL_LONG, label: 'no fault — our direction held up on the real path' },
  direction: { color: COL_SHORT, label: 'DIRECTION fault — the opposite policy beat us on the real path' },
  selection: { color: COL_WARN, label: 'SELECTION fault — abstaining would have beaten taking this trade' },
};
const FaultBadge: React.FC<{ cf: any }> = ({ cf }) => {
  const fc = cf?.fault_class;
  if (cf?.status !== 'ok' || !fc) {
    return <span style={{ fontSize: 10, color: COL_MUTE }}>
      counterfactual: {cf?.status || 'unavailable'} (twin could not replay this path)</span>;
  }
  const m = FAULT_META[fc] || { color: COL_MUTE, label: fc };
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 10.5 }}>
      <span style={{ padding: '1px 7px', borderRadius: 4, color: m.color, border: `1px solid ${m.color}`,
                     background: '#10162a', fontWeight: 700 }}>FAULT: {fc.toUpperCase()}</span>
      <span style={{ color: COL_MUTE }}>{m.label}
        {cf.confidence ? ` · ${cf.confidence} confidence` : ''}
        {cf.fault_margin != null ? ` · margin ${Number(cf.fault_margin).toFixed(4)}` : ''}</span>
    </span>
  );
};

// Synchronized life trace — the bounded recorded events. Per-tick path is not stored (honest), so the
// MFE/MAE excursion envelope is shown as the band and the discrete events as ordered markers.
const KIND_COLOR: Record<string, string> = {
  entry: COL_SHADOW, dca: COL_WARN, tp: COL_LONG, sl_final: COL_SHORT,
  brain_interventions: '#b08cff', exit: '#cdd6e6',
};
const LifeTrace: React.FC<{ lt: any }> = ({ lt }) => {
  const events: any[] = lt?.events || [];
  const mfe = lt?.mfe_usdt, mae = lt?.mae_usdt;
  const span = Math.max(Math.abs(mfe || 0), Math.abs(mae || 0), 1e-6);
  return (
    <div>
      <div style={{ fontSize: 8.5, color: COL_MUTE, letterSpacing: 0.3, marginBottom: 3 }}>
        LIFE TRACE — recorded events (continuous mark/fill path not stored per trade)</div>
      {/* MFE/MAE excursion envelope */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
        <span style={{ width: 64, fontSize: 9, color: COL_MUTE, textAlign: 'right' }}>excursion</span>
        <span style={{ position: 'relative', flex: 1, height: 12, background: '#1c2333', borderRadius: 3 }}>
          <span style={{ position: 'absolute', left: '50%', top: 0, bottom: 0, width: 1, background: '#3a4a63' }} />
          {mae != null && <span style={{ position: 'absolute', top: 0, height: '100%', background: COL_SHORT,
            right: '50%', width: `${(Math.abs(mae) / span) * 50}%`, borderRadius: '3px 0 0 3px' }} />}
          {mfe != null && <span style={{ position: 'absolute', top: 0, height: '100%', background: COL_LONG,
            left: '50%', width: `${(Math.abs(mfe) / span) * 50}%`, borderRadius: '0 3px 3px 0' }} />}
        </span>
        <span style={{ fontSize: 9, color: COL_MUTE, width: 120 }}>
          MAE <b style={{ color: COL_SHORT }}>{fmtUsd(mae)}</b> · MFE <b style={{ color: COL_LONG }}>{fmtUsd(mfe)}</b></span>
      </div>
      {/* ordered discrete events */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
        {events.map((e, i) => {
          const col = KIND_COLOR[e.kind] || COL_MUTE;
          const detail = e.kind === 'entry' ? `@${e.price ?? '—'} ${e.direction || ''} ×${e.leverage || ''}`
            : e.kind === 'exit' ? `@${e.price ?? '—'} · ${e.reason || ''} · ${fmtUsd(e.net_pnl_usdt)}`
            : e.kind === 'sl_final' ? `level ${e.level}`
            : e.kind === 'tp' ? `${e.level} → ${e.target ?? '—'}`
            : e.kind === 'dca' ? `${e.round} @${e.price ?? '—'}`
            : e.kind === 'brain_interventions' ? `${e.count} action(s)${e.influenced ? ' · influenced' : ''}`
            : '';
          return (
            <span key={i} style={{ display: 'inline-flex', flexDirection: 'column', minWidth: 0,
                                   borderLeft: `3px solid ${col}`, padding: '1px 6px', background: '#121826',
                                   borderRadius: 3, fontSize: 9.5 }}>
              <b style={{ color: col }}>{i + 1}. {e.kind}</b>
              <span style={{ color: COL_MUTE }}>{detail}</span>
            </span>
          );
        })}
        {events.length === 0 && <span style={{ color: COL_MUTE, fontSize: 10 }}>no recorded events</span>}
      </div>
    </div>
  );
};

// Actual vs counterfactual policy comparison (the twin replayed actual / opposite / abstain on the real
// forward path). Highlights which policy won → the fault class follows from it.
const PolicyRow: React.FC<{ name: string; p: any; best: boolean }> = ({ name, p, best }) => {
  const u = utilNum(p?.utility);
  const pnl = p?.net_pnl ?? p?.net_pnl_usdt;
  const col = u == null ? COL_MUTE : u >= 0 ? COL_LONG : COL_SHORT;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '2px 0' }}>
      <span style={{ width: 70, fontSize: 10, color: best ? '#cdd6e6' : COL_MUTE, fontWeight: best ? 700 : 400 }}>
        {best ? '★ ' : ''}{name}</span>
      <span style={{ width: 52, fontSize: 9.5, color: COL_MUTE }}>{p?.direction || '—'}</span>
      <Bar v={Math.min(1, Math.abs(u || 0) * 6)} color={col} w={70} />
      <span style={{ width: 70, fontSize: 9.5, color: col, textAlign: 'right' }}>util {u == null ? '—' : u.toFixed(4)}</span>
      <span style={{ width: 70, fontSize: 9.5, color: COL_MUTE, textAlign: 'right' }}>pnl {fmtUsd(pnl)}</span>
    </div>
  );
};

const TradeAutopsy: React.FC = () => {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string | null>(null);
  const [selId, setSelId] = useState<string | null>(null);
  const [nodeSel, setNodeSel] = useState<{ id: string | null; kind: 'node' | 'edge' }>({ id: null, kind: 'node' });

  useEffect(() => {
    getSciBrainAutopsy(16).then(d => { setData(d); if (!d.available) setErr(d.error || 'unavailable'); })
      .catch(e => setErr(String(e?.message || e)));
  }, []);

  const trades: any[] = data?.trades || [];
  const t = trades.find(x => x.trade_id === selId) || trades[0] || null;
  const graph = useMemo(() => toBrainGraph(t?.decision), [t]);

  if (err) return <div style={{ color: COL_WARN, fontSize: 12, padding: 12 }}>Autopsy unavailable: {err}</div>;
  if (!data) return <div style={{ color: COL_MUTE, fontSize: 12, padding: 12 }}>Loading trade autopsies…</div>;
  if (!trades.length) return <div style={{ color: COL_MUTE, fontSize: 12, padding: 12 }}>
    No closed scibrain trades with a matured outcome packet yet.</div>;

  const cf = t.counterfactual || {};
  const oc = t.outcome || {};
  const util = utilNum(oc.utility);
  const policies = [
    { name: 'actual', p: cf.actual, u: utilNum(cf.actual?.utility) },
    { name: 'opposite', p: cf.opposite, u: utilNum(cf.opposite?.utility) },
    { name: 'abstain', p: cf.abstain, u: utilNum(cf.abstain?.utility) },
  ];
  const bestName = cf.status === 'ok'
    ? policies.reduce((a, b) => ((b.u ?? -Infinity) > (a.u ?? -Infinity) ? b : a)).name : null;

  return (
    <div>
      {/* trade selector */}
      <div style={{ display: 'flex', gap: 6, overflowX: 'auto', paddingBottom: 6, marginBottom: 6 }}>
        {trades.map(x => {
          const on = x.trade_id === t.trade_id;
          const fc = x.counterfactual?.fault_class;
          const fcCol = fc === 'direction' ? COL_SHORT : fc === 'selection' ? COL_WARN
            : fc === 'none' ? COL_LONG : COL_MUTE;
          return (
            <button key={x.trade_id} onClick={() => { setSelId(x.trade_id); setNodeSel({ id: null, kind: 'node' }); }}
              aria-pressed={on} aria-label={`Autopsy ${x.pair} ${x.direction}`}
              style={{ cursor: 'pointer', flex: '0 0 auto', fontSize: 10, padding: '3px 8px', borderRadius: 4,
                       background: on ? '#1b2540' : '#10162a', color: on ? '#cdd6e6' : COL_MUTE,
                       border: `1px solid ${on ? '#3a4a63' : '#1f2a3a'}` }}>
              <b style={{ color: dirColor(x.direction) }}>{x.pair}</b>
              <span style={{ color: (x.net_pnl_usdt ?? 0) >= 0 ? COL_LONG : COL_SHORT }}> {fmtUsd(x.net_pnl_usdt)}</span>
              {fc && <span style={{ color: fcCol }}> ●</span>}
            </button>
          );
        })}
      </div>

      {/* header */}
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, flexWrap: 'wrap', marginBottom: 6 }}>
        <b style={{ color: dirColor(t.direction), fontSize: 13 }}>{t.pair} · {(t.direction || '').toUpperCase()}</b>
        <span style={{ fontSize: 10.5, color: COL_MUTE }}>
          entry {t.entry_price ?? '—'} → exit {t.exit_price ?? '—'} · {t.exit_reason || '—'}
          {' · '}hold {t.hold_time_s != null ? `${Math.round(t.hold_time_s / 60)}m` : '—'}</span>
        <span style={{ fontSize: 11 }}>net <b style={{ color: (t.net_pnl_usdt ?? 0) >= 0 ? COL_LONG : COL_SHORT }}>
          {fmtUsd(t.net_pnl_usdt)} USDT</b></span>
        <span style={{ fontSize: 11, color: COL_MUTE }}>ROC {fmtPct(oc.return_on_capital)} · utility{' '}
          <b style={{ color: util == null ? COL_MUTE : util >= 0 ? COL_LONG : COL_SHORT }}>
            {util == null ? '—' : util.toFixed(4)}</b></span>
        <span style={{ fontSize: 11, color: oc.won ? COL_LONG : COL_SHORT }}>{oc.won ? 'WON' : 'LOST'}</span>
      </div>

      <div style={{ display: 'flex', gap: 12, alignItems: 'flex-start', flexWrap: 'wrap' }}>
        {/* left: life trace + counterfactual + audit + remediation */}
        <div style={{ flex: '1 1 460px', minWidth: 360, display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div style={{ background: '#0c111b', border: '1px solid #1a2233', borderRadius: 8, padding: '8px 10px' }}>
            <LifeTrace lt={t.lifetrace} />
          </div>

          <div style={{ background: '#0c111b', border: '1px solid #1a2233', borderRadius: 8, padding: '8px 10px' }}>
            <div style={{ fontSize: 8.5, color: COL_MUTE, letterSpacing: 0.3, marginBottom: 4 }}>
              ACTUAL vs COUNTERFACTUAL — policies replayed on the real forward path ({cf.path_bars ?? '—'} bars, {cf.tf || '—'})</div>
            {cf.status === 'ok' ? policies.map(p =>
              <PolicyRow key={p.name} name={p.name} p={p.p} best={p.name === bestName} />)
              : <div style={{ fontSize: 10, color: COL_MUTE }}>twin replay {cf.status || 'unavailable'}</div>}
            <div style={{ marginTop: 6, borderTop: '1px solid #1a2233', paddingTop: 5 }}>
              <FaultBadge cf={cf} />
            </div>
          </div>

          {(t.audit?.narrative || t.audit?.verdict_direction) && (
            <div style={{ background: 'rgba(91,141,239,0.05)', border: '1px solid #1f2a3a', borderRadius: 8, padding: '8px 10px' }}>
              <div style={{ fontSize: 8.5, color: '#6cf', letterSpacing: 0.3, marginBottom: 2 }}>AT-OPEN AUDIT</div>
              <div style={{ fontSize: 10.5 }}>
                verdict <b style={{ color: dirColor(t.audit.verdict_direction) }}>{t.audit.verdict_direction || '—'}</b>
                <span style={{ color: t.audit.agrees_with_fusion ? COL_LONG : COL_SHORT }}>
                  {' '}· {t.audit.agrees_with_fusion ? 'agreed' : '⚠ disagreed'}</span>
                <span style={{ color: COL_MUTE }}> · wrong-dir risk {t.audit.wrong_direction_risk ?? '—'}
                  {t.audit.responsible_factor ? ` · blames ${t.audit.responsible_factor}` : ''}</span>
              </div>
              {t.audit.narrative && <div style={{ fontSize: 10, color: '#9aa7bd', marginTop: 2 }}>{t.audit.narrative}</div>}
            </div>
          )}

          {t.remediation && (t.remediation.circuit_improvement || t.remediation.reason) && (
            <div style={{ background: 'rgba(176,140,255,0.06)', border: '1px solid #2a2440', borderRadius: 8, padding: '8px 10px' }}>
              <div style={{ fontSize: 8.5, color: '#b08cff', letterSpacing: 0.3, marginBottom: 2 }}>
                PROPOSED CIRCUIT CHANGE (ChangeSpec) — shadow, never auto-applied</div>
              <div style={{ fontSize: 10.5, color: '#cdd6e6' }}>
                {t.remediation.module_to_adjust && t.remediation.module_to_adjust !== 'none' &&
                  <>🛠 <b>{t.remediation.module_to_adjust}</b> — </>}
                {t.remediation.circuit_improvement || t.remediation.reason}</div>
              {t.remediation.reason && t.remediation.circuit_improvement &&
                <div style={{ fontSize: 9.5, color: COL_MUTE, marginTop: 2 }}>{t.remediation.reason}</div>}
            </div>
          )}
        </div>

        {/* right: ENTRY-time circuit replay (same atlas adapter as the live view) + inspector */}
        <div style={{ flex: '2 1 640px', minWidth: 480 }}>
          <div style={{ fontSize: 8.5, color: COL_MUTE, letterSpacing: 0.3, marginBottom: 3 }}>
            ENTRY-TIME BRAIN CIRCUIT (why this trade was opened) — replayed from the frozen decision_snapshot</div>
          <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <BrainAtlas snap={graph} selectedId={nodeSel.id}
                onSelect={(id, kind) => setNodeSel({ id, kind })} />
            </div>
            <BrainInspector snap={graph} selectedId={nodeSel.id} selectedKind={nodeSel.kind} />
          </div>
        </div>
      </div>
    </div>
  );
};

export default TradeAutopsy;
