"""Multi-timeframe direction-predictability experiment (cont. 69y).

Question: does a model that sees 5m+15m+30m+1h features JOINTLY predict short-horizon
direction better than a single-TF model? Leak-safe: at decision time t we use only the
LAST COMPLETED bar of each TF strictly before t. Time-based train/test split.
"""
import os, csv, math, sys
sys.path.insert(0, "/app")
import numpy as np

HD = "/app/data/historical"
TFS = ["5m", "15m", "30m", "1h"]
TF_MS = {"5m":300_000, "15m":900_000, "30m":1_800_000, "1h":3_600_000}

def load(pair, tf):
    p = f"{HD}/{pair}/{tf}.csv"
    if not os.path.exists(p): return None
    ts=[];o=[];h=[];l=[];c=[];v=[]
    with open(p) as f:
        rd=csv.reader(f); next(rd,None)
        for r in rd:
            if not r or not r[0].lstrip("-").isdigit(): continue
            t=int(r[0]); t=t//1000 if t>1e14 else t
            try:
                ts.append(t);o.append(float(r[1]));h.append(float(r[2]))
                l.append(float(r[3]));c.append(float(r[4]));v.append(float(r[5]))
            except ValueError: continue
    if len(ts)<300: return None
    idx=np.argsort(ts)
    A=lambda x:np.array(x)[idx]
    return dict(ts=A(ts),o=A(o),h=A(h),l=A(l),c=A(c),v=A(v))

def feats_at(d, i):
    """Features from bars up to and INCLUDING i (i is a completed bar)."""
    if i < 30: return None
    c=d["c"]; h=d["h"]; l=d["l"]; v=d["v"]
    px=c[i]
    if px<=0: return None
    def ret(n): return math.log(px/c[i-n]) if c[i-n]>0 else 0.0
    win=c[i-20:i+1]
    hi=win.max(); lo=win.min()
    pos=(px-lo)/(hi-lo) if hi>lo else 0.5
    rr=np.diff(np.log(np.clip(c[i-20:i+1],1e-12,None)))
    vol=float(rr.std()) if len(rr)>1 else 0.0
    rng=(h[i]-l[i])/px if px>0 else 0.0
    vma=v[i-20:i+1].mean(); vr=v[i]/vma if vma>0 else 1.0
    # RSI(14)
    dif=np.diff(c[i-14:i+1]); up=dif[dif>0].sum(); dn=-dif[dif<0].sum()
    rsi=100.0 - 100.0/(1.0+up/dn) if dn>0 else (100.0 if up>0 else 50.0)
    # short MA slope
    ma5=c[i-4:i+1].mean(); ma20=win.mean()
    slope=(ma5-ma20)/px if px>0 else 0.0
    return [ret(1),ret(3),ret(6),ret(12),pos,vol,rng,vr,rsi/100.0,slope]

NF=10  # features per TF

def build(pairs, base_tf="15m", horizon_bars=2, deadband=0.0015):
    """Decision points = every base_tf bar. X_single = base TF feats; X_multi adds
    the last completed bar feats of every other TF. y = sign of forward return over
    horizon_bars on base TF (NaN within deadband -> dropped)."""
    Xs=[];Xm=[];Y=[];TT=[]
    for pair in pairs:
        D={tf:load(pair,tf) for tf in TFS}
        base=D[base_tf]
        if base is None: continue
        # precompute per-TF ascending ts for searchsorted
        for bi in range(40, len(base["ts"])-horizon_bars):
            t=base["ts"][bi]                       # decision bar close time
            fb=feats_at(base, bi)
            if fb is None: continue
            # forward return on base TF
            fut=base["c"][bi+horizon_bars]; cur=base["c"][bi]
            if cur<=0 or fut<=0: continue
            r=math.log(fut/cur)
            if abs(r)<deadband: continue           # flat deadband -> skip
            y=1 if r>0 else 0
            # decision happens at the CLOSE of base bar bi:
            decision_t = t + TF_MS[base_tf]
            # multi-TF: last bar of each other TF that has fully CLOSED by decision_t.
            # (corpus ts = bar OPEN time, so a bar is closed only when open+interval <= decision_t.
            #  Using open<=t leaked future: a 1h bar open 30m ago closes 30m in the FUTURE.)
            multi=list(fb); ok=True
            for tf in TFS:
                if tf==base_tf: continue
                d2=D[tf]
                if d2 is None: ok=False; break
                j=int(np.searchsorted(d2["ts"], decision_t, side="right"))-1
                while j>=40 and d2["ts"][j] + TF_MS[tf] > decision_t:
                    j-=1                                   # walk back to a CLOSED bar
                if j<40: ok=False; break
                f2=feats_at(d2, j)
                if f2 is None: ok=False; break
                multi+=f2
            if not ok: continue
            Xs.append(fb); Xm.append(multi); Y.append(y); TT.append(t)
    return np.array(Xs),np.array(Xm),np.array(Y),np.array(TT)

