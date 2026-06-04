// Direction Win Rate panel — added 2026-05-20 to visualise recovery from
// the sentiment-gate bias bug (see BLUEPRINT_COMPLIANCE_AUDIT.md D-03).
// Pulls /analytics/direction_win_rate; refreshes every 30s.
import React, { useEffect, useState } from 'react';
import { getDirectionWinRate } from '../api';
import { card, title } from './shared';

type Side = {
  n: number; wins: number; losses: number;
  win_pct: number | null;
  avg_pnl: number; total_pnl: number;
};
type Window = {
  long: Side; short: Side;
  total: { n: number; wins: number; win_pct: number | null; total_pnl: number };
};
type Data = {
  '1h': Window; '24h': Window; '7d': Window;
  open_now: { long: number; short: number };
};

const fmtPct = (v: number | null) =>
  v === null ? '—' : `${v.toFixed(1)}%`;
const fmtPnl = (v: number) =>
  `${v >= 0 ? '+' : ''}$${v.toFixed(2)}`;
const pctColor = (v: number | null) => {
  if (v === null) return '#888';
  if (v >= 55) return '#00ff88';
  if (v >= 45) return '#e0e0e0';
  return '#ff7777';
};
const pnlColor = (v: number) => v >= 0 ? '#00ff88' : '#ff7777';

const Bar: React.FC<{label: string; pct: number | null; n: number}> = ({label, pct, n}) => {
  const w = pct === null ? 0 : Math.max(2, Math.min(100, pct));
  return (
    <div style={{marginBottom: 6}}>
      <div style={{display: 'flex', justifyContent: 'space-between', fontSize: 11,
                   color: '#888', marginBottom: 2}}>
        <span>{label}</span>
        <span style={{color: pctColor(pct)}}>{fmtPct(pct)} ({n})</span>
      </div>
      <div style={{background: '#0d0d1a', borderRadius: 2, height: 5, overflow: 'hidden'}}>
        <div style={{
          background: pctColor(pct),
          height: 5,
          width: `${w}%`,
          transition: 'width 0.5s',
        }}/>
      </div>
    </div>
  );
};

const WindowBlock: React.FC<{label: string; w: Window}> = ({label, w}) => (
  <div style={{marginBottom: 14}}>
    <div style={{fontSize: 11, color: '#aaa', marginBottom: 6, textTransform: 'uppercase',
                 letterSpacing: 0.5}}>
      Last {label} — {w.total.n} closed, net&nbsp;
      <span style={{color: pnlColor(w.total.total_pnl)}}>{fmtPnl(w.total.total_pnl)}</span>
    </div>
    <Bar label="long"  pct={w.long.win_pct}  n={w.long.n}/>
    <Bar label="short" pct={w.short.win_pct} n={w.short.n}/>
    <div style={{display: 'flex', justifyContent: 'space-between', fontSize: 11,
                 color: '#666', marginTop: 4}}>
      <span>avg long PnL: <span style={{color: pnlColor(w.long.avg_pnl)}}>
        {fmtPnl(w.long.avg_pnl)}</span></span>
      <span>avg short PnL: <span style={{color: pnlColor(w.short.avg_pnl)}}>
        {fmtPnl(w.short.avg_pnl)}</span></span>
    </div>
  </div>
);

const DirectionWinRate: React.FC = () => {
  const [data, setData] = useState<Data | null>(null);
  const [err, setErr] = useState<string>('');

  useEffect(() => {
    const refresh = () => getDirectionWinRate()
      .then((d: Data) => { setData(d); setErr(''); })
      .catch((e: any) => setErr(e.response?.data?.detail || 'load failed'));
    refresh();
    const t = setInterval(refresh, 30000);
    return () => clearInterval(t);
  }, []);

  if (err) return (
    <div style={card}>
      <h3 style={title}>Direction Win Rate</h3>
      <div style={{color: '#ff7777', fontSize: 12}}>{err}</div>
    </div>
  );

  if (!data) return (
    <div style={card}>
      <h3 style={title}>Direction Win Rate</h3>
      <div style={{color: '#888', fontSize: 12}}>Loading…</div>
    </div>
  );

  const openLong = data.open_now.long;
  const openShort = data.open_now.short;
  const openTotal = openLong + openShort;
  const longPct = openTotal ? (100 * openLong / openTotal).toFixed(0) : '—';

  return (
    <div style={card}>
      <h3 style={title}>Direction Win Rate</h3>
      <div style={{fontSize: 11, color: '#888', marginBottom: 12,
                   background: '#0d0d1a', padding: '6px 8px', borderRadius: 4}}>
        Open now: <span style={{color: '#00d4ff'}}>{openLong}L</span>
        &nbsp;/&nbsp;
        <span style={{color: '#ffaa44'}}>{openShort}S</span>
        {openTotal > 0 && <span style={{color: '#666'}}>&nbsp;({longPct}% long)</span>}
      </div>
      <WindowBlock label="1h"  w={data['1h']}/>
      <WindowBlock label="24h" w={data['24h']}/>
      <WindowBlock label="7d"  w={data['7d']}/>
    </div>
  );
};

export default DirectionWinRate;
