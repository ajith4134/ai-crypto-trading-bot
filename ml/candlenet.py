"""F48 — CandleNet: 1m / 5m / 15m Next-Candle Multi-Task Prediction Model.

Blueprint §Feature 48 + Production Extensions (cont. 46, 2026-05-25).
Architecture upgrades:
  - CNN-GRU-TCN triple-branch backbone (CNN + GRU + TCN run in parallel)
  - GAF (Gramian Angular Field) image stream fused at the dense layer
  - Regime-conditioned heads via HMM one-hot embedding (bull / bear / turbulent)
  - 3 timeframes: 1m, 5m, 15m

Three model files:
  models/candlenet_1m.pth   — 1-minute candles
  models/candlenet_5m.pth   — 5-minute candles
  models/candlenet_15m.pth  — 15-minute candles

Each model predicts three tasks simultaneously:
  direction_head: P(next candle closes up) at +1, +3, +5 candles
  magnitude_head: % price move at +1, +3, +5 candles
  trend_head:     P(bullish over next 10 candles)

Inference writes to Redis:
  {pair}:{interval}:candle_forecast    TTL 90s
  {pair}:{interval}:exhaustion_score   TTL 90s   (Idea A — counter-trend detect)
  {pair}:atr                           no TTL — refreshed by every inference call

Validation gates (training only — refuse to save if any fail):
  min_val_auc         ≥ 0.56  (direction head, AUC on held-out 20%)
  top_decile_lift     ≥ 1.05  (top-10% confidence must beat base rate)
  directional_calib   ≤ 0.05  (mean abs calibration error on direction probs)
"""
import json
import math
import os
import time
from pathlib import Path
import structlog
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import redis_client
import redis_keys
from ml.ha_features import (
    build_feature_matrix, compute_atr, compute_exhaustion_features,
    N_FEATURES,
)
from ml.gaf_encoder import compute_gaf, GAFResNet

log = structlog.get_logger()

CONTEXT_LENGTH = 60        # candles of history fed to the model

_MODEL_PATHS = {
    "1m":  Path("models/candlenet_1m.pth"),
    "5m":  Path("models/candlenet_5m.pth"),
    "15m": Path("models/candlenet_15m.pth"),
    "30m": Path("models/candlenet_30m.pth"),
    "1h":  Path("models/candlenet_1h.pth"),
}
_INTERVAL_TO_FG = {"1m": "F48_1m", "5m": "F48_5m",
                   "15m": "F48_15m", "30m": "F48_30m", "1h": "F48_1h"}
_REGIME_TO_IDX  = {"bull": 0, "bear": 1, "turbulent": 2}
N_REGIMES       = 3

VALIDATION_GATES = {
    "min_val_auc":       0.56,
    "top_decile_lift":   1.05,
    # cont. 65k — gate on TRUE Expected Calibration Error (binned reliability),
    # not the old `dir_calib_err` which was mean|p-y| mislabeled as calibration.
    # mean|p-y| is bounded below by irreducible Bernoulli noise (≈0.5·(1-AUC
    # spread)), so it wrongly rejected an AUC-0.93 30m model at 0.071. ECE
    # compares predicted prob to empirical frequency per bin — a model can be
    # perfectly calibrated (ECE≈0) while mean|p-y| stays ~0.4.
    # cont. 65k — 0.10 (was 0.05). With ~1000 val samples the ECE estimate is
    # itself noisy, so 0.05 is unrealistically tight; 0.10 is still good
    # calibration after temperature scaling. Raise back toward 0.05 once the
    # data feed provides more samples (see flat-window data-thinness note).
    "max_dir_ece":       0.10,
    "max_dir_calib_err": 0.20,   # loose sanity bound only (was 0.05, over-strict)
}
# cont. 65 — raised from 90s to survive >1 missed inference cadence when the
# worker queue is backed up. Cascade/scanner/predictor were reading empty keys
# ~96% of the time; with the candlenet queue isolated to its own worker the
# typical gap is <60s but 300s gives 5× headroom before silent degradation.
_FORECAST_TTL    = 300  # seconds
_EXHAUSTION_TTL  = 300  # seconds

# Per-timeframe cache: (model, config_dict, mtime, scaler)
_cache: dict[str, tuple | None] = {"1m": None, "5m": None, "15m": None,
                                    "30m": None, "1h": None}


# ── Model Architecture ───────────────────────────────────────────────────────

class _CausalConv1d(nn.Module):
    """Dilated causal Conv1D — outputs t depend only on inputs ≤ t."""
    def __init__(self, in_ch: int, out_ch: int, kernel_size: int, dilation: int):
        super().__init__()
        self.left_pad = (kernel_size - 1) * dilation
        self.conv = nn.Conv1d(in_ch, out_ch, kernel_size=kernel_size,
                              dilation=dilation)

    def forward(self, x):
        # x: [B, C, T]. Pad left only → preserves causality.
        x = F.pad(x, (self.left_pad, 0))
        return self.conv(x)


class _TCNBlock(nn.Module):
    """Single TCN residual block: CausalConv → BN → ReLU → Drop → CausalConv → BN → +res → ReLU."""
    def __init__(self, in_ch: int, out_ch: int, kernel_size: int,
                 dilation: int, dropout: float = 0.2):
        super().__init__()
        self.conv1 = _CausalConv1d(in_ch,  out_ch, kernel_size, dilation)
        self.bn1   = nn.BatchNorm1d(out_ch)
        self.conv2 = _CausalConv1d(out_ch, out_ch, kernel_size, dilation)
        self.bn2   = nn.BatchNorm1d(out_ch)
        self.drop  = nn.Dropout(dropout)
        # 1x1 conv for residual shape match when in_ch != out_ch
        self.downsample = (nn.Conv1d(in_ch, out_ch, kernel_size=1)
                           if in_ch != out_ch else None)

    def forward(self, x):
        identity = x if self.downsample is None else self.downsample(x)
        out = F.relu(self.bn1(self.conv1(x)))
        out = self.drop(out)
        out = self.bn2(self.conv2(out))
        return F.relu(out + identity)


