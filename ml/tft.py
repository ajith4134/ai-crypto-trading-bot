"""L-02: TFT (Temporal Fusion Transformer) price forecasting."""
import json
from pathlib import Path
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()
_MODEL_PATH = Path("models/tft.pth")
_model       = None
_last_mtime  = 0.0


def _load():
    """Mtime-based cache invalidation so brain picks up freshly-pretrained TFT
    without container restart (matches the pattern used in direction_model.py
    and world_model/model.py). Resets `_model` cache when on-disk file changes."""
    global _model, _last_mtime
    if not _MODEL_PATH.exists():
        return _model
    mtime = _MODEL_PATH.stat().st_mtime
    if _model is not None and mtime == _last_mtime:
        return _model
    try:
        import torch
        from ml.architectures import TFTModel, inject_into_main
        inject_into_main()  # so legacy torch.save(model) .pth files load
        checkpoint = torch.load(_MODEL_PATH, map_location="cpu", weights_only=False)
        if isinstance(checkpoint, dict):
            # state_dict format (current). New TFT architecture has more keys than the
            # old LSTM stub — load_state_dict will raise on mismatch so old .pth files
            # can't be silently loaded into the new architecture.
            m = TFTModel()
            try:
                m.load_state_dict(checkpoint)
            except Exception as exc:
                log.warning("tft_state_dict_mismatch_must_repretrain",
                            error=str(exc)[:200])
                return _model
            _model = m
        else:
            # Legacy full-pickle format — wrong architecture now, refuse and warn.
            log.warning("tft_legacy_full_pickle_must_repretrain")
            return _model
        _model.eval()
        _last_mtime = mtime
        log.info("tft_model_loaded", mtime=round(mtime, 2))
    except Exception as exc:
        log.warning("tft_load_failed", error=str(exc)[:200])
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
        import torch
        closes = [float(json.loads(c)["c"]) for c in reversed(candles_raw)]

        # NORMALIZATION CONTRACT (must match pretrainer/main.py:train_tft):
        # divide every close by closes[0] so the model sees scale-invariant
        # relative prices. After forward pass, multiply quantile outputs by
        # closes[0] to recover absolute prices. Without this, the model trained
        # on relative inputs would receive raw $50000+ scalars and produce garbage.
        anchor = closes[0] if closes[0] > 0 else 1.0
        closes_norm = [c / anchor for c in closes]
        arr = torch.tensor(closes_norm, dtype=torch.float32).unsqueeze(0).unsqueeze(-1)
        with torch.no_grad():
            out = model(arr)
        # Unnormalize: q*_relative * anchor = q*_absolute
        forecast = {
            "q10": float(out[0][0]) * anchor,
            "q50": float(out[0][1]) * anchor,
            "q90": float(out[0][2]) * anchor,
        }
        # Forecast TTL was 300s; with 100 active pairs and ~5s brain cycle each
        # pair is touched every ~8min so the cache expired BETWEEN touches and
        # F19 contributed zero to direction_confidence most of the time. 1h
        # quantile forecasts are reasonably stable over 30 min — raise TTL so
        # the per-signal lazy compute actually amortises across the active set.
        r.set(
            redis_keys.PRICE_FORECAST.replace("{pair}", pair).replace("{interval}", timeframe),
            json.dumps(forecast), ex=1800,
        )
        return forecast
    except Exception as exc:
        log.error("tft_forecast_failed", pair=pair, error=str(exc))
        return {}


def get_price_forecast_batch(pairs: list[str], timeframe: str,
                             context: int = 100, chunk: int = 128) -> dict[str, dict]:
    """cont. 74 — BATCHED quantile forecast. One forward pass per chunk over all pairs
    that have `context` candles for this timeframe, instead of one pass per pair. Uses a
    UNIFORM context length (last `context` closes) so the sequences stack; thinner pairs
    are skipped here and still served lazily by get_price_forecast. Writes all
    PRICE_FORECAST keys via one Redis pipeline. Returns {pair: {q10,q50,q90}}."""
    out: dict[str, dict] = {}
    if not pairs or not _MODEL_PATH.exists():
        return out
    model = _load()
    if model is None:
        return out
    import torch
    r = redis_client.get()
    rows = []  # (pair, anchor, normalized_closes)
    for pair in pairs:
        try:
            candles_raw = r.lrange(
                redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", timeframe),
                0, context - 1)
            if len(candles_raw) < context:
                continue
            closes = [float(json.loads(c)["c"]) for c in reversed(candles_raw)]
            anchor = closes[0] if closes[0] > 0 else 1.0
            rows.append((pair, anchor, [c / anchor for c in closes]))
        except Exception:
            continue
    if not rows:
        return out
    for i in range(0, len(rows), max(1, chunk)):
        block = rows[i:i + chunk]
        try:
            X = torch.tensor([b[2] for b in block], dtype=torch.float32).unsqueeze(-1)  # [B,ctx,1]
            with torch.no_grad():
                o = model(X)  # [B, 3] quantiles
            o = o.tolist()
        except Exception as exc:
            log.error("tft_batch_forward_failed", tf=timeframe, n=len(block), error=str(exc)[:200])
            continue
        pipe = r.pipeline()
        for (pair, anchor, _), q in zip(block, o):
            try:
                fc = {"q10": float(q[0]) * anchor,
                      "q50": float(q[1]) * anchor,
                      "q90": float(q[2]) * anchor}
                out[pair] = fc
                pipe.set(redis_keys.PRICE_FORECAST.replace("{pair}", pair).replace("{interval}", timeframe),
                         json.dumps(fc), ex=1800)
            except Exception:
                continue
        try:
            pipe.execute()
        except Exception as exc:
            log.warning("tft_batch_pipe_failed", tf=timeframe, error=str(exc)[:150])
    return out
