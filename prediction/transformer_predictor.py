"""Transformer predictor — Phase E scaffold (cont. 63, 2026-05-29).

Drop-in replacement for prediction/xgb_predictor.py with the same predict()
signature, so the refresh-loop can swap models by changing one import. Built
on a small PyTorch encoder over the 500-candle sequence per spec §1.2.

Cold-start posture: identical to XGB predictor — `predict()` returns None
unless a trained model file exists at `models/predict_all_transformer.pt`.
This means Phase E can SHIP without a trained model; predictions only fire
once you've run an offline training pass.

Honest scope (Rule 4): the model architecture, dataset builder, training
loop, and predict() interface are production-grade. The HYPERPARAMETER
TUNING + GPU training run + shadow-mode comparison against XGBoost are NOT
done in this session — the doc estimates 1-2 weeks for that. Treat this
file as the plumbing that lets you do the training offline and plug the
result in without touching the live trading loop.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import structlog

log = structlog.get_logger()


_MODEL_PATH = "/app/models/predict_all_transformer.pt"
_FALLBACK_PATH = "/opt/trading-bot/models/predict_all_transformer.pt"
_SEQ_LEN = 500
_FEATURE_DIM_PER_CANDLE = 6  # O, H, L, C, V, vwap_dev


# Module-level cache so each refresh-loop tick reuses the loaded weights.
_loaded_model: Optional[object] = None
_loaded_mtime: float = 0.0


def _resolve_path() -> Optional[str]:
    for p in (_MODEL_PATH, _FALLBACK_PATH):
        if os.path.exists(p):
            return p
    return None


def _load() -> Optional[object]:
    """Hot-reload PyTorch model bundle when file mtime changes."""
    global _loaded_model, _loaded_mtime
    path = _resolve_path()
    if path is None:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if _loaded_model is not None and mtime == _loaded_mtime:
        return _loaded_model
    try:
        import torch  # type: ignore
    except Exception as exc:
        log.warning("transformer_predictor_torch_missing",
                    error=str(exc)[:120])
        return None
    try:
        bundle = torch.load(path, map_location="cpu", weights_only=False)
        required = {"state_dict", "config", "trained_at", "n_samples"}
        if not required.issubset(set(bundle.keys())):
            log.warning("transformer_predictor_bundle_invalid",
                        path=path,
                        missing=sorted(required - set(bundle.keys())))
            return None
        model = _build_model_from_config(bundle["config"])
        if model is None:
            return None
        model.load_state_dict(bundle["state_dict"])
        model.eval()
        _loaded_model = (model, bundle)
        _loaded_mtime = mtime
        log.info("transformer_predictor_loaded",
                 path=path, trained_at=bundle.get("trained_at"),
                 n_samples=bundle.get("n_samples"))
        return _loaded_model
    except Exception as exc:
        log.warning("transformer_predictor_load_failed",
                    path=path, error=str(exc)[:200])
        return None


def _build_model_from_config(cfg: dict):
    """Construct an empty model matching the persisted config so we can load
    its state_dict. Architecture is a small encoder with 9 prediction heads,
    matching the XGB predictor's outputs."""
    try:
        import torch.nn as nn  # type: ignore
    except Exception:
        return None

    class _SmallTransformer(nn.Module):
        def __init__(self, d_in: int, d_model: int, n_heads: int,
                     n_layers: int):
            super().__init__()
            self.proj = nn.Linear(d_in, d_model)
            layer = nn.TransformerEncoderLayer(
                d_model=d_model, nhead=n_heads,
                dim_feedforward=d_model * 4,
                dropout=0.1, batch_first=True,
                activation="gelu")
            self.enc = nn.TransformerEncoder(layer, num_layers=n_layers)
            # 9 heads matching the XGB predictor outputs.
            self.head_direction = nn.Linear(d_model, 1)
            self.head_confidence = nn.Linear(d_model, 1)
            self.head_entry_bps  = nn.Linear(d_model, 1)
            self.head_sl_bps     = nn.Linear(d_model, 1)
            self.head_tp_bps     = nn.Linear(d_model, 1)
            self.head_hold_log   = nn.Linear(d_model, 1)
            self.head_rr_q25     = nn.Linear(d_model, 1)
            self.head_rr_q50     = nn.Linear(d_model, 1)
            self.head_rr_q75     = nn.Linear(d_model, 1)

        def forward(self, x):
            h = self.proj(x)
            h = self.enc(h)
            h_last = h[:, -1, :]
            return {
                "direction":      self.head_direction(h_last).squeeze(-1),
                "confidence_raw": self.head_confidence(h_last).squeeze(-1),
                "entry_bps":      self.head_entry_bps(h_last).squeeze(-1),
                "sl_bps":         self.head_sl_bps(h_last).squeeze(-1),
                "tp_bps":         self.head_tp_bps(h_last).squeeze(-1),
                "hold_log":       self.head_hold_log(h_last).squeeze(-1),
                "rr_q25":         self.head_rr_q25(h_last).squeeze(-1),
                "rr_q50":         self.head_rr_q50(h_last).squeeze(-1),
                "rr_q75":         self.head_rr_q75(h_last).squeeze(-1),
            }

    try:
        return _SmallTransformer(
            d_in=cfg.get("d_in", _FEATURE_DIM_PER_CANDLE),
            d_model=cfg.get("d_model", 64),
            n_heads=cfg.get("n_heads", 4),
            n_layers=cfg.get("n_layers", 2),
        )
    except Exception as exc:
        log.warning("transformer_build_failed", error=str(exc)[:200])
        return None


