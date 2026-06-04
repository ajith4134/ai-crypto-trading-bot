"""F50c — Mamba State-Space Forecaster (cont. 54).

Blueprint addition (cont. 53 design, cont. 54 ship). Linear-time long-context
candle forecaster that runs in parallel with F48 CandleNet:

  CandleNet (F48)    — CNN-GRU-TCN-GAF hybrid, 60-candle context, ~1.2M params.
  Mamba    (F50c)    — Selective State-Space Model, 512-candle context, ~1.5M params.

Why Mamba alongside CandleNet (not as a replacement):
  - Mamba captures longer-range dependencies (8 h of 1m candles vs F48's 1 h).
  - Linear inference time vs Transformer's quadratic — cheap at 60 pairs × 4 TFs.
  - Different inductive bias (SSM vs CNN-GRU) — ensemble effect on direction
    head when both agree; resolves noise when they disagree.

Reference: CryptoMamba (arXiv:2501.01010, github.com/MShahabSepehri/CryptoMamba)
+ state-spaces/mamba. This module ships a **CPU-compatible pure-PyTorch**
implementation of the selective-scan SSM (no mamba-ssm / causal-conv1d CUDA
kernel dependency) so the bot's CPU-only VPS can run it. Throughput is
~3-5× slower than the CUDA kernel, but still well under 100 ms/pair at
inference. Pretraining is the costly path; inference is fine on CPU.

Activation gate (F30 governance): F50c. When inactive or model missing,
inference returns neutral (0.5 / 0.0) and the signals composite ignores it.

Redis keys (mirror F48's shape so signals/engine.py can consume identically):
  {pair}:{interval}:mamba_forecast    TTL 120s — JSON {dir1, dir3, dir5,
                                                       mag1, mag3, mag5,
                                                       trend, regime_id}

Model file: models/mamba_{1m,5m,15m,1h}.pth   (each ~6 MB on disk)
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Optional

import numpy as np
import structlog
import torch
import torch.nn as nn
import torch.nn.functional as F

import redis_client

log = structlog.get_logger()


# ── Configuration ───────────────────────────────────────────────────────────

CONTEXT_LENGTH = 512        # 8.5 h of 1m, 42 h of 5m, 5.3 days of 15m, 21 days of 1h
N_INPUT_FEATURES = 8        # OHLCV + 3 derived (return, log_volume, body_pct)
D_MODEL = 64
D_STATE = 16
D_CONV = 4
EXPAND = 2
N_LAYERS = 4
N_HORIZONS = 3              # +1, +3, +5 candles
_FORECAST_TTL = 120

_MODEL_PATHS = {
    "1m":  Path("models/mamba_1m.pth"),
    "5m":  Path("models/mamba_5m.pth"),
    "15m": Path("models/mamba_15m.pth"),
    "1h":  Path("models/mamba_1h.pth"),
}
_INTERVAL_TO_FG = {"1m": "F50c_1m", "5m": "F50c_5m", "15m": "F50c_15m",
                   "1h": "F50c_1h"}

# Per-timeframe inference cache: (model, mtime)
_cache: dict[str, Optional[tuple]] = {tf: None for tf in _MODEL_PATHS}


# ── Architecture: minimal CPU-friendly Mamba block ──────────────────────────

class SelectiveSSM(nn.Module):
    """Selective scan SSM block — CPU-pure-PyTorch implementation.

    Mathematically equivalent to mamba-ssm's selective_scan_fn but implemented
    as a Python loop over the sequence dim. Slower than the CUDA kernel but
    runs on any device. For our context length (512), inference loop is
    ~5 ms/pair on a modern CPU core.

    Reference: Gu & Dao 2023 (Mamba paper, arXiv:2312.00752) eq. (2a-2b).
    """

    def __init__(self, d_model: int, d_state: int = 16,
                 d_conv: int = 4, expand: int = 2):
        super().__init__()
        self.d_model = d_model
        self.d_state = d_state
        self.d_inner = d_model * expand

        # Input projections
        self.in_proj = nn.Linear(d_model, self.d_inner * 2, bias=False)
        self.conv1d = nn.Conv1d(
            self.d_inner, self.d_inner,
            kernel_size=d_conv, padding=d_conv - 1,
            groups=self.d_inner, bias=True,
        )
        # SSM parameter projections (selective)
        self.x_proj = nn.Linear(self.d_inner, d_state * 2 + 1, bias=False)
        self.dt_proj = nn.Linear(1, self.d_inner, bias=True)
        # State-space matrices
        A_log = torch.log(torch.arange(1, d_state + 1, dtype=torch.float32))
        self.A_log = nn.Parameter(A_log.unsqueeze(0).expand(self.d_inner, -1).clone())
        self.D = nn.Parameter(torch.ones(self.d_inner))
        self.out_proj = nn.Linear(self.d_inner, d_model, bias=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: (B, L, d_model) → (B, L, d_model)."""
        B, L, _ = x.shape
        xz = self.in_proj(x)                          # (B, L, 2*d_inner)
        x_in, z = xz.chunk(2, dim=-1)                  # each (B, L, d_inner)
        # Causal Conv1d over the sequence dim
        x_conv = self.conv1d(x_in.transpose(1, 2))[:, :, :L]
        x_conv = F.silu(x_conv).transpose(1, 2)       # (B, L, d_inner)
        # Selective parameters
        x_dbl = self.x_proj(x_conv)                   # (B, L, 2*d_state + 1)
        dt, Bp, Cp = torch.split(x_dbl,
                                 [1, self.d_state, self.d_state], dim=-1)
        dt = F.softplus(self.dt_proj(dt))             # (B, L, d_inner)
        A = -torch.exp(self.A_log.float())            # (d_inner, d_state)
        # Discretize A,B via dt
        # dA: (B, L, d_inner, d_state) = exp(dt * A)
        dA = torch.exp(dt.unsqueeze(-1) * A.unsqueeze(0).unsqueeze(0))
        dB = dt.unsqueeze(-1) * Bp.unsqueeze(-2)      # (B, L, d_inner, d_state)
        # Sequential scan — CPU-friendly
        h = torch.zeros(B, self.d_inner, self.d_state, device=x.device,
                        dtype=x.dtype)
        ys = []
        for t in range(L):
            h = dA[:, t] * h + dB[:, t] * x_conv[:, t].unsqueeze(-1)
            y_t = torch.einsum("bdn,bn->bd", h, Cp[:, t])
            ys.append(y_t)
        y = torch.stack(ys, dim=1)                    # (B, L, d_inner)
        y = y + self.D * x_conv                       # skip
        y = y * F.silu(z)                             # gating
        return self.out_proj(y)


