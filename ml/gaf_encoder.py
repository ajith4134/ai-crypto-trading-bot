"""F48 §Production Extension Idea F — Gramian Angular Field image encoder.

Encodes the last CONTEXT_LENGTH OHLC values as a 4-channel image preserving
temporal dependency in the image structure. A small ResNet-style CNN
compresses the (T, T, 4) image to a 128-dim embedding that is fused into
CandleNet's dense layer.

Research basis:
  arXiv:1901.05237 — "Encoding Candlesticks as Images for Pattern Classification
    Using CNNs" — foundational GAF-CNN paper.
  PMC:11935771 (2025) — GAF-CNN achieves 90-93% candlestick pattern
    classification accuracy on 3h/5h windows.

GAF formulation:
  1. Scale each channel X to [-1, 1] using min/max of the window
       X_scaled = (2*X - max(X) - min(X)) / (max(X) - min(X) + eps)
  2. Compute angular representation phi = arccos(X_scaled)
  3. GASF (Gramian Angular Summation Field) image:
       G[i, j] = cos(phi[i] + phi[j])
              = X_scaled[i] * X_scaled[j]
                - sqrt(1 - X_scaled[i]^2) * sqrt(1 - X_scaled[j]^2)

Each of O, H, L, C is encoded independently → 4-channel image.
"""
from __future__ import annotations
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


def _scale_to_unit(arr: np.ndarray) -> np.ndarray:
    """Scale a 1-D array to [-1, 1] using its own min/max."""
    a = arr.astype(np.float64)
    lo = float(a.min())
    hi = float(a.max())
    if hi - lo < 1e-12:
        return np.zeros_like(a, dtype=np.float32)
    out = (2.0 * a - hi - lo) / (hi - lo)
    # Clip to [-1, 1] in case of float error
    return np.clip(out, -1.0, 1.0).astype(np.float32)


def compute_gaf(opens, highs, lows, closes) -> np.ndarray:
    """Build a [T, T, 4] GASF tensor from raw OHLC arrays.

    Returns float32 ndarray of shape (T, T, 4) where T = len(opens).
    Values in [-1, 1].
    """
    o = _scale_to_unit(np.asarray(opens))
    h = _scale_to_unit(np.asarray(highs))
    l = _scale_to_unit(np.asarray(lows))
    c = _scale_to_unit(np.asarray(closes))

    def _gasf(x):
        sin = np.sqrt(np.clip(1.0 - x * x, 0.0, 1.0))
        # G[i,j] = x_i*x_j - sin_i*sin_j  (broadcasted)
        return (np.outer(x, x) - np.outer(sin, sin)).astype(np.float32)

    g_o = _gasf(o)
    g_h = _gasf(h)
    g_l = _gasf(l)
    g_c = _gasf(c)
    return np.stack([g_o, g_h, g_l, g_c], axis=-1)   # [T, T, 4]


class _ResBlock(nn.Module):
    """Single residual block: Conv → BN → ReLU → Conv → BN → +x → ReLU."""
    def __init__(self, ch: int):
        super().__init__()
        self.conv1 = nn.Conv2d(ch, ch, kernel_size=3, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(ch)
        self.conv2 = nn.Conv2d(ch, ch, kernel_size=3, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(ch)

    def forward(self, x):
        identity = x
        out = F.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        return F.relu(out + identity)


class GAFResNet(nn.Module):
    """Small ResNet over (B, 4, T, T) GAF images → 128-dim embedding.

    Architecture (kept small — input is already T×T where T=60):
      stem:    Conv2D(4→32, k=3, s=1) → BN → ReLU              [B, 32, T, T]
      down1:   Conv2D(32→64, k=3, s=2) → BN → ReLU             [B, 64, T/2, T/2]
      res1:    ResBlock(64)
      down2:   Conv2D(64→128, k=3, s=2) → BN → ReLU            [B, 128, T/4, T/4]
      res2:    ResBlock(128)
      pool:    AdaptiveAvgPool2d(1) → flatten                  [B, 128]
      proj:    Linear(128 → 128)                               [B, 128]
    """
    EMB_DIM = 128

    def __init__(self):
        super().__init__()
        self.stem = nn.Sequential(
            nn.Conv2d(4, 32, kernel_size=3, stride=1, padding=1, bias=False),
            nn.BatchNorm2d(32),
            nn.ReLU(inplace=True),
        )
        self.down1 = nn.Sequential(
            nn.Conv2d(32, 64, kernel_size=3, stride=2, padding=1, bias=False),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
        )
        self.res1 = _ResBlock(64)
        self.down2 = nn.Sequential(
            nn.Conv2d(64, 128, kernel_size=3, stride=2, padding=1, bias=False),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
        )
        self.res2 = _ResBlock(128)
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.proj = nn.Linear(128, self.EMB_DIM)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: [B, 4, T, T] image tensor → [B, 128] embedding."""
        h = self.stem(x)
        h = self.down1(h)
        h = self.res1(h)
        h = self.down2(h)
        h = self.res2(h)
        h = self.pool(h).flatten(1)        # [B, 128]
        return self.proj(h)