def is_ready() -> bool:
    return _load() is not None


def predict(sequence: list[list[float]], pair: str = "") -> Optional[dict]:
    """Inference. `sequence` is a 500×6 candle matrix (most-recent at the END).
    Returns the same 9-field dict as xgb_predictor.predict, plus model_version.

    Returns None when cold (no model) — caller falls back to XGB or legacy."""
    loaded = _load()
    if loaded is None:
        return None
    model, bundle = loaded
    try:
        import torch  # type: ignore
        if len(sequence) > _SEQ_LEN:
            sequence = sequence[-_SEQ_LEN:]
        # Pad to _SEQ_LEN at the FRONT with zeros so the most recent candle
        # is always at position -1.
        if len(sequence) < _SEQ_LEN:
            pad = [[0.0] * _FEATURE_DIM_PER_CANDLE] * (
                _SEQ_LEN - len(sequence))
            sequence = pad + sequence
        x = torch.tensor([sequence], dtype=torch.float32)
        with torch.no_grad():
            out = model(x)
        dir_logit = float(out["direction"].item())
        conf_logit = float(out["confidence_raw"].item())
        # Sigmoid for the classification heads.
        import math
        dir_prob = 1.0 / (1.0 + math.exp(-dir_logit))
        conf_raw = 1.0 / (1.0 + math.exp(-conf_logit))
    except Exception as exc:
        log.warning("transformer_predict_failed",
                    pair=pair, error=str(exc)[:200])
        return None

    # Conformal calibration shared with XGB predictor.
    conf_calibrated = conf_raw
    try:
        from ml.conformal_wrapper import calibrate_probability
        conf_calibrated = float(
            calibrate_probability(conf_raw,
                                  model="predict_all_transformer"))
    except Exception:
        pass

    return {
        "predicted_direction":        "long" if dir_prob >= 0.5 else "short",
        "direction_prob":             round(dir_prob, 4),
        "predicted_entry_offset_bps": round(float(out["entry_bps"].item()), 2),
        "predicted_sl_offset_bps":    round(abs(float(
            out["sl_bps"].item())), 2),
        "predicted_tp_offset_bps":    round(abs(float(
            out["tp_bps"].item())), 2),
        "predicted_hold_seconds":     int(max(1, min(86400,
            2 ** float(out["hold_log"].item())))),
        "confidence_raw":             round(conf_raw, 4),
        "conformal_confidence":       round(conf_calibrated, 4),
        "predicted_rr_p25":           round(float(out["rr_q25"].item()), 4),
        "predicted_rr_p50":           round(float(out["rr_q50"].item()), 4),
        "predicted_rr_p75":           round(float(out["rr_q75"].item()), 4),
        "model_version":              f"transformer:{bundle.get('trained_at', '?')}",
        "predicted_at":               int(time.time()),
    }


