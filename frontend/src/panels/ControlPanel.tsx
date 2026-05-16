// AI-03: Panel 1 — Control Panel
// Start Trading requires: min_open_trades, max_open_trades, max_position_usdt to be set first
import React, { useEffect, useState } from 'react';
import { getBotStatus, setBotMode } from '../api';
import axios from 'axios';
import { card, title, badge } from './shared';

const api = axios.create({ baseURL: '' });

const ControlPanel: React.FC = () => {
  const [status, setStatus] = useState<any>({});
  const [minOpen, setMinOpen] = useState('');
  const [maxOpen, setMaxOpen] = useState('');
  const [maxPosUsdt, setMaxPosUsdt] = useState('');
  const [saved, setSaved] = useState(false);
  const [saveErr, setSaveErr] = useState('');
  const [starting, setStarting] = useState(false);

  const refresh = () => getBotStatus().then(s => {
    setStatus(s);
    if (s.min_open_trades) setMinOpen(String(s.min_open_trades));
    if (s.max_open_trades) setMaxOpen(String(s.max_open_trades));
    if (s.max_position_usdt) setMaxPosUsdt(String(s.max_position_usdt));
    setSaved(!!s.settings_configured);
  }).catch(() => {});

  useEffect(() => { refresh(); const t = setInterval(refresh, 5000); return () => clearInterval(t); }, []);

  const paperClosed = status.paper_closed || 0;
  const liveUnlocked = paperClosed >= 2000 && status.stage === 4;
  const stageLabel = ['','Baby','Learning','Developing','Expert'][status.stage || 1] || 'Baby';
  const canStart = saved && !status.running;

  const saveSettings = async () => {
    setSaveErr('');
    const token = localStorage.getItem('token') || '';
    try {
      await api.put('/bot/settings',
        { min_open_trades: Number(minOpen), max_open_trades: Number(maxOpen), max_position_usdt: Number(maxPosUsdt) },
        { headers: { Authorization: `Bearer ${token}` } }
      );
      setSaved(true);
      refresh();
    } catch (e: any) {
      setSaveErr(e.response?.data?.detail || 'Save failed');
    }
  };

  const startBot = async () => {
    setStarting(true);
    const token = localStorage.getItem('token') || '';
    try {
      await api.post('/bot/start', {}, { headers: { Authorization: `Bearer ${token}` } });
      refresh();
    } catch (e: any) {
      alert(e.response?.data?.detail || 'Start failed');
    }
    setStarting(false);
  };

  const stopBot = async () => {
    const token = localStorage.getItem('token') || '';
    await api.post('/bot/stop', {}, { headers: { Authorization: `Bearer ${token}` } });
    refresh();
  };

  const inp: React.CSSProperties = {
    background: '#0d0d1a', color: '#fff', border: '1px solid #333',
    borderRadius: 4, padding: '4px 8px', width: '100%', boxSizing: 'border-box', fontSize: 13,
  };

  return (
    <div style={card}>
      <h3 style={title}>Control Panel</h3>

      {/* Status badges */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap' }}>
        <span style={badge(status.running ? '#00ff88' : '#ff4444')}>{status.running ? 'RUNNING' : 'STOPPED'}</span>
        <span style={badge('#0088ff')}>{(status.mode || 'paper').toUpperCase()}</span>
        <span style={badge('#aa88ff')}>Stage {status.stage || 1} — {stageLabel}</span>
      </div>

      {/* Paper trade progress */}
      <div style={{ marginBottom: 4, fontSize: 12, color: '#888' }}>
        Paper Trades: <span style={{ color: paperClosed >= 2000 ? '#00ff88' : '#ffaa00' }}>{paperClosed} / 2000</span>
      </div>
      <div style={{ background: '#0d0d1a', borderRadius: 4, height: 6, marginBottom: 16 }}>
        <div style={{ background: '#00d4ff', height: 6, borderRadius: 4, width: `${Math.min(100, (paperClosed / 2000) * 100)}%` }} />
      </div>

      {/* Required settings — must be filled before Start */}
      <div style={{ background: '#0d0d1a', borderRadius: 6, padding: 12, marginBottom: 12, border: saved ? '1px solid #00ff44' : '1px solid #ff8800' }}>
        <div style={{ fontSize: 11, color: saved ? '#00ff88' : '#ffaa00', marginBottom: 10, fontWeight: 'bold' }}>
          {saved ? '✅ Settings saved — Start Trading is enabled' : '⚠️ Set these before pressing Start Trading'}
        </div>

        <label style={{ fontSize: 12, color: '#aaa', display: 'block', marginBottom: 8 }}>
          Min open trades at once
          <input type="number" min="1" value={minOpen} onChange={e => { setMinOpen(e.target.value); setSaved(false); }}
            placeholder="e.g. 3" style={{ ...inp, marginTop: 4 }} />
        </label>

        <label style={{ fontSize: 12, color: '#aaa', display: 'block', marginBottom: 8 }}>
          Max open trades at once
          <input type="number" min="1" value={maxOpen} onChange={e => { setMaxOpen(e.target.value); setSaved(false); }}
            placeholder="e.g. 15" style={{ ...inp, marginTop: 4 }} />
        </label>

        <label style={{ fontSize: 12, color: '#aaa', display: 'block', marginBottom: 10 }}>
          Max capital per trade (USDT)
          <input type="number" min="1" step="10" value={maxPosUsdt} onChange={e => { setMaxPosUsdt(e.target.value); setSaved(false); }}
            placeholder="e.g. 100 (absolute $ limit per trade)" style={{ ...inp, marginTop: 4 }} />
        </label>

        {saveErr && <div style={{ color: '#ff4444', fontSize: 11, marginBottom: 8 }}>{saveErr}</div>}

        <button onClick={saveSettings}
          disabled={!minOpen || !maxOpen || !maxPosUsdt}
          style={{ width: '100%', padding: 8, background: '#0088ff', color: '#fff', border: 'none', borderRadius: 4, cursor: 'pointer', fontWeight: 'bold', fontSize: 13 }}>
          Save Settings
        </button>
      </div>

      {/* Start / Stop */}
      <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
        <button onClick={startBot} disabled={!canStart || starting}
          style={{ flex: 1, padding: 10, background: canStart ? '#00aa44' : '#1a3a1a', color: canStart ? '#fff' : '#666', border: 'none', borderRadius: 4, cursor: canStart ? 'pointer' : 'not-allowed', fontWeight: 'bold', fontSize: 13 }}>
          {starting ? 'Starting...' : canStart ? '▶ Start Trading' : saved ? 'Already Running' : 'Save Settings First'}
        </button>
        <button onClick={stopBot} disabled={!status.running}
          style={{ flex: 1, padding: 10, background: status.running ? '#aa2200' : '#2a1a1a', color: status.running ? '#fff' : '#666', border: 'none', borderRadius: 4, cursor: status.running ? 'pointer' : 'not-allowed', fontWeight: 'bold', fontSize: 13 }}>
          ⏹ Stop Trading
        </button>
      </div>

      {/* Live toggle */}
      <button onClick={() => setBotMode('live')} disabled={!liveUnlocked}
        style={{ width: '100%', padding: 8, background: liveUnlocked ? '#ff8800' : '#333', color: liveUnlocked ? '#000' : '#666', border: 'none', borderRadius: 4, cursor: liveUnlocked ? 'pointer' : 'not-allowed', fontSize: 12 }}>
        {liveUnlocked ? '🔴 Switch to LIVE Trading' : `Live locked (need 2000 trades + Stage 4)`}
      </button>
    </div>
  );
};

export default ControlPanel;
