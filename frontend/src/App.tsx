import React, { useEffect, useState } from 'react';
import { getToken, login, setToken } from './api';
import { connect, onMessage } from './ws';
import ControlPanel from './panels/ControlPanel';
import BrainStatus from './panels/BrainStatus';
import OpenTradesTable from './panels/OpenTradesTable';
import ClosedTradesTable from './panels/ClosedTradesTable';
import SignalMonitor from './panels/SignalMonitor';
import PairScanner from './panels/PairScanner';
import LaunchPad from './panels/LaunchPad';
import StrategyPanel from './panels/StrategyPanel';
import DirectionPanel from './panels/DirectionPanel';
import DirectionWinRate from './panels/DirectionWinRate';
import PerformanceAnalytics from './panels/PerformanceAnalytics';
import AccountRisk from './panels/AccountRisk';
import SystemHealth from './panels/SystemHealth';
import WebIntelPanel from './panels/WebIntelPanel';
import FeatureHealth from './panels/FeatureHealth';
import FeatureFiringHealth from './panels/FeatureFiringHealth';
import LLMProvidersPanel from './panels/LLMProvidersPanel';
import HedgeLearnedParamsPanel from './panels/HedgeLearnedParamsPanel';
import MemoryClustersPanel from './panels/MemoryClustersPanel';
import DecoderFeedPanel from './panels/DecoderFeedPanel';
import SelfPlayLOBPanel from './panels/SelfPlayLOBPanel';
import TelegramLog from './panels/TelegramLog';
import MLModelsPanel from './panels/MLModelsPanel';
import IntelligencePanel from './panels/IntelligencePanel';
import AIIntelligencePanel from './panels/AIIntelligencePanel';

import { WsEvent, WsContext } from './context';
import SummaryBar from './panels/SummaryBar';

class ErrorBoundary extends React.Component<{children: React.ReactNode}, {error: string|null}> {
  constructor(props: any) { super(props); this.state = {error: null}; }
  static getDerivedStateFromError(e: Error) { return {error: e.message}; }
  render() {
    if (this.state.error) return (
      <div style={{background:'#0a0a0a',color:'#ff4444',padding:32,fontFamily:'monospace'}}>
        <h2>Dashboard Error</h2>
        <pre style={{whiteSpace:'pre-wrap'}}>{this.state.error}</pre>
        <button onClick={()=>window.location.reload()} style={{marginTop:16,padding:'8px 16px',cursor:'pointer'}}>Reload</button>
      </div>
    );
    return this.props.children;
  }
}


const LoginPage: React.FC<{ onLogin: () => void }> = ({ onLogin }) => {
  const [user, setUser] = useState('admin');
  const [pass, setPass] = useState('');
  const [err, setErr] = useState('');
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try { const t = await login(user, pass); setToken(t); onLogin(); }
    catch { setErr('Invalid credentials'); }
  };
  return (
    <div style={{display:'flex',justifyContent:'center',alignItems:'center',height:'100vh',background:'#0a0a0a'}}>
      <form onSubmit={submit} style={{background:'#1a1a2e',padding:32,borderRadius:8,minWidth:320,border:'1px solid #2a2a4a'}}>
        <h2 style={{color:'#00d4ff',marginTop:0}}>Trading Bot Dashboard</h2>
        {err && <p style={{color:'#ff4444'}}>{err}</p>}
        <input value={user} onChange={e=>setUser(e.target.value)} placeholder="Username" style={{width:'100%',padding:8,marginBottom:12,background:'#0d0d1a',color:'#fff',border:'1px solid #333',borderRadius:4,boxSizing:'border-box'}}/>
        <input type="password" value={pass} onChange={e=>setPass(e.target.value)} placeholder="Password" style={{width:'100%',padding:8,marginBottom:16,background:'#0d0d1a',color:'#fff',border:'1px solid #333',borderRadius:4,boxSizing:'border-box'}}/>
        <button type="submit" style={{width:'100%',padding:10,background:'#00d4ff',color:'#000',border:'none',borderRadius:4,cursor:'pointer',fontWeight:'bold'}}>Login</button>
      </form>
    </div>
  );
};

const App: React.FC = () => {
  const [authed, setAuthed] = useState(!!getToken());
  const [lastEvent, setLastEvent] = useState<WsEvent | null>(null);
  const [wsOk, setWsOk] = useState(false);

  useEffect(() => {
    if (!authed) return;
    connect();
    onMessage((channel, data) => { setLastEvent({channel,data}); setWsOk(true); });
  }, [authed]);

  if (!authed) return <LoginPage onLogin={() => setAuthed(true)} />;

  return (
    <ErrorBoundary>
    <WsContext.Provider value={lastEvent}>
      <div style={{background:'#0a0a0a',minHeight:'100vh',color:'#e0e0e0',fontFamily:'monospace',fontSize:13}}>
        <div style={{background:'#1a1a2e',padding:'4px 16px',fontSize:11,color:wsOk?'#00ff88':'#ff8800',borderBottom:'1px solid #2a2a4a'}}>
          {wsOk?'● Live':'○ Connecting...'} &nbsp;|&nbsp; AI Crypto Trading Bot
        </div>
        <div style={{padding:'12px 12px 0'}}><SummaryBar /></div>
        <div style={{padding:12,display:'grid',gap:12,gridTemplateColumns:'repeat(auto-fit, minmax(380px, 1fr))'}}>
          <ControlPanel />
          <BrainStatus />
          <MLModelsPanel />
          <IntelligencePanel />
          <div style={{gridColumn:'1 / -1'}}><OpenTradesTable /></div>
          <SignalMonitor />
          <PairScanner />
          <LaunchPad />
          <StrategyPanel />
          <AIIntelligencePanel />
          <DirectionPanel />
          <DirectionWinRate />
          <div style={{gridColumn:'1 / -1'}}><ClosedTradesTable /></div>
          <WebIntelPanel />
          <FeatureHealth />
          <FeatureFiringHealth />
          <LLMProvidersPanel />
          <HedgeLearnedParamsPanel />
          <MemoryClustersPanel />
          <SelfPlayLOBPanel />
          <div style={{gridColumn:'1 / -1'}}><DecoderFeedPanel /></div>
          <TelegramLog />
          <div style={{gridColumn:'1 / -1'}}><PerformanceAnalytics /></div>
          <AccountRisk />
          <SystemHealth />
        </div>
      </div>
    </WsContext.Provider>
    </ErrorBoundary>
  );
};

export default App;
