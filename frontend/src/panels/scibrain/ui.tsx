// Shared visual primitives for the Scientist-Brain panels (Phase-6 VS-V1 split). Kept tiny + typed so
// the atlas, inspector, timeline, and fallback table all draw from one source of truth.
import React from 'react';

export const COL_LONG = '#21d07a', COL_SHORT = '#ff5470', COL_MUTE = '#888',
             COL_WARN = '#ffb454', COL_SHADOW = '#7fa6ff';

export const dirColor = (d: any): string =>
  d === 'long' ? COL_LONG : d === 'short' ? COL_SHORT : COL_MUTE;

// signed value -1..1 → red(short)..grey(0)..green(long)
export const signColor = (v: number): string =>
  v > 0.02 ? COL_LONG : v < -0.02 ? COL_SHORT : COL_MUTE;

export const pct = (v: any, d = 3): string =>
  (v === null || v === undefined || v === '') ? '—' : `${Number(v).toFixed(d)}%`;

// horizontal bar for a 0..1 magnitude
export const Bar: React.FC<{ v: number; color: string; w?: number }> = ({ v, color, w = 90 }) => (
  <span style={{ display: 'inline-block', width: w, height: 8, background: '#1c2333',
                 borderRadius: 4, overflow: 'hidden', verticalAlign: 'middle' }}>
    <span style={{ display: 'block', height: '100%',
                   width: `${Math.max(0, Math.min(1, v)) * 100}%`, background: color }} />
  </span>
);

// signed vote bar centred at 0 (left = short, right = long)
export const VoteBar: React.FC<{ v: number }> = ({ v }) => {
  const mag = Math.max(0, Math.min(1, Math.abs(v))) * 50;
  const pos = v >= 0;
  return (
    <span style={{ position: 'relative', display: 'inline-block', width: 100, height: 8,
                   background: '#1c2333', borderRadius: 4, verticalAlign: 'middle' }}>
      <span style={{ position: 'absolute', left: '50%', top: 0, bottom: 0, width: 1, background: '#3a4a63' }} />
      <span style={{ position: 'absolute', top: 0, height: '100%',
                     left: pos ? '50%' : `${50 - mag}%`, width: `${mag}%`,
                     background: pos ? COL_LONG : COL_SHORT }} />
    </span>
  );
};
