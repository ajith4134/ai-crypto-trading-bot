"""Event-driven SL/TP/activation/trail backtester over 1m corpus candles.

Replays each closed trade tick-by-1m-candle under a candidate capital-ladder policy
and reports expectancy (% of capital) vs the actual realized outcome.

Policy (all thresholds in % of CAPITAL, mirroring risk/manager.py capital-ladder):
  S   initial stop distance (e.g. 15 => -15% cap)
  A   activation: arm trail when peak%cap >= A
  l0/l1/l2  lock fraction of peak (pre-TP1 / post-TP1 / post-TP2)
  T1/T2  take-profit rungs (%cap); f1/f2 = fraction of position closed at each
Price for x%cap:  P = entry * (1 + sign*(x/100)/lev)
"""
import os, csv, sys
sys.path.insert(0, "/app")
from db import db_conn

HD = "/app/data/historical"
_MS_DAY = 86_400_000
NONSL_REASONS = {  # exits we KEEP as fallback (not replacing these)
    "mtf_15m_reversal_confirmed", "llm_council_exit", "dead_trade_time_exit",
    "dead_trade_force_close_max_age", "filtered_obi_severe_flip",
    "manual", "manual_close_all",
}

_cc = {}
_ts_cc = {}
def load(pair):
    if pair in _cc: return _cc[pair]
    p = f"{HD}/{pair}/1m.csv"; out = None
    if os.path.exists(p):
        out = []
        with open(p) as f:
            rd = csv.reader(f); next(rd, None)
            for r in rd:
                if not r or not r[0].lstrip("-").isdigit(): continue
                t = int(r[0]); t = t//1000 if t > 1e14 else t
                try: out.append((t, float(r[1]), float(r[2]), float(r[3]), float(r[4])))
                except ValueError: continue
        out.sort()
        if not out: out = None
    _cc[pair] = out; return out

def px(entry, lev, sign, xcap):
    return entry * (1.0 + sign*(xcap/100.0)/lev)

import bisect
def make_window(tr, max_hold_d=3):
    """Precompute the entry->horizon candle slice ONCE per trade (reused across policies)."""
    cs0 = load(tr["pair"])
    if not cs0: return None
    e_ms = tr["e_ms"]; horizon = min(e_ms + max_hold_d*_MS_DAY, cs0[-1][0])
    ts = _ts_cc.get(tr["pair"])
    if ts is None:
        ts = [c[0] for c in cs0]; _ts_cc[tr["pair"]] = ts
    lo = bisect.bisect_left(ts, e_ms); hi = bisect.bisect_right(ts, horizon)
    cs = cs0[lo:hi]
    if len(cs) < 2: return None
    if cs[0][0] > e_ms + 30*60*1000: return None        # not covered at start
    c0 = cs[0][4]
    if c0 <= 0 or abs(c0-tr["entry"])/tr["entry"] > 0.20: return None  # scale/pair mismatch
    return cs

def simulate(tr, pol):
    """Return final %cap under policy, or None if uncoverable. Uses precomputed tr['win']."""
    cs = tr.get("win")
    if cs is None: return None
    entry, lev, sign, cap = tr["entry"], tr["lev"], tr["sign"], tr["cap"]
    e_ms = tr["e_ms"]

    S, A = pol["S"], pol["A"]
    sl = px(entry, lev, sign, -S)
    peak = 0.0; armed = False
    pos = 1.0; realized = 0.0
    tp1 = px(entry, lev, sign, pol["T1"]); tp2 = px(entry, lev, sign, pol["T2"])
    tp1_done = tp2_done = False
    other = tr["other"]   # (ts_ms, net_pct) for non-SL actual exit, else None

    def leg(P, phi):
        return phi*lev*(P-entry)/entry*sign*100.0

    for (t, o, h, l, c) in cs:
        # non-SL fallback exit (reversal/time/manual): close remainder at actual outcome rate
        if other and t >= other[0]:
            realized += pos*other[1]; return realized
        fav = h if sign > 0 else l
        adv = l if sign > 0 else h
        fav_cap = lev*(fav-entry)/entry*sign*100.0
        if fav_cap > peak: peak = fav_cap
        if not armed and peak >= A: armed = True
        if armed:
            lam = pol["l2"] if tp2_done else (pol["l1"] if tp1_done else pol["l0"])
            lock = px(entry, lev, sign, lam*peak)
            sl = max(sl, lock) if sign > 0 else min(sl, lock)
        # gap-through stop on open
        opx = o
        gap_sl = (opx <= sl) if sign > 0 else (opx >= sl)
        if gap_sl:
            realized += leg(opx, pos); return realized
        # SL first (pessimistic)
        hit_sl = (adv <= sl) if sign > 0 else (adv >= sl)
        if hit_sl:
            realized += leg(sl, pos); return realized
        # TP1 partial
        if not tp1_done:
            hit = (h >= tp1) if sign > 0 else (l <= tp1)
            if hit:
                realized += leg(tp1, pos*pol["f1"]); pos *= (1-pol["f1"]); tp1_done = True
        # TP2 partial
        if tp1_done and not tp2_done:
            hit = (h >= tp2) if sign > 0 else (l <= tp2)
            if hit:
                realized += leg(tp2, pos*pol["f2"]); pos *= (1-pol["f2"]); tp2_done = True
        if pos <= 1e-9: return realized
    # ran out of horizon: close remainder at last close
    realized += leg(cs[-1][4], pos)
    return max(realized, -100.0)   # liquidation floor: can't lose >100% of margin

