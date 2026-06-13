"""Live CandleNet embedding capture (Phase A — cont. 55).

User-mandated constraint 2026-05-29: pattern clustering MUST use live exchange
candle data via ml.candlenet.run_inference, NOT closed-trade snapshots.

This module bridges live signal time → embedding stream:

  capture_for_pair(pair)
    1. Calls ml.candlenet.run_inference(pair, tf) for tf in (1m, 5m, 15m, 1h).
       run_inference reads the live OHLCV that data/feed.py continuously
       populates into Redis.
    2. Builds the 28-dim embedding via pattern.clusterer.embedding_from_forecasts.
    3. XADDs the embedding to Redis stream `pattern:embeddings` with a TTL
       cap so the trainer pulls only recent live data, never stale snapshots.

The celery beat task `capture_pattern_embeddings_for_active_pairs` (added in
celery_app.py) runs this every 1m for the scanner's top-N active pairs.

At Phase B, the prediction refresh loop will ALSO call capture_for_pair()
inline so the embedding it uses for cluster lookup is the same one that
landed in the stream — keeps training and inference distributions identical.
"""
from __future__ import annotations
import json
import time
import structlog

import redis_client

log = structlog.get_logger()

_STREAM_KEY    = "pattern:embeddings"
_STREAM_MAXLEN = 100_000        # ~70 days at 1 capture / pair / min for 50 pairs
_EMBED_TTL_S   = 7 * 24 * 3600  # trainer ignores entries older than this


def _governance_active() -> bool:
    try:
        from feature_governance.registry import is_active
        return bool(is_active("F46"))
    except Exception:
        return True


def capture_for_pair(pair: str,
                     market_regime: str | None = None,
                     scan_cycle_id: str | None = None) -> dict | None:
    """Pull live CandleNet forecasts for (1m, 5m, 15m, 1h) and XADD a 28-dim
    embedding to `pattern:embeddings`. Returns the embedding dict written
    (with metadata) or None if any timeframe is unavailable.

    The forecasts are derived from LIVE OHLCV in Redis (populated by
    data/feed.py) — explicitly NOT from closed-trade snapshots.
    """
    if not _governance_active():
        return None

    try:
        from ml.candlenet import run_inference
        from pattern.clusterer import embedding_from_forecasts, TF_LIST
    except Exception as exc:
        log.warning("pattern_live_capture_import_failed",
                    error=str(exc)[:200])
        return None

    forecasts: dict[str, dict] = {}
    for tf in TF_LIST:
        try:
            f = run_inference(pair, tf)
            if not f:
                # CandleNet not ready for this TF (model missing or governance off).
                # Skip this capture — no partial embeddings in the stream.
                log.debug("pattern_capture_skip_tf_empty", pair=pair, tf=tf)
                return None
            forecasts[tf] = f
        except Exception as exc:
            log.debug("pattern_capture_inference_failed",
                      pair=pair, tf=tf, error=str(exc)[:120])
            return None

    vec = embedding_from_forecasts(forecasts)
    if vec is None:
        return None

    now_ms = int(time.time() * 1000)
    payload = {
        "pair":            pair,
        "ts_ms":           str(now_ms),
        "market_regime":   str(market_regime or ""),
        "scan_cycle_id":   str(scan_cycle_id or ""),
        "embedding_json":  json.dumps(vec),
    }
    r = redis_client.get()
    try:
        r.xadd(_STREAM_KEY, payload, maxlen=_STREAM_MAXLEN, approximate=True)
        r.incr("pattern:embeddings:captured_count")
    except Exception as exc:
        log.warning("pattern_capture_xadd_failed",
                    pair=pair, error=str(exc)[:200])
        return None

    # Phase B (cont. 73) — assign the live cluster from the SAME embedding that
    # landed in the training stream (keeps train/inference distributions identical,
    # per the module docstring). Writes the canonical consumer key
    # `pattern:cluster_id:{pair}` (prediction/features.py:162, was always -1 because
    # nothing wrote it). predict_cluster returns -1 until a model is fitted; load()
    # is in-process cached so this adds ~no cost. TTL > capture cadence (60s) so the
    # key stays warm; a `_strength` companion key aids debugging/gating.
    cluster_id = -1
    try:
        from pattern.clusterer import predict_cluster
        cluster_id, strength = predict_cluster(vec)
        pipe = r.pipeline(transaction=False)
        pipe.setex(f"pattern:cluster_id:{pair}", 180, int(cluster_id))
        pipe.setex(f"pattern:cluster_strength:{pair}", 180, round(float(strength), 4))
        pipe.execute()
        # Rule 12 — emit counters so a silent assignment failure is visible.
        r.incr("pattern:assign:total")
        if cluster_id < 0:
            r.incr("pattern:assign:noise")
    except Exception as exc:
        log.debug("pattern_cluster_assign_failed", pair=pair, error=str(exc)[:120])
        r.incr("pattern:assign:error")

    return {
        "pair":           pair,
        "ts_ms":          now_ms,
        "market_regime":  market_regime,
        "embedding":      vec,
        "cluster_id":     cluster_id,
    }


