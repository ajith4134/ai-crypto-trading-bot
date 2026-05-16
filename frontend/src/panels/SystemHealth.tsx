// AI-18: Panel 17 — System Health
import React, { useContext, useEffect, useState } from 'react';
import { getSystemHealth } from '../api';
import { WsContext } from '../App';
import { card, title } from './shared';

const SERVICES = ['brain','data_feed','scanner','web_intel','dashboard','celery_worker','watchdog','llama_cpp','ollama','postgres','redis'];

const SystemHealth: React.FC = () => {
  const [health, setHealth] = useState<any>({});
  const evt = useContext(WsContext);

  const refresh = () => getSystemHealth().then(setHealth).catch(()=>{});
  useEffect(() => { refresh(); const t = setInterval(refresh,30000); return ()=>clearInterval(t); }, []);
  useEffect(() => { if (evt?.channel==='system_event') refresh(); }, [evt]);

  const serviceStatus = (name:string) => {
    const s = health.services?.[name];
    if (!s) return {color:'#555',label:'Unknown'};
    if (s.status==='ok') return {color:'#00ff88',label:'Running'};
    if (s.status==='degraded') return {color:'#ffaa00',label:'Degraded'};
    return {color:'#ff4444',label:'Down'};
  };

  return (
    <div style={card}>
      <h3 style={title}>System Health</h3>
      <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:6}}>
        {SERVICES.map(svc => {
          const {color,label} = serviceStatus(svc);
          return (
            <div key={svc} style={{background:'#0d0d1a',borderRadius:4,padding:'4px 8px',display:'flex',justifyContent:'space-between',fontSize:11}}>
              <span>{svc}</span>
              <span style={{color}}>{label}</span>
            </div>
          );
        })}
      </div>
      <div style={{marginTop:8,fontSize:11,color:'#555'}}>WS Clients: {health.ws_clients||0}</div>
    </div>
  );
};
export default SystemHealth;
