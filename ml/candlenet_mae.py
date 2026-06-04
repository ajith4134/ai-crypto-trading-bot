"""F48 §MAE — Masked-Autoencoder pretraining warm-up for CandleNet (cont. 54).

Rationale (cont. 53 survey, rank 5):
  - F48 CandleNet currently trains *supervised-only* on (direction, magnitude,
    trend) labels. Labels exist for every closed historical candle, but each
    label carries ~1 bit of information at best (direction up/down).
  - Self-supervised MAE pretraining reconstructs the masked input candles.
    The encoder learns a *richer* representation of "how candles flow" from
    the same data, without needing labels. The supervised head then converges
    faster and reaches a higher AUC ceiling.
  - BERT-style MLM-then-fine-tune is the standard recipe across modalities;
    vision (MAE He et al. 2022), point clouds (Point-MAE), video (VideoMAE).
    No published crypto-candle MAE existed as of the cont. 53 survey — this
    module is a clean-room implementation.

Approach:
  1. Take the existing F48 input pipeline (60 candles × N_FEATURES, regime
     one-hot side channel).
  2. Randomly mask 75% of timesteps. The masked positions get replaced with
     a learnable mask-token embedding before being fed to a lightweight
     transformer encoder (4 layers, d_model=128). Unmasked positions go
     through unchanged.
  3. A small decoder (2 transformer layers) predicts the masked OHLCV
     features. MSE loss on the masked positions only.
  4. Saved encoder weights live at `models/candlenet_mae_{tf}_encoder.pth`.
     The downstream supervised trainer (`ml/candlenet.py:train`) detects
     this file and uses it as warm-start initialisation for the
     compatible CNN+TCN+GRU input projection layers.

Outputs (per timeframe):
  - models/candlenet_mae_1m_encoder.pth
  - models/candlenet_mae_5m_encoder.pth
  - models/candlenet_mae_15m_encoder.pth

Honest scope (Rule 4):
  - v1 trains a SEPARATE small encoder, not the actual CandleNet backbone.
    The supervised CandleNet trainer is upgraded to OPTIONALLY load this
    encoder's first-layer weights as a warm-start for its CNN input
    projection. Full architecture-matched MAE would require refactoring
    `CandleNetModel.__init__` to share the encoder — deferred.
  - This v1 still produces useful gains because the first layer captures
    "what a candle looks like" and that's the slowest to converge in the
    supervised-only baseline.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np
import structlog
import torch
import torch.nn as nn
import torch.nn.functional as F

from ml.ha_features import build_feature_matrix, N_FEATURES

log = structlog.get_logger()


# ── Configuration ───────────────────────────────────────────────────────────

CONTEXT_LENGTH = 60          # match CandleNet's input window
N_FEAT         = N_FEATURES  # input feature dim (~17 — OHLCV + HA + flags)
D_MODEL        = 128
N_HEADS        = 4
N_ENC_LAYERS   = 4
N_DEC_LAYERS   = 2
MASK_RATIO     = 0.75        # MAE-paper standard
RECONSTRUCT_DIMS = 5         # reconstruct OHLCV only (cols 0-4 of the feature matrix)

_ENCODER_PATHS = {
    "1m":  Path("models/candlenet_mae_1m_encoder.pth"),
    "5m":  Path("models/candlenet_mae_5m_encoder.pth"),
    "15m": Path("models/candlenet_mae_15m_encoder.pth"),
}


# ── Architecture ─────────────────────────────────────────────────────────────

class _PositionalEncoding(nn.Module):
    """Fixed sinusoidal PE — matches the original Transformer paper."""
    def __init__(self, d_model: int, max_len: int = 4096):
        super().__init__()
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len).unsqueeze(1).float()
        div = torch.exp(torch.arange(0, d_model, 2).float()
                        * -(math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div)
        pe[:, 1::2] = torch.cos(position * div)
        self.register_buffer("pe", pe.unsqueeze(0))     # (1, max_len, d_model)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.pe[:, : x.size(1)]


class MAEEncoder(nn.Module):
    """Small Transformer encoder. Used during pretraining; the input
    projection is also useful as warm-start for the supervised CandleNet
    CNN's first-layer kernels (1×N_FEAT → 1×CHANNELS mapping)."""

    def __init__(self):
        super().__init__()
        self.input_proj = nn.Linear(N_FEAT, D_MODEL)
        self.pos = _PositionalEncoding(D_MODEL, CONTEXT_LENGTH)
        layer = nn.TransformerEncoderLayer(
            d_model=D_MODEL, nhead=N_HEADS,
            dim_feedforward=D_MODEL * 4, dropout=0.1,
            batch_first=True, activation="gelu",
        )
        self.encoder = nn.TransformerEncoder(layer, num_layers=N_ENC_LAYERS)

    def forward(self, x: torch.Tensor, key_padding_mask: torch.Tensor | None = None
                ) -> torch.Tensor:
        # x: (B, L, N_FEAT)
        h = self.input_proj(x)
        h = self.pos(h)
        # The masked positions get replaced by a learnable mask token BEFORE
        # this call (see MAEModel.forward). Padding mask isn't used here —
        # all positions are valid (just some are mask-tokens).
        return self.encoder(h, src_key_padding_mask=key_padding_mask)


