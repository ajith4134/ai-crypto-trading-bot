"""Layer 2a — Learned multi-timeframe FUSION (soft-attention replacement).

The cascade's hard 3-of-4 vote produces crude confidence (67/80/100) — the recurring
calibration pain. Research (arXiv 2508.02356) shows a LEARNED weighting over timeframes
is what makes confidence meaningful. Per the cont. 65k sign-off we start with the
"lightweight learned weighting" variant (full attention later): a CALIBRATED gradient-
boosted classifier that maps the per-TF CandleNet outputs (+ regime + microstructure
context) → P(up), trained on REAL trade outcomes.

Why this data: ~7.8k recent trades carry a `feature_vector` snapshot (cn_1m_dir1,
cn_5m_dir3, cn_15m_dir3, cn_1h_dir3, trends, mags, regime, ofi, vpin, vol_unit, …) AND
a realized outcome (net_pnl_usdt). The realized UP label is recoverable:
    up = (direction == "long") == (net_pnl_usdt > 0)
so we learn, directly from what actually happened, how to weight the timeframes — which
is both the soft-attention combiner AND a meta-labeling-style confidence model.

Output contract: predict_p_up(feature_dict) → calibrated P(up) in [0,1]. The cascade
turns that into direction (up if >0.5) + confidence (|p-0.5|*200, clamped 0-100),
replacing the hard vote when `cascade:use_fusion = "1"` and a model is loaded.

Model file: models/tf_fusion.pkl  (HistGradientBoosting + isotonic calibration).
Gated by validation: only saved/loaded if val AUC ≥ MIN_AUC and ECE ≤ MAX_ECE.
"""
from __future__ import annotations
import json
import os
import pickle
import time
from pathlib import Path
from typing import Optional

import numpy as np
import structlog

log = structlog.get_logger()

MODEL_PATH = Path(os.environ.get("TF_FUSION_PATH", "/app/models/tf_fusion.pkl"))

# Feature order is fixed — the model + inference must agree. Missing keys → neutral
# defaults (dir/trend → 0.5, everything else → 0.0) so a partial feature_vector still
# scores. ofi/vpin/vol_unit fold Layer-1 microstructure context into the fusion.
FEATURES = [
    "cn_1m_dir1", "cn_5m_dir3", "cn_15m_dir3", "cn_1h_dir3",
    "cn_1m_trend", "cn_5m_trend", "cn_15m_trend", "cn_1h_trend",
    "cn_5m_mag3", "cn_15m_mag3", "cn_1h_mag3",
    "regime_bull", "regime_bear",
    "ofi", "vpin", "vol_unit", "sentiment",
]
_DIR_DEFAULT = {"cn_1m_dir1", "cn_5m_dir3", "cn_15m_dir3", "cn_1h_dir3",
                "cn_1m_trend", "cn_5m_trend", "cn_15m_trend", "cn_1h_trend"}

MIN_SAMPLES = 800
MIN_AUC = 0.55          # must beat near-coin-flip to be worth using
MAX_ECE = 0.10          # calibrated confidence (same bar as CandleNet)

_cache: dict = {"model": None, "mtime": 0.0}


def _row_to_vector(fv: dict) -> list[float]:
    out = []
    for k in FEATURES:
        v = fv.get(k)
        if v is None:
            out.append(0.5 if k in _DIR_DEFAULT else 0.0)
        else:
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                out.append(0.5 if k in _DIR_DEFAULT else 0.0)
    return out


def _ece(p: np.ndarray, y: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0.0, 1.0, bins + 1)
    b = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    e = 0.0
    for i in range(bins):
        m = b == i
        if m.any():
            e += float(m.mean()) * abs(float(p[m].mean()) - float(y[m].mean()))
    return float(e)


