// AI-05: Panel 3 — ML & RL Models
import React from 'react';
import { card, title, Table } from './shared';

const MODELS = [
  ['HMM Regime','Regime Detection','Active','—','Regime accuracy'],
  ['TFT Forecast','Price Prediction','Active','—','MAPE'],
  ['PatchTST','Long Sequence','Active','—','MAPE'],
  ['GNN Correlation','Inter-Asset','Active','—','Lead-lag accuracy'],
  ['World Model','Latent Dynamics','Active','—','Prediction error'],
  ['Kelly Sizer','Position Sizing','Active','—','Capital efficiency'],
  ['MARL (3-tier)','Execution','Pending 300 trades','0%','Win rate'],
  ['MAML','Regime Adapt','Pending 500 trades','0%','Adaptation speed'],
  ['CryptoBERT','Sentiment','Active','—','Score accuracy'],
  ['FinBERT','Sentiment','Active','—','Score accuracy'],
];

const MLModelsPanel: React.FC = () => (
  <div style={card}>
    <h3 style={title}>ML & RL Models</h3>
    <Table cols={['Model','Purpose','Status','Progress','Metric']} rows={MODELS} maxH={220}/>
  </div>
);
export default MLModelsPanel;
