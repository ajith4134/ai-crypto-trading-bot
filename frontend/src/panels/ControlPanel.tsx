// AI-03: Panel 1 — Control Panel
// All 4 settings required before Start Trading is enabled.
import React, { useEffect, useState } from 'react';
import { getBotStatus, setBotMode } from '../api';
import axios from 'axios';
import { card, title, badge } from './shared';

const getToken = () => sessionStorage.getItem('token') || localStorage.getItem('token') || '';
const authApi = axios.create({ baseURL: '' });
authApi.interceptors.request.use(cfg => {
  cfg.headers['Authorization'] = `Bearer ${getToken()}`;
  return cfg;
});

const inp: React.CSSProperties = {
  background: '#0d0d1a', color: '#fff', border: '1px solid #444',
  borderRadius: 4, padding: '5px 8px', width: '100%',
  boxSizing: 'border-box', fontSize: 13, marginTop: 4,
};

const ControlPanel: React.FC = () => {
  const [status, setStatus]       = useState<any>({});
  const [minOpen, setMinOpen]     = useState('');
  const [maxOpen, setMaxOpen]     = useState('');
  const [maxPos,  setMaxPos]      = useState('');
  const [capital, setCapital]     = useState('');
  const [saved,   setSaved]       = useState(false);
  const [saveErr, setSaveErr]     = useState('');
  const [starting, setStarting]   = useState(false);

  const refresh = () => getBotStatus().then((s: any) => {
    setStatus(s);
    if (s.min_open_trades)      setMinOpen(String(s.min_open_trades));
    if (s.max_open_trades)      setMaxOpen(String(s.max_open_trades));
    if (s.max_position_usdt)    setMaxPos(String(s.max_position_usdt));
    if (s.starting_capital_usdt) setCapital(String(s.starting_capital_usdt));
    setSaved(!!s.settings_configured);
  }).catch(() => {});

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  const paperClosed  = status.paper_closed || 0;
  const liveUnlocked = paperClosed >= 2000 && status.stage === 4;
  const stageLabel   = ['','Baby','Learning','Developing','Expert'][status.stage || 1] || 'Baby';
  const allFilled    = minOpen && maxOpen && maxPos && capital;
  const canStart     = saved && !status.running;

  const saveSettings = async () => {
    setSaveErr('');
    try {
      await authApi.put('/bot/settings', {
        min_open_trades:       Number(minOpen),
        max_open_trades:       Number(maxOpen),
        max_position_usdt:     Number(maxPos),
        starting_capital_usdt: Number(capital),
      });
      setSaved(true);
      refresh();
    } catch (e: any) {
      setSaveErr(e.response?.data?.detail || 'Save failed');
      setSaved(false);
    }
  };

  const startBot = async () => {
    setStarting(true);
    try {
      await authApi.post('/bot/start', {});
      refresh();
    } catch (e: any) {
      alert(e.response?.data?.detail || 'Start failed');
    }
    setStarting(false);
  };

  const stopBot = async () => {
    await authApi.post('/bot/stop', {});
    refresh();
  };

  return (
    <div style={card}>
      <h3 style={title}>Control Panel</h3>

      {/* Status badges */}
      <div style={{ display:'flex', gap:8, marginBottom:12, flexWrap:'wrap' }}>
        <span style={badge(status.running ? '#00ff88' : '#ff4444')}>{status.running ? 'RUNNING' : 'STOPPED'}</span>
        <span style={badge('#0088ff')}>{(status.mode || 'paper').toUpperCase()}</span>
        <span style={badge('#aa88ff')}>Stage {status.stage || 1} — {stageLabel}</span>
      </div>

      {/* Paper trade progress bar */}
      <div style={{ marginBottom:4, fontSize:12, color:'#888' }}>
        Paper Trades:&nbsp;
        <span style={{ color: paperClosed >= 2000 ? '#00ff88' : '#ffaa00' }}>{paperClosed} / 2000</span>
      </div>
      <div style={{ background:'#0d0d1a', borderRadius:4, height:6, marginBottom:16 }}>
        <div style={{ background:'#00d4ff', height:6, borderRadius:4, width:`${Math.min(100,(paperClosed/2000)*100)}%`, transition:'width 0.5s' }}/>
      </div>

      {/* Pre-start settings form */}
      <div style={{
        background:'#0d0d1a', borderRadius:6, padding:14, marginBottom:12,
        border: saved ? '1px solid #00cc44' : '1px solid #ff8800',
      }}>
        <div style={{ fontSize:11, fontWeight:'bold', marginBottom:12,
          color: saved ? '#00ff88' : '#ffaa00' }}>
          {saved
            ? '✅ Settings saved — Start Trading is enabled'
            : '⚠️ Fill all 4 fields and Save before pressing Start Trading'}
        </div>

        <label style={{ fontSize:12, color:'#aaa', display:'block', marginBottom:10 }}>
          Starting capital (USDT) — your virtual paper trading balance
          <input type="number" min="100" step="100" value={capital}
            onChange={e => { setCapital(e.target.value); setSaved(false); }}
            placeholder="e.g. 10000" style={inp} />
        </label>

        <label style={{ fontSize:12, color:'#aaa', display:'block', marginBottom:10 }}>
          Max capital per single trade (USDT)
          <input type="number" min="1" step="10" value={maxPos}
            onChange={e => { setMaxPos(e.target.value); setSaved(false); }}
            placeholder="e.g. 200  (hard cap per trade)" style={inp} />
        </label>

        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:8, marginBottom:12 }}>
          <label style={{ fontSize:12, color:'#aaa' }}>
            Min open trades
            <input type="number" min="1" value={minOpen}
              onChange={e => { setMinOpen(e.target.value); setSaved(false); }}
              placeholder="e.g. 3" style={inp} />
          </label>
          <label style={{ fontSize:12, color:'#aaa' }}>
            Max open trades
            <input type="number" min="1" value={maxOpen}
              onChange={e => { setMaxOpen(e.target.value); setSaved(false); }}
              placeholder="e.g. 15" style={inp} />
          </label>
        </div>

        {saveErr && <div style={{ color:'#ff4444', fontSize:11, marginBottom:8 }}>{saveErr}</div>}

        <button onClick={saveSettings} disabled={!allFilled}
          style={{ width:'100%', padding:9, background: allFilled ? '#0055cc' : '#1a1a3a',
            color: allFilled ? '#fff' : '#555', border:'none', borderRadius:4,
            cursor: allFilled ? 'pointer' : 'not-allowed', fontWeight:'bold', fontSize:13 }}>
          Save Settings
        </button>
      </div>

      {/* Virtual balance display (once set) */}
      {status.virtual_balance && (
        <div style={{ fontSize:12, color:'#888', marginBottom:10, textAlign:'center' }}>
          Virtual balance:&nbsp;
          <span style={{ color:'#00d4ff', fontWeight:'bold' }}>
            ${parseFloat(status.virtual_balance).toLocaleString()} USDT
          </span>
        </div>
      )}

      {/* Start / Stop buttons */}
      <div style={{ display:'flex', gap:8, marginBottom:10 }}>
        <button onClick={startBot} disabled={!canStart || starting}
          style={{ flex:1, padding:11, fontWeight:'bold', fontSize:14, border:'none', borderRadius:4,
            background: canStart ? '#00aa44' : '#1a2a1a',
            color:      canStart ? '#fff'   : '#555',
            cursor:     canStart ? 'pointer' : 'not-allowed' }}>
          {starting ? 'Starting…' : canStart ? '▶ Start Trading' : saved ? 'Already Running' : 'Save Settings First'}
        </button>
        <button onClick={stopBot} disabled={!status.running}
          style={{ flex:1, padding:11, fontWeight:'bold', fontSize:14, border:'none', borderRadius:4,
            background: status.running ? '#880000' : '#2a1a1a',
            color:      status.running ? '#fff'   : '#555',
            cursor:     status.running ? 'pointer' : 'not-allowed' }}>
          ⏹ Stop
        </button>
      </div>

      {/* Live mode toggle */}
      <button onClick={() => setBotMode('live')} disabled={!liveUnlocked}
        style={{ width:'100%', padding:8, fontSize:12, border:'none', borderRadius:4,
          background: liveUnlocked ? '#cc6600' : '#2a2a2a',
          color:      liveUnlocked ? '#fff'   : '#555',
          cursor:     liveUnlocked ? 'pointer' : 'not-allowed' }}>
        {liveUnlocked ? '🔴 Switch to LIVE Trading' : `Live locked — need 2000 trades + Stage 4`}
      </button>
    </div>
  );
};

export default ControlPanel;
