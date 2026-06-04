"""
Reframed predict-all direction/move trainer — P3 of
next_impl/kline_corpus_model_reframe.md (cont. 69i).

Trains XGBoost direction (P[up]) + magnitude (% move) heads on the SHARED KLINE
CORPUS (P1) with STANDARDIZED FORWARD LABELS (P2), using OHLCV-only features
(prediction.kline_features) computable live without train/serve skew. This REPLACES
the old trade-log-trained heads that collapsed to constant per-pair output (the
68%-zero-fill + regime-skew degeneracy). RR/confidence heads stay on cleaned trades
(execution-calibrated) — out of scope here.

Run:
  docker compose exec celery_worker_candlenet python -m prediction.xgb_kline_trainer
Saves models/predict_all_kline.pkl ; validation prints per-pair P[up] (must VARY).
"""
from __future__ import annotations

import os
import pickle
import time

import numpy as np
import structlog

from prediction.kline_features import feature_matrix, features_at, FEATURE_NAMES

log = structlog.get_logger()

_MODEL_PATH = "/app/models/predict_all_kline.pkl"
_FALLBACK_PATH = "/opt/trading-bot/models/predict_all_kline.pkl"
_MIN_SAMPLES = 5000


def _active_pairs() -> list[str]:
    try:
        import redis_client
        ps = redis_client.get().smembers("scanner:active_pairs")
        return sorted(p.decode() if isinstance(p, bytes) else p for p in ps)
    except Exception:
        return []


def train(interval: str = "15m", horizon: int = 4, deadband: float = 0.0008,
          step: int = 3, pairs: list[str] | None = None,
          save_path: str | None = None) -> dict:
    from ml.forward_labels import load_ohlcv, direction_label, magnitude_label
    try:
        from xgboost import XGBClassifier, XGBRegressor
    except Exception as exc:
        return {"status": "xgboost_missing", "error": str(exc)[:120]}

    pairs = pairs or _active_pairs()
    X_all, yd_all, ym_all = [], [], []
    used_pairs = 0
    t0 = time.time()
    for pair in pairs:
        d = load_ohlcv(pair, interval)
        if d is None or len(d["close"]) < 200:
            continue
        o, h, l, c, v = d["open"], d["high"], d["low"], d["close"], d["volume"]
        X, idx = feature_matrix(o, h, l, c, v, step=step)
        if len(idx) == 0:
            continue
        dlab = direction_label(c, horizon, deadband)
        mlab = magnitude_label(c, horizon)
        for k, i in enumerate(idx):
            dl = dlab[i]
            if not np.isfinite(dl):          # flat (deadband) or no future
                continue
            X_all.append(X[k]); yd_all.append(dl); ym_all.append(mlab[i])
        used_pairs += 1

    n = len(X_all)
    if n < _MIN_SAMPLES:
        return {"status": "insufficient_samples", "n": n, "needed": _MIN_SAMPLES}

    X = np.asarray(X_all, dtype=np.float32)
    yd = np.asarray(yd_all, dtype=np.float32)
    ym = np.asarray(ym_all, dtype=np.float32)

    # time-ordered-ish holdout (last 15%) for an honest AUC read
    cut = int(n * 0.85)
    clf = XGBClassifier(max_depth=5, n_estimators=300, learning_rate=0.05,
                        subsample=0.8, colsample_bytree=0.8,
                        objective="binary:logistic", tree_method="hist",
                        n_jobs=4, eval_metric="auc", verbosity=0)
    clf.fit(X[:cut], yd[:cut])
    reg = XGBRegressor(max_depth=5, n_estimators=300, learning_rate=0.05,
                       subsample=0.8, colsample_bytree=0.8,
                       objective="reg:squarederror", tree_method="hist",
                       n_jobs=4, verbosity=0)
    reg.fit(X[:cut], np.abs(ym[:cut]))

    # holdout AUC
    try:
        from sklearn.metrics import roc_auc_score
        val_auc = float(roc_auc_score(yd[cut:], clf.predict_proba(X[cut:])[:, 1]))
    except Exception:
        val_auc = None

    bundle = {
        "feature_names": FEATURE_NAMES,
        "interval": interval, "horizon": horizon, "deadband": deadband,
        "direction": clf, "magnitude": reg,
        "trained_at": int(time.time()), "n_samples": n,
        "n_pairs": used_pairs, "up_rate": float(yd.mean()),
        "val_auc": val_auc,
    }
    path = save_path or _MODEL_PATH
    try:
        with open(path, "wb") as f:
            pickle.dump(bundle, f)
    except Exception:
        path = _FALLBACK_PATH
        with open(path, "wb") as f:
            pickle.dump(bundle, f)

    report = {"status": "trained", "n_samples": n, "n_pairs": used_pairs,
              "up_rate": round(float(yd.mean()), 4),
              "val_auc": round(val_auc, 4) if val_auc else None,
              "elapsed_s": round(time.time() - t0, 1), "path": path}
    log.info("predict_all_kline_trained", **report)
    return report