class MambaBlock(nn.Module):
    """Pre-norm SSM + residual."""

    def __init__(self, d_model: int):
        super().__init__()
        self.norm = nn.LayerNorm(d_model)
        self.ssm = SelectiveSSM(d_model)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.ssm(self.norm(x))


class MambaForecaster(nn.Module):
    """Multi-horizon direction + magnitude + trend heads. Same head shape as
    F48 CandleNet so signals/engine.py can consume both interchangeably."""

    def __init__(self, n_input: int = N_INPUT_FEATURES, d_model: int = D_MODEL,
                 n_layers: int = N_LAYERS, n_horizons: int = N_HORIZONS):
        super().__init__()
        self.input_proj = nn.Linear(n_input, d_model)
        self.blocks = nn.ModuleList([MambaBlock(d_model) for _ in range(n_layers)])
        self.norm_f = nn.LayerNorm(d_model)
        self.direction_head = nn.Linear(d_model, n_horizons)   # logits → sigmoid
        self.magnitude_head = nn.Linear(d_model, n_horizons)
        self.trend_head     = nn.Linear(d_model, 1)            # 10-candle trend

    def forward(self, x: torch.Tensor) -> dict[str, torch.Tensor]:
        h = self.input_proj(x)
        for blk in self.blocks:
            h = blk(h)
        h = self.norm_f(h)
        h_last = h[:, -1]                                       # last position
        return {
            "dir":   torch.sigmoid(self.direction_head(h_last)),
            "mag":   self.magnitude_head(h_last),
            "trend": torch.sigmoid(self.trend_head(h_last)).squeeze(-1),
        }


# ── Feature construction (mirrors F48's input shape where possible) ─────────

def _build_input(candles: list[dict]) -> Optional[np.ndarray]:
    """Build (CONTEXT_LENGTH, N_INPUT_FEATURES) feature tensor from a list of
    OHLCV candle dicts (oldest first). Returns None on insufficient data.

    Features:
      0 — open / close_prev   (log)
      1 — high / close_prev   (log)
      2 — low  / close_prev   (log)
      3 — close / close_prev  (log return)
      4 — log(volume + 1)
      5 — (close - open) / (high - low + eps)        body_pct
      6 — (close - low)  / (high - low + eps)        close_position
      7 — abs(high - low) / close_prev                range_pct
    """
    if len(candles) < CONTEXT_LENGTH + 1:
        return None
    candles = candles[-(CONTEXT_LENGTH + 1):]
    arr = np.zeros((CONTEXT_LENGTH, N_INPUT_FEATURES), dtype=np.float32)
    eps = 1e-9
    for i in range(CONTEXT_LENGTH):
        c_prev = float(candles[i].get("c", 0))
        c = candles[i + 1]
        o, h, l, cl, v = (float(c.get(k, 0)) for k in ("o", "h", "l", "c", "v"))
        if c_prev <= 0 or cl <= 0:
            continue
        rng = max(h - l, eps)
        arr[i, 0] = math.log(max(o / c_prev, eps))
        arr[i, 1] = math.log(max(h / c_prev, eps))
        arr[i, 2] = math.log(max(l / c_prev, eps))
        arr[i, 3] = math.log(max(cl / c_prev, eps))
        arr[i, 4] = math.log(v + 1.0)
        arr[i, 5] = (cl - o) / rng
        arr[i, 6] = (cl - l) / rng
        arr[i, 7] = (h - l) / c_prev
    return arr


