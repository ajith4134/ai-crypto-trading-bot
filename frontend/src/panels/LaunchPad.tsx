// cont. 70: Launch-Pad — the 10-deep on-deck buffer (P6). Shows the funnel slots
// with the 3 movement metrics (kept separate, not blended) + shadow MAE/MFE.
// cont. 71: deciding badge — shown while the engine is running gates for this slot.
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

const DecidingBadge: React.FC = () => (
  <span style={{
    display: 'inline-block',
    marginLeft: 6,
    fontSize: 9,
    color: '#ffb454',
    background: 'rgba(255,180,84,0.12)',
    border: '1px solid rgba(255,180,84,0.45)',
    borderRadius: 3,
    padding: '1px 5px',
    animation: 'lp-pulse 1s ease-in-out infinite',
    verticalAlign: 'middle',
    letterSpacing: 0.5,
  }}>⏳ deciding…</span>
);

const LaunchPad: React.FC = () => {
  const [data, setData] = useState<any>({ slots: [], enabled: false, open_count: 0, regime: 'unknown' });
  useEffect(() => {
    const load = () => getLaunchPad().then(setData).catch(() => {});
    load();
    const id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, []);

  const slots = data.slots || [];
  const rows = slots.map((s: any) => [
    s.slot,
    s.symbol
      ? <span style={{ color: s.direction === 'short' ? '#ff5470' : '#21d07a', fontWeight: 600 }}>
          {s.symbol}
          {s.source === 'replay' && <small style={{ color: '#ffaa00', fontSize: 9, fontFamily: 'monospace', fontWeight: 'normal' }}> (replay signal)</small>}
          {s.deciding && <DecidingBadge />}
        </span>
      : <span style={{ color: '#555' }}>empty</span>,
    s.direction ? (s.direction === 'short' ? 'SHORT' : 'LONG') : '—',
    s.deciding
      ? <span style={{ color: '#ffb454', animation: 'lp-pulse 1s ease-in-out infinite', fontSize: 11 }}>deciding…</span>
      : (s.state || '—'),
    s.qualified ? <span style={{ color: '#21d07a' }}>✓</span> : <span style={{ color: '#888' }}>·</span>,
    pnlCell(s.shadow_pnl_pct),
    pnlCell(s.peak_profit_pct),
    pnlCell(s.peak_loss_pct),
    fmt(s.mv_candlenet, 4),
    fmt(s.mv_predicted, 3),
    fmt(s.mv_realized, 3),
    s.flips_count ?? 0,
    // cont. 70e2 — trailing price movement (last 15m/30m/1h, live)
    pnlCell(s.trail_15m), pnlCell(s.trail_30m), pnlCell(s.trail_60m),
  ]);

  const filled = slots.filter((s: any) => s.symbol).length;
  const deciding = slots.filter((s: any) => s.deciding).length;
  const statusColor = data.enabled ? '#21d07a' : '#ffb454';

  return (
    <>
      <style>{`
        @keyframes lp-pulse {
          0%, 100% { opacity: 1; }
          50%       { opacity: 0.3; }
        }
      `}</style>
      <div style={{ ...card, gridColumn: '1 / -1' }}>
        <h3 style={title}>
          Launch-Pad — On-Deck Buffer ({filled}/{data.depth || 10})
          <span style={{ marginLeft: 12, color: statusColor, fontSize: 11 }}>
            {data.enabled ? '● FUNNEL LIVE' : '○ shadow-only'}
          </span>
          <span style={{ marginLeft: 12, color: '#888', fontSize: 11 }}>
            regime: {data.regime} · opens: {data.open_count}
          </span>
          {deciding > 0 && (
            <span style={{ marginLeft: 12, color: '#ffb454', fontSize: 11, animation: 'lp-pulse 1s ease-in-out infinite' }}>
              ⏳ {deciding} deciding
            </span>
          )}
        </h3>
        <Table
          cols={['Slot', 'Symbol', 'Dir', 'State', 'Green', 'Shadow', 'Peak+', 'MAE',
                 'mvCN', 'mvPred', 'mvReal', 'Flips',
                 '15m', '30m', '1h']}
          rows={rows}
          maxH={300}
        />
        <div style={{ fontSize: 10, color: '#888', marginTop: 4 }}>
          15m/30m/1h = trailing price move (last N minutes). ⏳ deciding = engine evaluating gates for this slot.
        </div>
      </div>
    </>
  );
};
export default LaunchPad;
