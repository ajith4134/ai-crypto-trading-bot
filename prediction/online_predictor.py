"""Online (live-data-driven) predictor — cont. 63 (2026-05-29).

Implements the user-mandated architecture (2026-05-30):
  * Train on LIVE data only — each closed trade calls learn_from_close()
    which runs sklearn `partial_fit` on every prediction head. No batch
    historical training step required.
  * Predict on demand — when the scanner picks a pair, signals/engine.py
    calls predict_for_pair(pair) SYNCHRONOUSLY before deciding to open.
  * Hold-and-wait — when confidence < gate, the engine pushes the pair to
    `signals:pending_prediction` (see signals/replay_pool.py-like consumer
    at the top of process_signals) and re-predicts each subsequent tick
    until confidence clears or the pair times out.
  * CPU-only — uses sklearn (already in the image). No PyTorch / GPU /
    River / LightGBM dependency.

Heads (mirroring xgb_predictor for drop-in interface):
  direction        — SGDClassifier(loss="log_loss") → probability
  confidence_raw   — SGDClassifier(loss="log_loss") → probability
  entry_bps        — SGDRegressor
  sl_bps           — SGDRegressor
  tp_bps           — SGDRegressor
  hold_log_seconds — SGDRegressor
  rr               — SGDRegressor (single point estimate; quantiles would
                     need quantile regression which sklearn SGD does not
                     ship; we emit p25/p50/p75 as ±50 % around the mean
                     as a conservative bracket)

Cold-start posture: predict_for_pair() returns None until the predictor
has seen at least `_MIN_TRAINING_SAMPLES` closed trades. Until then the
signal-side gate falls through to legacy behaviour.

Persistence: pickled to /app/models/online_predictor.pkl every
`_PERSIST_EVERY_N_UPDATES` updates so a restart doesn't lose training.
"""
from __future__ import annotations

import math
import os
import pickle
import threading
import time
from typing import Optional

import structlog

log = structlog.get_logger()


_MODEL_PATH = "/app/models/online_predictor.pkl"
_FALLBACK_PATH = "/opt/trading-bot/models/online_predictor.pkl"
_MIN_TRAINING_SAMPLES = 200          # cold-start guard
_PERSIST_EVERY_N_UPDATES = 50         # disk save cadence
_FEATURE_LOCK = threading.Lock()      # protects model updates


class _OnlineBundle:
    """Encapsulates the 7 SGD models + the running StandardScaler so they
    train/predict consistently. Pickleable for disk persistence."""

    def __init__(self, n_features: int):
        # Lazy-import sklearn so module import succeeds even before deps land.
        from sklearn.linear_model import SGDClassifier, SGDRegressor
        from sklearn.preprocessing import StandardScaler

        self.n_features = n_features
        self.scaler = StandardScaler()
        self.scaler_fitted = False
        self.direction = SGDClassifier(loss="log_loss", alpha=1e-4,
                                        random_state=42)
        self.confidence_raw = SGDClassifier(loss="log_loss", alpha=1e-4,
                                             random_state=42)
        self.entry_bps = SGDRegressor(alpha=1e-4, random_state=42,
                                       learning_rate="adaptive", eta0=0.01)
        self.sl_bps = SGDRegressor(alpha=1e-4, random_state=42,
                                    learning_rate="adaptive", eta0=0.01)
        self.tp_bps = SGDRegressor(alpha=1e-4, random_state=42,
                                    learning_rate="adaptive", eta0=0.01)
        self.hold_log = SGDRegressor(alpha=1e-4, random_state=42,
                                      learning_rate="adaptive", eta0=0.01)
        self.rr = SGDRegressor(alpha=1e-4, random_state=42,
                                learning_rate="adaptive", eta0=0.01)
        self.n_updates = 0
        self.trained_at = int(time.time())

    @property
    def is_warmed_up(self) -> bool:
        return self.n_updates >= _MIN_TRAINING_SAMPLES


