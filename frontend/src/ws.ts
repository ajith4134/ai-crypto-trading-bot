// AI-01: WebSocket client — connects to /ws/dashboard, auto-reconnects
type Handler = (channel: string, data: any) => void;

let _ws: WebSocket | null = null;
let _handlers: Handler[] = [];
let _reconnectDelay = 1000;

export const onMessage = (h: Handler) => { _handlers.push(h); };

export const connect = () => {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  _ws = new WebSocket(`${proto}://${window.location.host}/ws/dashboard`);

  _ws.onopen = () => {
    _reconnectDelay = 1000;
    console.log('[WS] connected');
  };

  _ws.onmessage = (evt) => {
    try {
      if (typeof evt.data !== 'string') return;
      const msg = JSON.parse(evt.data);
      if (!msg || typeof msg !== 'object') return;
      let data = msg.data;
      if (typeof data === 'string') {
        try { data = JSON.parse(data); } catch { /* keep as string */ }
      }
      _handlers.forEach(h => { try { h(msg.channel, data); } catch {} });
    } catch { /* ignore malformed */ }
  };

  _ws.onclose = () => {
    console.log(`[WS] disconnected — reconnecting in ${_reconnectDelay}ms`);
    setTimeout(() => { connect(); }, _reconnectDelay);
    _reconnectDelay = Math.min(_reconnectDelay * 2, 30000);
  };

  _ws.onerror = () => _ws?.close();
};

export const wsConnected = () => _ws?.readyState === WebSocket.OPEN;
