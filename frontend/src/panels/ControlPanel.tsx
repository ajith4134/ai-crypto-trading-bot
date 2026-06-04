// AI-03: Panel 1 — Control Panel (cont. 31: + Start New Session)
import React, { useEffect, useRef, useState } from 'react';
import { getBotStatus, setBotMode, getToken, startSession, getCurrentSession, clearSession, switchBotMode, getModeChangeStatus, getFapiRecovery } from '../api';
import axios from 'axios';
import { card, title, badge } from './shared';

const authApi = axios.create({ baseURL: '' });
authApi.interceptors.request.use(cfg => {
  const tok = getToken();
  if (tok) cfg.headers['Authorization'] = `Bearer ${tok}`;
  return cfg;
});

const inp: React.CSSProperties = {
  background: '#0d0d1a', color: '#fff', border: '1px solid #444',
  borderRadius: 4, padding: '5px 8px', width: '100%',
  boxSizing: 'border-box', fontSize: 13, marginTop: 4,
};


// fapi.binance.com IP ban monitor — shown above the live trading button.
const FapiStatus: React.FC = () => {
  const [data, setData]     = useState<any>(null);
  const [loading, setLoading] = useState(true);

  const refresh = () => {
    getFapiRecovery()
      .then(d => { setData(d); setLoading(false); })
      .catch(() => setLoading(false));
  };

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30000);
    return () => clearInterval(t);
  }, []);

  if (loading && !data) return null;
  if (!data) return null;

  const now    = Date.now() / 1000;
  const status = data.status as string;
  const isBanned = status === 'banned';
  const isOk     = status === 'ok';

  const bannedMin = data.banned_for_seconds != null
    ? Math.round(data.banned_for_seconds / 60) : null;
  const checkedAgoSec = data.last_checked_ts
    ? Math.round(now - data.last_checked_ts) : null;
  const recoveredMin  = (isOk && data.last_ok_ts && data.first_banned_ts)
    ? Math.round((data.last_ok_ts - data.first_banned_ts) / 60) : null;

  const bg     = isBanned ? '#1a0000' : isOk ? '#001a00' : '#0d0d1a';
  const border = isBanned ? '#aa2222' : isOk ? '#008844' : '#2a2a4a';
  const icon   = isBanned ? '🚫' : isOk ? '✅' : '⟳';
  const headColor = isBanned ? '#ff6666' : isOk ? '#00ff88' : '#aaa';
  const head   = isBanned
    ? 'fapi.binance.com BANNED — live blocked'
    : isOk ? 'fapi.binance.com reachable — live ready'
    : 'fapi.binance.com — checking…';

  return (
    <div style={{ padding: '6px 8px', background: bg, border: `1px solid ${border}`,
                  borderRadius: 4, marginBottom: 6, fontSize: 11 }}>
      <span style={{ color: headColor, fontWeight: 'bold' }}>{icon} {head}</span>
      {isBanned && bannedMin != null && (
        <span style={{ color: '#888', marginLeft: 6 }}>banned {bannedMin}m ago</span>
      )}
      {isOk && recoveredMin != null && (
        <span style={{ color: '#666', marginLeft: 6 }}>recovered after {recoveredMin}m ban</span>
      )}
      {checkedAgoSec != null && (
        <span style={{ color: '#444', marginLeft: 6, fontSize: 10 }}>
          · checked {checkedAgoSec < 60 ? `${checkedAgoSec}s` : `${Math.round(checkedAgoSec/60)}m`} ago
        </span>
      )}
    </div>
  );
};


