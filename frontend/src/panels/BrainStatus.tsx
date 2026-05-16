// AI-04: Panel 2 — Brain Status
import React, { useContext, useEffect, useState } from 'react';
import { getBrainStatus } from '../api';
import { WsContext } from '../App';
import { card, title, P } from './shared';

const BrainStatus: React.FC = () => {
  const [brain, setBrain] = useState<any>({});
  const [log, setLog] = useState<string[]>([]);
  const evt = useContext(WsContext);

  useEffect(() => { getBrainStatus().then(setBrain).catch(()=>{}); }, []);

  useEffect(() => {
    if (!evt) return;
    if (evt.channel === 'brain_decision' || evt.channel === 'brain_metrics') {
      setBrain((b:any) => ({...b, ...evt.data}));
      const msg = evt.data.verdict?.reason || evt.data.regime || JSON.stringify(evt.data).slice(0,60);
      setLog(l => [msg, ...l].slice(0,20));
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
        <div style={{color:'#888',fontSize:11,marginBottom:4}}>Brain Decision Feed</div>
        <div style={{background:'#0d0d1a',borderRadius:4,padding:8,height:120,overflowY:'auto',fontSize:11}}>
          {log.length ? log.map((l,i)=><div key={i} style={{borderBottom:'1px solid #222',padding:'2px 0',color:'#aaa'}}>{l}</div>) : <span style={{color:'#555'}}>Waiting for events...</span>}
        </div>
      </div>
    </div>
  );
};
export default BrainStatus;
