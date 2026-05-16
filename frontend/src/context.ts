import React from 'react';
export type WsEvent = { channel: string; data: any };
export const WsContext = React.createContext<WsEvent | null>(null);
