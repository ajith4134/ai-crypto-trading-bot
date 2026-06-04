// F9 Miss Decoder + F12 Mismatch Decoder feed — see PROGRESS.md 2026-05-21 (cont. 3).
// Reads /decoders/recent which surfaces the latest LLM postmortems on shadow
// wins (rejected signals that would have won) and high-pot-loser / low-pot-winner
// trade pairs.
import React, { useEffect, useState } from 'react';
import { getRecentDecoders } from '../api';
import { card, title } from './shared';

type MissRow = {
  pair: string;
  direction: string;
  rejection_reason: string | null;
  peak_profit_pct: number | null;
  miss_decode_reason: string;
  created_at: string;
};

type MismatchRow = {
  loser_pair: string;
  loser_dir: string;
  loser_pot: number;
  loser_pnl_usdt: number;
  winner_pair: string;
  winner_dir: string;
  winner_pot: number;
  winner_pnl_usdt: number;
  decode_reason: string;
  decoded_at: string;
};

type Payload = {
  misses: MissRow[];
  mismatches: MismatchRow[];
  summary: {
    miss_decoded_count: number;
    mismatch_decoded_count: number;
    miss_last_age_seconds: number | null;
    mismatch_last_age_seconds: number | null;
  };
};

const fmtAge = (s: number | null): string => {
  if (s == null) return 'never';
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};
const fmtTime = (ts: string): string => {
  try { return new Date(ts).toLocaleString(); } catch { return ts; }
};

