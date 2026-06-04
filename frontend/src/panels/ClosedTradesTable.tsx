// AI-12: Panel 10 — Closed Trades Table (all blueprint columns)
import React, { useEffect, useState } from 'react';
import { getClosedTrades, exportClosedCSV } from '../api';
import { card, title } from './shared';

const fmt6 = (v: any) => v != null ? `$${(+v).toLocaleString(undefined,{maximumFractionDigits:6})}` : '—';
const fmt2 = (v: any) => v != null ? `$${(+v).toFixed(2)}` : '—';
const fmtPct = (pnl: any, cap: any) => {
  if (pnl == null || !cap || +cap === 0) return '—';
  return `${((+pnl / +cap) * 100).toFixed(1)}%`;
};
const fmtTime = (ts: any) => {
  if (!ts) return '—';
  const d = new Date(ts);
  return `${d.toLocaleDateString()} ${d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`;
};
const fmtHold = (s: any) => {
  const sec = +s || 0;
  const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60);
  return `${h}h ${m}m`;
};
const fmtStrategy = (sid: string) => {
  if (!sid) return '—';
  // Map known UUIDs to short names
  if (sid.startsWith('32ae2a0a')) return 'OFI-S1';
  if (sid.startsWith('2090bec4')) return 'Sent-S2';
  return sid.slice(0,8);
};

