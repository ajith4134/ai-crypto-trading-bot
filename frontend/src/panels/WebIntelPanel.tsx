// AI-13: Panel 12 — Web Intelligence
import React, { useEffect, useState } from 'react';
import { getWebIntelFeed } from '../api';
import { card, title, Table } from './shared';

const WebIntelPanel: React.FC = () => {
  const [feed, setFeed] = useState<any[]>([]);
  useEffect(() => { getWebIntelFeed().then(setFeed).catch(()=>{}); }, []);

  const rows = feed.slice(0,30).map(f => [
    new Date(f.fetched_at).toLocaleTimeString(),
    f.signal_type||'—',
    f.source?.slice(0,20)||'—',
    f.summary?.slice(0,60)||'—',
    f.sentiment||'—',
    `${(+f.confidence||0).toFixed(0)}%`,
    `${(+f.credibility_score||0).toFixed(2)}`,
  ]);

  return (
    <div style={card}>
      <h3 style={title}>Web Intelligence</h3>
      <Table cols={['Time','Type','Source','Summary','Sentiment','Conf','Cred']} rows={rows} maxH={220}/>
    </div>
  );
};
export default WebIntelPanel;