def capture_for_active_pairs() -> dict:
    """Iterate the scanner's active-pair list (Redis Set ACTIVE_PAIRS) and capture
    for each. Returns summary. Called by celery beat task."""
    import redis_keys
    r = redis_client.get()
    pairs: list[str] = []
    try:
        members = r.smembers(redis_keys.ACTIVE_PAIRS)
        for m in members or ():
            pairs.append(m.decode("utf-8", errors="replace")
                         if isinstance(m, bytes) else str(m))
    except Exception:
        pairs = []
    if not pairs:
        return {"status": "no_active_pairs", "captured": 0}

    regime = None
    try:
        regime = r.get(getattr(redis_keys, "CURRENT_REGIME", "current_regime"))
    except Exception:
        pass

    captured = 0
    skipped  = 0
    for pair in pairs:
        try:
            res = capture_for_pair(pair, market_regime=regime)
            if res is not None:
                captured += 1
            else:
                skipped += 1
        except Exception as exc:
            log.warning("pattern_capture_pair_failed",
                        pair=pair, error=str(exc)[:200])
            skipped += 1

    log.info("pattern_capture_summary",
             active_pairs=len(pairs),
             captured=captured,
             skipped=skipped)
    return {"status": "ok",
            "active_pairs": len(pairs),
            "captured": captured,
            "skipped":  skipped}


def read_recent_embeddings(max_count: int = 50_000,
                           max_age_s: int = _EMBED_TTL_S) -> list[dict]:
    """Read recent live captures from the Redis stream. Used by the trainer.

    Filters: only entries newer than `max_age_s` seconds; up to `max_count`.
    Returns list of dicts: {pair, ts_ms, market_regime, embedding (list[float])}.
    """
    r = redis_client.get()
    try:
        entries = r.xrevrange(_STREAM_KEY, count=max_count)
    except Exception as exc:
        log.warning("pattern_capture_read_failed", error=str(exc)[:200])
        return []

    now_ms      = int(time.time() * 1000)
    cutoff_ms   = now_ms - (max_age_s * 1000)
    out: list[dict] = []
    for _entry_id, fields in entries:
        try:
            ts_ms = int(fields.get(b"ts_ms", b"0") if isinstance(
                next(iter(fields), b""), bytes) else fields.get("ts_ms", "0"))
        except Exception:
            ts_ms = 0
        if ts_ms < cutoff_ms:
            continue
        try:
            # Redis client may return bytes or str — handle both.
            def _get(k):
                v = fields.get(k.encode() if any(isinstance(x, bytes)
                                                  for x in fields.keys()) else k)
                if isinstance(v, bytes):
                    return v.decode("utf-8", errors="replace")
                return v
            emb_raw = _get("embedding_json")
            emb = json.loads(emb_raw) if emb_raw else None
            if not emb:
                continue
            out.append({
                "pair":          _get("pair"),
                "ts_ms":         ts_ms,
                "market_regime": _get("market_regime"),
                "embedding":     emb,
            })
        except Exception as exc:
            log.debug("pattern_capture_entry_decode_failed",
                      error=str(exc)[:120])
            continue
    return out
