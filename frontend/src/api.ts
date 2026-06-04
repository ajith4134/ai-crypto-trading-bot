// AI-02: JWT auth and all REST API calls
import axios from 'axios';

const BASE = '';  // served by nginx on same origin

export const setToken = (t: string) => { sessionStorage.setItem('token', t); };
export const getToken = () => sessionStorage.getItem('token');
export const clearToken = () => { sessionStorage.removeItem('token'); };

const api = axios.create({ baseURL: BASE });
api.interceptors.request.use(cfg => {
  const tok = getToken(); if (tok) cfg.headers['Authorization'] = `Bearer ${tok}`;
  return cfg;
});
api.interceptors.response.use(r => r, err => {
  if (err.response?.status === 401) { clearToken(); window.location.href = '/login'; }
  return Promise.reject(err);
});

export const login = (username: string, password: string) =>
  api.post('/auth/login', { username, password }).then(r => r.data.access_token);

export const getBotStatus      = () => api.get('/bot/status').then(r => r.data);
export const setBotMode        = (mode: string) => api.post('/bot/mode', null, { params: { mode } });
// cont. 47 — full paper↔live transition (restarts brain + celery + data_feed)
export const switchBotMode = (payload: {
  target: 'live' | 'paper';
  max_position_usdt?: number;
  starting_capital_usdt?: number;
  leverage?: number;
  close_open_trades?: boolean;
  confirm: boolean;
}) => api.post('/bot/mode_switch', payload).then(r => r.data);
export const getModeChangeStatus = () => api.get('/bot/mode_change_status').then(r => r.data);
export const setBotSettings    = (s: object) => api.put('/bot/settings', s);
export const getOpenTrades     = () => api.get('/trades/open').then(r => Array.isArray(r.data) ? r.data : (r.data?.trades || []));
export const getOpenTradesFull  = () => api.get('/trades/open').then(r => r.data);  // includes total_open_pnl
export const getClosedTrades   = (params?: object) => api.get('/trades/closed', { params }).then(r => r.data);
export const getClosedTradesFull = (params?: object) => api.get('/trades/closed', { params }).then(r => r.data);
export const getBrainStatus         = () => api.get('/brain/status').then(r => r.data);
export const getAdvancedBrainStatus = () => api.get('/brain/advanced').then(r => r.data);
export const getAnalytics      = () => api.get('/analytics/metrics').then(r => r.data);
export const getAccountRisk    = () => api.get('/account/risk').then(r => r.data);
export const getFeaturesHealth = () => api.get('/features/health').then(r => r.data);
export const getSystemHealth   = () => api.get('/system/health').then(r => r.data);
export const getStrategies     = () => api.get('/strategies').then(r => r.data);
export const getActivePairs    = () => api.get('/pairs/active').then(r => r.data);
export const getLaunchPad      = () => api.get('/launchpad').then(r => r.data);
export const getWebIntelFeed   = () => api.get('/web_intel/feed').then(r => r.data);
export const exportClosedCSV      = () => api.get('/trades/closed/export', { responseType: 'blob' });
export const getEquityCurve       = () => api.get('/analytics/equity_curve').then(r => r.data);
export const getDirectionWinRate  = () => api.get('/analytics/direction_win_rate').then(r => r.data);
export const getFeatureHealthAll  = () => api.get('/system/feature_health').then(r => r.data);
export const getShadowWinRate     = () => api.get('/signals/shadow_win_rate').then(r => r.data);
export const getRecentSignals     = (limit = 50) => api.get('/signals/recent', { params: { limit } }).then(r => r.data);
export const getSignalAcceptanceRate = (hours = 24) => api.get('/signals/acceptance_rate', { params: { hours } }).then(r => r.data);
export const getMissedOpportunities  = (limit = 10) => api.get('/signals/missed_opportunities', { params: { limit } }).then(r => r.data);
export const getLLMProviders      = () => api.get('/llm/providers').then(r => r.data);
export const getHedgeLearnedParams = () => api.get('/hedge/learned_params').then(r => r.data);
export const getMemoryClusters     = () => api.get('/memory/clusters').then(r => r.data);
export const getRecentDecoders     = () => api.get('/decoders/recent').then(r => r.data);
export const getSelfPlayEpisodes   = () => api.get('/self_play/episodes').then(r => r.data);
export const getModelsStatus       = () => api.get('/models').then(r => r.data);
export const getFapiRecovery       = () => api.get('/system/fapi_recovery').then(r => r.data);
export const startSession          = (opts?: { label?: string; reset_virtual_balance?: boolean }) =>
  api.post('/sessions/start', { label: opts?.label, reset_virtual_balance: !!opts?.reset_virtual_balance }).then(r => r.data);
export const getCurrentSession     = () => api.get('/sessions/current').then(r => r.data);
export const clearSession          = () => api.post('/sessions/clear').then(r => r.data);

export const getSummary = async () => {
  const [openRes, closedRes, balance] = await Promise.all([
    api.get('/trades/open').then(r => r.data),
    api.get('/trades/closed?limit=10').then(r => r.data),  // just need summary, not all trades
    api.get('/bot/status').then(r => r.data),
  ]);
  const openTrades: any[] = openRes?.trades ?? (Array.isArray(openRes) ? openRes : []);
  const summary = closedRes?.summary ?? {};
  const totalUnrealised = openRes?.total_open_pnl ?? openTrades.reduce((s:number, t:any) => s + +(t.net_current_pnl||t.current_pnl_usdt||0), 0);
  return {
    open_count: openTrades.length,
    closed_count: summary.total ?? 0,
    unrealised_pnl: totalUnrealised,
    realised_pnl: summary.total_pnl ?? 0,
    win_rate: summary.win_rate_pct ?? 0,
    total_fees: summary.total_fees ?? 0,
    virtual_balance: +(balance?.virtual_balance || 0),
    // cont. 66 — real Binance balance + mode so SummaryBar can show both
    // the actual account total and the trading budget side-by-side in live.
    real_balance: balance?.real_balance != null ? +balance.real_balance : null,
    mode: balance?.mode || 'paper',
  };
};
