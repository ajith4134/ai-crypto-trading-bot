// AI Brain Intelligence Panel — Curiosity Engine (F38), Self-Play (F41), Research Engine (F36)
import React, { useEffect, useState } from 'react';
import { getAdvancedBrainStatus } from '../api';
import { card, title } from './shared';

const bar = (val: number, max: number, color: string) => (
  <div style={{background:'#0d0d1a',borderRadius:3,height:6,width:'100%',marginTop:3}}>
    <div style={{background:color,height:6,borderRadius:3,width:`${Math.min(100,(val/max)*100)}%`,transition:'width 0.5s'}}/>
  </div>
);

const Metric: React.FC<{label:string;value:React.ReactNode;sub?:string;color?:string}> = ({label,value,sub,color}) => (
  <div style={{marginBottom:10}}>
    <div style={{display:'flex',justifyContent:'space-between',alignItems:'baseline'}}>
      <span style={{color:'#888',fontSize:11}}>{label}</span>
      <span style={{color:color||'#e0e0e0',fontSize:13,fontWeight:'bold'}}>{value}</span>
    </div>
    {sub && <div style={{color:'#555',fontSize:10,marginTop:2}}>{sub}</div>}
  </div>
);

const AIIntelligencePanel: React.FC = () => {
  const [adv, setAdv] = useState<any>({});

  useEffect(() => {
    const load = () => getAdvancedBrainStatus().then(setAdv).catch(()=>{});
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, []);

  const curiosity   = +(adv.curiosity_score  || 0);
  const budget      = +(adv.exploration_budget || 10);
  const spWR        = +(adv.self_play_win_rate || 0);
  const spGames     = +(adv.self_play_games   || 0);
  const spLastPnl   = +(adv.self_play_last_pnl|| 0);
  const resQ        = +(adv.research_queue_length || 0);
  const domains     = +(adv.competence_domains   || 0);
  const worst       = adv.worst_learning_domain  || '—';
  const best        = adv.best_learning_domain   || '—';
  const gap         = adv.priority_gap           || '—';

  const curiosityColor = curiosity >= 70 ? '#ff6600' : curiosity >= 40 ? '#ffaa00' : '#00ff88';
  const spColor = spWR >= 55 ? '#00ff88' : spWR >= 45 ? '#ffaa00' : '#ff4444';

  return (
    <div style={card}>
      <h3 style={title}>AI Intelligence</h3>

      {/* F38 Curiosity Engine */}
      <div style={{marginBottom:14,paddingBottom:12,borderBottom:'1px solid #1a1a2e'}}>
        <div style={{fontSize:11,color:'#666',marginBottom:6,textTransform:'uppercase',letterSpacing:1}}>
          F38 Curiosity Engine
        </div>
        <Metric
          label="Novelty Score"
          value={`${curiosity.toFixed(1)} / 100`}
          color={curiosityColor}
          sub={curiosity>=70 ? '⚡ High — exploration hypothesis queued' : curiosity>=40 ? '~ Moderate novelty detected' : '✓ Normal market conditions'}
        />
        {bar(curiosity, 100, curiosityColor)}
        <div style={{marginTop:8}}>
          <Metric
            label="Exploration Budget"
            value={`${budget.toFixed(1)}%`}
            color="#aaaaff"
            sub="% of capacity allocated to novel pair/strategy exploration"
          />
          {bar(budget, 25, '#aaaaff')}
        </div>
      </div>

      {/* F41 Self-Play vs MarS */}
      <div style={{marginBottom:14,paddingBottom:12,borderBottom:'1px solid #1a1a2e'}}>
        <div style={{fontSize:11,color:'#666',marginBottom:6,textTransform:'uppercase',letterSpacing:1}}>
          F41 Self-Play vs MarS
        </div>
        <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:4,marginBottom:6}}>
          <Metric label="Win Rate" value={spGames>0 ? `${spWR.toFixed(1)}%` : '—'} color={spColor}/>
          <Metric label="Episodes" value={spGames||'—'} color="#aaa"/>
        </div>
        <Metric
          label="Last Episode PnL"
          value={spGames>0 ? `${spLastPnl>=0?'+':''}${spLastPnl.toFixed(4)}` : '—'}
          color={spLastPnl>=0?'#00ff88':'#ff4444'}
          sub={spGames===0 ? 'Runs every 30 min via Celery beat' : undefined}
        />
        {spGames > 0 && bar(spWR, 100, spColor)}
      </div>

      {/* F36 Strategy Research Engine */}
      <div style={{marginBottom:14,paddingBottom:12,borderBottom:'1px solid #1a1a2e'}}>
        <div style={{fontSize:11,color:'#666',marginBottom:6,textTransform:'uppercase',letterSpacing:1}}>
          F36 Research Engine
        </div>
        <Metric
          label="Hypothesis Queue"
          value={resQ}
          color={resQ>0?'#ffaa00':'#555'}
          sub={resQ>0 ? `${resQ} hypotheses awaiting AirLLM analysis` : 'Triggers every 25 trades at 100+ closed'}
        />
      </div>

      {/* F43 Metacognitive Competence Map */}
      <div>
        <div style={{fontSize:11,color:'#666',marginBottom:6,textTransform:'uppercase',letterSpacing:1}}>
          F43 Competence Map
        </div>
        <Metric label="Domains Tracked" value={domains||'—'} color="#aaa" sub="pair × regime direction/win domains"/>
        <Metric
          label="Priority Gap"
          value={gap !== '—' ? gap.replace(/_/g,' ').slice(0,30) : '—'}
          color="#ff8800"
          sub="Worst-performing domain — brain focuses learning here"
        />
        {worst !== '—' && (
          <div style={{display:'grid',gridTemplateColumns:'1fr 1fr',gap:4,marginTop:4}}>
            <Metric label="Worst Domain" value={worst.replace(/_/g,' ').slice(0,20)} color="#ff6666"/>
            <Metric label="Best Domain"  value={best.replace(/_/g,' ').slice(0,20)} color="#00ff88"/>
          </div>
        )}
        {domains === 0 && (
          <div style={{color:'#555',fontSize:11,marginTop:4}}>
            Activates after 100 closed trades · currently {domains} domains tracked
          </div>
        )}
      </div>
    </div>
  );
};
export default AIIntelligencePanel;