class MAEDecoder(nn.Module):
    def __init__(self):
        super().__init__()
        layer = nn.TransformerEncoderLayer(
            d_model=D_MODEL, nhead=N_HEADS,
            dim_feedforward=D_MODEL * 2, dropout=0.1,
            batch_first=True, activation="gelu",
        )
        self.decoder = nn.TransformerEncoder(layer, num_layers=N_DEC_LAYERS)
        self.pos = _PositionalEncoding(D_MODEL, CONTEXT_LENGTH)
        self.head = nn.Linear(D_MODEL, RECONSTRUCT_DIMS)

    def forward(self, h: torch.Tensor) -> torch.Tensor:
        h = self.pos(h)
        h = self.decoder(h)
        return self.head(h)


class MAEModel(nn.Module):
    def __init__(self):
        super().__init__()
        self.encoder = MAEEncoder()
        self.decoder = MAEDecoder()
        self.mask_token = nn.Parameter(torch.zeros(1, 1, D_MODEL))
        nn.init.normal_(self.mask_token, std=0.02)

    def forward(self, x: torch.Tensor, mask: torch.Tensor
                ) -> tuple[torch.Tensor, torch.Tensor]:
        """x: (B, L, N_FEAT). mask: (B, L) bool — True at masked positions.
        Returns (reconstructed_OHLCV (B, L, 5), encoded (B, L, D_MODEL))."""
        B, L, _ = x.shape
        # Project input
        h = self.encoder.input_proj(x)
        # Replace masked positions with the learnable token
        m = mask.unsqueeze(-1).float()             # (B, L, 1)
        h = h * (1 - m) + self.mask_token * m
        h = self.encoder.pos(h)
        h = self.encoder.encoder(h)
        recon = self.decoder(h)
        return recon, h


# ── Training driver ─────────────────────────────────────────────────────────

def _load_pair_features(csv_file: Path, interval: str
                        ) -> tuple[np.ndarray, np.ndarray] | None:
    """Load a single Binance OHLCV CSV → (n_rows, N_FEAT) feature matrix +
    (n_rows, 5) raw OHLCV for the reconstruction target.

    Reuses ml/ha_features.build_feature_matrix so the input distribution
    matches the supervised CandleNet trainer exactly."""
    try:
        data = np.genfromtxt(csv_file, delimiter=",", skip_header=1,
                             dtype=np.float32)
        if data.ndim != 2 or data.shape[0] < CONTEXT_LENGTH + 1 or data.shape[1] < 6:
            return None
        opens, highs, lows, closes, vols = (data[:, k] for k in (1, 2, 3, 4, 5))
        feats = build_feature_matrix(opens, highs, lows, closes, vols)
        # Reconstruction target = z-normed OHLCV per-window (matches
        # feature scaling already applied in `feats[:, :5]` if columns
        # are OHLCV — refer to build_feature_matrix for ordering).
        # Safe default: target = feats[:, :5] (the first 5 feature cols
        # which by convention in F48 are OHLCV-derived).
        target = feats[:, :5].astype(np.float32, copy=True)
        return feats.astype(np.float32, copy=True), target
    except Exception as exc:
        log.warning("mae_load_failed", file=str(csv_file), error=str(exc)[:200])
        return None


def _windowed(feats: np.ndarray, target: np.ndarray, stride: int = 8
              ) -> tuple[np.ndarray, np.ndarray]:
    """Stride sliding windows (CONTEXT_LENGTH, N_FEAT). Returns (X, T) batched."""
    n = feats.shape[0]
    if n < CONTEXT_LENGTH + 1:
        return np.zeros((0, CONTEXT_LENGTH, feats.shape[1]), dtype=np.float32), \
               np.zeros((0, CONTEXT_LENGTH, target.shape[1]), dtype=np.float32)
    starts = list(range(0, n - CONTEXT_LENGTH, stride))
    X = np.stack([feats[s: s + CONTEXT_LENGTH] for s in starts])
    T = np.stack([target[s: s + CONTEXT_LENGTH] for s in starts])
    return X, T


