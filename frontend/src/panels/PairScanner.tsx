// AI-09: Panel 7 — Pair Scanner
import React, { useEffect, useState } from 'react';
import { getActivePairs } from '../api';
import { card, title, Table } from './shared';

// cont. 70e2 — coloured price-movement % cell (green up / red down).
const mv = (v: any) => {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  if (Number.isNaN(n)) return '—';
  return (
    <span style={{ color: n > 0 ? '#21d07a' : n < 0 ? '#ff5470' : '#888' }}>
      {n >= 0 ? '+' : ''}{n.toFixed(2)}%
    </span>
  );
};

const PairScanner: React.FC = () => {
  const [pairs, setPairs] = useState<any[]>([]);
  useEffect(() => {
    const load = () => getActivePairs().then(setPairs).catch(() => {});
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  const rows = pairs.map(p => [
    p.symbol,
    p.is_active ? '✓ Active' : 'Inactive',
    p.volume_score?.toFixed(0) || '—',
    p.volatility_score?.toFixed(0) || '—',
    p.spread_score?.toFixed(0) || '—',
    p.win_rate_score?.toFixed(0) || '—',
    p.composite_score?.toFixed(1) || '—',
    // trailing-window movement (last 15m/30m/1h, live)
    mv(p.trail_15m), mv(p.trail_30m), mv(p.trail_60m),
  ]);

  return (
    <div style={card}>
      <h3 style={title}>Pair Scanner ({pairs.length} active)</h3>
      <Table
        cols={['Pair', 'Status', 'Vol', 'Volat', 'Spread', 'WinRate', 'Score',
               '15m', '30m', '1h']}
        rows={rows}
        maxH={220}
      />
      <div style={{ fontSize: 10, color: '#888', marginTop: 4 }}>
        15m/30m/1h = trailing price move (last N minutes).
      </div>
    </div>
  );
};
export default PairScanner;