def train_from_trades(rows: list[dict]) -> dict:
    """Train the fusion model from trade rows. Each row: {feature_vector(dict|json),
    direction, net_pnl_usdt}. Returns metrics dict; saves model only if it passes gates."""
    try:
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.metrics import roc_auc_score
    except Exception as exc:
        return {"status": "sklearn_unavailable", "error": str(exc)[:120]}

    X, y = [], []
    for r in rows:
        fv = r.get("feature_vector")
        if isinstance(fv, str):
            try:
                fv = json.loads(fv)
            except Exception:
                continue
        if not isinstance(fv, dict):
            continue
        direction = r.get("direction")
        pnl = r.get("net_pnl_usdt")
        if direction not in ("long", "short") or pnl is None:
            continue
        try:
            pnl = float(pnl)
        except (TypeError, ValueError):
            continue
        if pnl == 0:
            continue  # ambiguous (breakeven) — drop
        # Realized up label: long+win or short+loss ⇒ price went up.
        up = 1 if ((direction == "long") == (pnl > 0)) else 0
        X.append(_row_to_vector(fv))
        y.append(up)

    n = len(X)
    if n < MIN_SAMPLES:
        return {"status": "insufficient_data", "samples": n}

    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y, dtype=np.int64)
    # Rows arrive newest-first; reverse to chronological so the split is a proper
    # FORWARD validation (train on older, validate on the most recent trades) —
    # the realistic test of whether the combiner generalizes to live conditions.
    X = X[::-1]
    y = y[::-1]
    split = int(n * 0.8)
    Xtr, Xval, ytr, yval = X[:split], X[split:], y[:split], y[split:]
    if len(np.unique(ytr)) < 2 or len(np.unique(yval)) < 2:
        return {"status": "degenerate_labels", "samples": n,
                "pos_rate": round(float(y.mean()), 4)}

    base = HistGradientBoostingClassifier(
        max_depth=3, max_iter=200, learning_rate=0.05,
        l2_regularization=1.0, random_state=42, early_stopping=True)
    clf = CalibratedClassifierCV(base, method="isotonic", cv=3)
    clf.fit(Xtr, ytr)

    p = clf.predict_proba(Xval)[:, 1]
    try:
        auc = float(roc_auc_score(yval, p))
    except Exception:
        auc = 0.5
    ece = _ece(p, yval.astype(float))
    metrics = {"status": "trained", "samples": n, "val_auc": round(auc, 4),
               "val_ece": round(ece, 4), "pos_rate": round(float(y.mean()), 4)}

    if auc < MIN_AUC or ece > MAX_ECE:
        metrics["status"] = "rejected_validation"
        metrics["reason"] = f"auc {auc:.3f}<{MIN_AUC} or ece {ece:.3f}>{MAX_ECE}"
        log.warning("tf_fusion_rejected", **metrics)
        return metrics

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump({"model": clf, "features": FEATURES, "metrics": metrics,
                     "trained_at": int(time.time())}, f)
    _cache["model"] = None  # force reload
    log.info("tf_fusion_trained_and_saved", path=str(MODEL_PATH), **metrics)
    return metrics


def _load():
    try:
        mtime = MODEL_PATH.stat().st_mtime
    except OSError:
        return None
    if _cache["model"] is not None and _cache["mtime"] == mtime:
        return _cache["model"]
    try:
        with open(MODEL_PATH, "rb") as f:
            bundle = pickle.load(f)
        _cache["model"] = bundle
        _cache["mtime"] = mtime
        return bundle
    except Exception as exc:
        log.warning("tf_fusion_load_failed", error=str(exc)[:120])
        return None


def predict_p_up(feature_dict: dict) -> Optional[float]:
    """Calibrated P(up) from the multi-TF feature dict, or None if no model."""
    bundle = _load()
    if not bundle:
        return None
    try:
        x = np.asarray([_row_to_vector(feature_dict)], dtype=np.float64)
        return float(bundle["model"].predict_proba(x)[:, 1][0])
    except Exception as exc:
        log.debug("tf_fusion_predict_failed", error=str(exc)[:120])
        return None


def is_ready() -> bool:
    return _load() is not None
