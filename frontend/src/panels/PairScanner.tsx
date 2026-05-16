// AI-09: Panel 7 — Pair Scanner
import React, { useEffect, useState } from 'react';
import { getActivePairs } from '../api';
import { card, title, Table } from './shared';

const PairScanner: React.FC = () => {
  const [pairs, setPairs] = useState<any[]>([]);
  useEffect(() => { getActivePairs().then(setPairs).catch(()=>{}); }, []);

  const rows = pairs.map(p => [
    p.symbol,
    p.is_active ? '✓ Active' : 'Inactive',
    p.volume_score?.toFixed(0)||'—',
    p.volatility_score?.toFixed(0)||'—',
    p.spread_score?.toFixed(0)||'—',
    p.win_rate_score?.toFixed(0)||'—',
    p.composite_score?.toFixed(1)||'—',
  ]);

  return (
    <div style={card}>
      <h3 style={title}>Pair Scanner ({pairs.length} active)</h3>
      <Table cols={['Pair','Status','Vol','Volat','Spread','WinRate','Score']} rows={rows} maxH={220}/>
    </div>
  );
};
export default PairScanner;