_bundle: Optional[_OnlineBundle] = None


def _resolve_path() -> str:
    """Prefer /app path (container view) when writable; fall back to /opt."""
    if os.path.exists("/app/models") or os.access("/app/models", os.W_OK):
        return _MODEL_PATH
    return _FALLBACK_PATH


def _load_bundle() -> Optional[_OnlineBundle]:
    """Load the pickled bundle from disk, or None if missing/corrupt."""
    global _bundle
    if _bundle is not None:
        return _bundle
    for path in (_MODEL_PATH, _FALLBACK_PATH):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as fh:
                _bundle = pickle.load(fh)
            log.info("online_predictor_loaded",
                     path=path, n_updates=_bundle.n_updates,
                     warmed=_bundle.is_warmed_up)
            return _bundle
        except Exception as exc:
            log.warning("online_predictor_load_failed",
                        path=path, error=str(exc)[:200])
            continue
    return None


def _ensure_bundle(n_features: int) -> _OnlineBundle:
    """Get-or-create the in-memory bundle. Initialises a fresh one when no
    persisted file exists."""
    global _bundle
    if _bundle is not None:
        if getattr(_bundle, "n_features", n_features) == n_features:
            return _bundle
        # cont. 70 Track 2 — feature-schema change (FEATURE_COLUMNS grew). A
        # persisted bundle at the old width would mismatch every to_vector input.
        # Discard + recreate fresh at the new width; the river model relearns
        # online within minutes. Self-heals on any future feature-set change.
        log.warning("online_predictor_reset_on_feature_change",
                    old_n=getattr(_bundle, "n_features", None), new_n=n_features)
        _bundle = None
    _bundle = _load_bundle()
    if _bundle is not None:
        if getattr(_bundle, "n_features", n_features) == n_features:
            return _bundle
        log.warning("online_predictor_reset_on_feature_change",
                    old_n=getattr(_bundle, "n_features", None), new_n=n_features)
        _bundle = None
    _bundle = _OnlineBundle(n_features=n_features)
    return _bundle


def _persist(bundle: _OnlineBundle) -> None:
    """Best-effort disk write. Failure ≡ in-memory state survives but a
    restart will rewind."""
    path = _resolve_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump(bundle, fh)
        log.info("online_predictor_persisted",
                 path=path, n_updates=bundle.n_updates)
    except Exception as exc:
        log.warning("online_predictor_persist_failed",
                    path=path, error=str(exc)[:200])


def is_ready() -> bool:
    """True when ≥ _MIN_TRAINING_SAMPLES live closed trades have updated
    the model. Cold otherwise — predict_for_pair returns None."""
    b = _load_bundle()
    return b is not None and b.is_warmed_up


