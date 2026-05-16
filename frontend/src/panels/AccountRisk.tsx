// AI-17: Panel 16 — Account Risk Monitor
import React, { useContext, useEffect, useState } from 'react';
import { getAccountRisk } from '../api';
import { WsContext } from '../context';
import { card, title, P } from './shared';

const AccountRisk: React.FC = () => {
  const [risk, setRisk] = useState<any>({});
  const evt = useContext(WsContext);

  const refresh = () => getAccountRisk().then(setRisk).catch(()=>{});
  useEffect(() => { refresh(); }, []);
  useEffect(() => { if (evt?.channel==='price_update') refresh(); }, [evt]);

  const mr = +(risk.margin_ratio||0);
  const mrColor = mr > 0.8 ? '#ff4444' : mr > 0.6 ? '#ffaa00' : '#00ff88';
  const cbActive = risk.circuit_breaker_active;

  return (
    <div style={card}>
      <h3 style={title}>Account Risk</h3>
      {cbActive && <div style={{background:'#440000',border:'1px solid #ff4444',borderRadius:4,padding:8,marginBottom:8,color:'#ff4444',fontSize:12}}>⚠ CIRCUIT BREAKER ACTIVE</div>}
      <div style={{marginBottom:12}}>
        <div style={{display:'flex',justifyContent:'space-between',fontSize:12,marginBottom:4}}>
          <span style={{color:'#888'}}>Margin Ratio</span>
          <span style={{color:mrColor}}>{(mr*100).toFixed(1)}%</span>
        </div>
        <div style={{background:'#0d0d1a',borderRadius:4,height:8}}>
          <div style={{background:mrColor,height:8,borderRadius:4,width:`${Math.min(100,mr*100)}%`,transition:'width 0.3s'}}/>
        </div>
      </div>
      <P label="Free Margin" value={`$${(+risk.free_margin||0).toLocaleString()}`}/>
      <P label="Total Exposure" value={`$${(+risk.total_exposure||0).toLocaleString()}`}/>
      <P label="Unrealised PnL" value={`$${(+risk.unrealised_pnl||0).toFixed(2)}`} color={(+risk.unrealised_pnl||0)>=0?'#00ff88':'#ff4444'}/>
    </div>
  );
};
export default AccountRisk;
