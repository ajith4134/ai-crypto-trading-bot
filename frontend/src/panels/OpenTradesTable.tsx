// AI-07: Panel 5 — Open Trades Table (all 15 columns, live updates)
import React, { useContext, useEffect, useState } from 'react';
import { getOpenTradesFull } from '../api';
import { WsContext } from '../context';
import { card, title, Table } from './shared';

const statusColor: Record<string, string> = {
  applied: '#00ff88',
  gate_applied: '#00d4ff',
  suppressed: '#ff9966',
  abstained: '#777',
  advised: '#d6a6ff',
  counterfactual_only: '#ffaa00',
  unavailable: '#555',
};

const InfluenceCell: React.FC<{trade:any}> = ({trade}) => {
  const manifest = trade.influence_manifest || trade.signals_at_entry?.influence_manifest;
  const postOpen = Array.isArray(trade.post_open_influences) ? trade.post_open_influences : [];
  const influences = [...(manifest?.influences || []), ...postOpen];
  const summary = manifest?.summary || {};
  const audit = trade.trade_audit || trade.signals_at_entry?.audit;
  const rec = trade.trade_recommendation || trade.signals_at_entry?.recommendation;
  if (!manifest && !audit && !rec && postOpen.length === 0) {
    return <span style={{color:'#555'}}>not captured</span>;
  }
  const applied = +(summary.applied || 0) + +(summary.gate_applied || 0);
  const nonCausal = +(summary.suppressed || 0) + +(summary.abstained || 0)
    + +(summary.advised || 0) + +(summary.counterfactual_only || 0) + postOpen.length;
  return (
    <details style={{minWidth:180,maxWidth:360,fontFamily:'monospace',fontSize:9}}>
      <summary style={{cursor:'pointer',color:'#ccc'}}>
        <span style={{color:'#00ff88'}}>{applied} applied</span>
        {' · '}
        <span style={{color:'#ffaa00'}}>{nonCausal} visible/non-causal</span>
        {manifest?.provenance_quality === 'partial_historical_snapshot'
          && <span style={{color:'#ff9966'}}> · partial history</span>}
      </summary>
      <div style={{marginTop:5,maxHeight:230,overflowY:'auto'}}>
        {Array.isArray(manifest?.limitations) && manifest.limitations.map((item:string, idx:number) =>
          <div key={`limitation-${idx}`} style={{color:'#ff9966',paddingBottom:3}}>limit: {item}</div>
        )}
        {influences.map((inf:any, idx:number) => {
          const status = inf.status || 'unavailable';
          const raw = inf.raw_vote ?? inf.score;
          const effect = inf.actual_effect;
          return (
            <div key={`${inf.source || 'influence'}-${idx}`}
              style={{borderTop:'1px solid #222',padding:'4px 0',lineHeight:1.35}}>
              <b style={{color:statusColor[status] || '#aaa'}}>{inf.source || 'unknown'}</b>
              {' '}
              <span style={{color:statusColor[status] || '#888'}}>{status}</span>
              {' · '}
              <span style={{color:'#888'}}>{inf.authority || 'observe'}</span>
              {raw != null && <span style={{color:'#bbb'}}> · raw {typeof raw === 'number' ? raw.toFixed(3) : raw}</span>}
              {inf.router_gain != null && <span style={{color:'#bbb'}}> · gain {(+inf.router_gain).toFixed(3)}</span>}
              {effect != null && effect !== 0 && <div style={{color:'#aaa'}}>effect: {String(effect)}</div>}
              {(inf.explanation || inf.reason) && <div style={{color:'#777'}}>{inf.explanation || inf.reason}</div>}
            </div>
          );
        })}
        {audit && (
          <div style={{borderTop:'1px solid #333',paddingTop:4,color:'#bbb'}}>
            audit: {audit.agrees_with_fusion ? 'agrees' : 'disagrees'}
            {' · '}wrong-dir {audit.wrong_direction_risk ?? 'n/a'}
          </div>
        )}
        {rec && (
          <div style={{color:'#d6a6ff'}}>
            advise: {rec.recommended_action || 'HOLD'}{rec.applied ? ' (applied)' : ' (not applied)'}
          </div>
        )}
      </div>
    </details>
  );
};

const OpenTradesTable: React.FC = () => {
  const [trades, setTrades] = useState<any[]>([]);
  const [sort] = useState<string>('entry_time');
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
    <InfluenceCell trade={t}/>,
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
    // Cerebellum bounded calibration adjustment (Phase-7f task-8 — first limited canary authority):
    // base→adjusted conviction (±0.05 cap) and whether it was APPLIED to size/leverage. "off" = owner
    // kill switch disarmed, "unearned" = head did not beat baseline OOS (both still show the would-be Δ).
    (() => {
      const c = t.cerebellum;
      if (!c) return <span style={{color:'#555'}}>—</span>;
      const d = +c.delta || 0;
      const dcol = d > 0 ? '#00ff88' : d < 0 ? '#ff6666' : '#888';
      const state = c.applied
        ? <span style={{color:'#00ff88'}}>✓ applied</span>
        : <span style={{color:'#888'}}>{c.armed===false ? 'off' : (c.earned===false ? 'unearned' : 'no')}</span>;
      return (
        <span style={{fontSize:10,fontFamily:'monospace'}}>
          {(+c.base).toFixed(2)}→<span style={{color:'#00d4ff'}}>{(+c.adjusted).toFixed(2)}</span>
          {' '}<span style={{color:dcol}}>({d>=0?'+':''}{d.toFixed(2)})</span>
          {' '}{state}
        </span>
      );
    })(),
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
      <Table cols={['Pair','Dir','Strategy','Influence','Entry','Avg Entry','Mark','PnL','Peak PnL','SL','TP1','TP2','Capital','Lev','Notional','Cap%','Regime','TF','CN-5m','CN-15m','CN-30m','CN-1h','Cluster','Potential','Conf','Cerebellum','Hold','Status']} rows={rows} maxH={250}/>
    </div>
  );
};
export default OpenTradesTable;
