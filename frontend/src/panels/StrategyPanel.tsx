// AI-10: Panel 8 — Strategy Panel
import React, { useEffect, useState } from 'react';
import { getStrategies } from '../api';
import { card, title, Table } from './shared';

const StrategyPanel: React.FC = () => {
  const [strats, setStrats] = useState<any[]>([]);
  useEffect(() => { getStrategies().then(setStrats).catch(()=>{}); }, []);

  const rows = strats.map(s => [
    s.id?.slice(0,8)||'—',
    s.status,
    `${(+s.win_rate||0).toFixed(1)}%`,
    `$${(+s.avg_pnl_usdt||0).toFixed(2)}`,
    (s.sharpe_ratio||0).toFixed(2),
    s.trade_count||0,
    s.generation||0,
    s.source||'—',
  ]);

  return (
    <div style={card}>
      <h3 style={title}>Strategies ({strats.length})</h3>
      <Table cols={['ID','Status','Win%','Avg PnL','Sharpe','Trades','Gen','Source']} rows={rows} maxH={220}/>
    </div>
  );
};
export default StrategyPanel;
