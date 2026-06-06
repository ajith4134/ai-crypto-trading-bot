"""
PROFESSOR A/B LAB — Phase 2, step 1 (READ-ONLY analysis).

Replays the new collapsed-core scoring idea over REAL closed trades (each row has a
stored feature_vector + actual net_pnl_usdt), to answer with EVIDENCE, not assumption:
  1. Which feature_vector features actually predict profit (univariate, signed by trade dir).
  2. Does a simple, interpretable, DATA-FITTED score separate winners from losers
     OUT-OF-SAMPLE (TimeSeriesSplit, so train is always older than test).
  3. If that score were used as an entry FILTER (take only score>=T), what expectancy /
     profit-factor / coverage results vs taking all trades (the old core's behavior)?

Nothing here writes to the DB or touches live trading. Run:
  docker exec trading-bot-brain-1 python /app/brain/ab_lab.py
"""
import os, json
import numpy as np
import pandas as pd
import psycopg2
from scipy.stats import spearmanr
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import TimeSeriesSplit, cross_val_predict
from sklearn.metrics import roc_auc_score

REDESIGN_TS = pd.Timestamp("2026-06-03", tz="UTC")


def load_trades():
    dsn = os.environ.get("DB_CONNECTION_STRING") or os.environ.get("DATABASE_URL")
    conn = psycopg2.connect(dsn)
    q = """
        SELECT id, direction, market_regime, net_pnl_usdt, capital_usdt, exit_time,
               feature_vector
        FROM trades
        WHERE status='closed' AND feature_vector IS NOT NULL
          AND capital_usdt > 0 AND net_pnl_usdt IS NOT NULL
        ORDER BY exit_time
    """
    rows = []
    with conn.cursor() as cur:
        cur.execute(q)
        cols = [c[0] for c in cur.description]
        for r in cur.fetchall():
            rows.append(dict(zip(cols, r)))
    conn.close()
    return rows


def featurize(fv, direction):
    """Direction-SIGNED features: + means 'evidence in the trade's favour'.
    Regime is included as a single signed alignment term (the data decides its sign)."""
    s = 1.0 if direction == "long" else -1.0
    def g(k):
        try:
            v = fv.get(k, 0.0)
            return float(v) if v is not None and v != "" else 0.0
        except (TypeError, ValueError):
            return 0.0
    f = {}
    # CandleNet multi-TF directional edge (dir>0.5 = up) and trend, signed
    for tf in ("1m", "5m", "15m", "30m", "1h"):
        dk = "cn_1m_dir1" if tf == "1m" else f"cn_{tf}_dir3"
        f[f"cn_{tf}_dir"] = (g(dk) - 0.5) * s
        f[f"cn_{tf}_trend"] = (g(f"cn_{tf}_trend") - 0.5) * s
    f["ofi"]            = g("ofi") * s
    f["obi"]            = g("bid_ask_imbalance") * s
    f["cvd_z"]          = g("cvd_z") * s
    f["xsmom_rank"]     = (g("xsmom_rank") - 0.5) * s
    f["xsmom_7d"]       = g("xsmom_return_7d") * s
    f["funding_contra"] = -g("funding_rate") * s          # crowd-fade
    f["netflow"]        = g("exchange_netflow_z") * s
    f["change24_mom"]   = g("change_24h") * s
    f["regime_align"]   = s * (g("regime_bull") - g("regime_bear"))  # +aligned / -opposed
    f["is_short"]       = 0.0 if direction == "long" else 1.0
    f["regime_turb"]    = g("regime_turbulent")
    f["signal_strength"]= g("signal_strength")
    f["dir_acc"]        = g("dir_acc_pair")
    f["vpin"]           = g("vpin")
    f["liq_cascade"]    = g("liq_cascade_prob")
    f["sentiment_align"]= (g("sentiment") - 0.5) * s
    f["vol_unit"]       = g("vol_unit")
    return f


