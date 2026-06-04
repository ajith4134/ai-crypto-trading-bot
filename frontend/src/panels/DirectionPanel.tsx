// AI-11: Panel 9 — Direction Prediction
import React, { useEffect, useState } from 'react';
import { getBrainStatus, getClosedTrades } from '../api';
import { card, title, P } from './shared';

const DirectionPanel: React.FC = () => {
  const [brain, setBrain] = useState<any>({});
  const [pairStats, setPairStats] = useState<any[]>([]);

  useEffect(() => {
    getBrainStatus().then(setBrain).catch(() => {});
    // Build per-pair accuracy from closed trades
    getClosedTrades({ limit: 500 }).then((res: any) => {
      const trades: any[] = Array.isArray(res) ? res : (res.trades || []);
      const byPair: Record<string, {total: number; wins: number; dirFails: number}> = {};
      for (const t of trades) {
        if (!t.pair) continue;
        if (!byPair[t.pair]) byPair[t.pair] = { total: 0, wins: 0, dirFails: 0 };
        byPair[t.pair].total++;
        if ((+(t.net_pnl_usdt || 0)) > 0) byPair[t.pair].wins++;
        if (t.failure_type === 'direction') byPair[t.pair].dirFails++;
      }
      const rows = Object.entries(byPair)
        .map(([pair, s]) => ({
          pair,
          total: s.total,
          wins: s.wins,
          dirFails: s.dirFails,
          accuracy: s.total > 0 ? ((s.wins / s.total) * 100) : 0,
        }))
        .filter(r => r.total >= 2)
        .sort((a, b) => b.total - a.total)
        .slice(0, 15);
      setPairStats(rows);
    }).catch(() => {});
  }, []);

  const acc = brain.directional_accuracy ?? 0;
  const accColor = acc >= 55 ? '#00ff88' : acc >= 40 ? '#ffaa00' : '#ff4444';

  return (
    <div style={card}>
      <h3 style={title}>Direction Prediction</h3>

      <div style={{display:'flex',gap:24,marginBottom:12,flexWrap:'wrap'}}>
        <div style={{textAlign:'center'}}>
          <div style={{fontSize:28,fontWeight:'bold',color:accColor}}>{acc.toFixed(1)}%</div>
          <div style={{fontSize:10,color:'#555'}}>Overall Directional Accuracy</div>
        </div>
        <div style={{flex:1,minWidth:140}}>
          <P label="Direction Model" value="Stage 2 — Sentiment + OFI + Regime" color="#00d4ff"/>
          <P label="Stage Target" value="55% accuracy → activate X-09 model at 100 trades"/>
          <P label="OPRO Counter" value={`${brain.opro_counter || 0} trades optimised`} color="#aaa"/>
        </div>
      </div>

      {/* Per-pair breakdown */}
      {pairStats.length > 0 ? (
        <>
          <div style={{fontSize:11,color:'#666',marginBottom:4}}>
            Per-pair win rate (proxy for directional accuracy):
          </div>
          <div style={{maxHeight:180,overflowY:'auto'}}>
            <table style={{width:'100%',borderCollapse:'collapse',fontSize:11}}>
              <thead>
                <tr>
                  {['Pair','Trades','Wins','Dir Fails','Accuracy'].map(h =>
                    <th key={h} style={{textAlign:'left',padding:'2px 6px',color:'#555',
                      borderBottom:'1px solid #1a1a2e',fontWeight:400}}>{h}</th>
                  )}
                </tr>
              </thead>
              <tbody>
                {pairStats.map(r => {
                  const col = r.accuracy >= 50 ? '#00ff88' : r.accuracy >= 35 ? '#ffaa00' : '#ff4444';
                  return (
                    <tr key={r.pair} style={{borderBottom:'1px solid #0d0d1a'}}>
                      <td style={{padding:'2px 6px',color:'#00d4ff'}}>{r.pair}</td>
                      <td style={{padding:'2px 6px',color:'#888'}}>{r.total}</td>
                      <td style={{padding:'2px 6px',color:'#00ff88'}}>{r.wins}</td>
                      <td style={{padding:'2px 6px',color:'#ff6666'}}>{r.dirFails}</td>
                      <td style={{padding:'2px 6px',color:col,fontWeight:'bold'}}>
                        {r.accuracy.toFixed(0)}%
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      ) : (
        <div style={{color:'#555',fontSize:11,marginTop:8}}>
          Per-pair breakdown appears after 2+ trades per pair
        </div>
      )}
    </div>
  );
};
export default DirectionPanel;
