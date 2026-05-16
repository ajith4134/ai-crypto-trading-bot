// AI-06: Panel 4 — Intelligence → Open Trade Performance
import React, { useContext, useEffect, useState } from 'react';
import { getOpenTrades } from '../api';
import { WsContext } from '../App';
import { card, title, Table } from './shared';

const IntelligencePanel: React.FC = () => {
  const [trades, setTrades] = useState<any[]>([]);
  const evt = useContext(WsContext);

  useEffect(() => { getOpenTrades().then(setTrades).catch(()=>{}); }, []);
  useEffect(() => {
    if (evt?.channel === 'trade_opened') getOpenTrades().then(setTrades).catch(()=>{});
  }, [evt]);

  const rows = trades.map(t => [
    t.pair, t.direction?.toUpperCase(), t.strategy_id?.slice(0,8)||'—',
    t.trade_potential_score||'—', t.direction_confidence||'—',
    t.brain_actions?.length||0,
  ]);

  return (
    <div style={card}>
      <h3 style={title}>Intelligence → Open Trades</h3>
      <Table cols={['Pair','Dir','Strategy','Potential','Conf','Brain Actions']} rows={rows} maxH={180}/>
    </div>
  );
};
export default IntelligencePanel;
