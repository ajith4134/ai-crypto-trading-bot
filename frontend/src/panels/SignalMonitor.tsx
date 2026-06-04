// AI-08: Panel 6 — Signal Monitor (Blueprint 15.6)
// All 4 panel elements: Recent Signals Feed, Shadow Win Rate, Signal Acceptance Rate, Missed Opportunities
import React, { useContext, useEffect, useState } from 'react';
import {
  getShadowWinRate, getRecentSignals,
  getSignalAcceptanceRate, getMissedOpportunities,
} from '../api';
import { WsContext } from '../context';
import { card, title } from './shared';

const SignalMonitor: React.FC = () => {
  const [signals, setSignals] = useState<any[]>([]);
  const [shadowWR, setShadowWR] = useState<any>({total:0,won:0,rate:0});
  const [acceptance, setAcceptance] = useState<any>({total:0,accepted:0,rate:0,window_hours:24});
  const [missed, setMissed] = useState<any[]>([]);
  const evt = useContext(WsContext);

  useEffect(() => {
    const refresh = () => {
      getShadowWinRate().then(setShadowWR).catch(() => {});
      getSignalAcceptanceRate(24).then(setAcceptance).catch(() => {});
      getMissedOpportunities(10).then(setMissed).catch(() => {});
    };
    refresh();
    getRecentSignals(50).then((recent: any[]) => {
      setSignals(recent.map(s => ({ ...s, _fromHistory: true })));
    }).catch(() => {});
    const t = setInterval(refresh, 30000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    if (!evt) return;
    if (evt.channel === 'signal_generated') {
      setSignals(s => [evt.data, ...s].slice(0, 100));
    }
  }, [evt]);

  const noShadow = !shadowWR.total;
  // Staleness: warn when the newest sample is more than 24h old (sweeper backed up
  // or recent rejections haven't yet aged past the 72h CF window).
  const newestAgeHrs = shadowWR.sample_newest
    ? (Date.now() - new Date(shadowWR.sample_newest).getTime()) / 3600000
    : null;
  const stale = newestAgeHrs !== null && newestAgeHrs > 24;
  const lowN = shadowWR.total > 0 && shadowWR.total < 30;
  const fmtTime = (iso?: string) => {
    if (!iso) return '';
    const d = new Date(iso);
    return d.toLocaleDateString([], {month:'short', day:'numeric'});
  };

  return (
    <div style={card}>
      <h3 style={title}>Signal Monitor</h3>

      {/* Metrics row */}
      <div style={{marginBottom:8,fontSize:12,display:'flex',gap:16,flexWrap:'wrap'}}>
        <span>
          Acceptance Rate ({acceptance.window_hours}h):{' '}
          <span style={{color: acceptance.rate < 5 ? '#ff6666' : acceptance.rate < 20 ? '#ffaa00' : '#00ff88'}}>
            {acceptance.rate?.toFixed(2)}%
          </span>
          <span style={{color:'#555',marginLeft:6}}>
            ({acceptance.accepted}/{acceptance.total})
          </span>
        </span>
        <span>
          Shadow Win Rate:{' '}
          {noShadow
            ? <span style={{color:'#555'}}>No counterfactual data yet</span>
            : <>
                <span style={{color: shadowWR.rate > 60 ? '#ff6666' : '#ffaa00'}}>
                  {shadowWR.rate?.toFixed(1)}%
                </span>
                <span style={{color:'#555',marginLeft:6}}>
                  ({shadowWR.won}/{shadowWR.total})
                </span>
              </>
          }
        </span>
      </div>

      {/* Staleness + sample-size warning row */}
      {!noShadow && (stale || lowN || shadowWR.pending_eval > 100) && (
        <div style={{marginBottom:8,fontSize:10,color:'#ffaa00',
          background:'#2a1f0a',padding:'4px 6px',borderRadius:3,border:'1px solid #4a3a1a'}}>
          {lowN && <>n={shadowWR.total} — too small to be reliable. </>}
          {stale && shadowWR.sample_newest &&
            <>Newest sample {fmtTime(shadowWR.sample_newest)} ({newestAgeHrs!.toFixed(0)}h ago). </>}
          {shadowWR.pending_eval > 100 &&
            <>{shadowWR.pending_eval} rejected signals pending CF evaluation.</>}
        </div>
      )}

      {/* Missed Opportunities */}
      {missed.length > 0 && (
        <div style={{marginBottom:8}}>
          <div style={{fontSize:11,color:'#888',marginBottom:3}}>
            Recent Missed Opportunities ({missed.length})
          </div>
          <div style={{maxHeight:90,overflowY:'auto',fontSize:10}}>
            {missed.map((m, i) => (
              <div key={i} style={{borderBottom:'1px solid #1a1a2e',padding:'3px 0',
                display:'flex',gap:6,alignItems:'flex-start'}}>
                <span style={{color:'#00d4ff',minWidth:90}}>{m.pair}</span>
                <span style={{minWidth:36}}>{m.direction?.toUpperCase()}</span>
                <span style={{color:'#888',minWidth:60}}>
                  {m.peak_profit_pct != null ? `+${(+m.peak_profit_pct).toFixed(2)}%` : ''}
                </span>
                <span style={{color:'#aaa',flex:1,lineHeight:1.3}}>
                  {m.miss_decode_reason || `(rejected: ${m.rejection_reason})`}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Recent signals feed */}
      <div style={{height:180,overflowY:'auto',fontSize:11}}>
        {signals.map((s, i) => {
          const time = s.generated_at
            ? new Date(s.generated_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})
            : '';
          return (
            <div key={i} style={{borderBottom:'1px solid #1a1a2e',padding:'3px 0',
              color: s.accepted ? '#00ff88' : '#ff6666',
              display:'flex',gap:8,alignItems:'center'}}>
              <span style={{color:'#555',fontSize:10,minWidth:52}}>{time}</span>
              <span>{s.accepted ? '✓' : '✗'}</span>
              <span style={{color:'#00d4ff',minWidth:110}}>{s.pair}</span>
              <span style={{minWidth:42}}>{s.direction?.toUpperCase()}</span>
              {!s.accepted && s.rejection_reason &&
                <span style={{color:'#888',fontSize:10}}>({s.rejection_reason})</span>}
              {s.signal_strength != null &&
                <span style={{color:'#555',fontSize:10,marginLeft:'auto'}}>str:{(+s.signal_strength).toFixed(1)}</span>}
            </div>
          );
        })}
        {!signals.length && (
          <div style={{color:'#555',padding:'16px 0',textAlign:'center'}}>
            No signals in history yet
          </div>
        )}
      </div>
      <div style={{marginTop:6,fontSize:10,color:'#555'}}>
        Showing last {signals.length} signals
      </div>
    </div>
  );
};
export default SignalMonitor;
