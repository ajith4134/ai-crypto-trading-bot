"""
Kline-derived features for the reframed predict-all model — P3 of
next_impl/kline_corpus_model_reframe.md (cont. 69i).

Why a NEW feature set: the old predict-all used the 32-col live FEATURE_COLUMNS
(ofi/vpin/sentiment/cn_* …) which can't be reconstructed historically → it was
forced to train on our sparse, regime-skewed trade log. These features are computed
ONLY from OHLCV, so the SAME function builds the training matrix from the kline corpus
AND the live inference vector — no train/serve skew, unlimited regime-robust data.

All features at index i use ONLY bars ≤ i (leak-safe).
"""
from __future__ import annotations

import numpy as np

# Fixed feature order (train == serve). Keep in sync with FEATURE_NAMES.
FEATURE_NAMES = (
    "ret_1", "ret_3", "ret_6", "ret_12", "ret_24",
    "vol_12", "vol_24",
    "rsi_14",
    "ema_dist_20",
    "range_1",
    "vol_ratio_20",
    "mom_accel",
    "hl_pos_24",
    "ret_skew_24",
    # cont. 69q — price-structure (support/resistance + prior-window levels).
    # All OHLCV-derived + leak-safe, so the train==serve invariant holds.
    "dist_to_hi_50",     # (50-bar high - close)/close ≥ 0 — room below resistance
    "dist_to_lo_50",     # (close - 50-bar low)/close ≥ 0 — room above support
    "dist_to_prev_hi",   # distance to prior-window high (≈ "previous session" high)
    "dist_to_prev_lo",   # distance to prior-window low  (≈ "previous session" low)
)
N_FEATURES = len(FEATURE_NAMES)
_SR_LOOKBACK = 50      # bars for recent support/resistance range
_PREV_WIN = 24         # prior-window size for "previous session" high/low proxy
_WARMUP = 51           # bars needed before the first valid row (covers 50-bar S/R)


def _rsi(close: np.ndarray, i: int, period: int = 14) -> float:
    if i < period:
        return 50.0
    d = np.diff(close[i - period:i + 1])
    up = d[d > 0].sum()
    dn = -d[d < 0].sum()
    if dn == 0:
        return 100.0 if up > 0 else 50.0
    rs = (up / period) / (dn / period)
    return 100.0 - 100.0 / (1.0 + rs)


def _ema(close: np.ndarray, i: int, span: int = 20) -> float:
    if i < 1:
        return float(close[i])
    k = 2.0 / (span + 1.0)
    start = max(0, i - 3 * span)
    e = float(close[start])
    for j in range(start + 1, i + 1):
        e = close[j] * k + e * (1 - k)
    return e


def features_at(o, h, l, c, v, i: int) -> list[float] | None:
    """Feature vector at bar i (leak-safe). None if insufficient warmup."""
    if i < _WARMUP or i >= len(c):
        return None
    def _ret(k):
        p = c[i - k]
        return float(c[i] / p - 1.0) if p > 0 else 0.0
    logret = np.diff(np.log(np.clip(c[i - 24:i + 1], 1e-12, None)))
    vol12 = float(np.std(logret[-12:])) if len(logret) >= 12 else 0.0
    vol24 = float(np.std(logret)) if len(logret) else 0.0
    ema20 = _ema(c, i, 20)
    ema_dist = float((c[i] - ema20) / ema20) if ema20 > 0 else 0.0
    rng = float((h[i] - l[i]) / c[i]) if c[i] > 0 else 0.0
    vmean = float(np.mean(v[i - 20:i])) if i >= 20 else float(np.mean(v[:i + 1]))
    vol_ratio = float(v[i] / vmean) if vmean > 0 else 1.0
    mom_accel = _ret(3) - _ret(6)
    win_h = float(np.max(h[i - 24:i + 1])); win_l = float(np.min(l[i - 24:i + 1]))
    hl_pos = float((c[i] - win_l) / (win_h - win_l)) if win_h > win_l else 0.5
    skew = float(np.mean((logret - logret.mean()) ** 3)) if len(logret) > 2 else 0.0
    # cont. 69q — price-structure: recent S/R range + prior-window ("prev session")
    # extremes. All use bars ≤ i (leak-safe). Distances normalised by close.
    px = c[i] if c[i] > 0 else 1.0
    hi_50 = float(np.max(h[i - (_SR_LOOKBACK - 1):i + 1]))
    lo_50 = float(np.min(l[i - (_SR_LOOKBACK - 1):i + 1]))
    dist_to_hi_50 = (hi_50 - c[i]) / px
    dist_to_lo_50 = (c[i] - lo_50) / px
    # Prior window = [i-2W : i-W] (the session BEFORE the current one).
    prev_hi = float(np.max(h[i - 2 * _PREV_WIN:i - _PREV_WIN]))
    prev_lo = float(np.min(l[i - 2 * _PREV_WIN:i - _PREV_WIN]))
    dist_to_prev_hi = (prev_hi - c[i]) / px
    dist_to_prev_lo = (c[i] - prev_lo) / px
    return [_ret(1), _ret(3), _ret(6), _ret(12), _ret(24),
            vol12, vol24, _rsi(c, i), ema_dist, rng, vol_ratio,
            mom_accel, hl_pos, skew,
            dist_to_hi_50, dist_to_lo_50, dist_to_prev_hi, dist_to_prev_lo]


def feature_matrix(o, h, l, c, v, step: int = 1):
    """Build (X, indices) over a full series for TRAINING. `step` subsamples to
    decorrelate adjacent overlapping windows (default 1 = every bar)."""
    X, idx = [], []
    for i in range(_WARMUP, len(c), step):
        f = features_at(o, h, l, c, v, i)
        if f is not None and all(np.isfinite(f)):
            X.append(f)
            idx.append(i)
    return np.asarray(X, dtype=np.float32), np.asarray(idx, dtype=np.int64)
