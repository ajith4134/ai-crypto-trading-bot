// AI-07: Panel 5 — Open Trades Table (all 15 columns, live updates)
import React, { useContext, useEffect, useState } from 'react';
import { getOpenTrades } from '../api';
import { WsContext } from '../context';
import { card, title, Table } from './shared';

const OpenTradesTable: React.FC = () => {
  const [trades, setTrades] = useState<any[]>([]);
  const [sort, setSort] = useState<string>('entry_time');
  const evt = useContext(WsContext);

  const refresh = () => getOpenTrades().then(setTrades).catch(()=>{});
  useEffect(() => { refresh(); const t = setInterval(refresh, 5000); return () => clearInterval(t); }, []);
  useEffect(() => {
    if (evt && ['trade_opened','trade_closed','sl_moved','dca_triggered','price_update'].includes(evt.channel)) refresh();
  }, [evt]);

  const sorted = [...trades].sort((a,b) => (b[sort]||0) > (a[sort]||0) ? 1 : -1);

  const rows = sorted.map(t => {
    const pnl = +(t.current_pnl_usdt ?? t.net_pnl_usdt ?? 0);
    const mark = +(t.current_mark_price ?? t.entry_price ?? 0);
    return [
    t.pair, t.direction?.toUpperCase(),
    `$${(+t.entry_price||0).toLocaleString(undefined,{maximumFractionDigits:6})}`,
    `$${(+(t.average_entry||t.entry_price)||0).toLocaleString(undefined,{maximumFractionDigits:6})}`,
    <span style={{color:'#00d4ff'}}>${mark.toLocaleString(undefined,{maximumFractionDigits:6})}</span>,
    <span style={{color:pnl>=0?'#00ff88':'#ff4444'}}>${pnl.toFixed(4)}</span>,
    `$${(+t.peak_pnl_usdt||0).toFixed(4)}`,
    `$${(+t.trailing_sl_level||0).toLocaleString()}`,
    (() => { const d = typeof t.dca_status === 'string' ? JSON.parse(t.dca_status || '{}') : (t.dca_status || {}); return d.round_1_triggered ? (d.round_2_triggered ? 'R1+R2' : 'R1') : 'None'; })(),
    `$${(+t.capital_usdt||0).toFixed(0)}`,
    `${t.leverage||0}x`,
    t.trade_potential_score||'—',
    t.direction_confidence||'—',
    t.entry_time ? `${Math.floor((Date.now()-new Date(t.entry_time).getTime())/3600000)}h` : '—',
    <span style={{background:'#00443a',color:'#00ff88',padding:'1px 6px',borderRadius:3,fontSize:11}}>OPEN</span>,
  ]});

  return (
    <div style={card}>
      <h3 style={title}>Open Trades ({trades.length})</h3>
      <Table cols={['Pair','Dir','Entry','Avg Entry','Mark','PnL','Peak PnL','SL','DCA','Capital','Lev','Potential','Conf','Hold','Status']} rows={rows} maxH={250}/>
    </div>
  );
};
export default OpenTradesTable;