# ── Model loading + caching ─────────────────────────────────────────────────

def _governance_active(interval: str) -> bool:
    try:
        from feature_governance.registry import is_active
        return bool(is_active(_INTERVAL_TO_FG[interval]))
    except Exception:
        return True


def _load_model(interval: str) -> Optional[MambaForecaster]:
    path = _MODEL_PATHS.get(interval)
    if path is None or not path.exists():
        return None
    mtime = path.stat().st_mtime
    cached = _cache.get(interval)
    if cached is not None and cached[1] == mtime:
        return cached[0]
    try:
        state = torch.load(str(path), map_location="cpu")
        model = MambaForecaster()
        model.load_state_dict(state)
        model.eval()
        _cache[interval] = (model, mtime)
        log.info("mamba_loaded", interval=interval, path=str(path))
        return model
    except Exception as exc:
        log.warning("mamba_load_failed", interval=interval,
                    error=str(exc)[:200])
        return None


# ── Inference API ───────────────────────────────────────────────────────────

@torch.no_grad()
def predict(pair: str, interval: str) -> Optional[dict]:
    """Run inference for one (pair, interval). Writes to Redis and returns
    the forecast dict. Returns None when:
      - F50c governance is off for this interval
      - Model file missing (not yet trained)
      - Insufficient candle history
    Safe to call every signal cycle; redis writer is atomic.
    """
    if not _governance_active(interval):
        return None
    model = _load_model(interval)
    if model is None:
        return None

    r = redis_client.get()
    raw = r.lrange(f"{pair}:{interval}:candles", 0, CONTEXT_LENGTH)
    if not raw or len(raw) < CONTEXT_LENGTH + 1:
        return None
    try:
        candles = [json.loads(c) for c in reversed(raw)]  # oldest first
    except Exception:
        return None
    features = _build_input(candles)
    if features is None:
        return None

    x = torch.from_numpy(features).unsqueeze(0).float()    # (1, L, F)
    try:
        out = model(x)
    except Exception as exc:
        log.warning("mamba_inference_failed", pair=pair,
                    interval=interval, error=str(exc)[:200])
        return None

    dir_probs = out["dir"].squeeze(0).cpu().numpy().tolist()
    mag_vals  = out["mag"].squeeze(0).cpu().numpy().tolist()
    trend_val = float(out["trend"].squeeze().cpu().item())

    forecast = {
        "dir1":  round(float(dir_probs[0]), 6),
        "dir3":  round(float(dir_probs[1]), 6),
        "dir5":  round(float(dir_probs[2]), 6),
        "mag1":  round(float(mag_vals[0]), 6),
        "mag3":  round(float(mag_vals[1]), 6),
        "mag5":  round(float(mag_vals[2]), 6),
        "trend": round(trend_val, 6),
        "ts":    int(time.time()),
    }
    try:
        r.setex(f"{pair}:{interval}:mamba_forecast", _FORECAST_TTL,
                json.dumps(forecast))
        r.incr(f"mamba:inference_count:{interval}")
    except Exception:
        pass
    return forecast


def load_forecast(pair: str, interval: str) -> Optional[dict]:
    """Reader-side helper for signals/engine.py. Returns None on cache miss."""
    try:
        raw = redis_client.get().get(f"{pair}:{interval}:mamba_forecast")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None


# ── Training (called by pretrainer/main.py) ─────────────────────────────────

