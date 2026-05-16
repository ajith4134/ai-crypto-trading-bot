// AI-07: Panel 5 — Open Trades Table (all 15 columns, live updates)
import React, { useContext, useEffect, useState } from 'react';
import { getOpenTrades } from '../api';
import { WsContext } from '../context';
import { card, title, Table } from './shared';

const OpenTradesTable: React.FC = () => {
  const [trades, setTrades] = useState<any[]>([]);
  const [sort, setSort] = useState<string>('entry_time');
  const evt = useContext(WsContext);

  const [totalPnl, setTotalPnl] = useState(0);
  const refresh = () => getOpenTrades().then((res: any) => {
    if (Array.isArray(res)) { setTrades(res); }
    else { setTrades(res.trades || []); setTotalPnl(res.total_open_pnl || 0); }
  }).catch(()=>{});
  useEffect(() => { refresh(); const t = setInterval(refresh, 5000); return () => clearInterval(t); }, []);
  useEffect(() => {
    if (evt && ['trade_opened','trade_closed','sl_moved','dca_triggered','price_update'].includes(evt.channel)) refresh();
  }, [evt]);

  const sorted = [...trades].sort((a,b) => (b[sort]||0) > (a[sort]||0) ? 1 : -1);

  const rows = sorted.map(t => {
    const pnl = +(t.net_current_pnl ?? t.current_pnl_usdt ?? t.net_pnl_usdt ?? 0);
    const pct = +(t.pct_change ?? 0);
    const mark = +(t.current_mark_price ?? t.entry_price ?? 0);
    return [
    t.pair, t.direction?.toUpperCase(),
    `$${(+t.entry_price||0).toLocaleString(undefined,{maximumFractionDigits:6})}`,
    `$${(+(t.average_entry||t.entry_price)||0).toLocaleString(undefined,{maximumFractionDigits:6})}`,
    <span style={{color:'#00d4ff'}}>${mark.toLocaleString(undefined,{maximumFractionDigits:6})}</span>,
    <span style={{color:pnl>=0?'#00ff88':'#ff4444'}}>${pnl.toFixed(2)} <small style={{fontSize:10}}>({pct>=0?'+':''}{pct.toFixed(3)}%)</small></span>,
    `$${(+t.peak_pnl_usdt||0).toFixed(4)}`,
    t.trailing_sl_level && +t.trailing_sl_level > 0 ? <span style={{color:'#ffaa00'}}>${(+t.trailing_sl_level).toLocaleString(undefined,{maximumFractionDigits:6})}</span> : <span style={{color:'#555'}}>Setting...</span>,
    (() => { const d = typeof t.dca_status === 'string' ? JSON.parse(t.dca_status || '{}') : (t.dca_status || {}); return d.round_1_triggered ? (d.round_2_triggered ? 'R1+R2' : 'R1') : 'None'; })(),
    `$${(+t.capital_usdt||0).toFixed(0)}`,
    `${t.leverage||0}x`,
    t.trade_potential_score||'—',
    t.direction_confidence||'—',
    t.hold_hours !== undefined ? `${t.hold_hours.toFixed(1)}h` : (t.entry_time ? `${((Date.now()-new Date(t.entry_time).getTime())/3600000).toFixed(1)}h` : '—'),
    <span style={{background:'#00443a',color:'#00ff88',padding:'1px 6px',borderRadius:3,fontSize:11}}>OPEN</span>,
  ]});

  return (
    <div style={card}>
      <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:12}}>
        <h3 style={{...title,margin:0}}>Open Trades ({trades.length})</h3>
        <span style={{fontSize:14,fontWeight:'bold',color:totalPnl>=0?'#00ff88':'#ff4444'}}>
          Total: {totalPnl>=0?'+':''}{totalPnl.toFixed(2)} USDT
        </span>
      </div>
      <Table cols={['Pair','Dir','Entry','Avg Entry','Mark','PnL','Peak PnL','SL','DCA','Capital','Lev','Potential','Conf','Hold','Status']} rows={rows} maxH={250}/>
    </div>
  );
};
export default OpenTradesTable;
