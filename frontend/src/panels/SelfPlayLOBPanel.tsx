// F41 LOB simulator episode history — see PROGRESS.md 2026-05-21 (cont. 4).
// Reads /self_play/episodes which surfaces up to 50 recent episodes from the
// self_play:episode_history Redis list (LPUSH/LTRIM bounded). Each episode
// carries microstructure stats (spread, slippage_bps, levels traversed) and PnL.
import React, { useEffect, useState } from 'react';
import { getSelfPlayEpisodes } from '../api';
import { card, title } from './shared';

type Episode = {
  ts: number;
  total_pnl: number;
  fills: number;
  mean_spread: number;
  mean_slippage_bps: number;
  levels_traversed: number;
  steps: number;
};

type Payload = {
  episodes: Episode[];
  summary: {
    win_rate: number;
    games: number;
    last_pnl: number;
    last_spread: number;
    last_slip_bps: number;
    last_fills: number;
    last_age_seconds: number | null;
  };
};

const fmtAge = (s: number | null): string => {
  if (s == null) return 'never';
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};

const SelfPlayLOBPanel: React.FC = () => {
  const [data, setData] = useState<Payload | null>(null);
  const [err, setErr] = useState('');

  useEffect(() => {
    const fetchOnce = () => getSelfPlayEpisodes()
      .then((d: Payload) => { setData(d); setErr(''); })
      .catch((e: any) => setErr(e.response?.data?.detail || 'load failed'));
    fetchOnce();
    const t = setInterval(fetchOnce, 60000);
    return () => clearInterval(t);
  }, []);

  if (err) return (
    <div style={card}>
      <h3 style={title}>F41 Self-Play vs MarS (LOB)</h3>
      <div style={{color: '#ff7777', fontSize: 12}}>{err}</div>
    </div>
  );
  if (!data) return (
    <div style={card}>
      <h3 style={title}>F41 Self-Play vs MarS (LOB)</h3>
      <div style={{color: '#888', fontSize: 12}}>Loading…</div>
    </div>
  );

  const s = data.summary;
  const eps = data.episodes || [];

  // Sparkline of PnL over recent episodes (reverse to chronological)
  const chronological = [...eps].reverse();
  const pnls = chronological.map(e => e.total_pnl);
  let sparkline: React.ReactNode = null;
  if (pnls.length >= 2) {
    const mn = Math.min(...pnls);
    const mx = Math.max(...pnls);
    const range = mx - mn || 1;
    const w = 320, h = 50;
    const pts = pnls.map((p, i) => {
      const x = (i / (pnls.length - 1)) * w;
      const y = h - ((p - mn) / range) * (h - 4) - 2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    const zeroY = h - ((0 - mn) / range) * (h - 4) - 2;
    sparkline = (
      <svg width={w} height={h} style={{background: '#0d0d1a', borderRadius: 4}}>
        {mn < 0 && mx > 0 && (
          <line x1={0} y1={zeroY} x2={w} y2={zeroY} stroke="#444" strokeDasharray="2,2"/>
        )}
        <polyline points={pts} fill="none" stroke="#00d4ff" strokeWidth={1.5}/>
      </svg>
    );
  }

  return (
    <div style={card}>
      <h3 style={title}>F41 Self-Play vs MarS (LOB)</h3>
      <div style={{display: 'flex', gap: 16, flexWrap: 'wrap', marginBottom: 10,
                   fontSize: 11, color: '#aaa'}}>
        <span>games: <b style={{color: '#e0e0e0'}}>{s.games}</b></span>
        <span>win rate: <b style={{color: s.win_rate >= 50 ? '#00ff88' : '#ffaa00'}}>
          {s.win_rate.toFixed(1)}%</b></span>
        <span>last PnL: <b style={{
          color: s.last_pnl >= 0 ? '#00ff88' : '#ff7777'}}>
          {s.last_pnl >= 0 ? '+' : ''}{s.last_pnl.toFixed(2)}</b></span>
        <span>last spread: <b style={{color: '#00d4ff'}}>{s.last_spread.toFixed(2)}</b></span>
        <span>last slip: <b style={{color: '#00d4ff'}}>{s.last_slip_bps.toFixed(2)} bps</b></span>
        <span>last fills: <b style={{color: '#aaa'}}>{s.last_fills}</b></span>
        <span>age: <b style={{color: '#aaa'}}>{fmtAge(s.last_age_seconds)}</b></span>
      </div>

      {sparkline && (
        <div style={{marginBottom: 10}}>
          <div style={{fontSize: 10, color: '#666', marginBottom: 4}}>
            PnL across {eps.length} most recent episodes (oldest → newest)
          </div>
          {sparkline}
        </div>
      )}

      <div style={{overflowY: 'auto', maxHeight: 240}}>
        <table style={{width: '100%', borderCollapse: 'collapse', fontSize: 11}}>
          <thead>
            <tr style={{color: '#888', borderBottom: '1px solid #2a2a4a'}}>
              <th style={{textAlign: 'left',  padding: '4px 6px'}}>When</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Total PnL</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Fills</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Mean Spread</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Slip (bps)</th>
              <th style={{textAlign: 'right', padding: '4px 6px'}}>Levels</th>
            </tr>
          </thead>
          <tbody>
            {eps.map((e, i) => (
              <tr key={i} style={{borderBottom: '1px solid #111'}}>
                <td style={{padding: '4px 6px', color: '#aaa', fontSize: 10}}>
                  {new Date(e.ts * 1000).toLocaleString()}
                </td>
                <td style={{padding: '4px 6px', textAlign: 'right',
                            color: e.total_pnl >= 0 ? '#00ff88' : '#ff7777'}}>
                  {e.total_pnl >= 0 ? '+' : ''}{e.total_pnl.toFixed(2)}
                </td>
                <td style={{padding: '4px 6px', textAlign: 'right', color: '#e0e0e0'}}>
                  {e.fills}
                </td>
                <td style={{padding: '4px 6px', textAlign: 'right', color: '#00d4ff'}}>
                  {e.mean_spread.toFixed(2)}
                </td>
                <td style={{padding: '4px 6px', textAlign: 'right', color: '#00d4ff'}}>
                  {e.mean_slippage_bps.toFixed(2)}
                </td>
                <td style={{padding: '4px 6px', textAlign: 'right', color: '#aaa'}}>
                  {e.levels_traversed}
                </td>
              </tr>
            ))}
            {eps.length === 0 && (
              <tr><td colSpan={6} style={{textAlign: 'center', padding: 20, color: '#555'}}>
                No episodes recorded yet
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
      <div style={{marginTop: 8, fontSize: 10, color: '#666', lineHeight: 1.4}}>
        Agent-based LOB with Poisson maker/taker/cancel arrivals; strategies execute
        market orders through the book so slippage and partial fills are real.
        See <code style={{color: '#aaa'}}>self_play/mars.py</code>.
      </div>
    </div>
  );
};

export default SelfPlayLOBPanel;
