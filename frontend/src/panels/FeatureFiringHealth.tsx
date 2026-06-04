// Feature Firing Health — runs tools/feature_health.py via /system/feature_health
// and renders per-feature firing evidence. Different from FeatureHealth which
// shows governance lifecycle (active/probation/turned_off).
import React, { useEffect, useState } from 'react';
import { getFeatureHealthAll } from '../api';
import { card, title } from './shared';

type Row = {
  feature_id: string;
  name: string;
  status: 'firing' | 'stale' | 'dead' | 'no_check' | 'error';
  evidence: string;
  last_seen_age_seconds: number | null;
};

type Payload = {
  summary: {
    total: number; firing: number; stale: number;
    dead: number; no_check: number; error: number;
  };
  results: Row[];
  generated_at_ts: number;
};

const statusColor = (s: string) => ({
  firing:   '#00ff88',
  stale:    '#ffaa00',
  dead:     '#ff4444',
  no_check: '#888',
  error:    '#ff00ff',
}[s] || '#aaa');

const FeatureFiringHealth: React.FC = () => {
  const [data, setData] = useState<Payload | null>(null);
  const [err, setErr] = useState('');
  const [filter, setFilter] = useState<'all' | 'firing' | 'stale' | 'dead' | 'no_check'>('all');

  useEffect(() => {
    const fetch = () => getFeatureHealthAll()
      .then((d: Payload) => { setData(d); setErr(''); })
      .catch((e: any) => setErr(e.response?.data?.detail || 'load failed'));
    fetch();
    const t = setInterval(fetch, 60000);  // backend caches 60s; poll matches
    return () => clearInterval(t);
  }, []);

  if (err) return (
    <div style={card}>
      <h3 style={title}>Feature Firing Health</h3>
      <div style={{color: '#ff7777', fontSize: 12}}>{err}</div>
    </div>
  );

  if (!data) return (
    <div style={card}>
      <h3 style={title}>Feature Firing Health</h3>
      <div style={{color: '#888', fontSize: 12}}>Loading…</div>
    </div>
  );

  const s = data.summary;
  const filtered = filter === 'all'
    ? data.results
    : data.results.filter(r => r.status === filter);

  const pill = (label: string, n: number, key: typeof filter, color: string) => (
    <button key={key}
      onClick={() => setFilter(key)}
      style={{
        background: filter === key ? color : 'transparent',
        color:      filter === key ? '#000' : color,
        border: `1px solid ${color}`, borderRadius: 12,
        padding: '2px 10px', fontSize: 11, marginRight: 6,
        cursor: 'pointer', fontWeight: 'bold',
      }}>
      {label} {n}
    </button>
  );

  return (
    <div style={card}>
      <h3 style={title}>Feature Firing Health ({s.total})</h3>
      <div style={{marginBottom: 10}}>
        {pill('all',      s.total,    'all',      '#00d4ff')}
        {pill('firing',   s.firing,   'firing',   '#00ff88')}
        {pill('stale',    s.stale,    'stale',    '#ffaa00')}
        {pill('dead',     s.dead,     'dead',     '#ff4444')}
        {pill('no_check', s.no_check, 'no_check', '#888')}
      </div>
      <div style={{maxHeight: 360, overflowY: 'auto', fontSize: 11}}>
        <table style={{width: '100%', borderCollapse: 'collapse'}}>
          <thead style={{position: 'sticky', top: 0, background: '#1a1a2e'}}>
            <tr>
              <th style={{textAlign: 'left',  padding: '4px 6px', color: '#888', borderBottom: '1px solid #2a2a4a'}}>ID</th>
              <th style={{textAlign: 'left',  padding: '4px 6px', color: '#888', borderBottom: '1px solid #2a2a4a'}}>Name</th>
              <th style={{textAlign: 'left',  padding: '4px 6px', color: '#888', borderBottom: '1px solid #2a2a4a'}}>Status</th>
              <th style={{textAlign: 'left',  padding: '4px 6px', color: '#888', borderBottom: '1px solid #2a2a4a'}}>Evidence</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map(r => (
              <tr key={r.feature_id} style={{borderBottom: '1px solid #111'}}>
                <td style={{padding: '3px 6px', color: '#00d4ff'}}>{r.feature_id}</td>
                <td style={{padding: '3px 6px'}}>{r.name}</td>
                <td style={{padding: '3px 6px', color: statusColor(r.status), fontWeight: 'bold'}}>
                  {r.status}
                </td>
                <td style={{padding: '3px 6px', color: '#aaa', fontSize: 10}}>{r.evidence}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};

export default FeatureFiringHealth;