const ClosedTradesTable: React.FC = () => {
  const [trades, setTrades] = useState<any[]>([]);
  const [summary, setSummary] = useState<any>({});
  const [filter, setFilter] = useState('');
  const [dirFilter, setDirFilter] = useState('');

  useEffect(() => {
    getClosedTrades({limit:200}).then((res: any) => {
      if (Array.isArray(res)) { setTrades(res); }
      else { setTrades(res.trades || []); setSummary(res.summary || {}); }
    }).catch(()=>{});
  }, []);

  const filtered = trades.filter(t =>
    (!filter || t.pair?.includes(filter.toUpperCase())) &&
    (!dirFilter || t.direction === dirFilter)
  );

  const th = (label: string, minW = 80) =>
    <th style={{padding:'4px 8px',color:'#888',borderBottom:'1px solid #2a2a4a',whiteSpace:'nowrap',
      fontWeight:500,fontSize:11,minWidth:minW,textAlign:'left'}}>{label}</th>;

  const td = (content: React.ReactNode, color?: string, align: 'left'|'right' = 'left') =>
    <td style={{padding:'3px 8px',borderBottom:'1px solid #0d0d1a',fontSize:11,
      color: color || '#d0d0d0', whiteSpace:'nowrap', textAlign:align}}>{content}</td>;

  const doExport = async () => {
    const blob = await exportClosedCSV().then(r=>r.data);
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href=url; a.download='closed_trades.csv'; a.click();
  };

  const totalPnl = +(summary.total_pnl ?? 0);
  const winRate  = +(summary.win_rate_pct ?? 0);
  const wins     = +(summary.wins ?? 0);
  const totalCount = +(summary.total ?? trades.length);

  return (
    <div style={card}>
      {/* Header */}
      <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:8}}>
        <h3 style={{...title,margin:0}}>Closed Trades ({totalCount})</h3>
        <div style={{display:'flex',gap:6,alignItems:'center'}}>
          <input value={filter} onChange={e=>setFilter(e.target.value)}
            placeholder="Filter pair..." style={{background:'#0d0d1a',color:'#fff',border:'1px solid #333',
            borderRadius:3,padding:'2px 6px',fontSize:11,width:90}}/>
          <select value={dirFilter} onChange={e=>setDirFilter(e.target.value)}
            style={{background:'#0d0d1a',color:'#fff',border:'1px solid #333',borderRadius:3,
            padding:'2px 4px',fontSize:11}}>
            <option value="">All</option>
            <option value="long">Long</option>
            <option value="short">Short</option>
          </select>
          <button onClick={doExport} style={{background:'#222',color:'#aaa',border:'1px solid #333',
            borderRadius:3,padding:'2px 8px',cursor:'pointer',fontSize:11}}>
            Export CSV
          </button>
        </div>
      </div>

      {/* Summary bar */}
      <div style={{display:'flex',gap:20,marginBottom:10,padding:'6px 10px',background:'#0a0a1a',
        borderRadius:4,fontSize:12,flexWrap:'wrap'}}>
        <span>Total PnL: <strong style={{color:totalPnl>=0?'#00ff88':'#ff4444'}}>
          {totalPnl>=0?'+':''}{totalPnl.toFixed(2)} USDT</strong></span>
        <span>Win Rate: <strong style={{color:winRate>=50?'#00ff88':'#ffaa00'}}>
          {winRate.toFixed(1)}%</strong> ({wins}W / {totalCount-wins}L)</span>
        <span>Fees: <strong style={{color:'#ff8800'}}>
          ${(+(summary.total_fees??0)).toFixed(2)}</strong></span>
        <span style={{color:'#555',fontSize:11}}>
          Showing {filtered.length} of {totalCount}</span>
      </div>

      {/* Table — horizontally scrollable */}
      <div style={{overflowX:'auto',overflowY:'auto',maxHeight:320}}>
        <table style={{borderCollapse:'collapse',fontSize:11,width:'max-content',minWidth:'100%'}}>
          <thead style={{position:'sticky',top:0,background:'#0d0d1a',zIndex:1}}>
            <tr>
              {/* Identity */}
              {th('Pair',70)} {th('Dir',45)} {th('Entry Time',110)} {th('Entry Price',90)}
              {th('Avg Entry',90)} {th('Capital',70)} {th('Lev',35)}
              {th('Notional',75)} {th('Cap%',55)} {th('Regime',55)}
              {th('CN-5m',55)} {th('CN-15m',55)} {th('CN-30m',55)} {th('CN-1h',55)} {th('Cluster',55)}
              {th('Timeframe',65)} {th('Strategy',65)} {th('Pot',40)} {th('Conf',40)}
              {/* Exit & Performance */}
              {th('Exit Price',90)} {th('Exit Time',110)} {th('Hold',60)}
              {th('Net PnL',75)} {th('PnL %',55)} {th('Final PnL',75)}
              {th('Peak +',65)} {th('Peak −',65)} {th('Fees',55)}
              {th('TP1',90)} {th('TP2',90)}
              {th('Exit Reason',90)}
              {/* Brain */}
              {th('Brain',45)} {th('Actions',50)}
              {/* Analysis */}
              {th('Failure',90)} {th('Counterfact.',80)} {th('Quality',55)}
              {th('Stage',40)}
            </tr>
          </thead>
          <tbody>
            {filtered.map((t, i) => {
              const net   = +(t.net_pnl_usdt||0);
              const cap   = +(t.capital_usdt||0);
              const peakP = +(t.peak_pnl_usdt||0);
              const peakL = +(t.peak_loss_usdt||0);
              const q     = t.trade_quality_score;
              const win   = net > 0;
              const bgRow = i%2===0 ? '#080814' : '#0a0a16';

              const failureCell = (() => {
                const ft = t.failure_type;
                if (win || !ft) return <span style={{color:'#00ff88'}}>N/A</span>;
                if (ft === 'direction') return <span style={{color:'#ff6666'}}>Direction ↕</span>;
                if (ft === 'signal')    return <span style={{color:'#ffaa00'}}>Signal ✗</span>;
                return ft;
              })();

              const qualityCell = (() => {
                if (q == null) return <span style={{color:'#555'}}>—</span>;
                const col = +q>=60?'#00ff88':+q>=40?'#ffaa00':'#ff4444';
                return <span style={{color:col}}>{(+q).toFixed(1)}</span>;
              })();

              const cfRaw = t.counterfactual_result;
              const cfVal = cfRaw != null ? +(typeof cfRaw==='object'?JSON.stringify(cfRaw):cfRaw) : null;
              const cfCell = cfVal != null
                ? <span style={{color:cfVal>=0?'#00ff88':'#ff4444'}}>{cfVal>=0?'+':''}{cfVal.toFixed(2)}</span>
                : <span style={{color:'#555'}}>—</span>;

              return (
                <tr key={t.id} style={{background:bgRow}}>
                  {/* Identity */}
                  {td(<strong style={{color:'#00d4ff'}}>{t.pair}</strong>)}
                  {td(t.direction?.toUpperCase(), t.direction==='long'?'#00ff88':'#ff6666')}
                  {td(fmtTime(t.entry_time),'#888')}
                  {td(fmt6(t.entry_price))}
                  {td(fmt6(t.average_entry),'#aaa')}
                  {td(`$${cap.toFixed(0)}`,'#e0e0e0')}
                  {td(`${t.leverage||'—'}×`,'#aaa')}
                  {/* cont. 65f — Phase 1 visibility columns */}
                  {td(`$${((+t.position_size_usdt) || (cap * (+t.leverage||0))).toFixed(0)}`,'#cce')}
                  {td(t.capital_pct != null ? <span style={{color:'#ccaaff'}}>{(+t.capital_pct*100).toFixed(2)}%</span> : <span style={{color:'#555'}}>—</span>)}
                  {td((() => { const r = t.market_regime; if (!r) return <span style={{color:'#555'}}>—</span>;
                    const c = r==='bull'?'#00ff88':r==='bear'?'#ff6666':'#ffaa00';
                    return <span style={{color:c,fontSize:10}}>{r.toUpperCase()}</span>; })())}
                  {td((() => { const v = t.feature_vector?.cn_5m_dir3; if (v == null || +v === 0) return <span style={{color:'#555'}}>—</span>;
                    const f = +v; const arrow = f > 0.5 ? '↑' : '↓'; const conf = Math.abs(f - 0.5) * 2;
                    return <span style={{color: f>0.5?'#00ff88':'#ff6666',fontSize:11}}>{arrow}{(conf*100).toFixed(0)}%</span>; })())}
                  {td((() => { const v = t.feature_vector?.cn_15m_dir3; if (v == null || +v === 0) return <span style={{color:'#555'}}>—</span>;
                    const f = +v; const arrow = f > 0.5 ? '↑' : '↓'; const conf = Math.abs(f - 0.5) * 2;
                    return <span style={{color: f>0.5?'#00ff88':'#ff6666',fontSize:11}}>{arrow}{(conf*100).toFixed(0)}%</span>; })())}
                  {td((() => { const v = t.feature_vector?.cn_30m_dir3; if (v == null || +v === 0) return <span style={{color:'#555'}}>—</span>;
                    const f = +v; const arrow = f > 0.5 ? '↑' : '↓'; const conf = Math.abs(f - 0.5) * 2;
                    return <span style={{color: f>0.5?'#00ff88':'#ff6666',fontSize:11}}>{arrow}{(conf*100).toFixed(0)}%</span>; })())}
                  {td((() => { const v = t.feature_vector?.cn_1h_dir3; if (v == null || +v === 0) return <span style={{color:'#555'}}>—</span>;
                    const f = +v; const arrow = f > 0.5 ? '↑' : '↓'; const conf = Math.abs(f - 0.5) * 2;
                    return <span style={{color: f>0.5?'#00ff88':'#ff6666',fontSize:11}}>{arrow}{(conf*100).toFixed(0)}%</span>; })())}
                  {td(t.pattern_cluster_id != null ? <span style={{color:'#aaaaff',fontFamily:'monospace',fontSize:10}}>#{t.pattern_cluster_id}</span> : <span style={{color:'#555'}}>—</span>)}
                  {td(t.timeframe||'—','#888')}
                  {td(fmtStrategy(t.strategy_id),'#888')}
                  {td(t.trade_potential_score!=null?(+t.trade_potential_score).toFixed(1):'—','#aaa')}
                  {td(t.direction_confidence!=null?(+t.direction_confidence).toFixed(1):'—','#aaa')}
                  {/* Exit & Performance */}
                  {td(fmt6(t.exit_price))}
                  {td(fmtTime(t.exit_time),'#888')}
                  {td(fmtHold(t.hold_time_seconds),'#aaa')}
                  {td(<span style={{color:net>=0?'#00ff88':'#ff4444'}}>{net>=0?'+':''}{net.toFixed(2)}</span>)}
                  {td(<span style={{color:net>=0?'#00ff88':'#ff4444'}}>{fmtPct(net,cap)}</span>)}
                  {td(<span style={{color:(+(t.final_pnl_usdt||0))>=0?'#00dd77':'#dd4444'}}>{(+(t.final_pnl_usdt||0)).toFixed(2)}</span>)}
                  {td(<span style={{color:'#00ccff'}}>{peakP>0?`+${peakP.toFixed(2)}`:'—'}</span>)}
                  {td(<span style={{color:'#ff6666'}}>{peakL<0?peakL.toFixed(2):(peakL>0?`-${peakL.toFixed(2)}`:'—')}</span>)}
                  {td(`$${(+(t.fees_usdt||0)).toFixed(2)}`,'#888')}
                  {td((() => {
                    const tp1 = +(t.tp1_target||0);
                    if (tp1 <= 0) return <span style={{color:'#555'}}>—</span>;
                    const entry = +(t.entry_price||0);
                    const cap   = +(t.capital_usdt||0);
                    const lev   = +(t.leverage||1);
                    const ds    = t.direction === 'long' ? 1 : -1;
                    let pct = +(t.mag1_pct||0);
                    if (pct === 0 && entry > 0) pct = (tp1 - entry) / entry * 100 * ds;
                    const usdt = cap * lev * Math.abs(pct) / 100;
                    return <span style={{color:'#00ccff'}}>+${usdt.toFixed(2)}{pct!==0 && <small style={{fontSize:9,color:'#888'}}> ({Math.abs(pct).toFixed(2)}%)</small>}</span>;
                  })())}
                  {td((() => {
                    const tp2 = +(t.tp2_target||0);
                    if (tp2 <= 0) return <span style={{color:'#555'}}>—</span>;
                    const entry = +(t.entry_price||0);
                    const cap   = +(t.capital_usdt||0);
                    const lev   = +(t.leverage||1);
                    const ds    = t.direction === 'long' ? 1 : -1;
                    let pct = +(t.mag3_pct||0);
                    if (pct === 0 && entry > 0) pct = (tp2 - entry) / entry * 100 * ds;
                    const usdt = cap * lev * Math.abs(pct) / 100;
                    return <span style={{color:'#00ccff'}}>+${usdt.toFixed(2)}{pct!==0 && <small style={{fontSize:9,color:'#888'}}> ({Math.abs(pct).toFixed(2)}%)</small>}</span>;
                  })())}
                  {td(t.exit_reason||'—','#aaa')}
                  {/* Brain */}
                  {td(t.brain_influenced ? <span style={{color:'#00ff88'}}>Yes</span> : <span style={{color:'#555'}}>No</span>)}
                  {td(t.intervention_count||0,'#888')}
                  {/* Analysis */}
                  {td(failureCell)}
                  {td(cfCell)}
                  {td(qualityCell)}
                  {td(t.brain_stage||'—','#555')}
                </tr>
              );
            })}
            {filtered.length === 0 && (
              <tr><td colSpan={35} style={{textAlign:'center',padding:20,color:'#555'}}>No closed trades</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
};
export default ClosedTradesTable;
