"""Walk-forward backtest harness (Phase F, cont. 63 2026-05-29).

Required gate before any new predict-all model gets promoted to live.

Splits the last N days of closed trades into K folds with a 2-week embargo
between train and test, retrains the predictor on each fold, evaluates on
the held-out window, and emits a per-fold report dict.

The report drives a manual promote decision: if ECE is monotonically below
0.15 across all folds AND RR_p50 prediction R² ≥ 0.20, the model is
considered safe to promote. The harness does NOT auto-promote — that
remains an explicit human action (file copy + restart) to enforce the
human-in-the-loop safety per the doc's "Forward-walk backtest required
before any predictor model goes live in paper" requirement.

Invocation:
  docker compose exec brain python -m pretrainer.walk_forward --days 90 --folds 5
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone

import structlog

log = structlog.get_logger()


_EMBARGO_DAYS = 14
_MIN_TRAIN_ROWS = 200
_MIN_TEST_ROWS = 50


def _fetch_window_rows(start_iso: str, end_iso: str) -> list[dict]:
    """Closed trades + matched signal feature_vectors within [start, end]."""
    from db import db_conn
    sql = """
        SELECT
            t.id, t.pair, t.direction, t.entry_price, t.average_entry,
            t.exit_price, t.trailing_sl_level,
            t.peak_pnl_usdt, t.net_pnl_usdt, t.capital_usdt, t.leverage,
            t.hold_time_seconds, t.market_regime, t.actual_rr, t.predicted_rr,
            t.pattern_cluster_id, t.exit_time,
            s.feature_vector, s.signal_strength, s.trade_potential_score
        FROM trades t
        LEFT JOIN signals s ON s.trade_id = t.id
        WHERE t.status     = 'closed'
          AND t.exit_time  >= %s AND t.exit_time < %s
          AND t.entry_price IS NOT NULL
          AND t.capital_usdt > 0
        ORDER BY t.exit_time ASC
    """
    rows: list[dict] = []
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (start_iso, end_iso))
                cols = [d[0] for d in cur.description]
                for raw in cur.fetchall():
                    rows.append(dict(zip(cols, raw)))
    except Exception as exc:
        log.warning("walk_forward_fetch_failed",
                    start=start_iso, end=end_iso, error=str(exc)[:200])
    return rows


def _evaluate_fold(model_bundle_path: str,
                   test_rows: list[dict]) -> dict:
    """Score the just-trained model on the test window. Computes:
      direction accuracy, confidence Brier, Expected Calibration Error,
      RR_p50 R². Returns a metrics dict."""
    try:
        import pickle
        with open(model_bundle_path, "rb") as fh:
            bundle = pickle.load(fh)
    except Exception as exc:
        return {"status": "bundle_load_failed", "error": str(exc)[:200]}

    from prediction.xgb_predictor import _row_to_features_and_targets

    try:
        import numpy as _np
    except Exception:
        return {"status": "numpy_missing"}

    X_list, T_list = [], []
    for r in test_rows:
        pair = _row_to_features_and_targets(r)
        if pair is None:
            continue
        X_list.append(pair[0])
        T_list.append(pair[1])
    if len(X_list) < _MIN_TEST_ROWS:
        return {"status": "insufficient_test_rows",
                "n_test": len(X_list), "needed": _MIN_TEST_ROWS}
    X = _np.asarray(X_list, dtype=_np.float32)
    dir_y = _np.asarray([t["direction"] for t in T_list])
    conf_y = _np.asarray([t["confidence"] for t in T_list])
    rr_y = _np.asarray([t["rr"] for t in T_list])

    dir_pred_prob = bundle["direction"].predict_proba(X)[:, 1]
    conf_pred_prob = bundle["confidence_raw"].predict_proba(X)[:, 1]
    rr_q50 = bundle["rr_q50"].predict(X)

    # Direction accuracy
    dir_acc = float(_np.mean((dir_pred_prob >= 0.5) == (dir_y == 1)))
    # Brier
    brier = float(_np.mean((conf_pred_prob - conf_y) ** 2))
    # ECE — 10-bin estimator
    bins = _np.linspace(0, 1, 11)
    ece = 0.0
    for i in range(10):
        lo, hi = bins[i], bins[i + 1]
        mask = (conf_pred_prob >= lo) & (conf_pred_prob < hi if i < 9
                                          else conf_pred_prob <= hi)
        if int(mask.sum()) > 0:
            bin_conf = float(conf_pred_prob[mask].mean())
            bin_acc = float(conf_y[mask].mean())
            ece += (mask.sum() / len(conf_y)) * abs(bin_acc - bin_conf)
    # RR R²
    ss_res = float(_np.sum((rr_y - rr_q50) ** 2))
    ss_tot = float(_np.sum((rr_y - rr_y.mean()) ** 2))
    rr_r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    return {
        "status":           "ok",
        "n_test":           len(X_list),
        "direction_acc":    round(dir_acc, 4),
        "confidence_brier": round(brier, 4),
        "ece":              round(ece, 4),
        "rr_p50_r2":        round(rr_r2, 4),
    }


def run(days: int = 90, folds: int = 5) -> dict:
    """Walk-forward driver. Returns a report dict suitable for human review."""
    if folds < 2:
        return {"status": "folds_too_small", "folds": folds}
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    fold_size = timedelta(days=days // folds)
    embargo = timedelta(days=_EMBARGO_DAYS)

    fold_reports: list[dict] = []
    for k in range(folds):
        # Test window is fold k. Train window is everything strictly BEFORE
        # the embargo before the test window.
        test_start = start + fold_size * k
        test_end = test_start + fold_size
        train_end = test_start - embargo
        if train_end <= start:
            fold_reports.append({
                "fold": k, "status": "no_train_window_available"})
            continue

        train_rows = _fetch_window_rows(start.isoformat(),
                                         train_end.isoformat())
        test_rows = _fetch_window_rows(test_start.isoformat(),
                                        test_end.isoformat())
        if len(train_rows) < _MIN_TRAIN_ROWS:
            fold_reports.append({
                "fold": k, "status": "insufficient_train",
                "n_train": len(train_rows)})
            continue

        # Train on this fold to a temp bundle path so we don't clobber the
        # production model.
        from prediction.xgb_predictor import _row_to_features_and_targets, _MIN_TRAIN_SAMPLES
        try:
            import numpy as _np
            from xgboost import XGBClassifier, XGBRegressor
        except Exception as exc:
            return {"status": "xgboost_missing", "error": str(exc)[:200]}
        X_list, T_list = [], []
        for r in train_rows:
            pair = _row_to_features_and_targets(r)
            if pair is None:
                continue
            X_list.append(pair[0])
            T_list.append(pair[1])
        if len(X_list) < _MIN_TRAIN_SAMPLES:
            fold_reports.append({
                "fold": k, "status": "insufficient_after_filter",
                "n_train": len(X_list)})
            continue
        X = _np.asarray(X_list, dtype=_np.float32)

        from prediction.features import FEATURE_COLUMNS
        bundle = {
            "feature_columns": FEATURE_COLUMNS,
            "trained_at":      int(time.time()),
            "n_samples":       X.shape[0],
            "fold":            k,
        }
        try:
            def _clf():
                return XGBClassifier(max_depth=4, n_estimators=120,
                                      learning_rate=0.1,
                                      objective="binary:logistic",
                                      tree_method="hist", n_jobs=4,
                                      use_label_encoder=False, verbosity=0)
            def _reg():
                return XGBRegressor(max_depth=4, n_estimators=120,
                                     learning_rate=0.1,
                                     objective="reg:squarederror",
                                     tree_method="hist", n_jobs=4, verbosity=0)
            def _quant(a):
                return XGBRegressor(max_depth=4, n_estimators=120,
                                     learning_rate=0.1,
                                     objective="reg:quantileerror",
                                     quantile_alpha=a,
                                     tree_method="hist", n_jobs=4, verbosity=0)
            bundle["direction"]      = _clf().fit(
                X, _np.asarray([t["direction"] for t in T_list]))
            bundle["confidence_raw"] = _clf().fit(
                X, _np.asarray([t["confidence"] for t in T_list]))
            bundle["entry_bps"]      = _reg().fit(
                X, _np.asarray([t["entry_bps"] for t in T_list]))
            bundle["sl_bps"]         = _reg().fit(
                X, _np.asarray([t["sl_bps"] for t in T_list]))
            bundle["tp_bps"]         = _reg().fit(
                X, _np.asarray([t["tp_bps"] for t in T_list]))
            bundle["hold_log_seconds"] = _reg().fit(
                X, _np.asarray([t["hold_log"] for t in T_list]))
            rr_arr = _np.asarray([t["rr"] for t in T_list])
            bundle["rr_q25"] = _quant(0.25).fit(X, rr_arr)
            bundle["rr_q50"] = _quant(0.50).fit(X, rr_arr)
            bundle["rr_q75"] = _quant(0.75).fit(X, rr_arr)
        except Exception as exc:
            fold_reports.append({"fold": k, "status": "fit_failed",
                                 "error": str(exc)[:200]})
            continue

        tmp_path = f"/tmp/predict_all_xgb_fold{k}.pkl"
        try:
            import pickle
            with open(tmp_path, "wb") as fh:
                pickle.dump(bundle, fh)
        except Exception as exc:
            fold_reports.append({"fold": k, "status": "save_failed",
                                 "error": str(exc)[:200]})
            continue

        metrics = _evaluate_fold(tmp_path, test_rows)
        metrics["fold"] = k
        metrics["train_window"] = (start.isoformat(), train_end.isoformat())
        metrics["test_window"] = (test_start.isoformat(), test_end.isoformat())
        metrics["n_train"] = len(X_list)
        fold_reports.append(metrics)
        # Don't keep the per-fold bundle file around.
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    # Promotion decision (advisory only — human still copies the file).
    ok_folds = [f for f in fold_reports if f.get("status") == "ok"]
    promote_ok = (
        len(ok_folds) >= max(2, folds - 1)
        and all(f["ece"] <= 0.15 for f in ok_folds)
        and all(f["rr_p50_r2"] >= 0.20 for f in ok_folds)
    )
    report = {
        "days":              days,
        "folds":             folds,
        "embargo_days":      _EMBARGO_DAYS,
        "fold_reports":      fold_reports,
        "promote_recommended": promote_ok,
        "promote_reason":    ("all folds: ECE<=0.15 AND R²>=0.20"
                              if promote_ok else
                              "one or more fold thresholds not met"),
        "generated_at":      int(time.time()),
    }
    print(json.dumps(report, indent=2, default=str))
    return report


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--folds", type=int, default=5)
    args = p.parse_args()
    run(days=args.days, folds=args.folds)


if __name__ == "__main__":
    main()
