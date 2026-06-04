// AI-05: Panel 3 — ML & RL Models. Rewritten cont. 28 to render real
// per-model status from /models endpoint (file mtimes + redis training
// metrics). Replaces the prior hardcoded MODELS array that showed every
// model as "Active" with no actual signal.
import React, { useEffect, useState } from 'react';
import { card, title, Table } from './shared';
import { getModelsStatus } from '../api';

type ModelRow = {
  name: string;
  purpose: string;
  status: string;          // active | stale | frozen | pending | pretrained | missing
  age_hours: number | null;
  metric_label: string;
  metric_value: any;
  progress_pct: number | null;
};

const STATUS_COLOR: Record<string, string> = {
  active: '#3fb950',       // green
  stale: '#d29922',        // amber
  frozen: '#8b949e',       // gray
  pending: '#58a6ff',      // blue
  pretrained: '#a371f7',   // purple
  missing: '#f85149',      // red
};

const fmtAge = (h: number | null): string => {
  if (h === null || h === undefined) return '—';
  if (h < 1) return `${Math.round(h * 60)}m ago`;
  if (h < 48) return `${h.toFixed(1)}h ago`;
  return `${Math.round(h / 24)}d ago`;
};

const fmtMetric = (v: any): string => {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number') return v.toFixed(4);
  return String(v);
};

const fmtProgress = (status: string, pct: number | null): string => {
  if (pct !== null && pct !== undefined) return `${pct}%`;
  if (status === 'active') return '✓';
  if (status === 'pretrained') return '∞';
  return '—';
};

const StatusBadge: React.FC<{ status: string }> = ({ status }) => (
  <span style={{
    color: STATUS_COLOR[status] || '#8b949e',
    fontWeight: 600,
    fontSize: 11,
    textTransform: 'uppercase',
  }}>
    {status}
  </span>
);

const MLModelsPanel: React.FC = () => {
  const [rows, setRows] = useState<ModelRow[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const fetchOnce = () => getModelsStatus()
      .then(d => { setRows(d.models || []); setErr(null); })
      .catch(e => setErr(String(e?.message || e)));
    fetchOnce();
    const t = setInterval(fetchOnce, 30000);
    return () => clearInterval(t);
  }, []);

  if (err) {
    return (
      <div style={card}>
        <h3 style={title}>ML &amp; RL Models</h3>
        <div style={{ color: '#f85149', fontSize: 12 }}>Failed to load: {err}</div>
      </div>
    );
  }

  const tableRows = rows.map(r => [
    r.name,
    r.purpose,
    <StatusBadge status={r.status} />,
    fmtAge(r.age_hours),
    fmtProgress(r.status, r.progress_pct),
    `${r.metric_label}: ${fmtMetric(r.metric_value)}`,
  ]);

  return (
    <div style={card}>
      <h3 style={title}>ML &amp; RL Models</h3>
      <Table
        cols={['Model', 'Purpose', 'Status', 'Last trained', 'Progress', 'Metric']}
        rows={tableRows as any}
        maxH={260}
      />
    </div>
  );
};
export default MLModelsPanel;
