// AI-07: Panel 5 — Open Trades Table (all 15 columns, live updates)
import React, { useContext, useEffect, useState } from 'react';
import { getOpenTradesFull } from '../api';
import { WsContext } from '../context';
import { card, title, Table } from './shared';

const OpenTradesTable: React.FC = () => {
  const [trades, setTrades] = useState<any[]>([]);
  const [sort, setSort] = useState<string>('entry_time');
  const evt = useContext(WsContext);

  const [totalPnl, setTotalPnl] = useState(0);
  const refresh = () => getOpenTradesFull().then((res: any) => {
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
    const stratName = t.strategy_name || '—';
    return [
    (t.entry_source === 'replay'
      ? <span>{t.pair} <small style={{color:'#ffaa00',fontSize:9,fontFamily:'monospace'}}>(replay)</small></span>
      : t.pair),
    t.direction?.toUpperCase(),
    <span style={{color:stratName === '—' ? '#555' : '#aa88ff', fontSize:10, fontFamily:'monospace'}}>{stratName}</span>,
    `$${(+t.entry_price||0).toLocaleString(undefined,{maximumFractionDigits:6})}`,
    `$${(+(t.average_entry||t.entry_price)||0).toLocaleString(undefined,{maximumFractionDigits:6})}`,
    <span style={{color:'#00d4ff'}}>${mark.toLocaleString(undefined,{maximumFractionDigits:6})}</span>,
    <span style={{color:pnl>=0?'#00ff88':'#ff4444'}}>${pnl.toFixed(2)} <small style={{fontSize:10}}>({pct>=0?'+':''}{pct.toFixed(3)}%)</small></span>,
    (() => {
      const gain = +(t.peak_pnl_usdt||0);
      const loss = +(t.peak_loss_usdt||0);
      return (
        <span style={{fontSize:11}}>
          <span style={{color:'#00ff88'}}>+{gain.toFixed(2)}</span>
          {' / '}
          <span style={{color:'#ff4444'}}>{loss.toFixed(2)}</span>
        </span>
      );
    })(),
    (() => {
      const sl = +(t.trailing_sl_level || 0);
      const slPnl = +(t.sl_locked_pnl_usdt ?? 0);
      if (sl <= 0) return <span style={{color:'#555'}}>Setting...</span>;
      return (
        <span style={{color:'#ffaa00'}}>
          ${sl.toLocaleString(undefined,{maximumFractionDigits:6})}
          {' '}
          <small style={{fontSize:10,color:slPnl>=0?'#00ff88':'#ff4444'}}>
            ({slPnl>=0?'+':''}{slPnl.toFixed(2)}$)
          </small>
        </span>
      );
    })(),
    (() => {
      const tp1 = +(t.tp1_target || 0);
      if (tp1 <= 0) return <span style={{color:'#555'}}>—</span>;
      const entry = +(t.average_entry || t.entry_price || 0);
      const cap   = +(t.capital_usdt || 0);
      const lev   = +(t.leverage || 1);
      const dsign = t.direction === 'long' ? 1 : -1;
      let pct = +(t.mag1_pct || 0);
      if (pct === 0 && entry > 0) pct = (tp1 - entry) / entry * 100 * dsign;
      const usdt = cap * lev * Math.abs(pct) / 100;
      const fired = t.tp1_fired ? <span style={{color:'#00ff88',fontSize:9}}> ✓</span> : null;
      return (
        <span style={{color:'#00ccff'}}>
          +${usdt.toFixed(2)}
          {pct !== 0 && <small style={{fontSize:10,color:'#888'}}> ({Math.abs(pct).toFixed(2)}%)</small>}
          {fired}
        </span>
      );
    })(),
    (() => {
      const tp2 = +(t.tp2_target || 0);
      if (tp2 <= 0) return <span style={{color:'#555'}}>—</span>;
      const entry = +(t.average_entry || t.entry_price || 0);
      const cap   = +(t.capital_usdt || 0);
      const lev   = +(t.leverage || 1);
      const dsign = t.direction === 'long' ? 1 : -1;
      let pct = +(t.mag3_pct || 0);
      if (pct === 0 && entry > 0) pct = (tp2 - entry) / entry * 100 * dsign;
      const usdt = cap * lev * Math.abs(pct) / 100;
      return (
        <span style={{color:'#00ccff'}}>
          +${usdt.toFixed(2)}
          {pct !== 0 && <small style={{fontSize:10,color:'#888'}}> ({Math.abs(pct).toFixed(2)}%)</small>}
        </span>
      );
    })(),
    `$${(+t.capital_usdt||0).toFixed(0)}`,
    `${t.leverage||0}x`,
    // Notional (cont. 65f Phase 1 column derived from cap × lev for visibility)
    `$${((+t.position_size_usdt) || ((+t.capital_usdt||0) * (+t.leverage||0))).toFixed(0)}`,
    // Capital % of brain equity — Phase 1 persistence, NULL on pre-deploy trades
    t.capital_pct != null ? <span style={{color:'#ccaaff'}}>{(+t.capital_pct*100).toFixed(2)}%</span> : <span style={{color:'#555'}}>—</span>,
    // Market regime at entry
    (() => { const r = t.market_regime; if (!r) return <span style={{color:'#555'}}>—</span>;
      const c = r==='bull'?'#00ff88':r==='bear'?'#ff6666':'#ffaa00';
      return <span style={{color:c,fontSize:10}}>{r.toUpperCase()}</span>; })(),
    // Source timeframe of the originating signal
    <span style={{color:'#888',fontSize:10}}>{t.timeframe || '—'}</span>,
    // CandleNet 5m dir3: 0.0 strong-bear, 1.0 strong-bull; arrow + confidence
    (() => { const v = t.feature_vector?.cn_5m_dir3; if (v == null || +v === 0) return <span style={{color:'#555'}}>—</span>;
      const f = +v; const conf = Math.abs(f - 0.5) * 2;
      const arrow = f > 0.5 ? '↑' : '↓'; const col = f > 0.5 ? '#00ff88' : '#ff6666';
      return <span style={{color:col,fontSize:11}}>{arrow}{(conf*100).toFixed(0)}%</span>; })(),
    // CandleNet 15m dir3
    (() => { const v = t.feature_vector?.cn_15m_dir3; if (v == null || +v === 0) return <span style={{color:'#555'}}>—</span>;
      const f = +v; const conf = Math.abs(f - 0.5) * 2;
      const arrow = f > 0.5 ? '↑' : '↓'; const col = f > 0.5 ? '#00ff88' : '#ff6666';
      return <span style={{color:col,fontSize:11}}>{arrow}{(conf*100).toFixed(0)}%</span>; })(),
    // CandleNet 30m dir3 (cont. 66)
    (() => { const v = t.feature_vector?.cn_30m_dir3; if (v == null || +v === 0) return <span style={{color:'#555'}}>—</span>;
      const f = +v; const conf = Math.abs(f - 0.5) * 2;
      const arrow = f > 0.5 ? '↑' : '↓'; const col = f > 0.5 ? '#00ff88' : '#ff6666';
      return <span style={{color:col,fontSize:11}}>{arrow}{(conf*100).toFixed(0)}%</span>; })(),
    // CandleNet 1h dir3 (cont. 66 — shows '—' until a 1h forecast producer exists)
    (() => { const v = t.feature_vector?.cn_1h_dir3; if (v == null || +v === 0) return <span style={{color:'#555'}}>—</span>;
      const f = +v; const conf = Math.abs(f - 0.5) * 2;
      const arrow = f > 0.5 ? '↑' : '↓'; const col = f > 0.5 ? '#00ff88' : '#ff6666';
      return <span style={{color:col,fontSize:11}}>{arrow}{(conf*100).toFixed(0)}%</span>; })(),
    // HDBSCAN learned cluster (F9 postmortem RAG)
    t.pattern_cluster_id != null ? <span style={{color:'#aaaaff',fontFamily:'monospace',fontSize:10}}>#{t.pattern_cluster_id}</span> : <span style={{color:'#555'}}>—</span>,
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
      <Table cols={['Pair','Dir','Strategy','Entry','Avg Entry','Mark','PnL','Peak PnL','SL','TP1','TP2','Capital','Lev','Notional','Cap%','Regime','TF','CN-5m','CN-15m','CN-30m','CN-1h','Cluster','Potential','Conf','Hold','Status']} rows={rows} maxH={250}/>
    </div>
  );
};
export default OpenTradesTable;
