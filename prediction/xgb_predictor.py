"""XGBoost multi-head predictor for predict-all-before-open — cont. 63.

Wraps an XGBoost regressor pipeline that emits the 7 trade-decision fields
mandated by next_impl/predict_all_before_open.md §0:
    predicted_direction      (binary classification: long vs short, plus skip
                              fallback at the conformal gate)
    predicted_entry_offset_bps  (offset from current mark, signed)
    predicted_sl_offset_bps     (distance, always positive)
    predicted_tp_offset_bps     (distance, always positive)
    predicted_hold_seconds      (regression, log-transformed)
    confidence_raw              (raw model probability)
    rr_p25 / rr_p50 / rr_p75    (RR distribution via quantile regression)

The predictor is COLD by default — `predict()` returns None unless a trained
model file exists at `models/predict_all_xgb.pkl`. This lets Phase B ship
safely; consumers (signals/engine.py) default to legacy behaviour when
predict() returns None.

Training is provided via `train_from_history()` which pulls closed trades +
counterfactuals from Postgres, builds features via prediction.features, fits
six XGBoost models (one per head where regression is needed), saves the
bundle to disk, and emits a training report dict.

Cold-start safety: even after a model is loaded, every prediction passes the
ml.conformal_wrapper for calibrated confidence — the raw model probability
is never used directly.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import structlog

log = structlog.get_logger()


_MODEL_PATH = "/app/models/predict_all_xgb.pkl"
_FALLBACK_MODEL_PATH = "/opt/trading-bot/models/predict_all_xgb.pkl"
_MIN_TRAIN_SAMPLES = 200          # below this, training aborts as insufficient
_DEFAULT_RR_TARGETS = (0.25, 0.5, 0.75)

# Lazy cache — module-level so multiple refresh-loop ticks reuse the bundle.
_loaded_bundle: Optional[dict] = None
_loaded_mtime: float = 0.0
_SHAPE_WARNED: bool = False   # cont. 70 — one-shot warn for the shape-dormancy guard


def _resolve_model_path() -> Optional[str]:
    """Return whichever model path exists, or None if neither does."""
    for p in (_MODEL_PATH, _FALLBACK_MODEL_PATH):
        if os.path.exists(p):
            return p
    return None


def _load_bundle() -> Optional[dict]:
    """Load (or hot-reload) the XGBoost model bundle from disk."""
    global _loaded_bundle, _loaded_mtime
    path = _resolve_model_path()
    if path is None:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if _loaded_bundle is not None and mtime == _loaded_mtime:
        return _loaded_bundle
    try:
        import pickle
        with open(path, "rb") as fh:
            bundle = pickle.load(fh)
        # Validate bundle shape — fail loudly if the file is corrupted.
        required = {"direction", "entry_bps", "sl_bps", "tp_bps",
                    "hold_log_seconds", "confidence_raw",
                    "rr_q25", "rr_q50", "rr_q75",
                    "feature_columns", "trained_at", "n_samples"}
        if not required.issubset(set(bundle.keys())):
            log.warning("predict_all_bundle_missing_keys",
                        path=path,
                        missing=sorted(required - set(bundle.keys()))[:5])
            return None
        _loaded_bundle = bundle
        _loaded_mtime = mtime
        log.info("predict_all_bundle_loaded", path=path,
                 trained_at=bundle.get("trained_at"),
                 n_samples=bundle.get("n_samples"),
                 features=len(bundle.get("feature_columns", [])))
        return bundle
    except Exception as exc:
        log.warning("predict_all_bundle_load_failed",
                    path=path, error=str(exc)[:200])
        return None


def is_ready() -> bool:
    """True iff a trained model file exists and loads. Cheap to call."""
    return _load_bundle() is not None


def predict(features_vec: list[float], pair: str = "") -> Optional[dict]:
    """Inference path. Returns None when cold (no model) — caller must
    handle gracefully. Returned dict has the 9 prediction fields above plus
    `model_version` and `predicted_at` for observability."""
    bundle = _load_bundle()
    if bundle is None:
        return None
    # cont. 70 Track 2 — feature_count DORMANCY guard. When FEATURE_COLUMNS gains
    # columns (new micro features cvd_z/ofi_l1/ofi_accel) the live vector grows,
    # but a model trained at the OLD width would raise inside XGBoost. Stay dormant
    # (return None) until a retrain re-bundles at the new width. The predict-all
    # gate is OFF (prediction:gate_enabled=0) so this has zero live-trade impact —
    # it only suppresses a stale shadow prediction. Mirrors the cont.69q
    # xgb_kline_trainer `_bundle_current` pattern.
    _exp = bundle.get("feature_count") or len(bundle.get("feature_columns") or ())
    if _exp and len(features_vec) != _exp:
        global _SHAPE_WARNED
        if not _SHAPE_WARNED:
            log.warning("predict_all_dormant_shape_mismatch",
                        live_len=len(features_vec), model_len=_exp,
                        note="awaiting predict-all retrain at new feature width")
            _SHAPE_WARNED = True
        return None
    # XGBoost expects a 2-D numpy array; build it lazily to avoid hard
    # importing numpy at module-load time.
    try:
        import numpy as _np
        x = _np.asarray([features_vec], dtype=_np.float32)
    except Exception as exc:
        log.warning("predict_all_numpy_failed", error=str(exc)[:120])
        return None
    try:
        dir_prob = float(bundle["direction"].predict_proba(x)[0, 1])
        entry_bps = float(bundle["entry_bps"].predict(x)[0])
        sl_bps    = abs(float(bundle["sl_bps"].predict(x)[0]))
        tp_bps    = abs(float(bundle["tp_bps"].predict(x)[0]))
        hold_log  = float(bundle["hold_log_seconds"].predict(x)[0])
        conf_raw  = float(bundle["confidence_raw"].predict_proba(x)[0, 1])
        rr_q25    = float(bundle["rr_q25"].predict(x)[0])
        rr_q50    = float(bundle["rr_q50"].predict(x)[0])
        rr_q75    = float(bundle["rr_q75"].predict(x)[0])
    except Exception as exc:
        log.warning("predict_all_inference_failed",
                    pair=pair, error=str(exc)[:200])
        return None

    # Conformal calibration — raw probability → coverage-guaranteed band.
    # When ml.conformal_wrapper is unavailable, fall through with raw conf.
    conf_calibrated = conf_raw
    try:
        from ml.conformal_wrapper import calibrate_probability
        conf_calibrated = float(
            calibrate_probability(conf_raw, model="predict_all_xgb"))
    except Exception:
        pass

    return {
        "predicted_direction":     "long" if dir_prob >= 0.5 else "short",
        "direction_prob":          round(dir_prob, 4),
        "predicted_entry_offset_bps": round(entry_bps, 2),
        "predicted_sl_offset_bps":    round(sl_bps, 2),
        "predicted_tp_offset_bps":    round(tp_bps, 2),
        "predicted_hold_seconds":     int(max(1, min(86400, 2 ** hold_log))),
        "confidence_raw":             round(conf_raw, 4),
        "conformal_confidence":       round(conf_calibrated, 4),
        "predicted_rr_p25":           round(rr_q25, 4),
        "predicted_rr_p50":           round(rr_q50, 4),
        "predicted_rr_p75":           round(rr_q75, 4),
        "model_version":              str(bundle.get("trained_at", "unknown")),
        "predicted_at":               int(time.time()),
    }


def _fetch_training_rows(lookback_days: int = 60) -> list[dict]:
    """Pull closed trades (+ matched signal row) from Postgres for training.
    Each row gets the features available at signal time (best-effort — we
    snapshot from `signals.feature_vector` JSON when present, otherwise zero-
    fill via prediction.features.FEATURE_COLUMNS).

    Target columns derived from the closed trade:
      - direction_y      (signal direction, 1=long, 0=short)
      - entry_bps_y      (signed offset from signal mark to entry fill)
      - sl_bps_y         (distance from entry to realised SL price)
      - tp_bps_y         (distance from entry to realised peak)
      - hold_log_y       (log2 hold_time_seconds)
      - confidence_y     (1 if net_pnl_usdt > 0 else 0)
      - rr_y             (actual_rr if present else net_pnl / capital)
    """
    from db import db_conn
    rows: list[dict] = []
    sql = """
        SELECT
            t.id, t.pair, t.direction, t.entry_price, t.average_entry,
            t.exit_price, t.trailing_sl_level,
            t.peak_pnl_usdt, t.net_pnl_usdt, t.capital_usdt, t.leverage,
            t.hold_time_seconds, t.market_regime, t.actual_rr, t.predicted_rr,
            t.pattern_cluster_id,
            s.feature_vector, s.signal_strength, s.potential_score AS trade_potential_score
        FROM trades t
        LEFT JOIN signals s ON s.trade_id = t.id
        WHERE t.status     = 'closed'
          AND t.exit_time  > NOW() - INTERVAL %s
          AND t.entry_price IS NOT NULL
          AND t.capital_usdt > 0
        ORDER BY t.exit_time ASC
    """
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (f"{int(lookback_days)} days",))
                cols = [d[0] for d in cur.description]
                for raw in cur.fetchall():
                    rows.append(dict(zip(cols, raw)))
    except Exception as exc:
        log.warning("predict_all_train_fetch_failed", error=str(exc)[:200])
        return []
    return rows


def _row_to_features_and_targets(row: dict) -> Optional[tuple[list[float], dict]]:
    """Translate a single training row into (feature_vec, target_dict).
    Returns None if any critical target is unavailable."""
    import json
    import math

    from prediction.features import FEATURE_COLUMNS, to_vector

    entry = float(row.get("average_entry") or row.get("entry_price") or 0)
    exit_p = float(row.get("exit_price") or 0)
    capital = float(row.get("capital_usdt") or 0)
    leverage = float(row.get("leverage") or 1)
    hold_s = int(row.get("hold_time_seconds") or 0)
    net_pnl = float(row.get("net_pnl_usdt") or 0)
    direction = (row.get("direction") or "long").lower()
    if entry <= 0 or hold_s <= 0 or capital <= 0:
        return None

    # Feature vector: prefer the signal's stored feature_vector JSON, else
    # zero-fill via FEATURE_COLUMNS. The 0-fill is honest "no historical
    # snapshot available" rather than synthesising fake values.
    fv_raw = row.get("feature_vector")
    if fv_raw:
        try:
            fv = fv_raw if isinstance(fv_raw, dict) else json.loads(fv_raw)
            if not isinstance(fv, dict):
                fv = {}
        except Exception:
            fv = {}
    else:
        fv = {}
    feat_dict = {col: float(fv.get(col, 0.0) or 0.0) for col in FEATURE_COLUMNS}
    # Inject high-information columns the signal usually doesn't store.
    if row.get("signal_strength") is not None:
        feat_dict["signal_strength"] = float(row["signal_strength"])
    if row.get("trade_potential_score") is not None:
        feat_dict["trade_potential"] = float(row["trade_potential_score"])
    if row.get("pattern_cluster_id") is not None:
        feat_dict["pattern_cluster_id"] = float(row["pattern_cluster_id"])
    regime = str(row.get("market_regime") or "unknown").lower()
    feat_dict["regime_bull"]      = 1.0 if regime == "bull" else 0.0
    feat_dict["regime_bear"]      = 1.0 if regime == "bear" else 0.0
    feat_dict["regime_turbulent"] = 1.0 if regime == "turbulent" else 0.0

    # Targets
    direction_y = 1.0 if direction == "long" else 0.0
    entry_bps_y = ((entry - float(row.get("entry_price") or entry))
                   / entry) * 10_000.0
    # SL realisation distance — fallback to net_pnl when trailing_sl_level missing
    trail_sl = float(row.get("trailing_sl_level") or 0)
    if trail_sl > 0 and entry > 0:
        sl_bps_y = abs((trail_sl - entry) / entry) * 10_000.0
    else:
        sl_bps_y = abs(net_pnl) / max(capital * leverage, 1) * 10_000.0
    # TP realisation distance — peak away from entry
    peak_pnl = float(row.get("peak_pnl_usdt") or 0)
    if peak_pnl > 0 and capital > 0 and leverage > 0:
        tp_bps_y = (peak_pnl / (capital * leverage)) * 10_000.0
    else:
        tp_bps_y = abs(exit_p - entry) / entry * 10_000.0 if entry > 0 else 0.0
    hold_log_y = math.log2(max(1, hold_s))
    confidence_y = 1.0 if net_pnl > 0 else 0.0
    rr = row.get("actual_rr") or row.get("predicted_rr") or 0
    try:
        rr_y = float(rr or 0)
    except (TypeError, ValueError):
        rr_y = 0.0
    if rr_y == 0 and sl_bps_y > 0:
        # Empirical RR fallback — realised peak vs realised SL distance.
        rr_y = tp_bps_y / sl_bps_y if sl_bps_y > 0 else 0.0
    rr_y = max(-5.0, min(20.0, rr_y))

    return (to_vector(feat_dict), {
        "direction":     direction_y,
        "entry_bps":     entry_bps_y,
        "sl_bps":        sl_bps_y,
        "tp_bps":        tp_bps_y,
        "hold_log":      hold_log_y,
        "confidence":    confidence_y,
        "rr":            rr_y,
    })


def train_from_history(lookback_days: int = 60,
                       save_path: Optional[str] = None) -> dict:
    """Fit the 9 XGBoost heads from the last N days of closed trades.

    NOT called from the live trading loop. Called from:
      - pretrainer/walk_forward.py for forward-validated promotion
      - manual run via `docker compose exec brain python -m prediction.xgb_predictor`

    Returns a report dict. Persists the bundle to disk on success.
    """
    rows = _fetch_training_rows(lookback_days)
    if len(rows) < _MIN_TRAIN_SAMPLES:
        log.info("predict_all_train_insufficient",
                 n_rows=len(rows), needed=_MIN_TRAIN_SAMPLES)
        return {"status": "insufficient_samples",
                "n_rows": len(rows), "needed": _MIN_TRAIN_SAMPLES}

    try:
        import numpy as _np
        from xgboost import XGBClassifier, XGBRegressor
    except Exception as exc:
        log.warning("predict_all_train_xgboost_missing",
                    error=str(exc)[:200])
        return {"status": "xgboost_missing", "error": str(exc)[:200]}

    from prediction.features import FEATURE_COLUMNS

    X_list, y_list = [], []
    for r in rows:
        pair = _row_to_features_and_targets(r)
        if pair is None:
            continue
        X_list.append(pair[0])
        y_list.append(pair[1])
    if len(X_list) < _MIN_TRAIN_SAMPLES:
        return {"status": "insufficient_after_filtering",
                "n_rows": len(X_list), "needed": _MIN_TRAIN_SAMPLES}
    X = _np.asarray(X_list, dtype=_np.float32)
    n = X.shape[0]

    # Fit each head independently. Classification heads use logistic; regressors
    # use squared-error; quantile heads use quantile loss.
    def _clf():
        return XGBClassifier(max_depth=4, n_estimators=120, learning_rate=0.1,
                              objective="binary:logistic",
                              tree_method="hist", n_jobs=4,
                              use_label_encoder=False, verbosity=0)

    def _reg():
        return XGBRegressor(max_depth=4, n_estimators=120, learning_rate=0.1,
                             objective="reg:squarederror",
                             tree_method="hist", n_jobs=4, verbosity=0)

    def _quant(alpha: float):
        # XGBoost ≥ 1.7 supports quantile via reg:quantileerror
        return XGBRegressor(max_depth=4, n_estimators=120, learning_rate=0.1,
                             objective="reg:quantileerror",
                             quantile_alpha=alpha,
                             tree_method="hist", n_jobs=4, verbosity=0)

    bundle: dict = {
        "feature_columns": FEATURE_COLUMNS,
        "trained_at":      int(time.time()),
        "n_samples":       n,
        "lookback_days":   lookback_days,
    }
    try:
        bundle["direction"]        = _clf().fit(
            X, _np.asarray([y["direction"] for y in y_list]))
        bundle["confidence_raw"]   = _clf().fit(
            X, _np.asarray([y["confidence"] for y in y_list]))
        bundle["entry_bps"]        = _reg().fit(
            X, _np.asarray([y["entry_bps"] for y in y_list]))
        bundle["sl_bps"]           = _reg().fit(
            X, _np.asarray([y["sl_bps"] for y in y_list]))
        bundle["tp_bps"]           = _reg().fit(
            X, _np.asarray([y["tp_bps"] for y in y_list]))
        bundle["hold_log_seconds"] = _reg().fit(
            X, _np.asarray([y["hold_log"] for y in y_list]))
        rr_y_arr = _np.asarray([y["rr"] for y in y_list])
        bundle["rr_q25"]           = _quant(0.25).fit(X, rr_y_arr)
        bundle["rr_q50"]           = _quant(0.50).fit(X, rr_y_arr)
        bundle["rr_q75"]           = _quant(0.75).fit(X, rr_y_arr)
    except Exception as exc:
        log.warning("predict_all_train_fit_failed", error=str(exc)[:200])
        return {"status": "fit_failed", "error": str(exc)[:200]}

    # cont. 65 fix: when training from inside the brain container, prefer
    # the in-container mount (/app/models, writable, bind-mounted to host
    # /opt/trading-bot/models). The host fallback path is only correct when
    # this module is invoked from outside the container.
    out_path = save_path or (_MODEL_PATH if os.path.isdir("/app/models")
                              else _FALLBACK_MODEL_PATH)
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        import pickle
        with open(out_path, "wb") as fh:
            pickle.dump(bundle, fh)
    except Exception as exc:
        log.warning("predict_all_save_failed",
                    path=out_path, error=str(exc)[:200])
        return {"status": "save_failed", "path": out_path,
                "error": str(exc)[:200]}

    report = {
        "status":          "ok",
        "path":            out_path,
        "n_samples":       n,
        "trained_at":      bundle["trained_at"],
        "lookback_days":   lookback_days,
        "feature_count":   len(FEATURE_COLUMNS),
    }
    log.info("predict_all_trained", **report)
    return report


if __name__ == "__main__":  # pragma: no cover  — manual invocation only
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    print(train_from_history(lookback_days=days))