const DecoderFeedPanel: React.FC = () => {
  const [data, setData] = useState<Payload | null>(null);
  const [err, setErr] = useState('');
  const [tab, setTab] = useState<'miss'|'mismatch'>('miss');

  useEffect(() => {
    const fetchOnce = () => getRecentDecoders()
      .then((d: Payload) => { setData(d); setErr(''); })
      .catch((e: any) => setErr(e.response?.data?.detail || 'load failed'));
    fetchOnce();
    const t = setInterval(fetchOnce, 60000);
    return () => clearInterval(t);
  }, []);

  if (err) return (
    <div style={card}>
      <h3 style={title}>F9/F12 Decoder Feed</h3>
      <div style={{color: '#ff7777', fontSize: 12}}>{err}</div>
    </div>
  );
  if (!data) return (
    <div style={card}>
      <h3 style={title}>F9/F12 Decoder Feed</h3>
      <div style={{color: '#888', fontSize: 12}}>Loading…</div>
    </div>
  );

  const s = data.summary;
  const tabBtn = (k: 'miss'|'mismatch', label: string, n: number): React.CSSProperties => ({
    background: tab === k ? '#2a2a4a' : '#0d0d1a',
    color: tab === k ? '#00d4ff' : '#888',
    border: '1px solid #2a2a4a', borderRadius: 4,
    padding: '4px 10px', cursor: 'pointer', fontSize: 11,
  });

  return (
    <div style={card}>
      <h3 style={title}>F9/F12 Decoder Feed</h3>
      <div style={{display: 'flex', gap: 16, flexWrap: 'wrap', marginBottom: 10,
                   fontSize: 11, color: '#aaa', alignItems: 'center'}}>
        <span>F9 decoded: <b style={{color: '#00d4ff'}}>{s.miss_decoded_count}</b>
          <span style={{color: '#666', marginLeft: 4}}>({fmtAge(s.miss_last_age_seconds)})</span>
        </span>
        <span>F12 decoded: <b style={{color: '#00d4ff'}}>{s.mismatch_decoded_count}</b>
          <span style={{color: '#666', marginLeft: 4}}>({fmtAge(s.mismatch_last_age_seconds)})</span>
        </span>
        <div style={{display: 'flex', gap: 6, marginLeft: 'auto'}}>
          <button style={tabBtn('miss', 'F9 Misses', data.misses.length)}
                  onClick={() => setTab('miss')}>F9 Misses ({data.misses.length})</button>
          <button style={tabBtn('mismatch', 'F12 Mismatches', data.mismatches.length)}
                  onClick={() => setTab('mismatch')}>F12 Mismatches ({data.mismatches.length})</button>
        </div>
      </div>

      <div style={{overflowY: 'auto', maxHeight: 360}}>
        {tab === 'miss' && (
          data.misses.length === 0
            ? <div style={{color: '#555', fontSize: 12, textAlign: 'center', padding: 20}}>
                No decoded shadow wins yet.
              </div>
            : data.misses.map((m, i) => (
              <div key={i} style={{padding: '8px 0', borderBottom: '1px solid #2a2a4a'}}>
                <div style={{display: 'flex', gap: 12, fontSize: 11, marginBottom: 4}}>
                  <b style={{color: '#00d4ff'}}>{m.pair}</b>
                  <span style={{color: m.direction === 'long' ? '#00ff88' : '#ff6666'}}>
                    {m.direction?.toUpperCase()}
                  </span>
                  <span style={{color: '#aaa'}}>rejected: <code style={{color: '#ffaa00'}}>
                    {m.rejection_reason || '?'}</code></span>
                  {m.peak_profit_pct != null && (
                    <span style={{color: '#00ff88'}}>
                      peak +{(+m.peak_profit_pct).toFixed(2)}%
                    </span>
                  )}
                  <span style={{color: '#666', marginLeft: 'auto'}}>{fmtTime(m.created_at)}</span>
                </div>
                <div style={{color: '#d0d0d0', fontSize: 11, lineHeight: 1.5,
                             paddingLeft: 8, borderLeft: '2px solid #2a2a4a'}}>
                  {m.miss_decode_reason}
                </div>
              </div>
            ))
        )}
        {tab === 'mismatch' && (
          data.mismatches.length === 0
            ? <div style={{color: '#555', fontSize: 12, textAlign: 'center', padding: 20}}>
                No decoded mismatches yet. F12 only fires when the bot produces
                a high-pot (≥60) losing trade alongside a low-pot (&lt;40) winning trade
                within 24h.
              </div>
            : data.mismatches.map((m, i) => (
              <div key={i} style={{padding: '8px 0', borderBottom: '1px solid #2a2a4a'}}>
                <div style={{display: 'flex', gap: 12, fontSize: 11, marginBottom: 4}}>
                  <span><b style={{color: '#ff7777'}}>{m.loser_pair}</b>
                    <span style={{color: '#aaa'}}> pot={(+m.loser_pot).toFixed(0)} → </span>
                    <b style={{color: '#ff4444'}}>{(+m.loser_pnl_usdt).toFixed(2)}</b></span>
                  <span style={{color: '#555'}}>vs</span>
                  <span><b style={{color: '#88ff88'}}>{m.winner_pair}</b>
                    <span style={{color: '#aaa'}}> pot={(+m.winner_pot).toFixed(0)} → </span>
                    <b style={{color: '#00ff88'}}>+{(+m.winner_pnl_usdt).toFixed(2)}</b></span>
                  <span style={{color: '#666', marginLeft: 'auto'}}>{fmtTime(m.decoded_at)}</span>
                </div>
                <div style={{color: '#d0d0d0', fontSize: 11, lineHeight: 1.5,
                             paddingLeft: 8, borderLeft: '2px solid #2a2a4a'}}>
                  {m.decode_reason}
                </div>
              </div>
            ))
        )}
      </div>
      <div style={{marginTop: 8, fontSize: 10, color: '#666', lineHeight: 1.4}}>
        LLM postmortems via the 5-provider Llama 3.3 70B chain. Suggested
        filter/scorer changes are stored but NOT auto-applied to live trading —
        human-in-loop review per Rule-4 design note.
      </div>
    </div>
  );
};

export default DecoderFeedPanel;
