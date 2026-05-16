// AI-14: Panel 13 — Feature Health Monitor
import React, { useEffect, useState } from 'react';
import { getFeaturesHealth } from '../api';
import { card, title, Table } from './shared';

const FeatureHealth: React.FC = () => {
  const [features, setFeatures] = useState<any[]>([]);
  useEffect(() => { getFeaturesHealth().then(setFeatures).catch(()=>{}); }, []);

  const statusColor = (s:string) => s==='active'?'#00ff88':s==='probation'?'#ffaa00':s==='turned_off'?'#ff4444':'#888';

  const rows = features.map(f => [
    f.feature_id,
    f.feature_name,
    `Phase ${f.activation_phase}`,
    <span style={{color:statusColor(f.status)}}>{f.status}</span>,
    `${((+f.current_weight||0)*100).toFixed(0)}%`,
    f.failure_mode||'—',
  ]);

  return (
    <div style={card}>
      <h3 style={title}>Feature Health ({features.length})</h3>
      <Table cols={['ID','Name','Phase','Status','Weight','Failure Mode']} rows={rows} maxH={220}/>
    </div>
  );
};
export default FeatureHealth;
