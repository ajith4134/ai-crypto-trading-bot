// AI-08: Panel 6 — Signal Monitor
import React, { useContext, useEffect, useState } from 'react';
import { WsContext } from '../context';
import { card, title } from './shared';

const SignalMonitor: React.FC = () => {
  const [signals, setSignals] = useState<any[]>([]);
  const [shadowWR, setShadowWR] = useState({total:0,won:0,rate:0});
  const evt = useContext(WsContext);

  useEffect(() => {
    if (!evt) return;
    if (evt.channel === 'signal_generated') setSignals(s => [evt.data, ...s].slice(0,50));
  }, [evt]);

  return (
    <div style={card}>
      <h3 style={title}>Signal Monitor</h3>
      <div style={{marginBottom:8,fontSize:12}}>
        Shadow Win Rate: <span style={{color:'#ffaa00'}}>{shadowWR.rate.toFixed(1)}%</span>
        <span style={{color:'#555',marginLeft:8}}>({shadowWR.won}/{shadowWR.total})</span>
      </div>
      <div style={{height:200,overflowY:'auto',fontSize:11}}>
        {signals.map((s,i)=>(
          <div key={i} style={{borderBottom:'1px solid #1a1a2e',padding:'3px 0',color:s.accepted?'#00ff88':'#ff6666'}}>
            {s.accepted?'✓':'✗'} {s.pair} {s.direction?.toUpperCase()} {s.rejection_reason?`(${s.rejection_reason})`:''}
          </div>
        ))}
        {!signals.length && <span style={{color:'#555'}}>No signals yet...</span>}
      </div>
    </div>
  );
};
export default SignalMonitor;