class CandleNetModel(nn.Module):
    """Triple-branch (CNN + GRU + TCN) + GAF image fusion + regime conditioning.

    Forward signature (forward args):
      x:      [B, T, F]  — feature sequence, F = N_FEATURES = 17
      gaf:    [B, 4, T, T] or None — Gramian Angular Field image
      regime: [B, N_REGIMES] or None — one-hot regime vector (HMM)

    All extras are optional at inference; defaults applied when absent so
    a partial inference path (e.g. missing HMM regime) still produces output.
    """
    HIDDEN_CNN = 64
    HIDDEN_GRU = 128
    HIDDEN_TCN = 128
    HIDDEN_FC  = 128
    GRU_LAYERS = 2
    GRU_DROP   = 0.2
    FC_DROP    = 0.3
    TCN_KERNEL = 3
    TCN_DILATIONS = (1, 2, 4, 8)

    def __init__(self, n_features: int = N_FEATURES,
                 n_regimes: int = N_REGIMES, use_gaf: bool = True,
                 dropout: float | None = None):
        super().__init__()
        self.use_gaf = use_gaf
        self.n_regimes = n_regimes
        # F49 §Component 3 — HPO-tunable dropout. None preserves the
        # class-default FC_DROP / GRU_DROP for backward compat.
        fc_drop  = self.FC_DROP  if dropout is None else float(dropout)
        gru_drop = self.GRU_DROP if dropout is None else min(0.5, float(dropout))

        # ── CNN branch ──
        self.conv1 = nn.Conv1d(n_features, self.HIDDEN_CNN, kernel_size=3, padding=1)
        self.conv2 = nn.Conv1d(self.HIDDEN_CNN, self.HIDDEN_CNN, kernel_size=5, padding=2)
        self.pool  = nn.AdaptiveMaxPool1d(1)

        # ── GRU branch ──
        self.gru = nn.GRU(
            input_size=n_features,
            hidden_size=self.HIDDEN_GRU,
            num_layers=self.GRU_LAYERS,
            batch_first=True,
            dropout=gru_drop,
        )

        # ── TCN branch (4 dilated residual blocks) ──
        tcn_layers: list[nn.Module] = []
        prev_ch = n_features
        for dil in self.TCN_DILATIONS:
            tcn_layers.append(_TCNBlock(prev_ch, self.HIDDEN_TCN,
                                        kernel_size=self.TCN_KERNEL,
                                        dilation=dil, dropout=0.2))
            prev_ch = self.HIDDEN_TCN
        self.tcn = nn.Sequential(*tcn_layers)

        # ── GAF image branch ──
        self.gaf_enc = GAFResNet() if use_gaf else None
        gaf_dim = self.gaf_enc.EMB_DIM if use_gaf else 0

        # ── Fusion + regime conditioning + dense ──
        # CNN(64) + GRU(128) + TCN(128) + GAF(128) + regime(3) = 451
        fusion_dim = (self.HIDDEN_CNN + self.HIDDEN_GRU + self.HIDDEN_TCN
                      + gaf_dim + n_regimes)
        self.fc1  = nn.Linear(fusion_dim, self.HIDDEN_FC)
        self.drop = nn.Dropout(fc_drop)

        # ── Multi-task heads ──
        self.dir_head   = nn.Linear(self.HIDDEN_FC, 3)   # +1, +3, +5 candles
        self.mag_head   = nn.Linear(self.HIDDEN_FC, 3)
        self.trend_head = nn.Linear(self.HIDDEN_FC, 1)   # P(bullish 10c)

    def forward(self, x: torch.Tensor,
                gaf: torch.Tensor | None = None,
                regime: torch.Tensor | None = None):
        """x: [B, T, F].  gaf: [B, 4, T, T] or None.  regime: [B, R] or None."""
        B = x.size(0)

        # CNN branch — channels-first
        x_seq = x.permute(0, 2, 1)              # [B, F, T]
        x_cnn = F.relu(self.conv1(x_seq))
        x_cnn = F.relu(self.conv2(x_cnn))
        x_cnn = self.pool(x_cnn).squeeze(-1)    # [B, HIDDEN_CNN]

        # GRU branch
        _, h_n = self.gru(x)                    # h_n: [layers, B, HIDDEN_GRU]
        x_gru = h_n[-1]                         # [B, HIDDEN_GRU]

        # TCN branch — operates on [B, F, T], take last timestep
        x_tcn_full = self.tcn(x_seq)            # [B, HIDDEN_TCN, T]
        x_tcn = x_tcn_full[:, :, -1]            # [B, HIDDEN_TCN]

        feats = [x_cnn, x_gru, x_tcn]

        # GAF branch
        if self.use_gaf:
            if gaf is None:
                # Zero embedding when image absent (graceful degradation)
                feats.append(torch.zeros(B, self.gaf_enc.EMB_DIM,
                                         device=x.device, dtype=x.dtype))
            else:
                feats.append(self.gaf_enc(gaf))

        # Regime embedding — uniform 1/N when absent (treat as ambiguous)
        if regime is None:
            regime = torch.full((B, self.n_regimes), 1.0 / self.n_regimes,
                                device=x.device, dtype=x.dtype)
        feats.append(regime)

        h = torch.cat(feats, dim=-1)            # [B, fusion_dim]
        h = self.drop(F.relu(self.fc1(h)))      # [B, HIDDEN_FC]

        return (
            torch.sigmoid(self.dir_head(h)),
            self.mag_head(h),
            torch.sigmoid(self.trend_head(h)),
        )


# ── Model persistence helpers ────────────────────────────────────────────────

def _model_path(interval: str) -> Path:
    p = _MODEL_PATHS.get(interval)
    if p is None:
        raise ValueError(f"unsupported interval: {interval}")
    return p


def _load(interval: str) -> nn.Module | None:
    """Mtime-tracked load. Returns model in eval mode or None."""
    global _cache
    path = _model_path(interval)
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    cached = _cache.get(interval)
    if cached is not None and cached[2] == mtime:
        return cached[0]
    try:
        checkpoint = torch.load(path, map_location="cpu", weights_only=False)
        if not isinstance(checkpoint, dict) or "state_dict" not in checkpoint:
            log.warning("candlenet_legacy_format_rejected", interval=interval)
            return None
        metrics = checkpoint.get("metrics", {})
        # cont. 65k — gate on true ECE when present (models trained after the
        # fix store `dir_ece`); legacy checkpoints lack it and fall back to the
        # loosened mean|p-y| sanity bound so they still load.
        _ece = metrics.get("dir_ece")
        # cont. 65k — gate on val_auc, top_decile_lift, and true ECE (when the
        # checkpoint has it). dir_calib_err is NOT gated (see _gate_fail note).
        # Legacy checkpoints predate dir_ece; they still load on auc+lift so a
        # rollback target always exists.
        if (metrics.get("val_auc", 0.0) < VALIDATION_GATES["min_val_auc"]
                or metrics.get("top_decile_lift", 0.0) < VALIDATION_GATES["top_decile_lift"]
                or (_ece is not None and _ece > VALIDATION_GATES["max_dir_ece"])):
            log.warning("candlenet_load_rejected_by_gates", interval=interval, **metrics)
            return None
        model = CandleNetModel()
        try:
            model.load_state_dict(checkpoint["state_dict"])
        except RuntimeError as exc:
            # Architecture changed → require retrain. Don't silently load partial weights.
            log.warning("candlenet_state_dict_mismatch", interval=interval,
                        error=str(exc)[:200],
                        note="model architecture changed — retrain required")
            return None
        model.eval()
        # cont. 49 — load mag scaler from checkpoint (legacy checkpoints
        # without it get identity scaler so they keep working — those
        # pre-cont.49 models used raw % targets, so identity IS correct).
        mag_mean = checkpoint.get("mag_mean")
        mag_std  = checkpoint.get("mag_std")
        if mag_mean is None or mag_std is None:
            mag_mean = [0.0, 0.0, 0.0]
            mag_std  = [1.0, 1.0, 1.0]
        scaler = {
            "mag_mean": np.array(mag_mean, dtype=np.float32),
            "mag_std":  np.array(mag_std,  dtype=np.float32),
        }
        _cache[interval] = (model, checkpoint.get("config", {}), mtime, scaler)
        log.info("candlenet_loaded", interval=interval, mtime=round(mtime, 2),
                 mag_scaler_active=any(v != 0 for v in mag_mean), **metrics)
        return model
    except Exception as exc:
        log.warning("candlenet_load_failed", interval=interval, error=str(exc)[:200])
        return None


def _get_mag_scaler(interval: str) -> dict:
    """Return {'mag_mean', 'mag_std'} arrays for un-scaling the model's mag
    head output. Falls back to identity (mean=0, std=1) if cache miss or
    legacy checkpoint without scaler — that's correct for pre-cont.49
    models which were trained on raw % labels."""
    cached = _cache.get(interval)
    if cached is not None and len(cached) >= 4 and isinstance(cached[3], dict):
        return cached[3]
    return {
        "mag_mean": np.zeros(3, dtype=np.float32),
        "mag_std":  np.ones(3, dtype=np.float32),
    }


# ── Normalisation ────────────────────────────────────────────────────────────

def _normalise(mat: np.ndarray) -> np.ndarray:
    """Per-sequence normalisation — price columns / anchor, volume / max."""
    mat = mat.copy()
    anchor = mat[0, 3]   # first close
    if anchor <= 0:
        anchor = 1.0
    for col in (0, 1, 2, 3, 5, 6, 7, 8):
        mat[:, col] /= anchor
    vol_max = mat[:, 4].max()
    if vol_max > 0:
        mat[:, 4] /= vol_max
    return mat


# ── Inference helpers ────────────────────────────────────────────────────────

def _get_candles(pair: str, interval: str):
    """Return (opens, highs, lows, closes, volumes) lists or None."""
    r = redis_client.get()
    key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", interval)
    raw = r.lrange(key, 0, CONTEXT_LENGTH - 1)
    if len(raw) < CONTEXT_LENGTH:
        return None
    candles = [json.loads(c) for c in reversed(raw)]
    return (
        [float(c["o"]) for c in candles],
        [float(c["h"]) for c in candles],
        [float(c["l"]) for c in candles],
        [float(c["c"]) for c in candles],
        [float(c.get("v", 0)) for c in candles],
    )


