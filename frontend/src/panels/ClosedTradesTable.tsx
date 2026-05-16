// AI-12: Panel 10 — Closed Trades Table
import React, { useEffect, useState } from 'react';
import { getClosedTrades, exportClosedCSV } from '../api';
import { card, title, Table } from './shared';

const ClosedTradesTable: React.FC = () => {
  const [trades, setTrades] = useState<any[]>([]);
  const [filter, setFilter] = useState('');

  useEffect(() => { getClosedTrades({limit:200}).then(setTrades).catch(()=>{}); }, []);

  const filtered = trades.filter(t => !filter || t.pair?.includes(filter.toUpperCase()));

  const rows = filtered.map(t => [
    t.pair, t.direction?.toUpperCase(),
    `$${(+t.entry_price||0).toLocaleString()}`,
    `$${(+t.exit_price||0).toLocaleString()}`,
    <span style={{color:(+t.net_pnl_usdt||0)>=0?'#00ff88':'#ff4444'}}>${(+t.net_pnl_usdt||0).toFixed(2)}</span>,
    `$${(+t.fees_usdt||0).toFixed(2)}`,
    `${Math.floor((+t.hold_time_seconds||0)/3600)}h`,
    t.exit_reason||'—',
    t.failure_type||'—',
    t.brain_stage||'—',
  ]);

  const doExport = async () => {
    const blob = await exportClosedCSV().then(r=>r.data);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href=url; a.download='closed_trades.csv'; a.click();
  };

  return (
    <div style={card}>
      <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:12}}>
        <h3 style={{...title,margin:0}}>Closed Trades ({trades.length})</h3>
        <div style={{display:'flex',gap:8}}>
          <input value={filter} onChange={e=>setFilter(e.target.value)} placeholder="Filter pair..." style={{background:'#0d0d1a',color:'#fff',border:'1px solid #333',borderRadius:3,padding:'2px 8px',fontSize:12}}/>
          <button onClick={doExport} style={{background:'#333',color:'#fff',border:'none',borderRadius:3,padding:'2px 8px',cursor:'pointer',fontSize:12}}>Export CSV</button>
        </div>
      </div>
      <Table cols={['Pair','Dir','Entry','Exit','Net PnL','Fees','Hold','Exit Reason','Failure','Stage']} rows={rows} maxH={280}/>
    </div>
  );
};
export default ClosedTradesTable;
