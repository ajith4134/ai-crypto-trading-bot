// AI-11: Panel 9 — Direction Prediction
import React, { useEffect, useState } from 'react';
import { getBrainStatus } from '../api';
import { card, title, P } from './shared';

const DirectionPanel: React.FC = () => {
  const [brain, setBrain] = useState<any>({});
  useEffect(() => { getBrainStatus().then(setBrain).catch(()=>{}); }, []);

  return (
    <div style={card}>
      <h3 style={title}>Direction Prediction</h3>
      <P label="Overall Directional Accuracy" value={`${(brain.directional_accuracy||0).toFixed(1)}%`} color="#00d4ff"/>
      <P label="Direction Model Status" value="Active" color="#00ff88"/>
      <P label="Last Failure Type" value={brain.last_failure||'—'}/>
      <div style={{marginTop:12,color:'#555',fontSize:11}}>Per-pair breakdown available after 50+ trades per pair.</div>
    </div>
  );
};
export default DirectionPanel;