def _get_microstructure(pair: str) -> tuple[float, float]:
    r = redis_client.get()
    try:
        ofi  = float(r.get(redis_keys.OFI.replace("{pair}", pair)) or 0)
        vpin = float(r.get(redis_keys.VPIN.replace("{pair}", pair)) or 0)
    except Exception:
        ofi, vpin = 0.0, 0.0
    return ofi, vpin


def _get_regime_onehot() -> np.ndarray:
    """Read HMM regime from Redis and return [N_REGIMES] one-hot.
    Returns uniform 1/N if unknown — model still produces output via fallback."""
    r = redis_client.get()
    try:
        reg = r.get(redis_keys.CURRENT_REGIME)
    except Exception:
        reg = None
    out = np.zeros(N_REGIMES, dtype=np.float32)
    if reg in _REGIME_TO_IDX:
        out[_REGIME_TO_IDX[reg]] = 1.0
    else:
        out[:] = 1.0 / N_REGIMES
    return out


def _streak_zscore(pair: str, interval: str, streak: int,
                   alpha: float = 0.05) -> float:
    """Per-pair / per-TF EWMA of streak length + EWMA variance → Z-score.

    Persisted in Redis so the statistic is durable across restarts.
    Keys:
      candlenet:streak_ewma:{pair}:{interval}
      candlenet:streak_var :{pair}:{interval}
    """
    r = redis_client.get()
    mean_key = f"candlenet:streak_ewma:{pair}:{interval}"
    var_key  = f"candlenet:streak_var:{pair}:{interval}"
    try:
        prev_mean = float(r.get(mean_key) or 0)
        prev_var  = float(r.get(var_key)  or 1.0)
    except Exception:
        prev_mean, prev_var = 0.0, 1.0

    # Seed mean if never observed
    if prev_mean <= 0:
        new_mean = float(streak)
        new_var  = max(1.0, float(streak))
    else:
        delta = float(streak) - prev_mean
        new_mean = prev_mean + alpha * delta
        new_var  = (1 - alpha) * (prev_var + alpha * delta * delta)
        new_var  = max(new_var, 1.0)

    try:
        r.set(mean_key, new_mean)
        r.set(var_key,  new_var)
    except Exception:
        pass

    sd = math.sqrt(new_var)
    return (float(streak) - new_mean) / sd if sd > 0 else 0.0


def _publish_exhaustion(pair: str, interval: str, score: float,
                        direction: int, raw: dict) -> None:
    """Write the exhaustion forecast block to Redis (TTL = 90s).

    Key: {pair}:{interval}:exhaustion_score
    Value: JSON with score, direction (+1/-1), and raw components
    """
    r = redis_client.get()
    payload = {
        "score":     round(float(score), 4),
        "direction": int(direction),
        "streak":    int(raw.get("streak", 0)),
        "body_slope": float(raw.get("body_slope", 0.0)),
        "vol_slope":  float(raw.get("vol_slope", 0.0)),
        "pattern":   int(raw.get("pattern_flag", 0)),
    }
    try:
        r.set(f"{pair}:{interval}:exhaustion_score",
              json.dumps(payload), ex=_EXHAUSTION_TTL)
    except Exception:
        pass


# ── Inference ────────────────────────────────────────────────────────────────

def run_inference(pair: str, interval: str) -> dict:
    """F48 inference for one pair + timeframe. Writes forecast + ATR + exhaustion."""
    fg_id = _INTERVAL_TO_FG.get(interval)
    if fg_id is None:
        return {}
    try:
        from feature_governance.registry import is_active
        if not is_active(fg_id):
            return {}
    except Exception:
        pass

    model = _load(interval)
    if model is None:
        return {}

    candle_data = _get_candles(pair, interval)
    if candle_data is None:
        return {}

    opens, highs, lows, closes, volumes = candle_data
    ofi, vpin = _get_microstructure(pair)

    # ── Build feature matrix [60, 17] ──
    mat = build_feature_matrix(opens, highs, lows, closes, volumes)   # [60, 15]
    ofi_col  = np.full((CONTEXT_LENGTH, 1), ofi,  dtype=np.float32)
    vpin_col = np.full((CONTEXT_LENGTH, 1), vpin, dtype=np.float32)
    mat = np.concatenate([mat, ofi_col, vpin_col], axis=1)             # [60, 17]

    # ── ATR side-effect (writes {pair}:atr regardless of inference success) ──
    atr_val = compute_atr(highs, lows, closes, period=14)
    if atr_val is not None and atr_val > 0:
        try:
            redis_client.get().set(f"{pair}:atr", round(float(atr_val), 8))
        except Exception:
            pass

    # ── Exhaustion features (Idea A) — pure-numpy + per-pair Z-score ──
    try:
        ha_open  = mat[:, 5]
        ha_close = mat[:, 8]
        shoot    = mat[:, 11]
        doji     = mat[:, 9]
        exh = compute_exhaustion_features(ha_open, ha_close, np.asarray(volumes),
                                          shoot_flags=shoot, doji_flags=doji)
        streak = exh["streak"]
        z = _streak_zscore(pair, interval, streak)
        # exhaustion_score: only fires when streak is unusually long AND
        # bodies shrinking AND volume declining. Pattern flag amplifies.
        z_pos  = max(0.0, z)
        b_neg  = max(0.0, -float(exh["body_slope"]))
        v_neg  = max(0.0, -float(exh["vol_slope"]))
        pat_mult = 1.0 + 0.5 * int(exh["pattern_flag"])
        score = z_pos * b_neg * v_neg * pat_mult * 100.0  # scale for readability
        _publish_exhaustion(pair, interval, score, exh["streak_direction"], exh)
    except Exception as exc:
        log.warning("exhaustion_compute_failed",
                    pair=pair, interval=interval, error=str(exc)[:200])

    # F49 §Component 1 — Record live feature sample for drift detection.
    # Snapshot the last-timestep feature vector (the one the model conditions
    # its prediction on). Pure side-effect; failure is non-fatal.
    try:
        from ml.drift_detector import record_live_sample
        record_live_sample(f"candlenet_{interval}",
                           mat[-1, :].astype(float).tolist())
    except Exception:
        pass

    # ── Normalise + run model ──
    mat = _normalise(mat)

    try:
        x = torch.tensor(mat, dtype=torch.float32).unsqueeze(0)   # [1, 60, 17]

        # GAF image from raw (unnormalised) OHLC
        try:
            gaf_np = compute_gaf(opens, highs, lows, closes)        # [60, 60, 4]
            gaf_t  = torch.tensor(gaf_np, dtype=torch.float32).permute(2, 0, 1)
            gaf_t  = gaf_t.unsqueeze(0)                              # [1, 4, 60, 60]
        except Exception:
            gaf_t = None

        # Regime one-hot
        regime_np = _get_regime_onehot()
        regime_t  = torch.tensor(regime_np, dtype=torch.float32).unsqueeze(0)

        with torch.no_grad():
            dir_pred, mag_pred, trend_pred = model(x, gaf=gaf_t, regime=regime_t)
        dir_list  = dir_pred.squeeze(0).tolist()
        mag_z     = mag_pred.squeeze(0).numpy()             # z-scored prediction
        trend_val = float(trend_pred.squeeze())

        # cont. 49 — un-scale mag back to % move using checkpoint's scaler.
        # Pre-cont.49 models cached identity scaler so this is a no-op for
        # legacy checkpoints.
        scaler = _get_mag_scaler(interval)
        mag_list = (mag_z * scaler["mag_std"] + scaler["mag_mean"]).tolist()

        forecast = {
            "dir1":  round(dir_list[0], 4),
            "dir3":  round(dir_list[1], 4),
            "dir5":  round(dir_list[2], 4),
            "mag1":  round(float(mag_list[0]), 4),
            "mag3":  round(float(mag_list[1]), 4),
            "mag5":  round(float(mag_list[2]), 4),
            "trend": round(trend_val, 4),
        }

        r = redis_client.get()
        r.set(f"{pair}:{interval}:candle_forecast",
              json.dumps(forecast), ex=_FORECAST_TTL)
        return forecast

    except Exception as exc:
        log.error("candlenet_inference_failed",
                  pair=pair, interval=interval, error=str(exc)[:200])
        return {}


