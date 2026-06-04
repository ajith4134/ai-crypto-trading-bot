"""Feature engineering for the predict-all XGBoost model — cont. 63 (2026-05-29).

Pulls per-symbol features from Redis (live, sub-second freshness) and the
trades / signals / counterfactuals tables (historical, for training).

Feature columns must be IDENTICAL between training time (offline, pretrainer/
walk_forward.py) and inference time (prediction/refresh_loop.py) — that
invariant is enforced by exporting FEATURE_COLUMNS as a frozen tuple at the
bottom of this module.

Categorical features (regime, direction) are one-hot encoded at the bottom.
Missing features default to 0 (XGBoost handles NaN natively; 0 is a safer
signal-of-absence for this codebase).
"""
from __future__ import annotations

from typing import Iterable, Optional

import structlog

import redis_client
import redis_keys

log = structlog.get_logger()


# Ordered feature list — DO NOT REORDER without retraining the model.
# Add new features by APPENDING only.
FEATURE_COLUMNS: tuple[str, ...] = (
    # Microstructure (60 s TTL Redis keys)
    "ofi",                # order-flow imbalance signed
    "vpin",               # VPIN toxicity
    "bid_ask_imbalance",
    # Realised volatility
    "vol_unit",
    "atr_norm",           # ATR / mark
    # Sentiment + regime
    "sentiment",          # [-1, +1] CryptoBERT+FinBERT or proxy
    "regime_bull",        # one-hot
    "regime_bear",
    "regime_turbulent",
    # Cross-asset (F45)
    "xsmom_rank",         # [0,1] percentile
    "xsmom_return_7d",
    # Cross-asset (F52)
    "exchange_netflow_z",
    # Liquidations (F58)
    "liq_nearest_above_pct",
    "liq_nearest_below_pct",
    "liq_cascade_prob",
    # On-chain (optional, may be 0)
    "funding_rate",
    "change_24h",
    # Direction prior (signal_strength when available)
    "signal_strength",
    "trade_potential",
    # Pattern cluster id (treated as ordinal — XGBoost handles this fine)
    "pattern_cluster_id",
    # cont. 65 — CandleNet multi-TF context (12 features).
    # The literal "predict entry direction based on last 5m/15m/1h candle
    # chart" feed. Each TF contributes (direction probability, magnitude,
    # trend strength) read from {pair}:{tf}:candle_forecast. 1m uses
    # short-horizon (dir1/mag1); 5m/15m/1h use mid-horizon (dir3/mag3).
    # 0-fill when forecast missing — XGBoost handles this natively and the
    # signal-of-absence is semantically meaningful here.
    "cn_1m_dir1",
    "cn_1m_mag1",
    "cn_1m_trend",
    "cn_5m_dir3",
    "cn_5m_mag3",
    "cn_5m_trend",
    "cn_15m_dir3",
    "cn_15m_mag3",
    "cn_15m_trend",
    "cn_1h_dir3",
    "cn_1h_mag3",
    "cn_1h_trend",
    # cont. 70 Track 2 — microstructure derivatives micro_ws/cvd_producer already
    # emit live but the model never saw. cvd_z = CVD z-scored over its rolling
    # history (raw cvd scale varies ~200x/pair → unusable un-normalized); ofi_l1 =
    # top-of-book OFI in [-1,1] ({pair}:micro:ofi_l1); ofi_accel = OFI acceleration
    # ({pair}:micro:ofi_accel). These have NO historical reconstruction (same
    # reason kline_features is OHLCV-only) → 0-fill in the offline training vector;
    # the ONLINE river learner gets them live via the live_features snapshot
    # (train==serve preserved). cont.69v: depth/OFI/CVD is where the real edge is.
    "cvd_z",
    "ofi_l1",
    "ofi_accel",
)


def _f(key: str, default: float = 0.0) -> float:
    try:
        v = redis_client.get().get(key)
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_get(key: str, default: str = "") -> str:
    try:
        v = redis_client.get().get(key)
        if v is None:
            return default
        if isinstance(v, bytes):
            return v.decode("utf-8", errors="ignore")
        return str(v)
    except Exception:
        return default


def _cvd_z(pair: str) -> float:
    """cont. 70 — normalize raw CVD (`{pair}:cvd_now`) by the std of its rolling
    history (`{pair}:cvd_history`, last 100 1m deltas). Raw CVD scale varies ~200x
    across pairs (BTC ~-600 vs ETH ~-120k) so the raw value is useless as a
    cross-pair feature; the z-score is comparable. Clipped to [-5, 5]. Returns 0.0
    when history is too short — honest signal-of-absence, not a synthesized value."""
    try:
        r = redis_client.get()
        now_raw = r.get(f"{pair}:cvd_now")
        if now_raw is None:
            return 0.0
        now = float(now_raw)
        hist = r.lrange(f"{pair}:cvd_history", 0, 99) or []
        vals = []
        for v in hist:
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                continue
        if len(vals) < 10:
            return 0.0
        mean = sum(vals) / len(vals)
        std = (sum((x - mean) ** 2 for x in vals) / len(vals)) ** 0.5
        if std <= 1e-9:
            return 0.0
        return max(-5.0, min(5.0, (now - mean) / std))
    except Exception:
        return 0.0