def learn_from_close(feature_dict: dict, outcome: dict) -> None:
    """ONE incremental learning step. Called from execution/{paper,live}.py
    close paths once per closed trade. Wrapped in try/except — never raises.

    feature_dict keys: same as prediction.features.FEATURE_COLUMNS.
    outcome dict keys: direction (1/0), confidence (1/0), entry_bps, sl_bps,
        tp_bps, hold_log, rr — all numeric."""
    try:
        from prediction.features import FEATURE_COLUMNS, to_vector
        import numpy as _np

        x = _np.asarray([to_vector(feature_dict)], dtype=_np.float32)
        with _FEATURE_LOCK:
            bundle = _ensure_bundle(n_features=len(FEATURE_COLUMNS))
            # Fit / partial-fit the scaler first; sklearn's StandardScaler
            # supports partial_fit() for running mean/var updates.
            bundle.scaler.partial_fit(x)
            bundle.scaler_fitted = True
            x_scaled = bundle.scaler.transform(x)

            # Each head learns its own column. For the 2 binary classifiers
            # we MUST pass `classes=[0,1]` on the very first partial_fit so
            # sklearn knows the full label set.
            y_dir = _np.asarray([int(outcome.get("direction", 0))])
            y_conf = _np.asarray([int(outcome.get("confidence", 0))])
            first = bundle.n_updates == 0
            try:
                if first:
                    bundle.direction.partial_fit(x_scaled, y_dir,
                                                  classes=_np.asarray([0, 1]))
                    bundle.confidence_raw.partial_fit(x_scaled, y_conf,
                                                       classes=_np.asarray([0, 1]))
                else:
                    bundle.direction.partial_fit(x_scaled, y_dir)
                    bundle.confidence_raw.partial_fit(x_scaled, y_conf)
            except Exception as exc:
                log.warning("online_predictor_clf_partial_fit_failed",
                            error=str(exc)[:200])
            # Regression heads.
            for head, key in (
                (bundle.entry_bps, "entry_bps"),
                (bundle.sl_bps,    "sl_bps"),
                (bundle.tp_bps,    "tp_bps"),
                (bundle.hold_log,  "hold_log"),
                (bundle.rr,        "rr"),
            ):
                try:
                    y = _np.asarray([float(outcome.get(key, 0.0))])
                    head.partial_fit(x_scaled, y)
                except Exception as exc:
                    log.debug("online_predictor_reg_partial_fit_failed",
                              head=key, error=str(exc)[:120])
            bundle.n_updates += 1
            if bundle.n_updates % _PERSIST_EVERY_N_UPDATES == 0:
                _persist(bundle)
        try:
            import redis_client as _rc
            r = _rc.get()
            r.set("prediction:online:n_updates", bundle.n_updates)
            r.set("prediction:online:last_update_ts", int(time.time()))
            r.incr("prediction:online:learn_call_count")
        except Exception:
            pass
    except Exception as exc:
        log.warning("online_predictor_learn_failed",
                    error=str(exc)[:200])


def learn_from_candle_close(feature_dict: dict,
                             direction_label: int,
                             confidence_label: Optional[int] = None) -> None:
    """F50e (cont. 64) — self-supervised candle-close training step.

    Updates ONLY the direction + confidence_raw heads. SL/TP/RR/hold heads
    require trade outcomes (handled by learn_from_close). Counts toward
    n_updates so is_ready() warms from candle stream too.

    feature_dict: same FEATURE_COLUMNS schema as live_features().
    direction_label: 1 if next bar closed UP, 0 if DOWN.
    confidence_label: 1 if abs(next_bar_return) > 0.5×ATR (decisive move),
        else 0. Defaults to direction_label when None.
    """
    try:
        from prediction.features import FEATURE_COLUMNS, to_vector
        import numpy as _np

        x = _np.asarray([to_vector(feature_dict)], dtype=_np.float32)
        y_dir = _np.asarray([int(1 if direction_label else 0)])
        y_conf = _np.asarray([int(1 if (confidence_label
                                          if confidence_label is not None
                                          else direction_label) else 0)])

        with _FEATURE_LOCK:
            bundle = _ensure_bundle(n_features=len(FEATURE_COLUMNS))
            bundle.scaler.partial_fit(x)
            bundle.scaler_fitted = True
            x_scaled = bundle.scaler.transform(x)

            first = bundle.n_updates == 0
            try:
                if first:
                    bundle.direction.partial_fit(
                        x_scaled, y_dir, classes=_np.asarray([0, 1]))
                    bundle.confidence_raw.partial_fit(
                        x_scaled, y_conf, classes=_np.asarray([0, 1]))
                else:
                    bundle.direction.partial_fit(x_scaled, y_dir)
                    bundle.confidence_raw.partial_fit(x_scaled, y_conf)
            except Exception as exc:
                log.warning("online_predictor_candle_partial_fit_failed",
                            error=str(exc)[:200])
                return
            bundle.n_updates += 1
            if bundle.n_updates % _PERSIST_EVERY_N_UPDATES == 0:
                _persist(bundle)
        try:
            import redis_client as _rc
            r = _rc.get()
            r.set("prediction:online:n_updates", bundle.n_updates)
            r.set("prediction:online:last_candle_update_ts", int(time.time()))
            r.incr("prediction:online:candle_update_count")
        except Exception:
            pass
    except Exception as exc:
        log.warning("online_predictor_candle_learn_failed",
                    error=str(exc)[:200])


