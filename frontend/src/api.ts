// AI-02: JWT auth and all REST API calls
import axios from 'axios';

const BASE = '';  // served by nginx on same origin

let _token: string | null = null;

export const setToken = (t: string) => { _token = t; };
export const getToken = () => _token;
export const clearToken = () => { _token = null; };

const api = axios.create({ baseURL: BASE });
api.interceptors.request.use(cfg => {
  if (_token) cfg.headers['Authorization'] = `Bearer ${_token}`;
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
export const getOpenTrades     = () => api.get('/trades/open').then(r => r.data);
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