// cont. 47 — Full paper↔live mode switcher.
// Calls /bot/mode_switch which stops the bot, closes any open positions,
// rewrites .env, restarts brain + celery + data_feed via watchdog.
// Bidirectional: paper→live AND live→paper.
const ModeSwitchButton: React.FC<{
  currentMode: 'live' | 'paper';
  liveUnlocked: boolean;
  maxPos?: number;
  startingCapital?: number;
  leverage?: number;
}> = ({ currentMode, liveUnlocked, maxPos, startingCapital, leverage }) => {
  const [phase, setPhase] = useState<'idle' | 'confirming' | 'pending' | 'complete' | 'failed'>('idle');
  const [errMsg, setErrMsg] = useState('');
  const [statusDetail, setStatusDetail] = useState<any>(null);
  const [liveCapital, setLiveCapital] = useState(String(startingCapital ?? 50));
  const [liveMaxPos,  setLiveMaxPos]  = useState(String(maxPos          ?? 10));

  const goingTo: 'live' | 'paper' = currentMode === 'live' ? 'paper' : 'live';
  const disabled = goingTo === 'live' && !liveUnlocked;

  const click = async () => {
    if (phase === 'idle') {
      setPhase('confirming');
      return;
    }
    if (phase === 'confirming') {
      setPhase('pending');
      setErrMsg('');
      try {
        const payload: any = {
          target: goingTo,
          confirm: true,
          close_open_trades: true,
        };
        if (goingTo === 'live') {
          payload.max_position_usdt     = parseFloat(liveMaxPos);
          payload.starting_capital_usdt = parseFloat(liveCapital);
          payload.leverage              = leverage ?? 5;
        }
        const r = await switchBotMode(payload);
        setStatusDetail(r);
        // Poll status until complete or failed
        const start = Date.now();
        const poll = async () => {
          if (Date.now() - start > 120000) {  // 2 min timeout
            setPhase('failed');
            setErrMsg('Timeout waiting for mode change (>120s)');
            return;
          }
          try {
            const s = await getModeChangeStatus();
            setStatusDetail(s);
            if (s.status === 'complete') {
              setPhase('complete');
              setTimeout(() => setPhase('idle'), 5000);
            } else if (s.status === 'failed') {
              setPhase('failed');
              setErrMsg(JSON.stringify(s.result || s));
            } else {
              setTimeout(poll, 3000);
            }
          } catch (e: any) {
            setTimeout(poll, 3000);
          }
        };
        setTimeout(poll, 3000);
      } catch (e: any) {
        setPhase('failed');
        setErrMsg(e?.response?.data?.detail || String(e));
      }
    }
  };

  const bg =
    phase === 'pending'   ? '#666'   :
    phase === 'failed'    ? '#660000':
    phase === 'complete'  ? '#006633':
    disabled              ? '#2a2a2a':
    goingTo === 'live'    ? '#cc6600':
                            '#0066aa';
  const label =
    phase === 'pending'   ? '⏳ Switching… (watchdog rewriting .env + restarting brain)' :
    phase === 'failed'    ? `❌ Failed — ${errMsg.slice(0, 80)}` :
    phase === 'complete'  ? `✅ Switched to ${goingTo.toUpperCase()}` :
    disabled              ? `Live locked — need 2000 trades + Stage 4` :
    goingTo === 'live'    ? '🔴 Switch to LIVE Trading' :
                            '⬅ Switch to PAPER Trading';

  return (
    <div>
      <button onClick={click} disabled={disabled || phase === 'pending'}
        style={{ width:'100%', padding:8, fontSize:12, border:'none', borderRadius:4,
          background: bg, color: '#fff',
          cursor: (disabled || phase === 'pending') ? 'not-allowed' : 'pointer' }}>
        {phase === 'confirming'
          ? `⚠ Confirm: Switch to ${goingTo.toUpperCase()}? (closes all open trades)`
          : label}
      </button>
      {phase === 'confirming' && goingTo === 'live' && (
        <div style={{ marginTop:6, padding:8, background:'#1a0d0d',
                      border:'1px solid #cc6600', borderRadius:4, fontSize:11 }}>
          <div style={{ marginBottom:4, color:'#ffaa00' }}>
            Set live trading envelope:
          </div>
          <label>Starting capital (USDT)
            <input style={inp} value={liveCapital}
                   onChange={e => setLiveCapital(e.target.value)} />
          </label>
          <label>Max position per trade (USDT)
            <input style={inp} value={liveMaxPos}
                   onChange={e => setLiveMaxPos(e.target.value)} />
          </label>
          <div style={{ marginTop:6, fontSize:10, color:'#aaa' }}>
            Real Binance MAINNET orders. Click button again to commit.
          </div>
        </div>
      )}
      {phase === 'confirming' && goingTo === 'paper' && (
        <div style={{ marginTop:6, padding:6, background:'#0d0d1a',
                      border:'1px solid #0066aa', borderRadius:4, fontSize:11, color:'#aaa' }}>
          Closes any open Binance position, then restarts brain in paper mode.
          Click again to commit.
        </div>
      )}
      {(phase === 'pending' || phase === 'complete') && statusDetail && (
        <div style={{ marginTop:6, padding:6, background:'#0d0d1a',
                      borderRadius:4, fontSize:10, color:'#aaa' }}>
          {JSON.stringify(statusDetail, null, 2).slice(0, 400)}
        </div>
      )}
    </div>
  );
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
  // Track whether user has unsaved edits — useRef so setInterval closure always reads current value
  const dirtyRef = useRef(false);

  const refresh = () => getBotStatus().then((s: any) => {
    setStatus(s);
    // Only overwrite form fields if user has NOT started editing them
    // This prevents the 5-second poll from reverting unsaved user input
    if (!dirtyRef.current) {
      if (s.min_open_trades)       setMinOpen(String(s.min_open_trades));
      if (s.max_open_trades)       setMaxOpen(String(s.max_open_trades));
      if (s.max_position_usdt)     setMaxPos(String(s.max_position_usdt));
      if (s.starting_capital_usdt) setCapital(String(s.starting_capital_usdt));
    }
    setSaved(!!s.settings_configured);
  }).catch(() => {});

  // Helper: mark form as dirty when user edits any field
  const markDirty = () => { dirtyRef.current = true; setSaved(false); };

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
      dirtyRef.current = false;  // clear dirty flag — refresh can now sync fields again
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

  // cont. 31 — session state
  const [session, setSession] = useState<any>({ active: false });
  const refreshSession = () => getCurrentSession().then(setSession).catch(() => {});
  useEffect(() => {
    refreshSession();
    const t = setInterval(refreshSession, 10000);
    return () => clearInterval(t);
  }, []);

  const [startingSession, setStartingSession] = useState(false);
  const handleStartNewSession = async () => {
    const resetBal = window.confirm(
      'Start New Session?\n\n' +
      'OK = also RESET virtual balance to starting capital (fresh start)\n' +
      'Cancel below = keep current balance, just mark new session\n\n' +
      'Trades will not be deleted — dashboard will filter to show only ' +
      'trades from this session onward.'
    );
    if (!window.confirm(
      resetBal
        ? `Confirmed: Start new session AND reset virtual balance to $${capital || '?'}.`
        : 'Confirmed: Start new session (balance unchanged).'
    )) return;
    setStartingSession(true);
    try {
      await startSession({ reset_virtual_balance: resetBal });
      refreshSession();
      refresh();
    } catch (e: any) {
      alert(e.response?.data?.detail || 'Start session failed');
    }
    setStartingSession(false);
  };

  const handleClearSession = async () => {
    if (!window.confirm('Clear session marker? Dashboard will revert to ALL-TIME view.')) return;
    try {
      await clearSession();
      refreshSession();
    } catch (e: any) {
      alert(e.response?.data?.detail || 'Clear session failed');
    }
  };

  const [closingAll, setClosingAll] = useState(false);
  const closeAllTrades = async () => {
    const openCount = status.open_trades_count ?? '';
    if (!window.confirm(
      `Force-close ALL open trades at current mark price?\n\n` +
      `Open trades: ${openCount}\n` +
      `This is irreversible. Capital + PnL will be returned to virtual balance, ` +
      `then you can reset settings and Start fresh.`
    )) return;
    setClosingAll(true);
    try {
      const res = await authApi.post('/bot/close_all_trades', { confirm: true });
      const r = res.data || {};
      alert(`Closed ${r.closed}/${r.requested}` + (r.failed ? ` (${r.failed} failed)` : ''));
      refresh();
    } catch (e: any) {
      alert(e.response?.data?.detail || 'Close-all failed');
    }
    setClosingAll(false);
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
            onChange={e => { setCapital(e.target.value); markDirty(); }}
            placeholder="e.g. 10000" style={inp} />
        </label>

        <label style={{ fontSize:12, color:'#aaa', display:'block', marginBottom:10 }}>
          Max capital per single trade (USDT)
          <input type="number" min="1" step="10" value={maxPos}
            onChange={e => { setMaxPos(e.target.value); markDirty(); }}
            placeholder="e.g. 200  (hard cap per trade)" style={inp} />
        </label>

        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:8, marginBottom:12 }}>
          <label style={{ fontSize:12, color:'#aaa' }}>
            Min open trades
            <input type="number" min="1" value={minOpen}
              onChange={e => { setMinOpen(e.target.value); markDirty(); }}
              placeholder="e.g. 3" style={inp} />
          </label>
          <label style={{ fontSize:12, color:'#aaa' }}>
            Max open trades
            <input type="number" min="1" value={maxOpen}
              onChange={e => { setMaxOpen(e.target.value); markDirty(); }}
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

      {/* fapi.binance.com ban monitor — shows recovery status above live button */}
      <FapiStatus />

      {/* Live ↔ Paper mode switcher (cont. 47 — real switch, not cosmetic).
          Calls /bot/mode_switch which stops bot, closes open trades, rewrites
          .env, restarts brain + celery + data_feed via watchdog. */}
      <ModeSwitchButton
        currentMode={(status.mode || 'paper') as 'live' | 'paper'}
        liveUnlocked={liveUnlocked}
        maxPos={status.max_position_usdt}
        startingCapital={status.starting_capital_usdt}
        leverage={status.leverage}
      />

      {/* Force close all trades — lets owner reset state before restarting */}
      <button onClick={closeAllTrades} disabled={closingAll}
        style={{ width:'100%', padding:8, fontSize:12, border:'1px solid #ff4444',
          borderRadius:4, marginTop:8, background:'#1a0d0d',
          color: closingAll ? '#555' : '#ff7777',
          cursor: closingAll ? 'not-allowed' : 'pointer' }}>
        {closingAll ? 'Closing all open trades…' : '⚠ Force-close all open trades (at mark price)'}
      </button>

      {/* cont. 31 — Start New Session */}
      <div style={{
        marginTop: 14, padding: 10, borderRadius: 6,
        background: '#0d0d1a', border: '1px solid #2a4a6a',
      }}>
        <div style={{ fontSize: 11, color: '#888', marginBottom: 6 }}>
          {session.active
            ? <>📊 <b style={{color:'#00d4ff'}}>SESSION ACTIVE</b> — started {new Date(session.session_start_ts * 1000).toLocaleString()}
              {session.label ? <> · "{session.label}"</> : null}
              <div style={{ marginTop: 4, color: '#aaa' }}>
                {session.closed_in_session} closed · realised{' '}
                <span style={{ color: (session.realised_pnl_usdt || 0) >= 0 ? '#00ff88' : '#ff4444', fontWeight:'bold' }}>
                  ${(session.realised_pnl_usdt || 0).toFixed(2)}
                </span>
                {' '}({session.pnl_pct_of_start != null ? `${session.pnl_pct_of_start >= 0 ? '+' : ''}${session.pnl_pct_of_start}%` : '—'})
                {' · '}wr {session.win_rate_pct != null ? `${session.win_rate_pct}%` : '—'}
              </div>
            </>
            : <>📊 <b>NO ACTIVE SESSION</b> — dashboard shows all-time data</>}
        </div>
        <div style={{ display: 'flex', gap: 6 }}>
          <button onClick={handleStartNewSession} disabled={startingSession}
            style={{ flex: 2, padding: 8, fontSize: 12, fontWeight: 'bold',
              border: '1px solid #00d4ff', borderRadius: 4,
              background: startingSession ? '#0a1a2a' : '#003355',
              color: startingSession ? '#555' : '#fff',
              cursor: startingSession ? 'wait' : 'pointer' }}>
            {startingSession ? 'Starting…' : '▶ Start New Session'}
          </button>
          {session.active && (
            <button onClick={handleClearSession}
              style={{ flex: 1, padding: 8, fontSize: 12, border: '1px solid #555',
                borderRadius: 4, background: 'transparent', color: '#aaa',
                cursor: 'pointer' }}>
              Clear
            </button>
          )}
        </div>
      </div>
    </div>
  );
};

export default ControlPanel;