def auc(yt, ys):
    # rank-based AUC
    order=np.argsort(ys); r=np.empty(len(ys)); r[order]=np.arange(1,len(ys)+1)
    pos=yt==1; npos=pos.sum(); nneg=(~pos).sum()
    if npos==0 or nneg==0: return float("nan")
    return (r[pos].sum()-npos*(npos+1)/2)/(npos*nneg)

def fit_eval(X, y, tt):
    # time split 70/30
    order=np.argsort(tt); X=X[order]; y=y[order]
    n=len(y); k=int(n*0.7)
    Xtr,ytr,Xte,yte=X[:k],y[:k],X[k:],y[k:]
    try:
        import xgboost as xgb
        m=xgb.XGBClassifier(n_estimators=200,max_depth=4,learning_rate=0.05,
                            subsample=0.8,colsample_bytree=0.8,eval_metric="logloss",
                            n_jobs=4)
        m.fit(Xtr,ytr)
        p=m.predict_proba(Xte)[:,1]
    except Exception as e:
        # fallback: logistic via numpy (standardize + GD)
        mu=Xtr.mean(0); sd=Xtr.std(0)+1e-9
        Xtr2=(Xtr-mu)/sd; Xte2=(Xte-mu)/sd
        w=np.zeros(Xtr2.shape[1]); b=0.0; lr=0.1
        for _ in range(300):
            z=Xtr2@w+b; pr=1/(1+np.exp(-z)); g=pr-ytr
            w-=lr*(Xtr2.T@g/len(ytr)+1e-3*w); b-=lr*g.mean()
        p=1/(1+np.exp(-(Xte2@w+b)))
    return auc(yte,p), len(ytr), len(yte), yte.mean()

if __name__=="__main__":
    # liquid pairs with full TF coverage
    allp=[d for d in os.listdir(HD) if os.path.isdir(f"{HD}/{d}") and d.endswith("USDT")]
    pairs=[p for p in allp if all(os.path.exists(f"{HD}/{p}/{tf}.csv") for tf in TFS)][:60]
    print(f"pairs used: {len(pairs)}", flush=True)
    for base_tf,H,lbl in [("5m",3,"15m-ahead"),("15m",2,"30m-ahead"),("15m",4,"60m-ahead"),("30m",2,"60m-ahead")]:
        Xs,Xm,Y,TT=build(pairs, base_tf=base_tf, horizon_bars=H)
        if len(Y)<500:
            print(f"{base_tf} H={H} ({lbl}): too few samples ({len(Y)})", flush=True); continue
        a_s,ntr,nte,base=fit_eval(Xs,Y,TT)
        a_m,_,_,_=fit_eval(Xm,Y,TT)
        print(f"{base_tf:3} H={H} ({lbl:10}) N={len(Y):6} up%={Y.mean()*100:4.1f} | "
              f"SINGLE-TF auc={a_s:.4f} | MULTI-TF auc={a_m:.4f} | lift={a_m-a_s:+.4f}", flush=True)