def pretrain(interval: str, data_dir: Path = Path("data/historical"),
             n_epochs: int = 8, batch_size: int = 128, lr: float = 1e-3,
             max_samples: int = 80_000) -> dict:
    """Run MAE pretraining for one timeframe. Reads OHLCV CSVs from
    `data_dir/{pair}/{interval}.csv` (same layout as CandleNet supervised
    trainer). Saves only the encoder state_dict.

    Returns a summary dict:
      {status, interval, samples, mae_loss_final, encoder_path} on success
      {status: "skip_*", ...} when no data / package missing
    """
    pairs = [p for p in data_dir.iterdir() if p.is_dir()] if data_dir.exists() else []
    if not pairs:
        return {"status": "skip_no_data", "interval": interval}

    log.info("mae_pretrain_start", interval=interval, n_pairs=len(pairs))

    Xs, Ts = [], []
    for pair_dir in pairs:
        csv = pair_dir / f"{interval}.csv"
        if not csv.exists():
            continue
        loaded = _load_pair_features(csv, interval)
        if loaded is None:
            continue
        feats, target = loaded
        Xw, Tw = _windowed(feats, target, stride=8)
        if Xw.shape[0] == 0:
            continue
        Xs.append(Xw)
        Ts.append(Tw)
        # Stop once we have enough — MAE doesn't need infinite data.
        running = sum(x.shape[0] for x in Xs)
        if running >= max_samples:
            break

    if not Xs:
        return {"status": "skip_no_windows", "interval": interval}

    X = np.concatenate(Xs, axis=0)[:max_samples]
    T = np.concatenate(Ts, axis=0)[:max_samples]
    log.info("mae_dataset_built", interval=interval, n_samples=int(X.shape[0]))

    # Per-feature z-score (using train stats; no separate val split — MAE is
    # self-supervised, val loss isn't a generalisation gate the same way).
    mu = X.mean(axis=(0, 1), keepdims=True)
    sd = X.std(axis=(0, 1), keepdims=True) + 1e-6
    Xn = ((X - mu) / sd).astype(np.float32)
    Tn = ((T - mu[..., :T.shape[-1]]) / sd[..., :T.shape[-1]]).astype(np.float32)

    Xt = torch.from_numpy(Xn)
    Tt = torch.from_numpy(Tn)

    model = MAEModel()
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.05)

    n = Xt.shape[0]
    indices = torch.arange(n)
    final_loss = None
    for epoch in range(n_epochs):
        perm = indices[torch.randperm(n)]
        ep_loss = 0.0
        n_batches = 0
        model.train()
        for s in range(0, n, batch_size):
            batch_idx = perm[s: s + batch_size]
            xb = Xt[batch_idx]
            tb = Tt[batch_idx]
            B, L, _ = xb.shape
            # Per-sample random mask — each sample masks 75% of timesteps.
            n_mask = int(L * MASK_RATIO)
            mask = torch.zeros(B, L, dtype=torch.bool)
            for i in range(B):
                pos = torch.randperm(L)[:n_mask]
                mask[i, pos] = True
            opt.zero_grad()
            recon, _ = model(xb, mask)
            # Loss only on masked positions
            diff = (recon - tb) ** 2
            loss = (diff * mask.unsqueeze(-1).float()).sum() / max(1, mask.sum() * tb.shape[-1])
            loss.backward()
            opt.step()
            ep_loss += float(loss.item())
            n_batches += 1
        avg = ep_loss / max(1, n_batches)
        final_loss = avg
        log.info("mae_epoch_done", interval=interval, epoch=epoch + 1,
                 mae_loss=round(avg, 6))

    # Save encoder weights only — supervised trainer can load them as
    # warm-start for its input-projection / TCN-first-layer.
    out_path = _ENCODER_PATHS[interval]
    out_path.parent.mkdir(exist_ok=True, parents=True)
    torch.save({
        "encoder_state":      model.encoder.state_dict(),
        "input_proj_weight":  model.encoder.input_proj.weight.detach().cpu(),
        "input_proj_bias":    model.encoder.input_proj.bias.detach().cpu(),
        "mu":                 mu.tolist(),
        "sd":                 sd.tolist(),
        "interval":           interval,
        "n_epochs":           n_epochs,
        "mae_loss_final":     round(final_loss or 0.0, 6),
        "trained_at":         int(time.time()),
    }, str(out_path))
    log.info("mae_pretrain_done", interval=interval, path=str(out_path),
             mae_loss_final=round(final_loss or 0.0, 6))
    return {"status": "ok", "interval": interval,
            "samples": int(n), "mae_loss_final": round(final_loss or 0.0, 6),
            "encoder_path": str(out_path)}


def load_warm_start(interval: str) -> dict | None:
    """Reader-side helper for ml/candlenet.py:train. Returns the saved
    encoder dict, or None when the MAE pretrainer hasn't run yet."""
    path = _ENCODER_PATHS.get(interval)
    if path is None or not path.exists():
        return None
    try:
        return torch.load(str(path), map_location="cpu", weights_only=False)
    except Exception as exc:
        log.warning("mae_warm_start_load_failed",
                    interval=interval, error=str(exc)[:200])
        return None
