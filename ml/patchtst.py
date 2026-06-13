"""L-03 / Blueprint Feature 20 — PatchTST long-sequence forecast wrapper.

Pre-fix this module was DEAD CODE — defined `get_longsequence_forecast` but no
caller existed anywhere in the codebase. Now:
  Producer: data/feed.py periodically calls get_longsequence_forecast for top pairs
  Consumer: signals/engine.py reads LONGSEQ_FORECAST to compute patchtst_bonus

Architecture: real HF transformers PatchTSTForPrediction (see ml/architectures.PatchTSTModel).
Input contract: 256 hourly closes, normalized per-sequence by the first close
(scale-invariant, same as TFT). Output is converted back to absolute prices.
"""
import json
from pathlib import Path
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()
_MODEL_PATH = Path("models/patchtst.pth")
_model      = None
_last_mtime = 0.0

CONTEXT_LENGTH    = 256   # must match architectures.PatchTSTModel.CONTEXT_LENGTH
PREDICTION_LENGTH = 16    # must match architectures.PatchTSTModel.PREDICTION_LENGTH


def _load():
    """Mtime-tracked load (matches direction_model / world_model / tft / gnn pattern)."""
    global _model, _last_mtime
    if not _MODEL_PATH.exists():
        return _model
    mtime = _MODEL_PATH.stat().st_mtime
    if _model is not None and mtime == _last_mtime:
        return _model
    try:
        import torch
        from ml.architectures import PatchTSTModel, inject_into_main
        inject_into_main()
        checkpoint = torch.load(_MODEL_PATH, map_location="cpu", weights_only=False)
        if isinstance(checkpoint, dict):
            m = PatchTSTModel()
            try:
                m.load_state_dict(checkpoint)
            except Exception as exc:
                log.warning("patchtst_state_dict_mismatch_must_repretrain", error=str(exc)[:200])
                return _model
            _model = m
        else:
            log.warning("patchtst_legacy_full_pickle_must_repretrain")
            return _model
        _model.eval()
        _last_mtime = mtime
        log.info("patchtst_model_loaded", mtime=round(mtime, 2))
    except Exception as exc:
        log.warning("patchtst_load_failed", error=str(exc)[:200])
    return _model


def get_longsequence_forecast(pair: str) -> dict:
    """Return long-range forecast for pair. Writes to LONGSEQ_FORECAST Redis key.

    Returns dict:
      predicted_final_price : absolute price at end of 16-hour horizon
      predicted_change_pct  : relative change vs current (× 100, so 1.5 = +1.5%)
      horizon_hours         : 16
    Empty dict on any failure / insufficient data.
    """
    r = redis_client.get()
    cached = r.get(redis_keys.LONGSEQ_FORECAST.replace("{pair}", pair))
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    if not _MODEL_PATH.exists():
        return {}

    try:
        # F30 governance gate
        try:
            from feature_governance.registry import is_active
            if not is_active("F20"):
                return {}
        except Exception:
            pass

        model = _load()
        if model is None:
            return {}

        import torch
        candles_raw = r.lrange(
            redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1h"),
            0, CONTEXT_LENGTH - 1,
        )
        if len(candles_raw) < CONTEXT_LENGTH:
            # PatchTST needs full 256-step context — without it the model can't run
            return {}
        # CANDLES is newest-first; reverse to chronological
        closes = [float(json.loads(c)["c"]) for c in reversed(candles_raw)]

        # Normalization contract — must match pretrainer/main.py:train_patchtst
        anchor = closes[0] if closes[0] > 0 else 1.0
        closes_norm = [c / anchor for c in closes]
        x = torch.tensor(closes_norm, dtype=torch.float32).unsqueeze(0).unsqueeze(-1)  # [1, 256, 1]

        with torch.no_grad():
            pred = model(x)  # [1, 16] — predicted relative prices for next 16 hours
        pred_list = pred.squeeze(0).tolist()

        # Take the final-horizon prediction (16 hours ahead)
        final_relative = float(pred_list[-1])
        predicted_final_price = final_relative * anchor
        current_price = closes[-1]
        predicted_change_pct = (predicted_final_price - current_price) / current_price * 100 if current_price > 0 else 0.0

        forecast = {
            "predicted_final_price": round(predicted_final_price, 8),
            "predicted_change_pct": round(predicted_change_pct, 4),
            "horizon_hours": PREDICTION_LENGTH,
        }
        r.set(redis_keys.LONGSEQ_FORECAST.replace("{pair}", pair), json.dumps(forecast), ex=1800)
        return forecast
    except Exception as exc:
        log.error("patchtst_forecast_failed", pair=pair, error=str(exc)[:200])
        return {}


def get_longsequence_forecast_batch(pairs: list[str], chunk: int = 128) -> dict[str, dict]:
    """cont. 74 — BATCHED forecast. Stacks every pair with full 256-step context into
    one tensor and runs a SINGLE forward pass per chunk, instead of one pass per pair.
    PatchTST (a transformer) batches the sequence dim natively, so this is ~N× faster
    than looping get_longsequence_forecast (measured: per-pair loop = ~6s/pair → the
    batch does hundreds of pairs in a few seconds). Writes all LONGSEQ_FORECAST keys via
    a single Redis pipeline. Returns {pair: forecast}. Chunked to bound peak memory.
    """
    out: dict[str, dict] = {}
    if not pairs or not _MODEL_PATH.exists():
        return out
    try:
        from feature_governance.registry import is_active
        if not is_active("F20"):
            return out
    except Exception:
        pass
    model = _load()
    if model is None:
        return out
    import torch
    r = redis_client.get()
    # Gather inputs (skip pairs without full context — same contract as the single path)
    rows = []  # (pair, anchor, current_price, normalized_closes)
    for pair in pairs:
        try:
            candles_raw = r.lrange(
                redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1h"),
                0, CONTEXT_LENGTH - 1)
            if len(candles_raw) < CONTEXT_LENGTH:
                continue
            closes = [float(json.loads(c)["c"]) for c in reversed(candles_raw)]
            anchor = closes[0] if closes[0] > 0 else 1.0
            rows.append((pair, anchor, closes[-1], [c / anchor for c in closes]))
        except Exception:
            continue
    if not rows:
        return out
    for i in range(0, len(rows), max(1, chunk)):
        block = rows[i:i + chunk]
        try:
            X = torch.tensor([b[3] for b in block], dtype=torch.float32).unsqueeze(-1)  # [B,256,1]
            with torch.no_grad():
                preds = model(X)  # [B, 16]
            preds = preds.tolist()
        except Exception as exc:
            log.error("patchtst_batch_forward_failed", n=len(block), error=str(exc)[:200])
            continue
        pipe = r.pipeline()
        for (pair, anchor, current, _), prow in zip(block, preds):
            try:
                final_relative = float(prow[-1])
                predicted_final_price = final_relative * anchor
                chg = (predicted_final_price - current) / current * 100 if current > 0 else 0.0
                fc = {
                    "predicted_final_price": round(predicted_final_price, 8),
                    "predicted_change_pct": round(chg, 4),
                    "horizon_hours": PREDICTION_LENGTH,
                }
                out[pair] = fc
                pipe.set(redis_keys.LONGSEQ_FORECAST.replace("{pair}", pair),
                         json.dumps(fc), ex=1800)
            except Exception:
                continue
        try:
            pipe.execute()
        except Exception as exc:
            log.warning("patchtst_batch_pipe_failed", error=str(exc)[:150])
    return out
