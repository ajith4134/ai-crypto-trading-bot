// AI-03: Panel 1 — Control Panel
import React, { useEffect, useState } from 'react';
import { getBotStatus, setBotMode, setBotSettings } from '../api';
import { card, title, badge } from './shared';

const ControlPanel: React.FC = () => {
  const [status, setStatus] = useState<any>({});
  const [minOpen, setMinOpen] = useState(3);
  const [maxOpen, setMaxOpen] = useState(15);

  useEffect(() => { getBotStatus().then(setStatus).catch(()=>{}); }, []);

  const paperClosed = status.paper_closed || 0;
  const liveUnlocked = paperClosed >= 2000 && status.stage === 4;
  const stageLabel = ['','Baby','Learning','Developing','Expert'][status.stage||1]||'Baby';

  return (
    <div style={card}>
      <h3 style={title}>Control Panel</h3>
      <div style={{display:'flex',gap:8,marginBottom:12,flexWrap:'wrap'}}>
        <span style={badge(status.running ? '#00ff88':'#ff4444')}>{status.running?'RUNNING':'STOPPED'}</span>
        <span style={badge('#0088ff')}>{(status.mode||'paper').toUpperCase()}</span>
        <span style={badge('#aa88ff')}>Stage {status.stage||1} — {stageLabel}</span>
      </div>
      <div style={{marginBottom:8,fontSize:12,color:'#888'}}>
        Paper Trades: <span style={{color:paperClosed>=2000?'#00ff88':'#ffaa00'}}>{paperClosed} / 2000</span>
      </div>
      <div style={{background:'#0d0d1a',borderRadius:4,height:6,marginBottom:12}}>
        <div style={{background:'#00d4ff',height:6,borderRadius:4,width:`${Math.min(100,(paperClosed/2000)*100)}%`}}/>
      </div>
      <div style={{display:'flex',gap:8,marginBottom:12}}>
        <label style={{fontSize:12,color:'#888'}}>Min open: <input type="number" value={minOpen} onChange={e=>setMinOpen(+e.target.value)} style={{width:50,background:'#0d0d1a',color:'#fff',border:'1px solid #333',borderRadius:3,padding:'2px 4px'}} /></label>
        <label style={{fontSize:12,color:'#888'}}>Max open: <input type="number" value={maxOpen} onChange={e=>setMaxOpen(+e.target.value)} style={{width:50,background:'#0d0d1a',color:'#fff',border:'1px solid #333',borderRadius:3,padding:'2px 4px'}} /></label>
        <button onClick={()=>setBotSettings({min_open_trades:minOpen,max_open_trades:maxOpen})} style={{background:'#333',color:'#fff',border:'none',borderRadius:3,padding:'2px 8px',cursor:'pointer',fontSize:12}}>Apply</button>
      </div>
      <button onClick={()=>setBotMode('live')} disabled={!liveUnlocked}
        style={{width:'100%',padding:8,background:liveUnlocked?'#ff8800':'#333',color:liveUnlocked?'#000':'#666',border:'none',borderRadius:4,cursor:liveUnlocked?'pointer':'not-allowed',fontWeight:'bold',fontSize:12}}>
        {liveUnlocked ? 'Switch to LIVE' : `Live locked (need 2000 trades, Stage 4)`}
      </button>
    </div>
  );
};
export default ControlPanel;
