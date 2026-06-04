// AI-10: Panel 8 — Strategy Panel
import React, { useEffect, useState } from 'react';
import { getStrategies } from '../api';
import { card, title, Table } from './shared';

const StrategyPanel: React.FC = () => {
  const [strats, setStrats] = useState<any[]>([]);
  const [status, setStatus] = useState<string>('loading');
  const [errMsg, setErrMsg] = useState<string>('');

  useEffect(() => {
    const load = () => {
      setStatus('loading');
      getStrategies()
        .then((d: any) => {
          if (Array.isArray(d)) {
            setStrats(d);
            setStatus(d.length > 0 ? 'ok' : 'empty');
            setErrMsg('');
          } else {
            setStrats([]);
            setStatus('error');
            setErrMsg('Unexpected response: ' + typeof d + ' — ' + JSON.stringify(d).slice(0, 80));
          }
        })
        .catch((e: any) => {
          setStrats([]);
          setStatus('error');
          const code = e?.response?.status;
          setErrMsg(code ? `HTTP ${code}: ${e.response.data?.detail || 'auth error'}` : (e.message || 'Network error'));
        });
    };
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, []);

  const rows = strats.map(s => [
    s.id?.slice(0, 8) || '—',
    s.name || '—',
    s.status,
    s.win_rate != null ? `${(+s.win_rate).toFixed(1)}%` : '—',
    s.avg_pnl_usdt != null ? `$${(+s.avg_pnl_usdt).toFixed(2)}` : '—',
    s.sharpe_ratio != null ? (+s.sharpe_ratio).toFixed(2) : '—',
    s.trade_count || 0,
    s.generation || 0,
    s.source || '—',
  ]);

  return (
    <div style={card}>
      <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',marginBottom:8}}>
        <h3 style={{...title, margin:0}}>Strategies ({strats.length})</h3>
        <span style={{fontSize:10,color: status==='ok'?'#00ff88': status==='loading'?'#888':'#ff4444'}}>
          {status==='loading'?'Loading...' : status==='ok'?'Live' : status==='empty'?'No strategies yet' : '⚠ '+errMsg}
        </span>
      </div>
      <Table
        cols={['ID','Name','Status','Win%','Avg PnL','Sharpe','Trades','Gen','Source']}
        rows={rows}
        maxH={220}
      />
      {status === 'empty' && (
        <div style={{color:'#555',fontSize:11,marginTop:8,textAlign:'center'}}>
          2 built-in strategies seeded — win rate will populate as trades close
        </div>
      )}
    </div>
  );
};
export default StrategyPanel;
