// Summary bar — total P&L, balance, win rate shown at top of dashboard
import React, { useEffect, useState } from 'react';
import { getSummary } from '../api';

const fmt = (n: number, decimals = 2) =>
  `${n >= 0 ? '+' : ''}$${Math.abs(n).toFixed(decimals)}`;

const SummaryBar: React.FC = () => {
  const [s, setS] = useState<any>({});

  const refresh = () => getSummary().then(setS).catch(() => {});

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 10000);
    return () => clearInterval(t);
  }, []);

  const tile = (label: string, value: string, color: string) => (
    <div style={{ flex: 1, textAlign: 'center', padding: '8px 12px', background: '#1a1a2e', borderRadius: 6, margin: '0 4px' }}>
      <div style={{ fontSize: 10, color: '#555', textTransform: 'uppercase', letterSpacing: 1 }}>{label}</div>
      <div style={{ fontSize: 18, fontWeight: 'bold', color, marginTop: 2 }}>{value}</div>
    </div>
  );

  const unrealColour = (s.unrealised_pnl || 0) >= 0 ? '#00ff88' : '#ff4444';
  const realisedColour = (s.realised_pnl || 0) >= 0 ? '#00ff88' : '#ff4444';

  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 0, marginBottom: 12 }}>
      {tile('Virtual Balance', `$${(s.virtual_balance || 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}`, '#00d4ff')}
      {tile('Unrealised P&L', fmt(s.unrealised_pnl || 0, 4), unrealColour)}
      {tile('Realised P&L', fmt(s.realised_pnl || 0, 4), realisedColour)}
      {tile('Open Trades', String(s.open_count || 0), '#ffaa00')}
      {tile('Closed Trades', String(s.closed_count || 0), '#888')}
      {tile('Win Rate', `${(s.win_rate || 0).toFixed(1)}%`, (s.win_rate || 0) >= 50 ? '#00ff88' : '#ff8800')}
    </div>
  );
};

export default SummaryBar;
