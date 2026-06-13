// DecisionTimeline — Phase-6 VS-V1. Time-ordered strip of recent decisions (real ts), each a chip
// coloured by direction with a conviction underbar; click selects the symbol the atlas/inspector render.
// The synchronized market/belief/risk/learning lanes arrive in VS-V2/V3; this is the selection spine.
import React from 'react';
import { COL_MUTE, dirColor } from './ui';

const ago = (ts: number): string => {
  if (!ts) return '';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${Math.floor(s / 3600)}h`;
};

const DecisionTimeline: React.FC<{
  decisions: any[];
  selected?: string | null;
  onSelect: (sym: string) => void;
}> = ({ decisions, selected, onSelect }) => {
  if (!decisions || decisions.length === 0) return null;
  const sorted = [...decisions].sort((a, b) => (b.ts || 0) - (a.ts || 0));
  return (
    <div style={{ display: 'flex', gap: 6, overflowX: 'auto', padding: '4px 2px 6px',
                  borderBottom: '1px solid #1a2030' }}>
      {sorted.map((d) => {
        const isSel = d.symbol === selected;
        const col = dirColor(d.direction);
        return (
          <div key={d.symbol} onClick={() => onSelect(d.symbol)}
            title={`${d.symbol} · ${d.direction || 'abstain'} · conv ${(d.conviction ?? 0).toFixed(2)} · ${d.regime || ''} · ${ago(d.ts)} ago`}
            style={{ flex: '0 0 auto', cursor: 'pointer', minWidth: 78, padding: '3px 6px',
                     background: isSel ? '#1b2540' : '#10162a', borderRadius: 5,
                     border: `1px solid ${isSel ? col : '#1f2a3a'}` }}>
            <div style={{ fontSize: 10.5, fontWeight: 600, color: col, whiteSpace: 'nowrap',
                          overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: 90 }}>{d.symbol}</div>
            <div style={{ fontSize: 9, color: COL_MUTE, display: 'flex', justifyContent: 'space-between' }}>
              <span>{d.direction ? d.direction[0].toUpperCase() : '·'} {(d.conviction ?? 0).toFixed(2)}</span>
              <span>{ago(d.ts)}</span>
            </div>
            <div style={{ height: 3, marginTop: 2, background: '#1c2333', borderRadius: 2 }}>
              <span style={{ display: 'block', height: '100%', borderRadius: 2,
                width: `${Math.max(2, Math.min(100, (d.conviction ?? 0) * 100))}%`,
                background: (d.conviction ?? 0) > 0 ? col : COL_MUTE }} />
            </div>
          </div>
        );
      })}
    </div>
  );
};

export default DecisionTimeline;