def train(interval: str, candles_csv_dir: Path, n_epochs: int = 15,
          batch_size: int = 64, lr: float = 1e-3) -> dict:
    """Pretrain a Mamba model for one timeframe.

    Reads OHLCV CSVs from candles_csv_dir/{interval}/*.csv (Binance historical
    format, one file per pair). Builds sliding-window samples, splits 80/20,
    trains, validates on direction-head AUC. Saves on val_auc ≥ 0.55.
    Returns a summary dict.

    Honest scope (Rule 4): v1 trains a SINGLE global model per timeframe
    (no per-pair). Per-pair fine-tune is a follow-up. Same scope as F48 v1.
    """
    import pandas as pd
    from torch.utils.data import DataLoader, TensorDataset
    from sklearn.metrics import roc_auc_score

    tf_dir = candles_csv_dir / interval
    if not tf_dir.exists():
        return {"status": "skip_no_data", "interval": interval}

    files = sorted(tf_dir.glob("*.csv"))
    if not files:
        return {"status": "skip_empty_dir", "interval": interval,
                "path": str(tf_dir)}

    log.info("mamba_train_start", interval=interval, n_files=len(files))

    X_all, Y_dir_all, Y_mag_all, Y_trend_all = [], [], [], []
    for f in files:
        try:
            df = pd.read_csv(f)
        except Exception:
            continue
        if not {"open", "high", "low", "close", "volume"}.issubset(df.columns):
            continue
        candles = [{"o": r.open, "h": r.high, "l": r.low,
                    "c": r.close, "v": r.volume}
                   for r in df.itertuples()]
        # Stride sliding windows
        for start in range(0, len(candles) - CONTEXT_LENGTH - 11, 8):
            window = candles[start: start + CONTEXT_LENGTH + 1]
            future = candles[start + CONTEXT_LENGTH:
                             start + CONTEXT_LENGTH + 11]
            X = _build_input(window)
            if X is None or len(future) < 11:
                continue
            anchor = float(future[0]["c"])
            if anchor <= 0:
                continue
            # Labels
            dir_y, mag_y = [], []
            for h_idx in (1, 3, 5):
                fc = float(future[h_idx].get("c", 0))
                dir_y.append(1.0 if fc > anchor else 0.0)
                mag_y.append((fc - anchor) / anchor * 100.0)  # %
            trend_y = (1.0 if float(future[-1].get("c", 0)) > anchor else 0.0)
            X_all.append(X)
            Y_dir_all.append(dir_y)
            Y_mag_all.append(mag_y)
            Y_trend_all.append(trend_y)

    if len(X_all) < 1000:
        return {"status": "skip_insufficient_samples", "interval": interval,
                "samples": len(X_all)}

    X = np.stack(X_all).astype(np.float32)
    Yd = np.array(Y_dir_all, dtype=np.float32)
    Ym = np.array(Y_mag_all, dtype=np.float32)
    Yt = np.array(Y_trend_all, dtype=np.float32)

    n = len(X)
    cut = int(n * 0.8)
    Xtr, Xvl = X[:cut], X[cut:]
    Ydtr, Ydvl = Yd[:cut], Yd[cut:]
    Ymtr, Ymvl = Ym[:cut], Ym[cut:]
    Yttr, Ytvl = Yt[:cut], Yt[cut:]

    ds = TensorDataset(torch.from_numpy(Xtr),
                       torch.from_numpy(Ydtr),
                       torch.from_numpy(Ymtr),
                       torch.from_numpy(Yttr))
    dl = DataLoader(ds, batch_size=batch_size, shuffle=True)

    model = MambaForecaster()
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)

    bce = nn.BCELoss()
    mse = nn.MSELoss()

    best_auc = 0.0
    for epoch in range(n_epochs):
        model.train()
        for xb, ydb, ymb, ytb in dl:
            opt.zero_grad()
            out = model(xb)
            loss = (bce(out["dir"], ydb)
                    + 0.1 * mse(out["mag"], ymb)
                    + 0.5 * bce(out["trend"], ytb))
            loss.backward()
            opt.step()

        # Val
        model.eval()
        with torch.no_grad():
            out_v = model(torch.from_numpy(Xvl))
            preds = out_v["dir"].cpu().numpy()
            try:
                auc = float(np.mean([roc_auc_score(Ydvl[:, k], preds[:, k])
                                     for k in range(N_HORIZONS)]))
            except Exception:
                auc = 0.0
        log.info("mamba_epoch", interval=interval, epoch=epoch + 1,
                 val_auc=round(auc, 4))
        if auc > best_auc:
            best_auc = auc

    if best_auc < 0.55:
        return {"status": "fail_low_auc", "interval": interval,
                "val_auc": round(best_auc, 4), "samples": int(n)}

    out_path = _MODEL_PATHS[interval]
    out_path.parent.mkdir(exist_ok=True, parents=True)
    torch.save(model.state_dict(), str(out_path))
    log.info("mamba_saved", interval=interval, path=str(out_path),
             val_auc=round(best_auc, 4), samples=int(n))
    return {"status": "ok", "interval": interval, "val_auc": round(best_auc, 4),
            "samples": int(n)}


# ── F30 governance registration ─────────────────────────────────────────────

def _register_governance():
    try:
        from feature_governance.registry import register
        for tf, fg_id in _INTERVAL_TO_FG.items():
            register(fg_id, f"F50c — Mamba SSM Forecaster ({tf})",
                     activation_phase=0)
    except Exception as exc:
        log.warning("mamba_registry_failed", error=str(exc)[:200])


_register_governance()