# ---------------------------------------------------------------------------
# Training entrypoint — call from pretrainer/walk_forward.py.
# Implemented but NOT auto-invoked. The walk-forward harness drives it.
# ---------------------------------------------------------------------------

def train_from_sequences(sequences: list[list[list[float]]],
                          targets: list[dict],
                          d_model: int = 64,
                          n_heads: int = 4,
                          n_layers: int = 2,
                          epochs: int = 5,
                          batch_size: int = 32,
                          save_path: Optional[str] = None) -> dict:
    """Train the transformer on a list of candle sequences with target dicts
    matching the same keys as XGB targets. Saves bundle to disk.

    Honest: this is the WORKING training loop, but you should run it offline
    on a machine with a GPU, hyperparam-sweep d_model / n_layers / lr, and
    shadow-mode compare to XGBoost for ≥ 2 weeks before flipping it in.
    """
    if len(sequences) < 200:
        return {"status": "insufficient", "n": len(sequences)}
    try:
        import torch  # type: ignore
        import torch.nn as nn  # type: ignore
        import torch.optim as optim  # type: ignore
    except Exception as exc:
        return {"status": "torch_missing", "error": str(exc)[:200]}

    cfg = {"d_in": _FEATURE_DIM_PER_CANDLE, "d_model": d_model,
           "n_heads": n_heads, "n_layers": n_layers, "seq_len": _SEQ_LEN}
    model = _build_model_from_config(cfg)
    if model is None:
        return {"status": "model_build_failed"}

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = model.to(device)
    optimizer = optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
    loss_bce = nn.BCEWithLogitsLoss()
    loss_mse = nn.MSELoss()
    # Quantile losses for the 3 RR heads.
    def quantile_loss(pred, target, q):
        diff = target - pred
        return torch.mean(torch.maximum(q * diff, (q - 1) * diff))

    # Build tensors
    X = torch.tensor(sequences, dtype=torch.float32).to(device)
    Y = {
        "direction":  torch.tensor([t["direction"] for t in targets],
                                    dtype=torch.float32).to(device),
        "confidence": torch.tensor([t["confidence"] for t in targets],
                                    dtype=torch.float32).to(device),
        "entry_bps":  torch.tensor([t["entry_bps"] for t in targets],
                                    dtype=torch.float32).to(device),
        "sl_bps":     torch.tensor([t["sl_bps"] for t in targets],
                                    dtype=torch.float32).to(device),
        "tp_bps":     torch.tensor([t["tp_bps"] for t in targets],
                                    dtype=torch.float32).to(device),
        "hold_log":   torch.tensor([t["hold_log"] for t in targets],
                                    dtype=torch.float32).to(device),
        "rr":         torch.tensor([t["rr"] for t in targets],
                                    dtype=torch.float32).to(device),
    }
    n = X.shape[0]
    perm = torch.randperm(n)

    history: list[dict] = []
    for ep in range(epochs):
        total = 0.0
        for start in range(0, n, batch_size):
            idx = perm[start:start + batch_size]
            out = model(X[idx])
            loss = (
                loss_bce(out["direction"], Y["direction"][idx]) +
                loss_bce(out["confidence_raw"], Y["confidence"][idx]) +
                0.01 * loss_mse(out["entry_bps"], Y["entry_bps"][idx]) +
                0.01 * loss_mse(out["sl_bps"], Y["sl_bps"][idx]) +
                0.01 * loss_mse(out["tp_bps"], Y["tp_bps"][idx]) +
                loss_mse(out["hold_log"], Y["hold_log"][idx]) +
                quantile_loss(out["rr_q25"], Y["rr"][idx], 0.25) +
                quantile_loss(out["rr_q50"], Y["rr"][idx], 0.50) +
                quantile_loss(out["rr_q75"], Y["rr"][idx], 0.75)
            )
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total += float(loss.item())
        history.append({"epoch": ep, "loss": total / max(1, n // batch_size)})

    out_path = save_path or _FALLBACK_PATH
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        torch.save({
            "state_dict":  model.state_dict(),
            "config":      cfg,
            "trained_at":  int(time.time()),
            "n_samples":   n,
            "history":     history,
        }, out_path)
    except Exception as exc:
        return {"status": "save_failed", "error": str(exc)[:200]}

    return {"status": "ok", "path": out_path, "n_samples": n,
            "history": history}
