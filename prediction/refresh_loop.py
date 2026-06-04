"""Prediction refresh loop — cont. 63 (2026-05-29).

Phase B worker. Every 30 s pulls the top-N scanner-ranked symbols, runs the
trained XGBoost (or Transformer) predictor over each, writes the result to:
  * Redis hash `predictions:{symbol}` (TTL 90 s) — read by signals/engine.py
    at signal-firing time
  * Postgres `predictions` table — for downstream pattern_effectiveness +
    drift-monitor consumers

Cold-start posture: when no trained model is loaded, refresh() writes a
sentinel row signalling "predictor cold" and the signal-side gate falls back
to legacy behaviour. No silent failures — every cold tick logs +
`prediction:refresh:cold_tick_count` increments.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Optional

import structlog

import redis_client
import redis_keys

log = structlog.get_logger()


_DEFAULT_TOP_N = 30
_HASH_TTL_S = 90
_REDIS_HASH_KEY = "predictions:{symbol}"


def _resolve_top_n() -> int:
    try:
        v = redis_client.get().get("prediction:refresh_top_n")
        if v is not None:
            return max(5, min(100, int(v)))
    except (TypeError, ValueError):
        pass
    return _DEFAULT_TOP_N


def _resolve_active_pairs(limit: int) -> list[str]:
    """Top-N pairs from scanner. Falls back to ACTIVE_PAIRS set when ranked
    list is absent. Anchors always included up front."""
    r = redis_client.get()
    out: list[str] = []
    try:
        anchors = list(r.smembers(redis_keys.SCANNER_ANCHOR_PAIRS) or ())
        anchors = [a.decode("utf-8") if isinstance(a, bytes) else a
                   for a in anchors]
        for a in anchors:
            if a and a not in out:
                out.append(a)
                if len(out) >= limit:
                    return out
    except Exception:
        pass
    try:
        active = list(r.smembers(redis_keys.ACTIVE_PAIRS) or ())
        active = [a.decode("utf-8") if isinstance(a, bytes) else a
                  for a in active]
        for p in active:
            if p and p not in out:
                out.append(p)
                if len(out) >= limit:
                    return out
    except Exception:
        pass
    return out


def _persist_prediction(symbol: str, pred: dict, mark: float) -> Optional[str]:
    """Write the prediction to both Redis and Postgres. Returns the
    prediction's UUID for downstream join when the trade opens."""
    pred_id = str(uuid.uuid4())
    pred_full = dict(pred)
    pred_full["id"] = pred_id
    pred_full["mark_at_predict"] = mark
    # Redis hash (consumed by signals/engine.py soft gate).
    try:
        r = redis_client.get()
        r.setex(_REDIS_HASH_KEY.format(symbol=symbol),
                _HASH_TTL_S, json.dumps(pred_full))
        r.incr("prediction:refresh:write_count")
    except Exception as exc:
        log.warning("prediction_refresh_redis_write_failed",
                    symbol=symbol, error=str(exc)[:120])

    # Postgres row (best-effort; not critical to predict-time flow).
    try:
        from db import db_conn
        import redis_keys as _rk
        current_regime = (redis_client.get().get(_rk.CURRENT_REGIME)
                          or b"unknown")
        if isinstance(current_regime, bytes):
            current_regime = current_regime.decode("utf-8", errors="ignore")
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO predictions
                      (id, symbol, tf_set, predicted_direction,
                       predicted_entry,
                       predicted_sl, predicted_tp, predicted_hold_seconds,
                       conformal_confidence, predicted_rr_p25,
                       predicted_rr_p50, predicted_rr_p75,
                       pattern_cluster_id, market_regime, model_version,
                       created_at, expires_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, NOW(),
                            NOW() + INTERVAL '90 seconds')
                    ON CONFLICT (id) DO NOTHING
                """, (
                    pred_id, symbol,
                    # cont. 65 — tf_set documents which TFs fed this prediction.
                    # When the candle features in prediction/features.py read
                    # all four `{pair}:{tf}:candle_forecast` JSONs, the answer
                    # is the literal "1m,5m,15m,1h" string.
                    "1m,5m,15m,1h",
                    pred.get("predicted_direction"),
                    _bps_to_price(mark, pred.get("predicted_entry_offset_bps"),
                                  pred.get("predicted_direction")),
                    _bps_to_price(mark, -abs(
                        pred.get("predicted_sl_offset_bps") or 0),
                                  pred.get("predicted_direction")),
                    _bps_to_price(mark, abs(
                        pred.get("predicted_tp_offset_bps") or 0),
                                  pred.get("predicted_direction")),
                    pred.get("predicted_hold_seconds"),
                    pred.get("conformal_confidence"),
                    pred.get("predicted_rr_p25"),
                    pred.get("predicted_rr_p50"),
                    pred.get("predicted_rr_p75"),
                    _safe_int(redis_client.get().get(
                        f"pattern:cluster_id:{symbol}"), default=None),
                    current_regime,
                    pred.get("model_version"),
                ))
                conn.commit()
    except Exception as exc:
        log.debug("prediction_refresh_db_write_skipped",
                  symbol=symbol, error=str(exc)[:200])
    return pred_id


def _bps_to_price(mark: float, bps: Optional[float],
                  direction: Optional[str]) -> Optional[float]:
    if mark is None or mark <= 0 or bps is None:
        return None
    try:
        b = float(bps)
    except (TypeError, ValueError):
        return None
    sign = 1.0 if (direction or "long").lower() == "long" else -1.0
    return round(mark * (1.0 + sign * b / 10_000.0), 8)


def _safe_int(v, default=None):
    if v is None:
        return default
    try:
        if isinstance(v, bytes):
            v = v.decode("utf-8", errors="ignore")
        return int(v)
    except (TypeError, ValueError):
        return default


def refresh() -> dict:
    """Single-tick refresh. Called by Celery beat every 30 s."""
    r = redis_client.get()
    enabled = (r.get("prediction:refresh_enabled") or b"1")
    if isinstance(enabled, bytes):
        enabled = enabled.decode("utf-8", errors="ignore")
    if enabled != "1":
        return {"status": "disabled"}

    from prediction.xgb_predictor import is_ready as _xgb_ready, predict as _xgb_predict
    from prediction.online_predictor import (is_ready as _online_ready,
                                              predict_for_pair as _online_predict)
    from prediction.features import live_features, to_vector

    xgb_on = _xgb_ready()
    online_on = _online_ready()
    if not xgb_on and not online_on:
        try:
            r.incr("prediction:refresh:cold_tick_count")
            r.set("prediction:refresh:last_cold_ts", int(time.time()))
        except Exception:
            pass
        # Emit ONE log every 10 cold ticks so the operator notices.
        try:
            cold_n = int(r.get("prediction:refresh:cold_tick_count") or 0)
            if cold_n % 10 == 0:
                log.info("prediction_refresh_cold_no_model",
                         cold_tick_count=cold_n,
                         msg=("train via pretrainer/walk_forward.py or wait "
                              "for F50e candle stream to warm online_predictor"))
        except Exception:
            pass
        return {"status": "cold", "n_pairs": 0}

    top_n = _resolve_top_n()
    pairs = _resolve_active_pairs(top_n)
    if not pairs:
        return {"status": "no_pairs"}

    n_written = 0
    n_failed = 0
    n_xgb = 0
    n_online = 0
    for symbol in pairs:
        try:
            mark = float(r.get(redis_keys.MARK_PRICE.replace(
                "{symbol}", symbol).replace("{pair}", symbol)) or 0)
            if mark <= 0:
                continue
            pred = None
            # XGBoost preferred when trained — calibrated quantiles + full
            # 7-head training. Otherwise fall back to the F50e-warmed online
            # predictor, which only needs the per-pair live feature vector.
            if xgb_on:
                feats = live_features(symbol)
                vec = to_vector(feats)
                pred = _xgb_predict(vec, pair=symbol)
                if pred is not None:
                    n_xgb += 1
            if pred is None and online_on:
                pred = _online_predict(symbol)
                if pred is not None:
                    n_online += 1
            if pred is None:
                n_failed += 1
                continue
            _persist_prediction(symbol, pred, mark)
            n_written += 1
        except Exception as exc:
            n_failed += 1
            log.warning("prediction_refresh_symbol_failed",
                        symbol=symbol, error=str(exc)[:200])
    try:
        r.set("prediction:refresh:last_n_xgb", n_xgb)
        r.set("prediction:refresh:last_n_online", n_online)
    except Exception:
        pass
    try:
        r.set("prediction:refresh:last_ok_ts", int(time.time()))
        r.set("prediction:refresh:last_written_count", n_written)
    except Exception:
        pass
    log.info("prediction_refresh_complete",
             n_pairs=len(pairs), n_written=n_written, n_failed=n_failed)
    return {"status": "ok", "n_pairs": len(pairs),
            "n_written": n_written, "n_failed": n_failed}