def predict_for_pair(pair: str,
                     signal_strength: Optional[float] = None,
                     trade_potential: Optional[float] = None) -> Optional[dict]:
    """SYNCHRONOUS per-pair prediction. Returns None if cold (not enough
    training samples) — caller (signals/engine.py) falls back to legacy.

    Output dict matches the xgb_predictor.predict() schema so the consumer
    can swap implementations transparently.
    """
    bundle = _load_bundle()
    if bundle is None or not bundle.is_warmed_up:
        return None
    try:
        from prediction.features import FEATURE_COLUMNS, live_features, to_vector
        import numpy as _np
        feats = live_features(pair, signal_strength=signal_strength,
                              trade_potential=trade_potential)
        x = _np.asarray([to_vector(feats)], dtype=_np.float32)
        if not bundle.scaler_fitted:
            return None
        x_scaled = bundle.scaler.transform(x)
        # Probabilities — sklearn SGDClassifier with log_loss exposes predict_proba.
        try:
            dir_prob = float(bundle.direction.predict_proba(x_scaled)[0, 1])
        except Exception:
            dir_prob = 0.5
        try:
            conf_raw = float(bundle.confidence_raw.predict_proba(x_scaled)[0, 1])
        except Exception:
            conf_raw = 0.5
        entry_bps = float(bundle.entry_bps.predict(x_scaled)[0])
        sl_bps    = abs(float(bundle.sl_bps.predict(x_scaled)[0]))
        tp_bps    = abs(float(bundle.tp_bps.predict(x_scaled)[0]))
        hold_log  = float(bundle.hold_log.predict(x_scaled)[0])
        rr_mean   = float(bundle.rr.predict(x_scaled)[0])
    except Exception as exc:
        log.warning("online_predictor_inference_failed",
                    pair=pair, error=str(exc)[:200])
        return None

    # Conformal calibration when available.
    conf_calibrated = conf_raw
    try:
        from ml.conformal_wrapper import calibrate_probability
        conf_calibrated = float(
            calibrate_probability(conf_raw, model="online_predictor"))
    except Exception:
        pass

    return {
        "predicted_direction":        "long" if dir_prob >= 0.5 else "short",
        "direction_prob":             round(dir_prob, 4),
        "predicted_entry_offset_bps": round(entry_bps, 2),
        "predicted_sl_offset_bps":    round(sl_bps, 2),
        "predicted_tp_offset_bps":    round(tp_bps, 2),
        "predicted_hold_seconds":     int(max(1, min(86400,
            2 ** max(-1, min(20, hold_log))))),
        "confidence_raw":             round(conf_raw, 4),
        "conformal_confidence":       round(conf_calibrated, 4),
        "predicted_rr_p25":           round(rr_mean * 0.5, 4),
        "predicted_rr_p50":           round(rr_mean, 4),
        "predicted_rr_p75":           round(rr_mean * 1.5, 4),
        "model_version":              f"online:{bundle.trained_at}:n{bundle.n_updates}",
        "predicted_at":               int(time.time()),
        "warmed_up":                  bundle.is_warmed_up,
        "n_training_samples":         bundle.n_updates,
    }


def state() -> dict:
    """Dashboard helper."""
    b = _load_bundle()
    if b is None:
        return {"loaded": False, "warmed_up": False, "n_updates": 0,
                "min_required": _MIN_TRAINING_SAMPLES}
    return {
        "loaded":         True,
        "warmed_up":      b.is_warmed_up,
        "n_updates":      b.n_updates,
        "min_required":   _MIN_TRAINING_SAMPLES,
        "trained_at":     b.trained_at,
        "scaler_fitted":  b.scaler_fitted,
    }


