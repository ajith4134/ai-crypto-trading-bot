// AI-16: Panel 15 — Performance Analytics with equity chart
import React, { useEffect, useRef, useState } from 'react';
import { getAnalytics } from '../api';
import { card, title, P, Table } from './shared';

const PerformanceAnalytics: React.FC = () => {
  const [metrics, setMetrics] = useState<any>({});
  const chartRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    getAnalytics().then(setMetrics).catch(()=>{});
  }, []);

  // Chart renders after trades accumulate — placeholder for now
  useEffect(() => {
    if (!chartRef.current) return;
    chartRef.current.innerHTML = '<div style="color:#555;padding:16px;text-align:center;font-size:12px">Equity curve will appear after first closed trades</div>';
  }, []);

  const windows = ['50','100','500'];
  const metricKeys = ['win_rate','net_pnl_usdt','sharpe','sortino','profit_factor','max_drawdown_usdt','avg_hold_hours','avg_win_loss_ratio','directional_accuracy'];
  const metricLabels = ['Win Rate %','Net PnL','Sharpe','Sortino','Profit Factor','Max Drawdown','Avg Hold (h)','Win/Loss Ratio','Dir Accuracy %'];

  return (
    <div style={card}>
      <h3 style={title}>Performance Analytics</h3>
      <div ref={chartRef} style={{marginBottom:16,background:'#0d0d1a',borderRadius:4}}/>
      <div style={{overflowX:'auto'}}>
        <table style={{width:'100%',borderCollapse:'collapse',fontSize:12}}>
          <thead>
            <tr>
              <th style={{textAlign:'left',padding:'4px 8px',color:'#888',borderBottom:'1px solid #2a2a4a'}}>Metric</th>
              {windows.map(w => <th key={w} style={{textAlign:'right',padding:'4px 8px',color:'#888',borderBottom:'1px solid #2a2a4a'}}>Last {w}</th>)}
            </tr>
          </thead>
          <tbody>
            {metricKeys.map((k,i) => (
              <tr key={k} style={{borderBottom:'1px solid #111'}}>
                <td style={{padding:'3px 8px',color:'#888'}}>{metricLabels[i]}</td>
                {windows.map(w => (
                  <td key={w} style={{padding:'3px 8px',textAlign:'right',color:'#e0e0e0'}}>
                    {metrics[w]?.[k]?.toFixed(2)||'—'}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};
export default PerformanceAnalytics;