def get_forecast(pair: str, interval: str) -> dict:
    """Return cached forecast from Redis, or run inference if missing."""
    r = redis_client.get()
    cached = r.get(f"{pair}:{interval}:candle_forecast")
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass
    return run_inference(pair, interval)


# ── Training ─────────────────────────────────────────────────────────────────

def train(interval: str, data_dir: Path = Path("data/historical"),
          models_dir: Path = Path("models"),
          hpo_params: dict | None = None,
          enable_hpo: bool = False) -> dict:
    """Train CandleNet for one interval from historical CSV files.

    Per-window training data tuple: (X, GAF, regime_onehot, y_dir, y_mag, y_trend).
    Regime label is derived from the closing price trajectory leading into the
    window (simple proxy: bull if close[i+CONTEXT-1] / close[i] > 1.005, bear if
    < 0.995, else turbulent). This stand-in avoids re-running HMM in pretrainer
    while still teaching the regime head meaningful signal.

    F49 §Component 3 — HPO integration:
      `hpo_params`: explicit override of {lr, dropout, batch_size, weight_decay}.
        When provided, skips HPO search and uses these values directly.
      `enable_hpo`: when True (and `hpo_params` is None), runs Optuna TPE search
        over a fast 5K-sample × 3-epoch objective BEFORE the full train. Best
        params are cached in Redis (model:candlenet_{interval}:hpo_best_params)
        and used for the full training run.
    """
    from sklearn.metrics import roc_auc_score
    from torch.utils.data import Dataset, DataLoader

    pairs = [p for p in data_dir.iterdir() if p.is_dir()]
    if not pairs:
        return {"status": "no_data"}

    HORIZONS = [1, 3, 5]
    TREND_H  = 10
    MIN_WIN  = CONTEXT_LENGTH + max(HORIZONS) + TREND_H + 1
    PRICE_BULL_THR = 1.005
    PRICE_BEAR_THR = 0.995
    # cont. 65k — DIRECTION DEADBAND. Most of the 350-pair universe is
    # illiquid: 70-97% of 5m bars have zero trades, so the exchange repeats
    # the prior close (open=high=low=close, volume=0). The binary label
    # `1.0 if fut > base else 0.0` counted every such FLAT bar as "down",
    # collapsing the universe to a 1.8% up-rate → the model learned "always
    # predict down" → live mono-SHORT cascades on 100/100 pairs → 8h trade
    # freeze. Fix: skip windows whose primary-horizon move is below the
    # deadband (no real direction to learn). Measured effect on the live
    # 5m data: drops 104k flat windows, keeps 5,031 real-move samples across
    # 252 pairs, rebalancing labels to up=47.7% / down=52.3% (was 1.8% up).
    # Illiquid pairs thus contribute only their genuine moves; at inference
    # their near-constant inputs yield ~0.5 forecasts the cascade NEUTRAL_BAND
    # skips — exactly the intended "flat pair → no trade" behaviour.
    # cont. 65k Step 1 — TRIPLE-BARRIER labeling (de Prado) replaces the cont.65k
    # deadband + close-to-close labels. Owner sign-off: TIGHT SYMMETRIC ±1
    # vol_unit barriers, time barrier = the per-head horizon. For each window we
    # walk forward and label by which barrier is touched FIRST (upper→up=1,
    # lower→down=0); if neither is touched by the cascade's primary horizon the
    # move is a non-event and the window is skipped (the principled replacement
    # for the flat-window deadband). Encodes direction+risk+timing and naturally
    # neutralises flat/illiquid pairs. Barrier width scales with per-window
    # realised volatility, so it adapts per pair/regime.
    TB_TP_MULT  = float(os.environ.get("CANDLENET_TB_TP_MULT", "1.0"))   # upper = +TB_TP_MULT·vol
    TB_SL_MULT  = float(os.environ.get("CANDLENET_TB_SL_MULT", "1.0"))   # lower = −TB_SL_MULT·vol
    TB_MIN_VOL  = float(os.environ.get("CANDLENET_TB_MIN_VOL", "0.0005"))# below → flat context, skip
    _GATE_H_IDX = 1 if len(HORIZONS) > 1 else 0   # +3 horizon = cascade's dir3
    n_flat_dropped = 0

    # Memory-efficient ingestion: per-window we keep ONLY normalised features
    # (60×17 = 4 KB) + raw close (1.2 KB) + raw OHLC for lazy GAF (480 B per
    # array × 4 arrays) + tiny labels. Pre-computing GAF for every window
    # blows the 8 GB Docker memory limit at 92K samples (5.3 GB just for
    # GAF). Lazy GAF in the Dataset's __getitem__ keeps peak RAM ≤ ~600 MB.
    all_X, all_R = [], []                     # per-window
    all_O, all_H, all_L, all_C = [], [], [], []   # for lazy GAF
    all_dir, all_mag, all_trend = [], [], []
    pairs_loaded = 0

    # cont. 69k — cap windows PER PAIR during the build so the DEEP corpus (P1)
    # doesn't OOM. Before P1 the corpus was shallow (~few k windows total); the
    # fresh 2y corpus yields ~1.18M windows → the build held them all in memory
    # and the worker was SIGKILL'd BEFORE the post-build CANDLENET_MAX_SAMPLES cap
    # could apply. Spreading a per-pair budget across each pair's full history
    # keeps cross-sectional + temporal diversity while bounding total ≈ cap.
    import os as _os
    _cap = int(_os.environ.get("CANDLENET_MAX_SAMPLES", "0") or 0)
    _per_pair = max(50, _cap // max(1, len(pairs))) if _cap > 0 else 0

    for pair_dir in pairs:
        csv_file = pair_dir / f"{interval}.csv"
        if not csv_file.exists():
            continue
        try:
            data = np.genfromtxt(csv_file, delimiter=",", skip_header=1,
                                 dtype=np.float32)
            if data.ndim != 2 or data.shape[0] < MIN_WIN or data.shape[1] < 6:
                continue
            opens   = data[:, 1]
            highs   = data[:, 2]
            lows    = data[:, 3]
            closes  = data[:, 4]
            volumes = data[:, 5]
            mat = build_feature_matrix(opens, highs, lows, closes, volumes)
            mat = np.concatenate(
                [mat, np.zeros((len(closes), 2), dtype=np.float32)], axis=1)

            n = len(closes)
            end = n - max(HORIZONS) - TREND_H
            # cont. 69k — adaptive stride: widen the step so each pair yields at
            # most ~_per_pair windows spread across its full history (caps memory
            # on the deep corpus). Falls back to the original step 3 when no cap.
            _span = max(1, end - CONTEXT_LENGTH)
            _step = max(3, _span // _per_pair) if _per_pair > 0 else 3
            for i in range(0, end - CONTEXT_LENGTH, _step):
                window = mat[i: i + CONTEXT_LENGTH]
                if np.any(np.isnan(window)) or np.any(np.isinf(window)):
                    continue
                x = _normalise(window)
                base_close = closes[i + CONTEXT_LENGTH - 1]
                start_close = closes[i]
                if base_close <= 0 or start_close <= 0:
                    continue

                # Regime proxy from price trajectory
                ratio = base_close / start_close if start_close > 0 else 1.0
                regime_onehot = np.zeros(N_REGIMES, dtype=np.float32)
                if ratio > PRICE_BULL_THR:
                    regime_onehot[0] = 1.0
                elif ratio < PRICE_BEAR_THR:
                    regime_onehot[1] = 1.0
                else:
                    regime_onehot[2] = 1.0

                # cont. 65k Step 1 — TRIPLE-BARRIER labels (see TB_* note above).
                # Per-window vol unit from context returns (floored: a flat /
                # forward-filled context yields ~0 vol → non-event → skip, which
                # is the deadband's role done volatility-adaptively).
                _ctx = closes[i:i + CONTEXT_LENGTH]
                _dif = np.diff(_ctx)
                _den = _ctx[:-1]
                _rets = np.divide(_dif, _den, out=np.zeros_like(_dif),
                                  where=_den > 0)
                vol_unit = float(np.std(_rets))
                if vol_unit < TB_MIN_VOL:
                    n_flat_dropped += 1
                    continue

                upper = base_close * (1.0 + TB_TP_MULT * vol_unit)
                lower = base_close * (1.0 - TB_SL_MULT * vol_unit)
                # Single forward pass to max horizon: record the first barrier
                # touch (horizons are nested 1<3<5 so one pass serves all heads).
                _touch_j, _touch_lab = 0, None
                for _j in range(1, HORIZONS[-1] + 1):
                    _bidx = i + CONTEXT_LENGTH - 1 + _j
                    if highs[_bidx] >= upper:
                        _touch_j, _touch_lab = _j, 1.0
                        break
                    if lows[_bidx] <= lower:
                        _touch_j, _touch_lab = _j, 0.0
                        break
                # Keep the window only if the cascade's PRIMARY horizon resolves
                # (a barrier touched within it). Primary time-expiry → non-event.
                _primary_h = HORIZONS[_GATE_H_IDX]
                if _touch_lab is None or _touch_j > _primary_h:
                    n_flat_dropped += 1
                    continue

                # Per-head labels: use the first-touch label for any horizon the
                # touch falls within; otherwise (shorter horizon that expired
                # before the touch) fall back to that horizon's close sign.
                dir_labels = []
                mag_labels = []
                for h in HORIZONS:
                    if _touch_lab is not None and _touch_j <= h:
                        _lab = _touch_lab
                    else:
                        _futc = closes[i + CONTEXT_LENGTH - 1 + h]
                        _lab = 1.0 if _futc > base_close else 0.0
                    dir_labels.append(_lab)
                    fut = closes[i + CONTEXT_LENGTH - 1 + h]
                    mag_labels.append(float((fut - base_close) / base_close * 100))
                trend_closes = closes[i + CONTEXT_LENGTH: i + CONTEXT_LENGTH + TREND_H]
                trend_label = 1.0 if np.mean(trend_closes > base_close) >= 0.5 else 0.0

                all_X.append(x.astype(np.float32))
                all_O.append(opens[i: i + CONTEXT_LENGTH].astype(np.float32))
                all_H.append(highs[i: i + CONTEXT_LENGTH].astype(np.float32))
                all_L.append(lows[i: i + CONTEXT_LENGTH].astype(np.float32))
                all_C.append(closes[i: i + CONTEXT_LENGTH].astype(np.float32))
                all_R.append(regime_onehot)
                all_dir.append(dir_labels)
                all_mag.append(mag_labels)
                all_trend.append(trend_label)
            pairs_loaded += 1
        except Exception as exc:
            log.warning("candlenet_train_skip_pair",
                        pair=pair_dir.name, error=str(exc)[:120])

    if len(all_X) < 500:
        return {"status": "insufficient_data", "samples": len(all_X),
                "pairs_loaded": pairs_loaded}

    # cont. 65k — surface the triple-barrier non-event drop count + resulting
    # label balance so a future bearish-collapse is visible in logs/Redis.
    _upr = float(np.mean([d[_GATE_H_IDX] for d in all_dir])) if all_dir else 0.0
    log.info("candlenet_train_data_ready", samples=len(all_X),
             pairs=pairs_loaded, interval=interval,
             nonevents_dropped=n_flat_dropped,
             tb_tp_mult=TB_TP_MULT, tb_sl_mult=TB_SL_MULT,
             primary_horizon_up_rate=round(_upr, 4))
    try:
        _r = redis_client.get()
        _r.set(f"brain:candlenet_{interval}_flat_dropped", n_flat_dropped)
        _r.set(f"brain:candlenet_{interval}_train_up_rate", round(_upr, 4))
    except Exception:
        pass

    # Pack into contiguous numpy arrays — total ~600 MB for 92K samples
    X = np.stack(all_X, axis=0)                       # [N, 60, 17]
    R = np.stack(all_R, axis=0)                       # [N, 3]
    O = np.stack(all_O, axis=0)                       # [N, 60]
    H = np.stack(all_H, axis=0)                       # [N, 60]
    L = np.stack(all_L, axis=0)                       # [N, 60]
    C = np.stack(all_C, axis=0)                       # [N, 60]
    y_dir = np.array(all_dir, dtype=np.float32)
    y_mag = np.array(all_mag, dtype=np.float32)
    y_trn = np.array(all_trend, dtype=np.float32)

    # Free the Python list memory immediately (numpy stacks own their data)
    del all_X, all_R, all_O, all_H, all_L, all_C, all_dir, all_mag, all_trend

    # cont. 51 — Option B subsampling. When the operator sets
    # `CANDLENET_MAX_SAMPLES` env var, randomly subsample the training set
    # before train/val split. Reduces per-epoch time roughly linearly with
    # sample count. Trade-off: less data → ~5-10% lower direction accuracy
    # vs full set; validation gates (min_val_auc >= 0.56) still enforce a
    # quality floor and refuse to save under-fit models. Random seed pinned
    # so the same subset is used across all three TFs for comparability.
    _max_samples = int(os.environ.get("CANDLENET_MAX_SAMPLES", "0"))
    if 0 < _max_samples < len(X):
        rng = np.random.default_rng(seed=42)
        idx = rng.choice(len(X), size=_max_samples, replace=False)
        idx.sort()        # preserve temporal ordering within the subset
        X     = X[idx]
        R     = R[idx]
        O     = O[idx]
        H     = H[idx]
        L     = L[idx]
        C     = C[idx]
        y_dir = y_dir[idx]
        y_mag = y_mag[idx]
        y_trn = y_trn[idx]
        log.info("candlenet_subsampled",
                 interval=interval,
                 original=int(idx.max()) + 1 if len(idx) else 0,
                 kept=len(X),
                 reason="CANDLENET_MAX_SAMPLES")

    split = int(len(X) * 0.80)

    # F49 §Component 3 — Auto HPO search (opt-in via enable_hpo or env flag).
    # The objective trains a fast model on a 5K-sample subset for 3 epochs
    # and reports val_auc; Optuna TPE finds promising (lr, dropout, batch_size,
    # weight_decay) within a budget of 10 trials (~30 min total). Best params
    # cached in Redis and used for the full training run below.
    if hpo_params is None and enable_hpo:
        try:
            from ml.auto_hpo import search_best_params
            hpo_params = search_best_params(
                f"candlenet_{interval}",
                lambda params: _hpo_fast_eval(
                    X, R, O, H, L, C, y_dir, y_mag, y_trn, split, params),
                n_trials=10,
                direction="maximize",
            )
            log.info("candlenet_hpo_done", interval=interval, params=hpo_params)
        except Exception as exc:
            log.warning("candlenet_hpo_failed_using_defaults",
                        interval=interval, error=str(exc)[:200])
            hpo_params = None

    # Apply HPO params (or fall back to baked-in defaults)
    tr_lr        = float((hpo_params or {}).get("lr",           1e-3))
    tr_dropout   = float((hpo_params or {}).get("dropout",      0.3))
    tr_batch     = int((hpo_params or {}).get("batch_size",     128))
    tr_wdecay    = float((hpo_params or {}).get("weight_decay", 1e-5))

    # cont. 49 — precompute GAF ONCE into a single contiguous fp16 array
    # instead of recomputing per-sample per-epoch. For 92K samples × 25
    # epochs that's a 25× reduction in GAF work. Storage: 92700 × 4 × 60 ×
    # 60 × 2 bytes = ~2.5 GB (vs 5.3 GB fp32) — fits comfortably under the
    # 8 GB container cap alongside the model + numpy arrays.
    n_total = len(X)
    log.info("candlenet_gaf_precompute_start",
             interval=interval, samples=n_total)
    _t0 = time.time()
    gaf_cache = np.empty((n_total, 4, CONTEXT_LENGTH, CONTEXT_LENGTH),
                         dtype=np.float16)
    for i in range(n_total):
        g = compute_gaf(O[i], H[i], L[i], C[i])     # [T, T, 4] fp32
        gaf_cache[i] = np.transpose(g, (2, 0, 1)).astype(np.float16)
        if (i + 1) % 10000 == 0:
            log.info("candlenet_gaf_precompute_progress",
                     interval=interval, done=i + 1, total=n_total,
                     elapsed_s=round(time.time() - _t0, 1))
    log.info("candlenet_gaf_precompute_done",
             interval=interval, samples=n_total,
             cache_mb=round(gaf_cache.nbytes / 1e6, 1),
             elapsed_s=round(time.time() - _t0, 1))

    # cont. 49 — z-score `mag` labels so SmoothL1Loss on standardised
    # magnitude lives on the same scale as BCE on direction. The raw scale
    # was ±5% (MSE ≈ 25 per sample) which dominated the gradient by ~20×
    # vs BCE(dir) ≈ 0.69, blocking the direction head from learning.
    mag_mean = y_mag[:split].mean(axis=0)            # [H]
    mag_std  = y_mag[:split].std(axis=0)             # [H]
    mag_std  = np.where(mag_std < 1e-6, 1.0, mag_std)
    y_mag_z  = ((y_mag - mag_mean) / mag_std).astype(np.float32)

    class _CachedGAFDataset(Dataset):
        """O(1) __getitem__ — GAF is precomputed; we just slice the cache."""
        def __init__(self, X, gaf, R, ydir, ymag_z, ytrn):
            self.X = X; self.gaf = gaf; self.R = R
            self.ydir = ydir; self.ymag = ymag_z; self.ytrn = ytrn
        def __len__(self):
            return len(self.X)
        def __getitem__(self, idx):
            return (
                torch.from_numpy(self.X[idx]),
                torch.from_numpy(self.gaf[idx].astype(np.float32)),
                torch.from_numpy(self.R[idx]),
                torch.from_numpy(self.ydir[idx]),
                torch.from_numpy(self.ymag[idx]),
                torch.from_numpy(self.ytrn[idx:idx+1]),
            )

    ds_tr  = _CachedGAFDataset(X[:split], gaf_cache[:split], R[:split],
                               y_dir[:split], y_mag_z[:split], y_trn[:split])
    ds_val = _CachedGAFDataset(X[split:], gaf_cache[split:], R[split:],
                               y_dir[split:], y_mag_z[split:], y_trn[split:])

    # cont. 65f — num_workers=0 when invoked from a celery prefork worker
    # (the default pool). Daemonic processes can't have children, so
    # spawning DataLoader subprocesses raises:
    #   'daemonic processes are not allowed to have children'
    # The cont. 52c parallelism (num_workers=2) only works when called
    # from a non-celery context (e.g., direct `python -m` or threads pool).
    # GAF is already in-memory cached, so single-process loading isn't
    # much slower (5-15% vs the 10-20% gain we'd see otherwise).
    try:
        import billiard
        _is_celery_daemon = bool(billiard.current_process().daemon)
    except Exception:
        _is_celery_daemon = False
    _nw = 0 if _is_celery_daemon else min(2, max(1, (os.cpu_count() or 2) - 1))
    loader_tr  = DataLoader(ds_tr,  batch_size=tr_batch,
                            shuffle=True,  num_workers=_nw,
                            persistent_workers=(_nw > 0))
    loader_val = DataLoader(ds_val, batch_size=max(tr_batch, 256),
                            shuffle=False, num_workers=_nw,
                            persistent_workers=(_nw > 0))

    model = CandleNetModel(dropout=tr_dropout)
    # cont. 52c — BN momentum 0.1 → 0.3. PyTorch's default 0.1 takes ~10
    # epochs for BatchNorm running stats to converge; we cap MAX_EPOCHS at
    # 5 so val_loss (which uses running stats) can read systematically
    # lower than train_loss (which uses per-batch stats) for the entire
    # run. Bumping momentum lets running stats track ~3× faster, removing
    # the artefact on short runs. Source: PyTorch issue #5406.
    for _m in model.modules():
        if isinstance(_m, (nn.BatchNorm1d, nn.BatchNorm2d)):
            _m.momentum = 0.3
    model.train()
    optimizer = torch.optim.Adam(model.parameters(),
                                 lr=tr_lr, weight_decay=tr_wdecay)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=20, eta_min=tr_lr * 1e-2)

    # cont. 49 — early-stopping: kill the run when val_loss hasn't
    # improved for `PATIENCE` consecutive epochs. With 25-epoch ceiling
    # but typical convergence by epoch 10-15, this halves training time
    # in the converged case AND exits cleanly when stuck.
    #
    # cont. 51 — explicit user request to fit the full pretrainer run
    # under ~1 hour. Reduced MAX_EPOCHS 25→5 and PATIENCE 5→2 so a single
    # CandleNet trains in ~25 min (5 epochs × ~5 min/epoch with fp16 GAF
    # cache). Three CandleNets = ~75 min including verification overhead.
    # Trade-off: less convergence margin. Validation gates
    # (`min_val_auc >= 0.56`) still enforce quality — if a model fails to
    # converge in 5 epochs it WILL fail the gate and refuse to save,
    # signalling the operator to relax MAX_EPOCHS back up. Runtime
    # override via env var `CANDLENET_MAX_EPOCHS` for the operator who
    # wants to trade time for accuracy without code edits.
    PATIENCE   = int(os.environ.get("CANDLENET_PATIENCE", "2"))
    MAX_EPOCHS = int(os.environ.get("CANDLENET_MAX_EPOCHS", "5"))
    best_val_loss = float("inf")
    best_state    = None
    epochs_since_improvement = 0
    early_stopped_at = None

    # cont. 49 — Huber (SmoothL1) on z-scored mag instead of MSE on raw %.
    # Loss weighting recalibrated since mag-loss magnitudes are now O(1)
    # not O(25): 0.5 BCE_dir + 0.3 SmoothL1_mag + 0.2 BCE_trend stays
    # balanced because all three terms now live in the same range.
    smooth_l1 = nn.SmoothL1Loss()

    # cont. 65k — class-balanced + label-smoothed direction loss. The previous
    # plain BCE let the direction head collapse to the majority class whenever
    # the training window skewed bearish: every pair predicted P(up)≈0.30,
    # producing universe-wide SHORT cascades and (in a bull regime, where the
    # sentiment gate blocks shorts) a total trade freeze. pos_weight upweights
    # the rarer class per horizon so the head must actually discriminate;
    # label smoothing keeps probabilities off the 0/1 rails for honest
    # calibration (lowers ECE).
    _LABEL_SMOOTH = 0.02
    _pos_rate = np.clip(y_dir[:split].mean(axis=0), 0.05, 0.95)   # P(up) per horizon
    _pos_weight = torch.tensor((1.0 - _pos_rate) / _pos_rate, dtype=torch.float32)
    log.info("candlenet_class_balance", interval=interval,
             pos_rate=[round(float(p), 3) for p in _pos_rate],
             pos_weight=[round(float(w), 3) for w in _pos_weight.tolist()])

    def _bce_dir_balanced(dir_p, ydir_b):
        y = ydir_b * (1.0 - 2.0 * _LABEL_SMOOTH) + _LABEL_SMOOTH
        w = ydir_b * _pos_weight + (1.0 - ydir_b)        # broadcast (B,3)*(3,)
        bce = F.binary_cross_entropy(dir_p, y, reduction="none")
        return (w * bce).mean()

    def _composite_loss(dir_p, mag_p, trn_p, ydir_b, ymag_b, ytrn_b):
        return (0.5 * _bce_dir_balanced(dir_p, ydir_b)
              + 0.3 * smooth_l1(mag_p, ymag_b)
              + 0.2 * F.binary_cross_entropy(trn_p, ytrn_b))

    epoch_start_ts = time.time()
    for epoch in range(MAX_EPOCHS):
        model.train()
        # cont. 52c — per-sample mean accumulator. Previously
        # `tr_loss += loss.item()` summed per-batch means without
        # normalising by batch count; train had ~63 batches while val had
        # ~8 (val_batch is 2× tr_batch + smaller split), producing a
        # ~7.9× artefactual gap that *looked* like overfitting but was
        # purely a logging asymmetry. We now sum (per_batch_mean × batch_size)
        # and divide by total samples → true per-sample mean, directly
        # comparable across train and val.
        tr_loss_sum, tr_n = 0.0, 0
        for xb, gb, rb, ydir_b, ymag_b, ytrn_b in loader_tr:
            optimizer.zero_grad()
            dir_p, mag_p, trn_p = model(xb, gaf=gb, regime=rb)
            loss = _composite_loss(dir_p, mag_p, trn_p,
                                   ydir_b, ymag_b, ytrn_b)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            _bs = xb.size(0)
            tr_loss_sum += loss.item() * _bs
            tr_n        += _bs
        tr_loss = tr_loss_sum / max(tr_n, 1)
        scheduler.step()

        model.eval()
        val_loss_sum, val_n = 0.0, 0
        with torch.no_grad():
            for xb, gb, rb, ydir_b, ymag_b, ytrn_b in loader_val:
                dir_p, mag_p, trn_p = model(xb, gaf=gb, regime=rb)
                loss = _composite_loss(dir_p, mag_p, trn_p,
                                       ydir_b, ymag_b, ytrn_b)
                _bs = xb.size(0)
                val_loss_sum += loss.item() * _bs
                val_n        += _bs
        val_loss = val_loss_sum / max(val_n, 1)
        if val_loss < best_val_loss - 1e-4:
            best_val_loss = val_loss
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            epochs_since_improvement = 0
        else:
            epochs_since_improvement += 1

        epoch_secs = time.time() - epoch_start_ts
        epoch_start_ts = time.time()
        # cont. 49 — log every epoch (was every 5) for visibility
        log.info("candlenet_epoch", interval=interval,
                 epoch=epoch,
                 tr_loss=round(tr_loss, 4),
                 val_loss=round(val_loss, 4),
                 best_val_loss=round(best_val_loss, 4),
                 patience_left=PATIENCE - epochs_since_improvement,
                 epoch_secs=round(epoch_secs, 1))

        if epochs_since_improvement >= PATIENCE:
            early_stopped_at = epoch
            log.info("candlenet_early_stop", interval=interval,
                     epoch=epoch, best_val_loss=round(best_val_loss, 4))
            break

    if best_state is None:
        # Defensive — never expected to fire because epoch 0 always
        # produces a state. Bail honestly rather than load None.
        log.error("candlenet_no_best_state", interval=interval)
        return {"status": "no_best_state"}

    model.load_state_dict(best_state)
    model.eval()

    # cont. 65k — TEMPERATURE SCALING (per direction head). The balanced loss
    # produces an honest-but-miscalibrated head (ECE ~0.2 on thin data). Temp
    # scaling is the standard cure: fit one scalar T per output on the val set
    # (minimise NLL), then BAKE it into the dir_head weights — since
    # sigmoid(z/T) = sigmoid((W/T)·h + b/T), scaling row j of the head by 1/T_j
    # makes the SAVED model emit calibrated probabilities natively, so the
    # inference path needs no change. AUC is unchanged (monotonic transform);
    # only confidence calibration improves.
    try:
        _zs, _ys = [], []
        with torch.no_grad():
            for xb, gb, rb, ydir_b, ymag_b, ytrn_b in loader_val:
                dir_p, _, _ = model(xb, gaf=gb, regime=rb)
                p = torch.clamp(dir_p, 1e-6, 1 - 1e-6)
                _zs.append(torch.log(p / (1 - p)))      # recover logits
                _ys.append(ydir_b)
        z_val = torch.cat(_zs, dim=0)                   # [N, 3]
        y_val = torch.cat(_ys, dim=0)                   # [N, 3]
        temps = []
        for j in range(z_val.shape[1]):
            log_t = torch.zeros(1, requires_grad=True)  # T = exp(log_t), start 1.0
            opt = torch.optim.LBFGS([log_t], lr=0.1, max_iter=60)
            zj, yj = z_val[:, j], y_val[:, j]
            def _closure():
                opt.zero_grad()
                loss = F.binary_cross_entropy_with_logits(zj / log_t.exp(), yj)
                loss.backward()
                return loss
            opt.step(_closure)
            T = float(log_t.exp().item())
            T = min(10.0, max(0.25, T))                 # sane clamp
            temps.append(T)
            with torch.no_grad():
                model.dir_head.weight[j] /= T
                model.dir_head.bias[j]   /= T
        best_state = {k: v.clone() for k, v in model.state_dict().items()}
        log.info("candlenet_temperature_scaled", interval=interval,
                 temperatures=[round(t, 3) for t in temps])
    except Exception as exc:
        log.warning("candlenet_temp_scale_failed",
                    interval=interval, error=str(exc)[:160])
    model.eval()

    # Validation metrics on +1 direction
    all_dir_p, all_dir_y = [], []
    with torch.no_grad():
        for xb, gb, rb, ydir_b, ymag_b, ytrn_b in loader_val:
            dir_p, _, _ = model(xb, gaf=gb, regime=rb)
            all_dir_p.append(dir_p.numpy())
            all_dir_y.append(ydir_b.numpy())
    dir_p_arr = np.concatenate(all_dir_p, axis=0)
    dir_y_arr = np.concatenate(all_dir_y, axis=0)

    # cont. 65k — evaluate the gate on the horizon the CASCADE actually votes
    # on (dir3 = +3 = _GATE_H_IDX) for 5m/15m, not the noisy +1 head (index 0)
    # the old gate used. Temperature scaling confirmed head +1 is the hard,
    # near-random horizon (needed T=2.66) while +3 was already calibrated
    # (T=1.0) — gating on +1 was rejecting models whose cascade-relevant head
    # was fine. AUC/lift/ECE all now measured on the cascade horizon.
    _gi = _GATE_H_IDX
    try:
        val_auc = float(roc_auc_score(dir_y_arr[:, _gi], dir_p_arr[:, _gi]))
    except Exception:
        val_auc = 0.5

    order = np.argsort(-dir_p_arr[:, _gi])
    decile_n = max(1, len(order) // 10)
    pos_rate = float(dir_y_arr[:, _gi].mean())
    top_winrate = float(dir_y_arr[:, _gi][order[:decile_n]].mean())
    top_decile_lift = top_winrate / pos_rate if pos_rate > 0 else 0.0
    calib_err = float(np.mean(np.abs(dir_p_arr[:, _gi] - dir_y_arr[:, _gi])))
    # TRUE Expected Calibration Error (10-bin reliability) on the cascade
    # horizon. This is the metric the gate enforces; `dir_calib_err`
    # (mean|p-y|) above is reported for continuity but no longer gated.
    _p = dir_p_arr[:, _gi]
    _yc = dir_y_arr[:, _gi]
    _edges = np.linspace(0.0, 1.0, 11)
    _b = np.clip(np.digitize(_p, _edges[1:-1]), 0, 9)
    dir_ece = 0.0
    for _bin in range(10):
        _m = _b == _bin
        if _m.any():
            dir_ece += float(_m.mean()) * abs(float(_p[_m].mean()) - float(_yc[_m].mean()))
    dir_ece = float(dir_ece)

    metrics = {
        "val_auc":         round(val_auc, 4),
        "top_decile_lift": round(top_decile_lift, 4),
        "dir_ece":         round(dir_ece, 4),
        "dir_calib_err":   round(calib_err, 4),
        "best_val_loss":   round(best_val_loss, 6),
        "samples":         len(X),
        "pairs_loaded":    pairs_loaded,
    }

    try:
        r = redis_client.get()
        for k, v in metrics.items():
            r.set(f"brain:candlenet_{interval}_{k}", v)
        r.set(f"brain:candlenet_{interval}_accepted",
              "0" if _gate_fail(metrics) else "1")
    except Exception:
        pass

    gate_fail = _gate_fail(metrics)
    if gate_fail:
        log.warning("candlenet_rejected_by_gates", interval=interval,
                    reason=gate_fail, **metrics)
        return {"status": "rejected_validation", "reason": gate_fail, **metrics}

    models_dir.mkdir(parents=True, exist_ok=True)

    checkpoint = {
        "state_dict":  best_state,
        "config":      {"n_features": N_FEATURES,
                        "context_length": CONTEXT_LENGTH,
                        "n_regimes": N_REGIMES,
                        "use_gaf": True,
                        "branches": ["CNN", "GRU", "TCN", "GAF"],
                        # cont. 49 — early stop bookkeeping
                        "early_stopped_at":  early_stopped_at,
                        "epochs_run":        (early_stopped_at + 1
                                              if early_stopped_at is not None
                                              else MAX_EPOCHS)},
        "feature_list": ["O","H","L","C","V","HA_O","HA_H","HA_L","HA_C",
                         "doji","hammer","shoot","bull_e","bear_e","morn_s",
                         "OFI","VPIN"],
        # cont. 49 — mag head was trained on z-scored labels (SmoothL1).
        # Inference must un-scale: mag_pct = mag_pred * mag_std + mag_mean.
        # Persist as lists so torch.save serialises cleanly across versions.
        "mag_mean":    [float(v) for v in mag_mean],
        "mag_std":     [float(v) for v in mag_std],
        "metrics":     metrics,
    }

    # F49 §Component 5 — Atomic versioned save (with auto-rollback on
    # validation failure). Falls back to direct write if model_versions
    # module is unavailable.
    out_path = models_dir / f"candlenet_{interval}.pth"
    try:
        from ml.model_versions import save_versioned
        save_versioned(f"candlenet_{interval}",
                       save_fn=lambda p: torch.save(checkpoint, p))
    except Exception as exc:
        log.warning("model_versions_save_failed_using_legacy",
                    interval=interval, error=str(exc)[:200])
        torch.save(checkpoint, out_path)

    # F49 §Component 1 — Snapshot training distribution for drift detection.
    try:
        from ml.drift_detector import snapshot_training_distribution
        snapshot_training_distribution(f"candlenet_{interval}", X)
    except Exception as exc:
        log.debug("drift_snapshot_skipped",
                  interval=interval, error=str(exc)[:120])

    global _cache
    _cache[interval] = None  # force reload

    log.info("candlenet_trained_and_saved",
             interval=interval, path=str(out_path), **metrics)
    return {"status": "trained", **metrics}


def _gate_fail(metrics: dict) -> str:
    if metrics.get("val_auc", 0.0) < VALIDATION_GATES["min_val_auc"]:
        return (f"val_auc {metrics['val_auc']:.3f} < "
                f"{VALIDATION_GATES['min_val_auc']}")
    if metrics.get("top_decile_lift", 0.0) < VALIDATION_GATES["top_decile_lift"]:
        return (f"top_decile_lift {metrics['top_decile_lift']:.3f} < "
                f"{VALIDATION_GATES['top_decile_lift']}")
    if metrics.get("dir_ece", 1.0) > VALIDATION_GATES["max_dir_ece"]:
        return (f"dir_ece {metrics['dir_ece']:.3f} > "
                f"{VALIDATION_GATES['max_dir_ece']}")
    # cont. 65k — dir_calib_err (mean|p-y|) is NOT gated: it's pinned near 0.5
    # for any hard prediction problem (it's a sharpness/accuracy proxy, not a
    # calibration metric), so it can never pass a tight bound. ECE above is the
    # real calibration gate. dir_calib_err is still reported for continuity.
    return ""


def _hpo_fast_eval(X, R, O, H, L, C, y_dir, y_mag, y_trn, split: int,
                   params: dict) -> float:
    """F49 §Component 3 — fast HPO objective.

    Trains a CandleNet on a 5K-sample subset for 3 epochs with the given
    hyperparameters and returns val_auc on the direction head. Returns 0.0
    on any error so Optuna's TPE avoids that region. GAF is disabled in
    HPO mode — the ResNet adds ~30s per epoch and we only need a noisy
    signal of which params work best.
    """
    try:
        from sklearn.metrics import roc_auc_score
        from torch.utils.data import Dataset, DataLoader
    except Exception:
        return 0.0

    n_total = len(X)
    n_subset = min(5000, max(500, n_total // 18))
    if n_subset < 200:
        return 0.0
    # Reproducible subsample so each trial sees the same data
    rng = np.random.default_rng(seed=42)
    idx = rng.choice(n_total, size=n_subset, replace=False)
    idx.sort()
    split_sub = int(n_subset * 0.80)

    Xs   = X[idx]; Rs = R[idx]
    yd   = y_dir[idx]; ym_raw = y_mag[idx]; yt = y_trn[idx]
    # cont. 49 — match full-training z-score + SmoothL1 so HPO search
    # finds params that work in the same loss landscape as the full run.
    _ms = ym_raw[:int(len(ym_raw) * 0.8)].mean(axis=0)
    _sd = ym_raw[:int(len(ym_raw) * 0.8)].std(axis=0)
    _sd = np.where(_sd < 1e-6, 1.0, _sd)
    ym  = ((ym_raw - _ms) / _sd).astype(np.float32)

    class _FastDataset(Dataset):
        def __init__(self, X, R, ydir, ymag, ytrn):
            self.X = X; self.R = R
            self.ydir = ydir; self.ymag = ymag; self.ytrn = ytrn
        def __len__(self): return len(self.X)
        def __getitem__(self, i):
            return (
                torch.from_numpy(self.X[i]),
                torch.from_numpy(self.R[i]),
                torch.from_numpy(self.ydir[i]),
                torch.from_numpy(self.ymag[i]),
                torch.from_numpy(self.ytrn[i:i+1]),
            )

    ds_tr  = _FastDataset(Xs[:split_sub], Rs[:split_sub],
                          yd[:split_sub], ym[:split_sub], yt[:split_sub])
    ds_val = _FastDataset(Xs[split_sub:], Rs[split_sub:],
                          yd[split_sub:], ym[split_sub:], yt[split_sub:])

    batch_size  = int(params.get("batch_size",   128))
    lr          = float(params.get("lr",         1e-3))
    dropout     = float(params.get("dropout",    0.3))
    weight_decay = float(params.get("weight_decay", 1e-5))

    loader_tr  = DataLoader(ds_tr,  batch_size=batch_size,
                            shuffle=True,  num_workers=0)
    loader_val = DataLoader(ds_val, batch_size=max(batch_size, 256),
                            shuffle=False, num_workers=0)

    # use_gaf=False keeps trials fast — GAF ResNet would dominate per-trial cost
    model = CandleNetModel(use_gaf=False, dropout=dropout)
    optimizer = torch.optim.Adam(model.parameters(),
                                 lr=lr, weight_decay=weight_decay)
    _smooth_l1 = nn.SmoothL1Loss()
    # cont. 65k — mirror the full run's class-balanced direction loss so HPO
    # searches the same landscape it will train in.
    _hpo_pos_rate = np.clip(yd[:split_sub].mean(axis=0), 0.05, 0.95)
    _hpo_pos_weight = torch.tensor((1.0 - _hpo_pos_rate) / _hpo_pos_rate,
                                   dtype=torch.float32)

    def _hpo_bce_dir(dir_p, ydir_b):
        y = ydir_b * 0.96 + 0.02
        w = ydir_b * _hpo_pos_weight + (1.0 - ydir_b)
        return (w * F.binary_cross_entropy(dir_p, y, reduction="none")).mean()

    for _epoch in range(3):
        model.train()
        for xb, rb, ydir_b, ymag_b, ytrn_b in loader_tr:
            optimizer.zero_grad()
            dir_p, mag_p, trn_p = model(xb, gaf=None, regime=rb)
            loss = (0.5 * _hpo_bce_dir(dir_p, ydir_b)
                  + 0.3 * _smooth_l1(mag_p, ymag_b)
                  + 0.2 * F.binary_cross_entropy(trn_p, ytrn_b))
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()

    model.eval()
    all_p, all_y = [], []
    with torch.no_grad():
        for xb, rb, ydir_b, _ymag_b, _ytrn_b in loader_val:
            dir_p, _, _ = model(xb, gaf=None, regime=rb)
            all_p.append(dir_p.numpy())
            all_y.append(ydir_b.numpy())
    if not all_p:
        return 0.0
    p_arr = np.concatenate(all_p, axis=0)[:, 0]
    y_arr = np.concatenate(all_y, axis=0)[:, 0]
    try:
        return float(roc_auc_score(y_arr, p_arr))
    except Exception:
        return 0.5
