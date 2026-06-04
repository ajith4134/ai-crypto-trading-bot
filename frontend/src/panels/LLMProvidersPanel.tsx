// LLM Providers — local Ollama (primary, cont. 38 redesign) + cloud failover
// chain. Local handles all background research / decode / OPRO calls; cloud
// is opt-in burst when local fails. If cloud hit_ratio_pct trends > 5-10%
// over a day, time to add Gemini Flash / OpenRouter (see llm/providers.py
// header for signup links).
import React, { useEffect, useState } from 'react';
import { getLLMProviders } from '../api';
import { card, title } from './shared';

type ProviderRow = {
  name: string;
  model: string;
  free_rpm: number | null;
  kind?: 'local' | 'cloud';
  configured: boolean;
  in_cooldown: boolean;
  degraded?: boolean;
  cooldown_ttl_seconds: number;
  successful_calls: number;
  rate_limit_hits: number;
  last_success_age_seconds: number | null;
  last_hit_age_seconds: number | null;
  last_hit_status_code: number | null;
  last_elapsed_seconds?: number | null;
};

type Payload = {
  providers: ProviderRow[];
  summary: {
    primary_provider?: string;
    primary_healthy?: boolean;
    total_configured: number;
    total_in_chain: number;
    total_successful_calls: number;
    total_rate_limit_hits: number;
    currently_in_cooldown: number;
    hit_ratio_pct: number;
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

const statusCell = (row: ProviderRow): { text: string; color: string } => {
  if (row.kind === 'local') {
    if (row.degraded) return { text: 'degraded', color: '#ff7777' };
    return { text: 'primary', color: '#00ff88' };
  }
  if (!row.configured) return { text: 'no key', color: '#666' };
  if (row.in_cooldown) return { text: `cooldown ${row.cooldown_ttl_seconds}s`, color: '#ff7777' };
  return { text: 'fallback', color: '#00d4ff' };
};

const hitRatioColor = (pct: number): string => {
  if (pct < 2)  return '#00ff88';   // plenty of headroom
  if (pct < 10) return '#ffaa00';   // watching it
  return '#ff4444';                  // chain is getting choked
};

const LLMProvidersPanel: React.FC = () => {
  const [data, setData] = useState<Payload | null>(null);
  const [err, setErr] = useState('');

  useEffect(() => {
    const fetchOnce = () => getLLMProviders()
      .then((d: Payload) => { setData(d); setErr(''); })
      .catch((e: any) => setErr(e.response?.data?.detail || 'load failed'));
    fetchOnce();
    const t = setInterval(fetchOnce, 15000);
    return () => clearInterval(t);
  }, []);

  if (err) return (
    <div style={card}>
      <h3 style={title}>LLM Providers</h3>
      <div style={{color: '#ff7777', fontSize: 12}}>{err}</div>
    </div>
  );
  if (!data) return (
    <div style={card}>
      <h3 style={title}>LLM Providers</h3>
      <div style={{color: '#888', fontSize: 12}}>Loading…</div>
    </div>
  );

  // Defensive defaults — if the backend is on an older image without the
  // /llm/providers endpoint, or returns an unexpected shape, fall back to
  // zeros rather than crashing the ErrorBoundary and taking the whole
  // dashboard down with this single panel.
  const s = data.summary ?? {
    total_configured: 0,
    total_in_chain: 0,
    total_successful_calls: 0,
    total_rate_limit_hits: 0,
    currently_in_cooldown: 0,
    hit_ratio_pct: 0,
  };
  const providers = Array.isArray(data.providers) ? data.providers : [];

  return (
    <div style={card}>
      <h3 style={title}>LLM Providers</h3>
      <div style={{display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 10,
                   fontSize: 11, color: '#aaa'}}>
        <span>primary: <b style={{color: s.primary_healthy === false ? '#ff7777' : '#00ff88'}}>
          {s.primary_provider ?? 'ollama_local'} {s.primary_healthy === false ? '(degraded)' : '(healthy)'}</b></span>
        <span>configured: <b style={{color: '#e0e0e0'}}>
          {s.total_configured}/{s.total_in_chain}</b></span>
        <span>calls served: <b style={{color: '#00d4ff'}}>{s.total_successful_calls}</b></span>
        <span>rate-limit hits: <b style={{color: '#ffaa00'}}>{s.total_rate_limit_hits}</b></span>
        <span>in cooldown: <b style={{color: s.currently_in_cooldown ? '#ff7777' : '#00ff88'}}>
          {s.currently_in_cooldown}</b></span>
        <span>hit ratio: <b style={{color: hitRatioColor(s.hit_ratio_pct ?? 0)}}>
          {(s.hit_ratio_pct ?? 0).toFixed(2)}%</b></span>
      </div>
      <div style={{overflowX: 'auto'}}>
        <table style={{width: '100%', borderCollapse: 'collapse', fontSize: 11}}>
          <thead>
            <tr style={{color: '#888', borderBottom: '1px solid #2a2a4a'}}>
              <th style={{textAlign: 'left',  padding: '4px 6px'}}>Provider</th>
              <th style={{textAlign: 'left',  padding: '4px 6px'}}>Model</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Free RPM</th>
              <th style={{textAlign: 'left',  padding: '4px 6px'}}>Status</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Calls</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Hits</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Last ok</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Last hit</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Last code</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Last latency</th>
            </tr>
          </thead>
          <tbody>
            {providers.map(p => {
              const st = statusCell(p);
              return (
                <tr key={p.name} style={{
                    borderBottom: '1px solid #111',
                    background: p.kind === 'local' ? '#0a1a14' : 'transparent',
                }}>
                  <td style={{padding: '4px 6px', color: '#e0e0e0',
                              fontWeight: p.configured ? 'bold' : 'normal'}}>
                    {p.name}{p.kind === 'local' ? ' 🏠' : ''}
                  </td>
                  <td style={{padding: '4px 6px', color: '#888',
                              fontFamily: 'monospace', fontSize: 10}}>
                    {p.model}
                  </td>
                  <td style={{padding: '4px 6px', textAlign: 'right', color: '#aaa'}}>
                    {p.free_rpm ?? (p.kind === 'local' ? '∞' : '—')}
                  </td>
                  <td style={{padding: '4px 6px', color: st.color, fontWeight: 'bold'}}>
                    {st.text}
                  </td>
                  <td style={{padding: '4px 6px', textAlign: 'right', color: '#00d4ff'}}>
                    {(p.successful_calls ?? 0).toLocaleString()}
                  </td>
                  <td style={{padding: '4px 6px', textAlign: 'right',
                              color: (p.rate_limit_hits ?? 0) > 0 ? '#ffaa00' : '#666'}}>
                    {(p.rate_limit_hits ?? 0).toLocaleString()}
                  </td>
                  <td style={{padding: '4px 6px', textAlign: 'right',
                              color: (p.last_success_age_seconds != null
                                     && p.last_success_age_seconds < 600) ? '#00ff88' : '#888'}}>
                    {fmtAge(p.last_success_age_seconds)}
                  </td>
                  <td style={{padding: '4px 6px', textAlign: 'right', color: '#888'}}>
                    {fmtAge(p.last_hit_age_seconds)}
                  </td>
                  <td style={{padding: '4px 6px', textAlign: 'right',
                              color: p.last_hit_status_code ? '#ff7777' : '#666'}}>
                    {p.last_hit_status_code ?? '—'}
                  </td>
                  <td style={{padding: '4px 6px', textAlign: 'right',
                              color: p.last_elapsed_seconds != null ? '#aaa' : '#666'}}>
                    {p.last_elapsed_seconds != null
                      ? `${p.last_elapsed_seconds.toFixed(0)}s`
                      : '—'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div style={{marginTop: 8, fontSize: 10, color: '#666', lineHeight: 1.4}}>
        Chain has 18 slots (1 Ollama + 17 catalog). Configure unset providers via{' '}
        <code style={{color: '#aaa'}}>.env</code>: locals set the URL
        (<code style={{color: '#aaa'}}>LLAMACPP_URL</code>,{' '}
        <code style={{color: '#aaa'}}>LMSTUDIO_URL</code>,{' '}
        <code style={{color: '#aaa'}}>JANAI_URL</code>,{' '}
        <code style={{color: '#aaa'}}>TEXTGEN_URL</code>,{' '}
        <code style={{color: '#aaa'}}>GPT4ALL_URL</code>), clouds set the API key
        (<code style={{color: '#aaa'}}>OPENROUTER_API_KEY</code>,{' '}
        <code style={{color: '#aaa'}}>GOOGLE_AI_STUDIO_API_KEY</code>,{' '}
        <code style={{color: '#aaa'}}>TOGETHER_API_KEY</code>,{' '}
        <code style={{color: '#aaa'}}>DEEPINFRA_API_KEY</code>,{' '}
        <code style={{color: '#aaa'}}>FIREWORKS_API_KEY</code>,{' '}
        <code style={{color: '#aaa'}}>HUGGINGFACE_API_KEY</code>,{' '}
        <code style={{color: '#aaa'}}>CLOUDFLARE_API_KEY</code>+<code style={{color: '#aaa'}}>_ACCOUNT_ID</code>).
        See <code style={{color: '#aaa'}}>llm/providers.py</code> for full list.
      </div>
    </div>
  );
};

export default LLMProvidersPanel;
