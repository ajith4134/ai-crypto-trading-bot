// cont. 70: Launch-Pad — the 10-deep on-deck buffer (P6). Shows the funnel slots
// with the 3 movement metrics (kept separate, not blended) + shadow MAE/MFE.
import React, { useEffect, useState } from 'react';
import { getLaunchPad } from '../api';
import { card, title, Table } from './shared';

const fmt = (v: any, d = 2) =>
  (v === null || v === undefined || v === '') ? '—' : Number(v).toFixed(d);

const pnlCell = (v: any) => {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  return <span style={{ color: n > 0 ? '#21d07a' : n < 0 ? '#ff5470' : '#888' }}>{n.toFixed(2)}%</span>;
};

const LaunchPad: React.FC = () => {
  const [data, setData] = useState<any>({ slots: [], enabled: false, open_count: 0, regime: 'unknown' });
  useEffect(() => {
    const load = () => getLaunchPad().then(setData).catch(() => {});
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  const slots = data.slots || [];
  const rows = slots.map((s: any) => [
    s.slot,
    s.symbol
      ? <span style={{ color: s.direction === 'short' ? '#ff5470' : '#21d07a', fontWeight: 600 }}>{s.symbol}</span>
      : <span style={{ color: '#555' }}>empty</span>,
    s.direction ? (s.direction === 'short' ? 'SHORT' : 'LONG') : '—',
    s.state || '—',
    s.qualified ? <span style={{ color: '#21d07a' }}>✓</span> : <span style={{ color: '#888' }}>·</span>,
    pnlCell(s.shadow_pnl_pct),
    pnlCell(s.peak_profit_pct),
    pnlCell(s.peak_loss_pct),
    fmt(s.mv_candlenet, 4),
    fmt(s.mv_predicted, 3),
    fmt(s.mv_realized, 3),
    s.flips_count ?? 0,
  ]);

  const filled = slots.filter((s: any) => s.symbol).length;
  const statusColor = data.enabled ? '#21d07a' : '#ffb454';

  return (
    <div style={{ ...card, gridColumn: '1 / -1' }}>
      <h3 style={title}>
        Launch-Pad — On-Deck Buffer ({filled}/{data.depth || 10})
        <span style={{ marginLeft: 12, color: statusColor, fontSize: 11 }}>
          {data.enabled ? '● FUNNEL LIVE' : '○ shadow-only'}
        </span>
        <span style={{ marginLeft: 12, color: '#888', fontSize: 11 }}>
          regime: {data.regime} · opens: {data.open_count}
        </span>
      </h3>
      <Table
        cols={['Slot', 'Symbol', 'Dir', 'State', 'Green', 'Shadow', 'Peak+', 'MAE', 'mvCN', 'mvPred', 'mvReal', 'Flips']}
        rows={rows}
        maxH={300}
      />
    </div>
  );
};
export default LaunchPad;
