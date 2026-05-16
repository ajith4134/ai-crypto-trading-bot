import React from 'react';
export const card: React.CSSProperties = {background:'#1a1a2e',borderRadius:8,padding:16,border:'1px solid #2a2a4a'};
export const title: React.CSSProperties = {color:'#00d4ff',marginTop:0,marginBottom:12,fontSize:14,textTransform:'uppercase',letterSpacing:1};
export const badge = (color: string): React.CSSProperties => ({background:color,color:'#000',padding:'2px 8px',borderRadius:4,fontSize:11,fontWeight:'bold'});
export const P: React.FC<{label:string;value:any;color?:string}> = ({label,value,color}) => (
  <div style={{display:'flex',justifyContent:'space-between',marginBottom:6}}>
    <span style={{color:'#888'}}>{label}</span>
    <span style={{color:color||'#e0e0e0'}}>{value??'—'}</span>
  </div>
);
export const Table: React.FC<{cols:string[];rows:any[][];maxH?:number}> = ({cols,rows,maxH=300}) => (
  <div style={{overflowY:'auto',maxHeight:maxH}}>
    <table style={{width:'100%',borderCollapse:'collapse',fontSize:12}}>
      <thead><tr>{cols.map(c=><th key={c} style={{textAlign:'left',padding:'4px 8px',color:'#888',borderBottom:'1px solid #2a2a4a',position:'sticky',top:0,background:'#1a1a2e'}}>{c}</th>)}</tr></thead>
      <tbody>{rows.map((r,i)=><tr key={i} style={{borderBottom:'1px solid #111'}}>{r.map((cell,j)=><td key={j} style={{padding:'4px 8px'}}>{cell}</td>)}</tr>)}</tbody>
    </table>
  </div>
);
