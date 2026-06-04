// F35 Fast→Slow consolidation output — see PROGRESS.md 2026-05-21 (cont. 6).
// Reads /memory/clusters which lists memory_clusters rows produced by
// memory/cognitive/consolidation.consolidate_fast_to_slow.
import React, { useEffect, useState } from 'react';
import { getMemoryClusters } from '../api';
import { card, title } from './shared';

type Cluster = {
  cluster_key: string;
  market_regime: string;
  pair_class: string;
  direction: string;
  outcome_class: string;
  n_trades: number;
  win_rate: number;
  avg_pnl_usdt: number;
  avg_hold_seconds: number;
  age_s: number;
};

type Payload = {
  clusters: Cluster[];
  summary: {
    count: number;
    consolidations: number;
    last_updated_clusters: number;
    last_created_clusters: number;
    last_run_age_seconds: number | null;
  };
};

const fmtAge = (s: number | null): string => {
  if (s == null) return '—';
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};
const fmtHold = (s: number): string => {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return `${h}h ${m}m`;
};

const MemoryClustersPanel: React.FC = () => {
  const [data, setData] = useState<Payload | null>(null);
  const [err, setErr] = useState('');

  useEffect(() => {
    const fetchOnce = () => getMemoryClusters()
      .then((d: Payload) => { setData(d); setErr(''); })
      .catch((e: any) => setErr(e.response?.data?.detail || 'load failed'));
    fetchOnce();
    const t = setInterval(fetchOnce, 60000);
    return () => clearInterval(t);
  }, []);

  if (err) return (
    <div style={card}>
      <h3 style={title}>F35 Memory Clusters (Slow Memory)</h3>
      <div style={{color: '#ff7777', fontSize: 12}}>{err}</div>
    </div>
  );
  if (!data) return (
    <div style={card}>
      <h3 style={title}>F35 Memory Clusters (Slow Memory)</h3>
      <div style={{color: '#888', fontSize: 12}}>Loading…</div>
    </div>
  );

  const s = data.summary;
  const clusters = data.clusters || [];

  return (
    <div style={card}>
      <h3 style={title}>F35 Memory Clusters ({s.count})</h3>
      <div style={{display: 'flex', gap: 16, flexWrap: 'wrap', marginBottom: 10,
                   fontSize: 11, color: '#aaa'}}>
        <span>consolidations: <b style={{color: '#00d4ff'}}>{s.consolidations}</b></span>
        <span>last run: <b style={{color: '#aaa'}}>{fmtAge(s.last_run_age_seconds)}</b></span>
        <span>last created: <b style={{color: '#00ff88'}}>{s.last_created_clusters}</b></span>
        <span>last updated: <b style={{color: '#aaa'}}>{s.last_updated_clusters}</b></span>
      </div>
      <div style={{overflowX: 'auto'}}>
        <table style={{width: '100%', borderCollapse: 'collapse', fontSize: 11}}>
          <thead>
            <tr style={{color: '#888', borderBottom: '1px solid #2a2a4a'}}>
              <th style={{textAlign: 'left',  padding: '4px 6px'}}>Cluster</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>n</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Win %</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Avg PnL</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Avg Hold</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Age</th>
            </tr>
          </thead>
          <tbody>
            {clusters.map(c => {
              const win = c.outcome_class === 'win';
              const loss = c.outcome_class === 'loss';
              return (
                <tr key={c.cluster_key} style={{borderBottom: '1px solid #111'}}>
                  <td style={{padding: '4px 6px',
                              color: win ? '#00ff88' : loss ? '#ff7777' : '#aaa',
                              fontFamily: 'monospace', fontSize: 10}}>
                    {c.cluster_key}
                  </td>
                  <td style={{padding: '4px 6px', textAlign: 'right',
                              color: '#e0e0e0', fontWeight: 'bold'}}>
                    {c.n_trades}
                  </td>
                  <td style={{padding: '4px 6px', textAlign: 'right',
                              color: c.win_rate >= 50 ? '#00ff88' : '#ff7777'}}>
                    {c.win_rate.toFixed(1)}
                  </td>
                  <td style={{padding: '4px 6px', textAlign: 'right',
                              color: c.avg_pnl_usdt >= 0 ? '#00ff88' : '#ff7777'}}>
                    {c.avg_pnl_usdt >= 0 ? '+' : ''}{c.avg_pnl_usdt.toFixed(2)}
                  </td>
                  <td style={{padding: '4px 6px', textAlign: 'right', color: '#aaa'}}>
                    {fmtHold(c.avg_hold_seconds || 0)}
                  </td>
                  <td style={{padding: '4px 6px', textAlign: 'right', color: '#666'}}>
                    {fmtAge(c.age_s)}
                  </td>
                </tr>
              );
            })}
            {clusters.length === 0 && (
              <tr><td colSpan={6} style={{textAlign: 'center', padding: 20, color: '#555'}}>
                No clusters yet — sleep consolidation hasn't run
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div style={{marginTop: 8, fontSize: 10, color: '#666', lineHeight: 1.4}}>
        Slow memory: clusters keyed by (regime, pair_class, direction, outcome).
        Updated via running average on each sleep consolidation run.
        See <code style={{color: '#aaa'}}>memory/cognitive/consolidation.py</code>.
      </div>
    </div>
  );
};

export default MemoryClustersPanel;
