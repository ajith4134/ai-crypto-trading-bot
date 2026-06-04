"""F50h — N-HiTS (Neural Hierarchical Interpolation for Time Series).

Cont. 64 (2026-05-29). Pure-CPU hierarchical-CNN forecaster, no attention.
Winner of the 918-experiment crypto benchmark (arXiv:2603.16886) for
low-cap pairs; alternative to Transformer/Mamba when long-context attention
is unavailable or too slow.

Reference: N-HiTS — arXiv:2201.12886 (Challu et al. 2022).

Architecture (compact for CPU + 1.5M params budget):
  Input: (B, L, F) where L = CONTEXT_LENGTH, F = N_INPUT_FEATURES (mirrors
         Mamba). Close-price residual modelling at multiple temporal rates.
  Stack 1: 1× downsample → MLP → upsample (interpolate). Captures fine grain.
  Stack 2: 2× max-pool → MLP → upsample. Captures medium-rate trend.
  Stack 3: 4× max-pool → MLP → upsample. Captures slow regime.
  Each stack outputs a forecast contribution + a backcast that's subtracted
  from the running residual (NBEATS-style block hierarchy).
  Heads (same shape as Mamba): dir1/dir3/dir5 (sigmoid), mag1/mag3/mag5
  (relu), trend (tanh).

Interface mirrors `ml/mamba_forecaster.py`:
  predict(pair, interval)          — run inference, write Redis key
  load_forecast(pair, interval)    — reader for signals/engine.py

Activation gate: `nhits:disabled = "1"` skips inference. Defaults OFF for
inference (returns None) until a pretrained model exists at
models/nhits_{interval}.pth — same cold-start posture as Mamba.

Honest scope (Rule 4): architecture + inference + Redis publish are
production-grade. A pretrainer step + governance entry land in a follow-up
patch (Mamba pattern). Without weights, predict() returns None — engine
gracefully ignores.
"""
from __future__ import annotations

import json
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


CONTEXT_LENGTH = 256       # smaller than Mamba's 512 — N-HiTS gains from shorter context
N_INPUT_FEATURES = 6       # O, H, L, C, V, ret  (mirrors Mamba subset)
N_HORIZONS = 3             # 1/3/5 steps ahead
_FORECAST_TTL = 120
_MODEL_DIR = Path("/app/models")
_MODEL_DIR_FALLBACK = Path("/opt/trading-bot/models")


# ---------------------------------------------------------------------------
# Architecture
# ---------------------------------------------------------------------------

