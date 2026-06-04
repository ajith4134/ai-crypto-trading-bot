// AI-16: Panel 15 — Performance Analytics with equity chart
import React, { useEffect, useState } from 'react';
import { getAnalytics, getEquityCurve } from '../api';
import { card, title } from './shared';

const PerformanceAnalytics: React.FC = () => {
  const [metrics, setMetrics] = useState<any>({});
  const [curve, setCurve] = useState<any[]>([]);

  useEffect(() => {
    getAnalytics().then(setMetrics).catch(() => {});
    getEquityCurve().then(setCurve).catch(() => {});
  }, []);

  // Simple SVG equity curve
  const chartH = 80, chartW = 500;
  const equityChart = (() => {
    if (curve.length < 2) return (
      <div style={{color:'#555',padding:'16px',textAlign:'center',fontSize:12}}>
        Equity curve will appear after closed trades accumulate
      </div>
    );
    const balances = curve.map((p: any) => +p.balance);
    const mn = Math.min(...balances), mx = Math.max(...balances);
    const range = mx - mn || 1;
    const pts = curve.map((p: any, i: number) => {
      const x = (i / (curve.length - 1)) * chartW;
      const y = chartH - ((+p.balance - mn) / range) * (chartH - 4) - 2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    const last = balances[balances.length - 1];
    const color = last >= balances[0] ? '#00ff88' : '#ff4444';
    return (
      <svg width="100%" viewBox={`0 0 ${chartW} ${chartH}`} style={{display:'block'}}>
        <polyline points={pts} fill="none" stroke={color} strokeWidth="1.5" strokeLinejoin="round"/>
        <text x={chartW - 4} y={12} textAnchor="end" fill="#888" fontSize="10">
          ${last.toFixed(0)}
        </text>
      </svg>
    );
  })();

  const windows = ['50', '100', '500'];
  const metricKeys = ['win_rate','net_pnl_usdt','sharpe','sortino','profit_factor','max_drawdown_usdt','avg_hold_hours','avg_win_loss_ratio','directional_accuracy'];
  const metricLabels = ['Win Rate %','Net PnL','Sharpe','Sortino','Profit Factor','Max Drawdown','Avg Hold (h)','Win/Loss Ratio','Dir Accuracy %'];

  return (
    <div style={card}>
      <h3 style={title}>Performance Analytics</h3>
      <div style={{marginBottom:16,background:'#0d0d1a',borderRadius:4,padding:'4px 8px'}}>
        {equityChart}
      </div>
      <div style={{overflowX:'auto'}}>
        <table style={{width:'100%',borderCollapse:'collapse',fontSize:12}}>
          <thead>
            <tr>
              <th style={{textAlign:'left',padding:'4px 8px',color:'#888',borderBottom:'1px solid #2a2a4a'}}>Metric</th>
              {windows.map(w => <th key={w} style={{textAlign:'right',padding:'4px 8px',color:'#888',borderBottom:'1px solid #2a2a4a'}}>Last {w}</th>)}
            </tr>
          </thead>
          <tbody>
            {metricKeys.map((k, i) => (
              <tr key={k} style={{borderBottom:'1px solid #111'}}>
                <td style={{padding:'3px 8px',color:'#888'}}>{metricLabels[i]}</td>
                {windows.map(w => (
                  <td key={w} style={{padding:'3px 8px',textAlign:'right',color:'#e0e0e0'}}>
                    {metrics[w]?.[k] != null ? (+metrics[w][k]).toFixed(2) : '—'}
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
