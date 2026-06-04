// F44 — Brain-learned hedge parameters. Shows each of the 5 scalars: current
// value vs blueprint default, n_samples accumulated, trusted flag (True iff
// n_samples >= MIN_SAMPLES so the learner has taken over from the default),
// and drift % from the blueprint default. See risk/hedge_params.py for the
// learning rule (constant-α MC + exploration) and PROGRESS.md cont. 10.
import React, { useEffect, useState } from 'react';
import { getHedgeLearnedParams } from '../api';
import { card, title } from './shared';

type Row = {
  name: string;
  desc: string;
  default: number;
  current: number;
  n_samples: number;
  trusted: boolean;
  min: number;
  max: number;
  explore_sigma: number;
  last_update_ts: number | null;
  last_update_age_seconds: number | null;
  drift_pct_from_default: number;
};

type Payload = {
  params: Row[];
  summary: {
    total_params: number;
    trusted_params: number;
    updates_count: number;
    last_update_age_seconds: number | null;
    last_reward: number | null;
  };
  generated_at_ts: number;
};

const fmtAge = (s: number | null): string => {
  if (s == null) return '—';
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};

const driftColor = (pct: number): string => {
  const a = Math.abs(pct);
  if (a < 2) return '#666';     // basically default
  if (a < 10) return '#00d4ff';  // learning, mild drift
  return '#ffaa00';              // significant drift from default
};

const HedgeLearnedParamsPanel: React.FC = () => {
  const [data, setData] = useState<Payload | null>(null);
  const [err, setErr] = useState('');

  useEffect(() => {
    const fetchOnce = () => getHedgeLearnedParams()
      .then((d: Payload) => { setData(d); setErr(''); })
      .catch((e: any) => setErr(e.response?.data?.detail || 'load failed'));
    fetchOnce();
    const t = setInterval(fetchOnce, 30000);
    return () => clearInterval(t);
  }, []);

  if (err) return (
    <div style={card}>
      <h3 style={title}>F44 Hedge Params (Brain-learned)</h3>
      <div style={{color: '#ff7777', fontSize: 12}}>{err}</div>
    </div>
  );
  if (!data) return (
    <div style={card}>
      <h3 style={title}>F44 Hedge Params (Brain-learned)</h3>
      <div style={{color: '#888', fontSize: 12}}>Loading…</div>
    </div>
  );

  const s = data.summary ?? {
    total_params: 0, trusted_params: 0, updates_count: 0,
    last_update_age_seconds: null, last_reward: null,
  };
  const params = Array.isArray(data.params) ? data.params : [];

  return (
    <div style={card}>
      <h3 style={title}>F44 Hedge Params (Brain-learned)</h3>
      <div style={{display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 10,
                   fontSize: 11, color: '#aaa'}}>
        <span>trusted: <b style={{color: '#e0e0e0'}}>
          {s.trusted_params}/{s.total_params}</b></span>
        <span>updates: <b style={{color: '#00d4ff'}}>{s.updates_count}</b></span>
        <span>last update: <b style={{color: '#aaa'}}>
          {fmtAge(s.last_update_age_seconds)}</b></span>
        <span>last reward: <b style={{
          color: s.last_reward == null ? '#666'
                 : s.last_reward > 0 ? '#00ff88' : '#ff7777'}}>
          {s.last_reward == null ? '—' : s.last_reward.toFixed(3)}</b></span>
      </div>
      <div style={{overflowX: 'auto'}}>
        <table style={{width: '100%', borderCollapse: 'collapse', fontSize: 11}}>
          <thead>
            <tr style={{color: '#888', borderBottom: '1px solid #2a2a4a'}}>
              <th style={{textAlign: 'left',  padding: '4px 6px'}}>Param</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Default</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Current</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Drift</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>n</th>
              <th style={{textAlign: 'left',  padding: '4px 6px'}}>State</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Bounds</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Last upd</th>
            </tr>
          </thead>
          <tbody>
            {params.map(p => (
              <tr key={p.name} style={{borderBottom: '1px solid #111'}}>
                <td style={{padding: '4px 6px', color: '#e0e0e0',
                            fontFamily: 'monospace', fontSize: 10}}>
                  {p.name}
                  <div style={{color: '#666', fontSize: 9, fontWeight: 'normal'}}>
                    {p.desc}
                  </div>
                </td>
                <td style={{padding: '4px 6px', textAlign: 'right', color: '#888'}}>
                  {p.default.toFixed(4)}
                </td>
                <td style={{padding: '4px 6px', textAlign: 'right',
                            color: '#e0e0e0', fontWeight: 'bold'}}>
                  {p.current.toFixed(4)}
                </td>
                <td style={{padding: '4px 6px', textAlign: 'right',
                            color: driftColor(p.drift_pct_from_default)}}>
                  {p.drift_pct_from_default >= 0 ? '+' : ''}
                  {p.drift_pct_from_default.toFixed(2)}%
                </td>
                <td style={{padding: '4px 6px', textAlign: 'right',
                            color: p.n_samples >= 10 ? '#00d4ff' : '#888'}}>
                  {p.n_samples}
                </td>
                <td style={{padding: '4px 6px',
                            color: p.trusted ? '#00ff88' : '#888',
                            fontWeight: p.trusted ? 'bold' : 'normal'}}>
                  {p.trusted ? 'learned' : 'default'}
                </td>
                <td style={{padding: '4px 6px', textAlign: 'right',
                            color: '#666', fontSize: 10}}>
                  [{p.min.toFixed(2)}, {p.max.toFixed(2)}]
                </td>
                <td style={{padding: '4px 6px', textAlign: 'right', color: '#666'}}>
                  {fmtAge(p.last_update_age_seconds)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{marginTop: 8, fontSize: 10, color: '#666', lineHeight: 1.4}}>
        Learner runs constant-α MC with exploration noise on each hedge close.
        Param shows <span style={{color: '#888'}}>default</span> until n_samples ≥ 10;
        after that it shows <span style={{color: '#00ff88'}}>learned</span>.
        See <code style={{color: '#aaa'}}>risk/hedge_params.py</code>.
      </div>
    </div>
  );
};

export default HedgeLearnedParamsPanel;