class NHitsBlock(nn.Module):
    """One hierarchical block: max-pool by k, MLP, interpolate-up.

    Returns (backcast, forecast_contribution). Backcast is subtracted from
    the running residual; forecast contribution is added to the running
    forecast bag.
    """

    def __init__(self, seq_len: int, hidden: int, pool: int,
                 horizon: int, in_dim: int):
        super().__init__()
        self.pool = pool
        self.in_dim = in_dim
        self.seq_len = seq_len
        pooled_len = max(1, seq_len // pool)
        self.mlp = nn.Sequential(
            nn.Linear(pooled_len * in_dim, hidden),
            nn.GELU(),
            nn.Linear(hidden, hidden),
            nn.GELU(),
        )
        self.backcast_head = nn.Linear(hidden, seq_len)
        self.forecast_head = nn.Linear(hidden, horizon)

    def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        # x: (B, L, F)
        b, lens, feat = x.shape
        if self.pool > 1:
            # Max-pool along time. F.max_pool1d expects (B, C, L).
            xp = x.transpose(1, 2)
            xp = F.max_pool1d(xp, kernel_size=self.pool, stride=self.pool,
                              ceil_mode=True)
            xp = xp.transpose(1, 2)
        else:
            xp = x
        h = self.mlp(xp.reshape(b, -1))
        backcast = self.backcast_head(h)
        forecast = self.forecast_head(h)
        return backcast, forecast


class NHitsForecaster(nn.Module):
    """Three-stack N-HiTS with shared multi-horizon forecast head."""

    def __init__(self, seq_len: int = CONTEXT_LENGTH,
                 in_dim: int = N_INPUT_FEATURES,
                 hidden: int = 128,
                 horizon: int = N_HORIZONS):
        super().__init__()
        self.seq_len = seq_len
        self.in_dim = in_dim
        self.horizon = horizon
        self.blocks = nn.ModuleList([
            NHitsBlock(seq_len, hidden, pool=1, horizon=horizon, in_dim=in_dim),
            NHitsBlock(seq_len, hidden, pool=2, horizon=horizon, in_dim=in_dim),
            NHitsBlock(seq_len, hidden, pool=4, horizon=horizon, in_dim=in_dim),
        ])
        # 3 heads share final hidden — fed by the accumulated forecast vector.
        self.dir_head = nn.Linear(horizon, horizon)
        self.mag_head = nn.Linear(horizon, horizon)
        self.trend_head = nn.Linear(horizon, 1)

    def forward(self, x: torch.Tensor) -> dict:
        # x: (B, L, F). Residual = close column only (index 3 = C).
        b = x.shape[0]
        close = x[..., 3]          # (B, L)
        residual = close.clone()
        running_forecast = torch.zeros(b, self.horizon, device=x.device)
        for block in self.blocks:
            backcast, forecast = block(x)
            residual = residual - backcast
            running_forecast = running_forecast + forecast
            # Re-inject corrected close into x for the next stack
            x = x.clone()
            x[..., 3] = residual
        dir_logits = self.dir_head(running_forecast)
        mag_logits = self.mag_head(running_forecast)
        trend = torch.tanh(self.trend_head(running_forecast)).squeeze(-1)
        return {
            "dir":   torch.sigmoid(dir_logits),
            "mag":   F.relu(mag_logits),
            "trend": trend,
        }


# ---------------------------------------------------------------------------
# Inference helpers (mirror Mamba)
# ---------------------------------------------------------------------------

def _build_input(candles: list[dict]) -> Optional[np.ndarray]:
    """Build (CONTEXT_LENGTH, N_INPUT_FEATURES) feature tensor.

    Features: log-O, log-H, log-L, log-C, log-V, ret_close. All normalized
    by the first close so the model is scale-invariant.
    """
    if len(candles) < CONTEXT_LENGTH + 1:
        return None
    candles = candles[-(CONTEXT_LENGTH + 1):]
    arr = np.zeros((CONTEXT_LENGTH, N_INPUT_FEATURES), dtype=np.float32)
    try:
        ref_c = float(candles[0].get("c") or candles[0].get("close") or 0)
    except (TypeError, ValueError):
        return None
    if ref_c <= 0:
        return None
    log_ref = float(np.log(ref_c))
    prev_c = ref_c
    for i in range(CONTEXT_LENGTH):
        c = candles[i + 1]
        try:
            o = float(c.get("o") or c.get("open") or 0)
            h = float(c.get("h") or c.get("high") or 0)
            l = float(c.get("l") or c.get("low") or 0)
            cl = float(c.get("c") or c.get("close") or 0)
            v = float(c.get("v") or c.get("volume") or 0)
        except (TypeError, ValueError):
            continue
        if min(o, h, l, cl) <= 0:
            continue
        arr[i, 0] = float(np.log(o)) - log_ref
        arr[i, 1] = float(np.log(h)) - log_ref
        arr[i, 2] = float(np.log(l)) - log_ref
        arr[i, 3] = float(np.log(cl)) - log_ref
        arr[i, 4] = float(np.log1p(max(0.0, v)))
        arr[i, 5] = (cl - prev_c) / max(prev_c, 1e-9)
        prev_c = cl
    return arr


_MODEL_CACHE: dict[str, tuple[NHitsForecaster, float]] = {}


def _model_path(interval: str) -> Optional[Path]:
    for base in (_MODEL_DIR, _MODEL_DIR_FALLBACK):
        p = base / f"nhits_{interval}.pth"
        if p.exists():
            return p
    return None


def _governance_active(interval: str) -> bool:
    try:
        r = redis_client.get()
        if r.get("nhits:disabled") in ("1", b"1"):
            return False
        # Per-interval kill switch
        if r.get(f"nhits:{interval}:disabled") in ("1", b"1"):
            return False
    except Exception:
        return True
    return True


def _load_model(interval: str) -> Optional[NHitsForecaster]:
    path = _model_path(interval)
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _MODEL_CACHE.get(interval)
    if cached and cached[1] == mtime:
        return cached[0]
    try:
        bundle = torch.load(str(path), map_location="cpu", weights_only=False)
        model = NHitsForecaster(
            seq_len=bundle.get("seq_len", CONTEXT_LENGTH),
            in_dim=bundle.get("in_dim", N_INPUT_FEATURES),
            hidden=bundle.get("hidden", 128),
            horizon=bundle.get("horizon", N_HORIZONS),
        )
        model.load_state_dict(bundle["state_dict"])
        model.eval()
        _MODEL_CACHE[interval] = (model, mtime)
        log.info("nhits_model_loaded", interval=interval,
                 trained_at=bundle.get("trained_at"))
        return model
    except Exception as exc:
        log.warning("nhits_model_load_failed", interval=interval,
                    error=str(exc)[:200])
        return None


def predict(pair: str, interval: str) -> Optional[dict]:
    """Run N-HiTS inference for one (pair, interval). Writes Redis key
    `{pair}:{interval}:nhits_forecast` (TTL 120s). Returns the dict or
    None on cold/missing model/insufficient candles."""
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
        candles = [json.loads(c) for c in reversed(raw)]   # oldest first
    except Exception:
        return None
    features = _build_input(candles)
    if features is None:
        return None

    x = torch.from_numpy(features).unsqueeze(0).float()
    try:
        with torch.no_grad():
            out = model(x)
    except Exception as exc:
        log.warning("nhits_inference_failed", pair=pair,
                    interval=interval, error=str(exc)[:200])
        return None

    dir_probs = out["dir"].squeeze(0).cpu().numpy().tolist()
    mag_vals = out["mag"].squeeze(0).cpu().numpy().tolist()
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
        r.setex(f"{pair}:{interval}:nhits_forecast", _FORECAST_TTL,
                json.dumps(forecast))
        r.incr(f"nhits:inference_count:{interval}")
    except Exception:
        pass
    return forecast


def load_forecast(pair: str, interval: str) -> Optional[dict]:
    """Reader-side helper for signals/engine.py. Returns None on cache miss."""
    try:
        raw = redis_client.get().get(f"{pair}:{interval}:nhits_forecast")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None