def load_trades():
    with db_conn() as c:
        cur = c.cursor()
        cur.execute("""SELECT pair,direction,entry_price,average_entry,capital_usdt,leverage,
                              net_pnl_usdt,exit_reason,
                              EXTRACT(EPOCH FROM entry_time)*1000, EXTRACT(EPOCH FROM exit_time)*1000
                       FROM trades WHERE status='closed' AND capital_usdt>0
                         AND net_pnl_usdt IS NOT NULL AND entry_time IS NOT NULL AND exit_time IS NOT NULL""")
        T = []
        for pair,dirn,ep,ae,cap,lev,net,er,ems,xms in cur.fetchall():
            cap=float(cap); lev=int(lev or 1); entry=float(ae or ep or 0)
            if entry<=0 or cap<=0: continue
            sign = 1.0 if dirn=="long" else -1.0
            net_pct = float(net)/cap*100.0
            other = (int(xms), net_pct) if er in NONSL_REASONS else None
            T.append(dict(pair=pair, sign=sign, entry=entry, lev=lev, cap=cap,
                          e_ms=int(ems), x_ms=int(xms), net_pct=net_pct, other=other, er=er))
        return T

def run(T, pol, label, debug=False):
    sims=[]; base=[]; worst=[]
    for tr in T:
        s = simulate(tr, pol)
        if s is None: continue
        s = max(s, -100.0)   # liquidation floor on every path
        sims.append(s); base.append(tr["net_pct"])
        if debug: worst.append((s, tr["pair"], tr["lev"], tr["er"]))
    if not sims:
        print(f"{label}: no coverage"); return
    n=len(sims)
    exp=sum(sims)/n; wr=100.0*sum(1 for x in sims if x>0)/n
    bexp=sum(base)/n; bwr=100.0*sum(1 for x in base if x>0)/n
    print(f"{label:42} cov={n:5} | POLICY exp={exp:+.3f}%cap win={wr:4.1f} | ACTUAL exp={bexp:+.3f} win={bwr:4.1f} | delta={exp-bexp:+.3f}")
    if debug:
        worst.sort()
        print("   worst 8:", [(round(w[0],1), w[1], w[2], w[3]) for w in worst[:8]])
    return exp

def expect(T, pol):
    sims=[]
    for tr in T:
        s = simulate(tr, pol)
        if s is None: continue
        sims.append(max(s,-100.0))
    if not sims: return None,0,0
    n=len(sims); return sum(sims)/n, 100.0*sum(1 for x in sims if x>0)/n, n

def base(p,**kw):
    d=dict(S=15,A=6,l0=0.60,l1=0.75,l2=0.85,T1=10,T2=20,f1=0.5,f2=0.5,max_hold_d=3)
    d.update(kw); return d

if __name__ == "__main__":
    import time
    T0 = load_trades()
    print(f"loaded {len(T0)} trades; precomputing candle windows...", flush=True)
    T = []
    for tr in T0:
        w = make_window(tr, max_hold_d=3)
        if w is not None:
            tr["win"] = w; T.append(tr)
    print(f"coverable trades: {len(T)}\n", flush=True)
    run(T, base(0,S=50,A=10,l0=0.50,T1=15,T2=30), "CURRENT  (S50 A10 l.50 TP15/30)", debug=True)
    run(T, base(0), "PROPOSED (S15 A6 l.60 TP10/20)", debug=True)

    for seg,Tseg in (("ALL",T),("lev5",[t for t in T if t["lev"]==5]),("lev20",[t for t in T if t["lev"]==20])):
        # ACTUAL baseline for this segment
        bx=[t["net_pct"] for t in Tseg]; bexp=sum(bx)/len(bx); bwr=100*sum(1 for v in bx if v>0)/len(bx)
        print(f"\n=== SEG {seg} (N={len(Tseg)}) ACTUAL exp={bexp:+.3f}%cap win={bwr:.1f} | l0=.50 TP8/16 ===", flush=True)
        print(f"{'S|A':>5}", *[f"{a:>8}" for a in (4,6,8,10)], flush=True)
        best=(-1e9,None)
        for S in (4,6,8,10,12,15,20,50):
            row=[]
            for A in (4,6,8,10):
                e,wr,n = expect(Tseg, base(0,S=S,A=A,l0=0.50,T1=8,T2=16))
                row.append(e)
                if e>best[0]: best=(e,(S,A,round(wr,1)))
            print(f"{S:>5}", *[f"{x:+8.3f}" for x in row], flush=True)
        print(f"best {seg}: (S,A)={best[1][:2]} win={best[1][2]} exp={best[0]:+.3f}%cap (vs actual {bexp:+.3f})", flush=True)
