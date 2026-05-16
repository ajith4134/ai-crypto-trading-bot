"""L-02: TFT (Temporal Fusion Transformer) price forecasting."""
import json
from pathlib import Path
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()
_MODEL_PATH = Path("models/tft.pth")
_model = None


def _load():
    global _model
    if _model is None and _MODEL_PATH.exists():
        import torch
        _model = torch.load(_MODEL_PATH, map_location="cpu")
        _model.eval()
    return _model


def get_price_forecast(pair: str, timeframe: str) -> dict:
    """Return quantile forecast dict; write to Redis."""
    r = redis_client.get()
    cached = r.get(redis_keys.PRICE_FORECAST.replace("{pair}", pair).replace("{interval}", timeframe))
    if cached:
        return json.loads(cached)

    if not _MODEL_PATH.exists():
        return {}

    try:
        model = _load()
        candles_raw = r.lrange(redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", timeframe), 0, 99)
        if len(candles_raw) < 10:
            return {}
        import torch, numpy as np
        closes = [float(json.loads(c)["c"]) for c in reversed(candles_raw)]
        arr = torch.tensor(closes, dtype=torch.float32).unsqueeze(0).unsqueeze(-1)
        with torch.no_grad():
            out = model(arr)
        forecast = {"q10": float(out[0][0]), "q50": float(out[0][1]), "q90": float(out[0][2])}
        r.set(
            redis_keys.PRICE_FORECAST.replace("{pair}", pair).replace("{interval}", timeframe),
            json.dumps(forecast), ex=300,
        )
        return forecast
    except Exception as exc:
        log.error("tft_forecast_failed", pair=pair, error=str(exc))
        return {}
