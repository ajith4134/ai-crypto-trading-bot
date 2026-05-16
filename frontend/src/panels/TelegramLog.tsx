// AI-15: Panel 14 — Telegram Notification Log
import React, { useContext, useState } from 'react';
import { WsContext } from '../context';
import { card, title } from './shared';

const TelegramLog: React.FC = () => {
  const [msgs, setMsgs] = useState<Array<{time:string;type:string;text:string}>>([]);
  const evt = useContext(WsContext);

  React.useEffect(() => {
    if (evt?.channel === 'alert') {
      setMsgs(m => [{time: new Date().toLocaleTimeString(), type:'alert', text: JSON.stringify(evt.data).slice(0,80)}, ...m].slice(0,50));
    }
  }, [evt]);

  return (
    <div style={card}>
      <h3 style={title}>Telegram Alerts (last 50)</h3>
      <div style={{height:200,overflowY:'auto',fontSize:11}}>
        {msgs.map((m,i) => (
          <div key={i} style={{borderBottom:'1px solid #1a1a2e',padding:'3px 0'}}>
            <span style={{color:'#555'}}>{m.time} </span>
            <span style={{color:'#ffaa00'}}>[{m.type}] </span>
            <span>{m.text}</span>
          </div>
        ))}
        {!msgs.length && <span style={{color:'#555'}}>No alerts yet...</span>}
      </div>
    </div>
  );
};
export default TelegramLog;