def live_features(pair: str,
                  signal_strength: Optional[float] = None,
                  trade_potential: Optional[float] = None) -> dict:
    """Build the feature dict for `pair` from live Redis state. Returns a dict
    keyed by FEATURE_COLUMNS. Missing values default to 0.

    Used by prediction/refresh_loop at inference time. The same function is
    re-used by pretrainer/walk_forward to ensure train/serve parity (with a
    historical-state-snapshot wrapper there)."""
    regime = _safe_get(redis_keys.CURRENT_REGIME, "unknown").lower()
    mark = _f(redis_keys.MARK_PRICE.replace("{pair}", pair))
    atr = _f(f"{pair}:atr")
    atr_norm = (atr / mark) if mark > 0 else 0.0

    # Pattern cluster — pattern/clusterer.py persists at runtime as a Redis
    # hash key. Falls back to -1 (sentinel for "no cluster assigned").
    try:
        cluster_id = int(redis_client.get().get(
            f"pattern:cluster_id:{pair}") or -1)
    except (TypeError, ValueError):
        cluster_id = -1

    f: dict[str, float] = {col: 0.0 for col in FEATURE_COLUMNS}
    f["ofi"]                     = _f(redis_keys.OFI.replace("{pair}", pair))
    f["vpin"]                    = _f(redis_keys.VPIN.replace("{pair}", pair))
    f["bid_ask_imbalance"]       = _f(redis_keys.BID_ASK_IMBALANCE.replace(
        "{pair}", pair))
    # cont. 70 Track 2 — microstructure derivatives (live-only; 0-fill historical).
    f["ofi_l1"]                  = _f(f"{pair}:micro:ofi_l1")
    f["ofi_accel"]               = _f(f"{pair}:micro:ofi_accel")
    f["cvd_z"]                   = _cvd_z(pair)
    f["vol_unit"]                = atr_norm if atr_norm > 0 else 0.01
    f["atr_norm"]                = atr_norm
    f["sentiment"]               = _f(redis_keys.SENTIMENT_PAIR.replace(
        "{pair}", pair),
        default=_f(redis_keys.SENTIMENT_GLOBAL, default=0.0))
    f["regime_bull"]             = 1.0 if regime == "bull" else 0.0
    f["regime_bear"]             = 1.0 if regime == "bear" else 0.0
    f["regime_turbulent"]        = 1.0 if regime == "turbulent" else 0.0
    f["xsmom_rank"]              = _f(
        redis_keys.XSMOM_RANK.replace("{pair}", pair))
    f["xsmom_return_7d"]         = _f(
        redis_keys.XSMOM_RETURN_7D.replace("{pair}", pair))
    f["exchange_netflow_z"]      = _f(
        redis_keys.EXCHANGE_NETFLOW_Z.replace("{pair}", pair))
    f["liq_nearest_above_pct"]   = _f(
        redis_keys.LIQ_NEAREST_ABOVE_PCT.replace("{pair}", pair))
    f["liq_nearest_below_pct"]   = _f(
        redis_keys.LIQ_NEAREST_BELOW_PCT.replace("{pair}", pair))
    f["liq_cascade_prob"]        = _f(
        redis_keys.LIQ_CASCADE_PROB.replace("{pair}", pair))
    f["funding_rate"]            = _f(
        redis_keys.FUNDING_RATE.replace("{pair}", pair))
    f["change_24h"]              = _f(
        redis_keys.TICKER_CHANGE_24H.replace("{pair}", pair))
    if signal_strength is not None:
        try:
            f["signal_strength"] = float(signal_strength)
        except (TypeError, ValueError):
            pass
    if trade_potential is not None:
        try:
            f["trade_potential"] = float(trade_potential)
        except (TypeError, ValueError):
            pass
    f["pattern_cluster_id"]      = float(cluster_id)

    # cont. 65 — CandleNet multi-TF context. Read {pair}:{tf}:candle_forecast
    # JSON (written by celery_app.candlenet_infer_all every 60s on the
    # dedicated `candlenet` queue, TTL 300s). 0-fill when missing — that
    # state is honest signal-of-absence, not synthetic data.
    import json as _json
    r = redis_client.get()
    # cont. 66: 30m added for dashboard visibility (CN-30m column). Deliberately
    # NOT in FEATURE_COLUMNS so the trained XGBoost predict-all vector keeps its
    # fixed shape — this only enriches the feature_vector SNAPSHOT stored on the
    # trade (which the dashboard reads), not the model input.
    for tf, dir_key, mag_key in (
        ("1m",  "dir1", "mag1"),
        ("5m",  "dir3", "mag3"),
        ("15m", "dir3", "mag3"),
        ("30m", "dir3", "mag3"),
        ("1h",  "dir3", "mag3"),
    ):
        try:
            raw = r.get(f"{pair}:{tf}:candle_forecast")
            if not raw:
                continue
            fc = _json.loads(raw)
            if not isinstance(fc, dict):
                continue
            f[f"cn_{tf}_{dir_key}"]  = float(fc.get(dir_key, 0.0) or 0.0)
            f[f"cn_{tf}_{mag_key}"]  = float(fc.get(mag_key, 0.0) or 0.0)
            f[f"cn_{tf}_trend"]      = float(fc.get("trend", 0.0) or 0.0)
        except (TypeError, ValueError, _json.JSONDecodeError):
            continue
    return f


def to_vector(features: dict) -> list[float]:
    """Stable ordering — pulls values in FEATURE_COLUMNS order. Used to feed
    XGBoost which expects a fixed-shape input."""
    return [float(features.get(col, 0.0) or 0.0) for col in FEATURE_COLUMNS]


def to_matrix(rows: Iterable[dict]) -> list[list[float]]:
    """Batch helper for training."""
    return [to_vector(r) for r in rows]