_BUNDLE = None


def _load_bundle():
    global _BUNDLE
    if _BUNDLE is not None:
        return _BUNDLE
    for p in (_MODEL_PATH, _FALLBACK_PATH):
        if os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    _BUNDLE = pickle.load(f)
                return _BUNDLE
            except Exception:
                continue
    return None


def _bundle_current(b) -> bool:
    """cont. 69q — guard against a stale model whose feature count no longer
    matches the current builder (e.g. after adding S/R columns). Feeding the new
    18-feature vector to a 14-feature model would error / mispredict. When stale,
    callers return None so the kline prior goes DORMANT until the retrain task
    rebuilds the bundle at the new N — graceful, no crash, no wrong predictions."""
    try:
        n = len(b.get("feature_names") or ())
        if n != len(FEATURE_NAMES):
            import redis_client
            redis_client.get().set("predict:kline:feature_mismatch", f"{n}!={len(FEATURE_NAMES)}")
            log.warning("kline_bundle_feature_mismatch",
                        bundle_n=n, builder_n=len(FEATURE_NAMES))
            return False
        return True
    except Exception:
        return False


def predict_pair(pair: str) -> dict | None:
    """Live-style prediction from the latest corpus bar. Returns {p_up, magnitude}."""
    b = _load_bundle()
    if b is None or not _bundle_current(b):
        return None
    from ml.forward_labels import load_ohlcv
    d = load_ohlcv(pair, b["interval"])
    if d is None or len(d["close"]) < 30:
        return None
    o, h, l, c, v = d["open"], d["high"], d["low"], d["close"], d["volume"]
    f = features_at(o, h, l, c, v, len(c) - 1)
    if f is None:
        return None
    X = np.asarray([f], dtype=np.float32)
    p_up = float(b["direction"].predict_proba(X)[0, 1])
    mag = float(b["magnitude"].predict(X)[0])
    return {"p_up": round(p_up, 4), "magnitude_pct": round(mag, 4)}


def predict_live(pair: str, interval: str | None = None) -> dict | None:
    """LIVE inference from Redis candles (NOT the ~1-day-lagged corpus). Reads
    {pair}:{interval}:candles, sorts ascending, builds features on the newest bar,
    predicts. Used by the publisher task so the deterministic scorer can consume a
    CURRENT kline prior. cont. 69j (P3 finish)."""
    import json
    import redis_client
    import redis_keys
    b = _load_bundle()
    if b is None or not _bundle_current(b):
        return None
    interval = interval or b["interval"]
    key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", interval)
    raw = redis_client.get().lrange(key, 0, -1)
    if not raw or len(raw) < 30:
        return None
    rows = []
    for c in raw:
        try:
            d = json.loads(c)
            rows.append((int(d["t"]), float(d["o"]), float(d["h"]),
                         float(d["l"]), float(d["c"]), float(d["v"])))
        except Exception:
            continue
    if len(rows) < 30:
        return None
    rows.sort(key=lambda x: x[0])              # ascending → last = newest
    arr = np.asarray(rows, dtype=np.float64)
    o, h, l, c, v = arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4], arr[:, 5]
    f = features_at(o, h, l, c, v, len(c) - 1)
    if f is None or not all(np.isfinite(f)):
        return None
    X = np.asarray([f], dtype=np.float32)
    return {"p_up": round(float(b["direction"].predict_proba(X)[0, 1]), 4),
            "magnitude_pct": round(float(b["magnitude"].predict(X)[0]), 4)}


if __name__ == "__main__":
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    import logging as _lg
    _lg.disable(_lg.CRITICAL)
    rep = train()
    print("TRAIN:", rep)
    if rep.get("status") == "trained":
        _BUNDLE = None   # force reload of freshly-saved bundle (module-level)
        print("VALIDATION — per-pair P[up] (must VARY, not constant):")
        for p in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT",
                  "BNBUSDT", "ARCUSDT", "LINKUSDT"]:
            r = predict_pair(p)
            if r:
                print(f"  {p:10s} p_up={r['p_up']:.3f}  mag={r['magnitude_pct']:.3f}%")
