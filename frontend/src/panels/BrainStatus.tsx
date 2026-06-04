// AI-04: Panel 2 — Brain Status
//
// Decision feed (cont. 41): previously rendered just `regime` because the
// fallback chain `verdict.reason || regime` always hit the second branch
// (verdict.reason is never populated by brain/soar.py — the verdict carries
// {mode, llm_available}, not a free-text reason). Result was 10× "bull bull
// bull" with no information. The new renderer composes a structured line
// from the rich fields that brain/soar.py:_decide actually emits.
import React, { useContext, useEffect, useState } from 'react';
import { getBrainStatus } from '../api';
import { WsContext } from '../context';
import { card, title, P } from './shared';

type BrainEvent = {
  ts?: string;
  channel?: string;
  regime?: string;
  stage?: number;
  turbulence?: number;
  sentiment?: number;
  world_model_uncertainty?: number;
  memrl_base_rate_pct?: number;
  memrl_sample_n?: number;
  metacog_confidence?: number;
  metacog_priority_gap?: string;
  verdict?: { mode?: string; llm_available?: boolean; reason?: string };
};

const fmtTs = (iso?: string): string => {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour12: false });
  } catch { return '—'; }
};

const sentimentColor = (s?: number): string => {
  if (s == null) return '#888';
  if (s > 0.55) return '#00ff88';
  if (s < 0.45) return '#ff7777';
  return '#ffaa00';
};

const turbColor = (t?: number): string => {
  if (t == null) return '#888';
  if (t > 2.5) return '#ff4444';   // circuit-breaker zone
  if (t > 1.5) return '#ffaa00';
  return '#00ff88';
};

const uncColor = (u?: number): string => {
  if (u == null) return '#888';
  if (u > 0.30) return '#ff7777';
  if (u > 0.15) return '#ffaa00';
  return '#00ff88';
};

// Render one decision row from the structured payload that brain/soar.py
// pushes into brain:decisions_log. Falls back gracefully if any field
// is missing so older events / brain_metrics-channel events still render.
const renderRow = (e: BrainEvent) => {
  const mode = e.verdict?.mode || 'unknown';
  const llm = e.verdict?.llm_available ? '🧠' : '⚙️';
  const reg = e.regime || '?';
  const turb = e.turbulence;
  const sent = e.sentiment;
  const wmu = e.world_model_uncertainty;
  const mc = e.metacog_confidence;
  const gap = e.metacog_priority_gap;
  return (
    <>
      <span style={{color:'#666'}}>{fmtTs(e.ts)}</span>{' '}
      <span style={{color:'#aaa'}}>{llm} {mode}</span>{' '}
      <span style={{color:'#00d4ff'}}>{reg}</span>{' '}
      <span style={{color:turbColor(turb)}}>turb {turb?.toFixed(1) ?? '—'}</span>{' '}
      <span style={{color:sentimentColor(sent)}}>sent {sent?.toFixed(2) ?? '—'}</span>{' '}
      <span style={{color:uncColor(wmu)}}>wmu {wmu?.toFixed(2) ?? '—'}</span>{' '}
      <span style={{color:'#bbb'}}>mc {mc?.toFixed(0) ?? '—'}%</span>
      {gap ? <span style={{color:'#ffaa00'}}> gap:{gap}</span> : null}
    </>
  );
};

const BrainStatus: React.FC = () => {
  const [brain, setBrain] = useState<any>({});
  const [log, setLog] = useState<BrainEvent[]>([]);
  const evt = useContext(WsContext);

  useEffect(() => { getBrainStatus().then(setBrain).catch(()=>{}); }, []);

  useEffect(() => {
    if (!evt) return;
    if (evt.channel === 'brain_decision' || evt.channel === 'brain_metrics') {
      setBrain((b:any) => ({...b, ...evt.data}));
      // Persist the full structured event so renderRow can compose a rich line.
      // Skip duplicates that share a timestamp (brain emits both channels at
      // close intervals and we don't want the feed flooded with twins).
      setLog(prev => {
        const last = prev[0];
        if (last && last.ts && evt.data.ts && last.ts === evt.data.ts) return prev;
        return [{...evt.data, channel: evt.channel}, ...prev].slice(0, 20);
      });
    }
  }, [evt]);

  return (
    <div style={card}>
      <h3 style={title}>Brain Status</h3>
      <P label="Stage" value={`${brain.stage||1} — ${['','Baby','Learning','Developing','Expert'][brain.stage||1]}`} color="#aa88ff"/>
      <P label="Regime" value={brain.regime||'unknown'} color="#00d4ff"/>
      <P label="Paper Closed" value={brain.paper_closed||0}/>
      <P label="Active Pairs" value={brain.active_pairs||0}/>
      <div style={{marginTop:12}}>
        <div style={{color:'#888',fontSize:11,marginBottom:4,display:'flex',justifyContent:'space-between'}}>
          <span>Brain Decision Feed</span>
          <span style={{color:'#555',fontSize:10}}>time · mode · regime · turb · sent · wmu · meta-conf · gap</span>
        </div>
        <div style={{background:'#0d0d1a',borderRadius:4,padding:8,height:160,overflowY:'auto',fontSize:11,
                     fontFamily:'monospace'}}>
          {log.length ? log.map((e,i) =>
            <div key={i} style={{borderBottom:'1px solid #222',padding:'2px 0',color:'#aaa',whiteSpace:'nowrap'}}>
              {renderRow(e)}
            </div>
          ) : <span style={{color:'#555'}}>Waiting for events...</span>}
        </div>
      </div>
    </div>
  );
};
export default BrainStatus;