def main():
    rows = load_trades()
    recs = []
    for t in rows:
        fv = t["feature_vector"]
        if isinstance(fv, str):
            try: fv = json.loads(fv)
            except Exception: continue
        if not isinstance(fv, dict) or "ofi" not in fv or "regime_bull" not in fv:
            continue
        feats = featurize(fv, t["direction"])
        cap = float(t["capital_usdt"] or 0) or 1.0
        feats["_net"] = float(t["net_pnl_usdt"])
        feats["_ret"] = float(t["net_pnl_usdt"]) / cap        # return on capital
        feats["_win"] = 1 if float(t["net_pnl_usdt"]) > 0 else 0
        feats["_dir"] = t["direction"]
        feats["_regime"] = t["market_regime"] or "?"
        et = pd.Timestamp(t["exit_time"])
        if et.tzinfo is None: et = et.tz_localize("UTC")
        feats["_t"] = et
        recs.append(feats)

    df = pd.DataFrame(recs).sort_values("_t").reset_index(drop=True)
    feat_cols = [c for c in df.columns if not c.startswith("_")]
    print(f"\n==== DATASET ====")
    print(f"trades usable: {len(df)}  (pre={int((df._t<REDESIGN_TS).sum())}, "
          f"post={int((df._t>=REDESIGN_TS).sum())})")
    print(f"baseline: winrate={df._win.mean()*100:.1f}%  "
          f"expectancy=${df._net.mean():.4f}/trade  net=${df._net.sum():.2f}  "
          f"PF={df._net[df._net>0].sum()/abs(df._net[df._net<0].sum()):.3f}")

    # ---- 1. UNIVARIATE: which features predict net PnL (Spearman) ----
    print(f"\n==== UNIVARIATE Spearman corr (feature vs net_pnl), full set ====")
    uni = []
    for c in feat_cols:
        if df[c].nunique() < 3: continue
        rho, p = spearmanr(df[c], df["_net"])
        uni.append((c, rho, p))
    uni.sort(key=lambda x: -abs(x[1]))
    for c, rho, p in uni:
        flag = "  <==" if (abs(rho) > 0.02 and p < 0.05) else ""
        print(f"  {c:18} rho={rho:+.4f}  p={p:.3g}{flag}")

    # ---- 2. DATA-FITTED interpretable score, OUT-OF-SAMPLE ----
    X = df[feat_cols].fillna(0.0).values
    y = df["_win"].values
    Xs = StandardScaler().fit_transform(X)
    tss = TimeSeriesSplit(n_splits=5)
    clf = LogisticRegression(max_iter=2000, C=0.5)
    # out-of-fold scores (each prediction made by a model trained on OLDER data only)
    # manual OOF: each test fold predicted by a model trained on OLDER folds only
    oof = np.full(len(y), np.nan)
    for tr, te in tss.split(Xs):
        clf.fit(Xs[tr], y[tr])
        oof[te] = clf.predict_proba(Xs[te])[:, 1]
    mask = ~np.isnan(oof)
    try:
        auc = roc_auc_score(y[mask], oof[mask])
    except Exception:
        auc = float("nan")
    print(f"\n==== DATA-FITTED SCORE (logistic, TimeSeriesSplit OOF) ====")
    print(f"OOF AUC (win prediction) = {auc:.4f}   (0.5 = no edge)")

    # coefficients from a full-fit (interpretability)
    clf.fit(Xs, y)
    coef = sorted(zip(feat_cols, clf.coef_[0]), key=lambda x: -abs(x[1]))
    print("  top coefficients (standardized; sign = effect on P(win)):")
    for c, w in coef[:18]:
        print(f"    {c:18} {w:+.4f}")

    # ---- 3. FILTER SIMULATION on OOF scores (true out-of-sample) ----
    dfx = df[mask].copy()
    dfx["score"] = oof[mask]
    print(f"\n==== A/B FILTER SIM (take only OOF score >= percentile T) ====")
    print(f"  {'keep_top':>9} {'n':>6} {'coverage':>9} {'winrate':>8} "
          f"{'expectancy':>11} {'net_usd':>10} {'PF':>6}")
    base_net = dfx["_net"].sum()
    for pct in (100, 80, 60, 50, 40, 30, 20, 10):
        thr = np.percentile(dfx["score"], 100 - pct)
        sel = dfx[dfx["score"] >= thr]
        if len(sel) == 0: continue
        wins = sel._net[sel._net > 0].sum(); loss = abs(sel._net[sel._net < 0].sum())
        pf = wins / loss if loss > 0 else float("inf")
        print(f"  {pct:>8}% {len(sel):>6} {len(sel)/len(dfx)*100:>8.1f}% "
              f"{sel._win.mean()*100:>7.1f}% ${sel._net.mean():>+9.4f} "
              f"${sel._net.sum():>+9.2f} {pf:>6.3f}")
    print(f"  (baseline take-all net = ${base_net:.2f} over {len(dfx)} trades)")

    # ---- 4. by direction: does the score fix the short problem? ----
    print(f"\n==== FILTER @ top-50% by DIRECTION (OOF) ====")
    for d in ("long", "short"):
        sub = dfx[dfx._dir == d]
        if len(sub) < 50: continue
        thr = np.percentile(sub["score"], 50)
        sel = sub[sub["score"] >= thr]
        print(f"  {d:5} all: net=${sub._net.sum():+.2f} exp=${sub._net.mean():+.4f} "
              f"wr={sub._win.mean()*100:.1f}%  |  top50%: net=${sel._net.sum():+.2f} "
              f"exp=${sel._net.mean():+.4f} wr={sel._win.mean()*100:.1f}%")


if __name__ == "__main__":
    main()
