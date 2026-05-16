"""L-03: PatchTST long-sequence forecast wrapper."""
import json
from pathlib import Path
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()
_MODEL_PATH = Path("models/patchtst.pth")
_model = None


def _load():
    global _model
    if _model is None and _MODEL_PATH.exists():
        import torch
        _model = torch.load(_MODEL_PATH, map_location="cpu")
        _model.eval()
    return _model


def get_longsequence_forecast(pair: str) -> dict:
    """Return long-range forecast dict; write to Redis per pair."""
    r = redis_client.get()
    cached = r.get(redis_keys.LONGSEQ_FORECAST.replace("{pair}", pair))
    if cached:
        return json.loads(cached)

    if not _MODEL_PATH.exists():
        return {}

    try:
        import torch
        model = _load()
        candles_raw = r.lrange(
            redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1h"), 0, 255
        )
        if len(candles_raw) < 32:
            return {}
        closes = [float(json.loads(c)["c"]) for c in reversed(candles_raw)]
        arr = torch.tensor(closes, dtype=torch.float32).unsqueeze(0).unsqueeze(-1)
        with torch.no_grad():
            out = model(arr)
        forecast = {"direction": "up" if float(out[0][-1]) > float(out[0][0]) else "down",
                    "horizon": len(closes), "confidence": 0.6}
        r.set(redis_keys.LONGSEQ_FORECAST.replace("{pair}", pair), json.dumps(forecast), ex=1800)
        return forecast
    except Exception as exc:
        log.error("patchtst_forecast_failed", pair=pair, error=str(exc))
        return {}