def features_from_trade(trade: dict, signal_row: Optional[dict] = None) -> dict:
    """Build a feature_dict from a closed-trade row at the time-of-open.
    Used by execution/{paper,live}.py close paths to feed learn_from_close().

    We reconstruct the live feature snapshot from:
      * signal_row.feature_vector JSON when present
      * trade-level fields (market_regime, signal_strength, trade_potential)
      * sensible zero-fill for missing live-only columns
    """
    import json
    from prediction.features import FEATURE_COLUMNS

    fv: dict = {}
    if signal_row and signal_row.get("feature_vector"):
        raw = signal_row["feature_vector"]
        try:
            fv = raw if isinstance(raw, dict) else json.loads(raw)
            if not isinstance(fv, dict):
                fv = {}
        except Exception:
            fv = {}
    out: dict = {col: float(fv.get(col, 0.0) or 0.0) for col in FEATURE_COLUMNS}
    if signal_row:
        if signal_row.get("signal_strength") is not None:
            try:
                out["signal_strength"] = float(signal_row["signal_strength"])
            except (TypeError, ValueError):
                pass
        if signal_row.get("trade_potential_score") is not None:
            try:
                out["trade_potential"] = float(signal_row["trade_potential_score"])
            except (TypeError, ValueError):
                pass
    regime = str(trade.get("market_regime") or "unknown").lower()
    out["regime_bull"]      = 1.0 if regime == "bull" else 0.0
    out["regime_bear"]      = 1.0 if regime == "bear" else 0.0
    out["regime_turbulent"] = 1.0 if regime == "turbulent" else 0.0
    if trade.get("pattern_cluster_id") is not None:
        try:
            out["pattern_cluster_id"] = float(trade["pattern_cluster_id"])
        except (TypeError, ValueError):
            pass
    return out


def outcome_from_trade(trade: dict) -> Optional[dict]:
    """Compute the 7 training targets from a closed trade row. Returns None
    when the row is unusable (no entry/capital/hold time)."""
    try:
        entry = float(trade.get("average_entry") or trade.get("entry_price") or 0)
        capital = float(trade.get("capital_usdt") or 0)
        leverage = float(trade.get("leverage") or 1)
        hold_s = int(trade.get("hold_time_seconds") or 0)
        net_pnl = float(trade.get("net_pnl_usdt") or 0)
        peak_pnl = float(trade.get("peak_pnl_usdt") or 0)
        exit_p = float(trade.get("exit_price") or 0)
        direction = (trade.get("direction") or "long").lower()
        if entry <= 0 or hold_s <= 0 or capital <= 0:
            return None
        notional = capital * leverage
        sl_bps = abs(net_pnl) / max(notional, 1) * 10_000.0
        tp_bps = (peak_pnl / max(notional, 1)) * 10_000.0 if peak_pnl > 0 else 0.0
        entry_bps = ((entry - float(trade.get("entry_price") or entry))
                     / max(entry, 1)) * 10_000.0
        rr = trade.get("actual_rr") or trade.get("predicted_rr") or 0
        try:
            rr_y = float(rr or 0)
        except (TypeError, ValueError):
            rr_y = 0.0
        if rr_y == 0 and sl_bps > 0:
            rr_y = tp_bps / sl_bps if sl_bps > 0 else 0.0
        rr_y = max(-5.0, min(20.0, rr_y))
        return {
            "direction":  1 if direction == "long" else 0,
            "confidence": 1 if net_pnl > 0 else 0,
            "entry_bps":  entry_bps,
            "sl_bps":     sl_bps,
            "tp_bps":     tp_bps,
            "hold_log":   math.log2(max(1, hold_s)),
            "rr":         rr_y,
        }
    except Exception as exc:
        log.debug("online_predictor_outcome_failed", error=str(exc)[:200])
        return None
