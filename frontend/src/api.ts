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
export const setBotSettings    = (s: object) => api.put('/bot/settings', s);
export const getOpenTrades     = () => api.get('/trades/open').then(r => Array.isArray(r.data) ? r.data : (r.data?.trades || []));
export const getOpenTradesFull  = () => api.get('/trades/open').then(r => r.data);  // includes total_open_pnl
export const getClosedTrades   = (params?: object) => api.get('/trades/closed', { params }).then(r => r.data);
export const getBrainStatus    = () => api.get('/brain/status').then(r => r.data);
export const getAnalytics      = () => api.get('/analytics/metrics').then(r => r.data);
export const getAccountRisk    = () => api.get('/account/risk').then(r => r.data);
export const getFeaturesHealth = () => api.get('/features/health').then(r => r.data);
export const getSystemHealth   = () => api.get('/system/health').then(r => r.data);
export const getStrategies     = () => api.get('/strategies').then(r => r.data);
export const getActivePairs    = () => api.get('/pairs/active').then(r => r.data);
export const getWebIntelFeed   = () => api.get('/web_intel/feed').then(r => r.data);
export const exportClosedCSV   = () => api.get('/trades/closed/export', { responseType: 'blob' });

export const getSummary = async () => {
  const [open, closed, balance] = await Promise.all([
    api.get('/trades/open').then(r => r.data),
    api.get('/trades/closed?limit=500').then(r => r.data),
    api.get('/bot/status').then(r => r.data),
  ]);
  const totalUnrealised = (open as any[]).reduce((s:number, t:any) => s + +(t.current_pnl_usdt||0), 0);
  const totalClosed = (closed as any[]).reduce((s:number, t:any) => s + +(t.net_pnl_usdt||0), 0);
  const wins = (closed as any[]).filter((t:any) => +(t.net_pnl_usdt||0) > 0).length;
  return {
    open_count: (open as any[]).length,
    closed_count: (closed as any[]).length,
    unrealised_pnl: totalUnrealised,
    realised_pnl: totalClosed,
    win_rate: (closed as any[]).length > 0 ? (wins / (closed as any[]).length * 100) : 0,
    virtual_balance: +(balance?.virtual_balance || 0),
  };
};
