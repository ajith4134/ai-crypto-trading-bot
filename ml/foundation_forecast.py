"""F50g — Time-Series Foundation Model Forecaster (cont. 54).

Blueprint addition cont. 54. Zero-shot forecaster using Amazon Chronos-2
(Apache-2.0). Solves the F19 TFT / F20 PatchTST cold-start gap: those models
need ~5 h of pretraining (cont. 49 pipeline) before they produce trusted
forecasts. Chronos gives a usable forecast on day 0 with no training.

When F19 hits its trusted gate (`model:tft:trusted == "1"`), this module
auto-defers — there is no point running both. The trade-off:
  - cold start (F19 untrained): Chronos contributes; F19 silent
  - F19 trusted:                  F19 contributes; Chronos auto-disabled

Reference: arXiv:2403.07815 (Chronos), arXiv:2511.18578 (Re-Visiting TS
Foundation Models in Finance, 2026 — Chronos-2 strongest community / docs).

Activation gate (F30 governance): F50g. When inactive or model missing,
predict() returns None and signals/engine.py ignores it.

cont.70f (Track 4): REPURPOSED as a soft VOLATILITY/magnitude prior for SIZING
+ SL/TP (not a direction gate — per cont.70e, per-candle direction is at its
input ceiling). The vol band `spread_frac` (=(q90-q10)/anchor) + raw q10/q90
quantiles are the payload. The legacy ±8 dir1h bonus in engine.py stays as-is.
The old TFT auto-defer is REMOVED: TFT is a direction model and provides no vol
band, so the vol prior must keep producing regardless of model:tft:trusted.

Redis keys:
  {pair}:foundation_forecast    TTL 600s — JSON {dir1h, mag1h, q10, q50, q90,
                                          anchor, spread, spread_frac, ts}
                                          (1 h horizon = 60×1m candles)
  foundation:disabled_reason    str       — "chronos_unavailable"
                                            | "model_unavailable"

Model file (downloaded by pretrainer): models/chronos_bolt_base/ directory.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import numpy as np
import structlog

import redis_client

log = structlog.get_logger()


# ── Configuration ───────────────────────────────────────────────────────────

# cont.70f — only the 1h candle buffer is deep enough live (~300); 1m/5m/15m
# buffers hold ~65. So the vol prior runs on 1h candles: ~10 d context -> a
# near-term (3 h) volatility band, which is the window a trade (held minutes-
# hours) actually faces. Tolerate variable depth (Chronos is zero-shot).
_INTERVAL = "1h"
CONTEXT_LENGTH = 256        # target 1h-candle context (~10.6 d)
_MIN_CONTEXT = 96           # accept down to ~4 d; below this skip
HORIZON_STEPS = 3           # 3×1h ahead — near-term vol band for sizing
_FORECAST_TTL = 1200        # 1h cadence -> longer TTL than the old 600
_MODEL_DIR = Path("models/chronos_bolt_base")
_FG_ID = "F50g"

# Cached pipeline — load once per process
_pipeline: dict = {"obj": None, "loaded_ts": None, "available": None}


# ── Governance + availability gates ─────────────────────────────────────────

def _governance_active() -> bool:
    try:
        from feature_governance.registry import is_active
        return bool(is_active(_FG_ID))
    except Exception:
        return True


def _tft_trusted() -> bool:
    """True if F19 TFT is trusted — Chronos defers to it."""
    try:
        return redis_client.get().get("model:tft:trusted") == "1"
    except Exception:
        return False


def _chronos_available() -> bool:
    """Lazy probe for the chronos-forecasting package. Cached for the
    process lifetime so we don't import-attempt every signal cycle."""
    if _pipeline["available"] is not None:
        return _pipeline["available"]
    try:
        import chronos                              # noqa: F401
        _pipeline["available"] = True
    except ImportError:
        _pipeline["available"] = False
        log.info("chronos_package_unavailable",
                 hint="pip install chronos-forecasting>=2.0")
    except Exception as exc:
        _pipeline["available"] = False
        log.warning("chronos_package_probe_failed", error=str(exc)[:200])
    return _pipeline["available"]


def _load_pipeline():
    """Load the Chronos-Bolt pipeline from the local model dir. Returns
    the pipeline object or None on failure. Cached."""
    if _pipeline["obj"] is not None:
        return _pipeline["obj"]
    if not _chronos_available():
        return None
    if not _MODEL_DIR.exists():
        try:
            redis_client.get().set("foundation:disabled_reason",
                                   "model_unavailable")
        except Exception:
            pass
        return None
    try:
        from chronos import BaseChronosPipeline
        pipeline = BaseChronosPipeline.from_pretrained(
            str(_MODEL_DIR), device_map="cpu", torch_dtype="float32",
        )
        _pipeline["obj"] = pipeline
        _pipeline["loaded_ts"] = int(time.time())
        log.info("chronos_loaded", path=str(_MODEL_DIR))
        return pipeline
    except Exception as exc:
        log.warning("chronos_load_failed", error=str(exc)[:200])
        return None


# ── Inference API ───────────────────────────────────────────────────────────

def predict(pair: str, interval: str = _INTERVAL) -> Optional[dict]:
    """Run Chronos zero-shot forecast for one pair. Writes the forecast to
    Redis and returns the dict. Returns None when:
      - F50g governance off
      - F19 TFT already trusted (auto-defer)
      - chronos-forecasting package not installed
      - Model weights not yet downloaded
      - Insufficient candle history

    The forecast horizon is 1 h (60 × 1 m candles). Returns:
      {dir1h, mag1h, q05, q95, ts} — same shape that risk/manager.py can
      consume as a TFT-proxy when F19 isn't trusted.
    """
    r = redis_client.get()
    if not _governance_active():
        return None
    # cont.70f — TFT auto-defer removed: this is now a VOL prior, not a TFT
    # direction proxy. TFT provides no volatility band, so we keep producing.

    pipeline = _load_pipeline()
    if pipeline is None:
        return None

    raw = r.lrange(f"{pair}:{interval}:candles", 0, CONTEXT_LENGTH - 1)
    if not raw or len(raw) < _MIN_CONTEXT:
        return None
    try:
        candles = [json.loads(c) for c in reversed(raw)]
        closes = np.asarray([float(c.get("c", 0)) for c in candles],
                            dtype=np.float32)
    except Exception:
        return None
    if (closes <= 0).any():
        return None

    try:
        import torch
        context = torch.from_numpy(closes).unsqueeze(0)
        # cont.70f — installed chronos takes `inputs` POSITIONALLY (the old
        # `context=` kwarg raises TypeError -> predict() silently None'd, which
        # is why this forecaster was dormant). Chronos-Bolt was trained on
        # quantiles [0.1..0.9] ONLY -> request [0.1,0.5,0.9] (asking 0.05/0.95
        # just clamps to 0.1/0.9 with a warning). Keys named q10/q90 honestly.
        quantiles, mean = pipeline.predict_quantiles(
            context,
            prediction_length=HORIZON_STEPS,
            quantile_levels=[0.1, 0.5, 0.9],
        )
        q = quantiles.squeeze(0).cpu().numpy()        # (H, 3)
        m = mean.squeeze(0).cpu().numpy()             # (H,)
    except Exception as exc:
        log.warning("chronos_predict_failed", pair=pair,
                    error=str(exc)[:200])
        return None

    anchor = float(closes[-1])
    if anchor <= 0:
        return None

    # 1 h horizon = last step of the 60-step prediction
    q10_1h = float(q[-1, 0])
    q50_1h = float(q[-1, 1])
    q90_1h = float(q[-1, 2])
    mean_1h = float(m[-1])

    # Direction probability: rough monotone transform of (q50 - anchor) /
    # spread. When q50 well above anchor → ~0.9; when near → ~0.5.
    spread = max(q90_1h - q10_1h, anchor * 1e-4)
    dir1h = 0.5 + 0.5 * max(-1.0, min(1.0, (q50_1h - anchor) / spread))
    mag1h_pct = (mean_1h - anchor) / anchor * 100.0
    # cont.70f — the Track-4 payload: RELATIVE vol band (spread as a fraction of
    # price) is the volatility prior consumed by sizing + SL/TP. q10/q90 are the
    # raw price quantiles for SL/TP placement.
    spread_frac = spread / anchor

    forecast = {
        "dir1h":      round(float(dir1h), 6),
        "mag1h":      round(float(mag1h_pct), 6),
        "q10":        round(q10_1h, 6),
        "q50":        round(q50_1h, 6),
        "q90":        round(q90_1h, 6),
        "anchor":     round(anchor, 6),
        "spread":     round(spread, 6),
        "spread_frac": round(float(spread_frac), 8),
        "ts":         int(time.time()),
    }
    try:
        r.setex(f"{pair}:foundation_forecast", _FORECAST_TTL,
                json.dumps(forecast))
        r.incr("foundation:inference_count")
        r.delete("foundation:disabled_reason")   # active again
    except Exception:
        pass
    return forecast


def load_forecast(pair: str) -> Optional[dict]:
    """Reader-side helper. Returns None on cache miss / disabled."""
    try:
        raw = redis_client.get().get(f"{pair}:foundation_forecast")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None


def run_forecast_sweep(max_pairs: int = 60) -> dict:
    """cont.70f — PRODUCER. Iterate a bounded active-pair set, run predict()
    for each, populate {pair}:foundation_forecast. Called by a celery beat
    task on the candlenet worker (torch + ml/ + model dir). Never raises.

    Kill switch: foundation:disabled == "1". Counters published to Redis.
    The Chronos pipeline is loaded once per worker process (cached) -> only
    the first sweep pays the ~7s warmup; steady-state ~300ms/pair on CPU."""
    r = redis_client.get()
    try:
        if r.get("foundation:disabled") in (b"1", "1"):
            r.incr("foundation:disabled_skip_count")
            return {"status": "disabled"}
    except Exception:
        pass
    if not _governance_active():
        return {"status": "governance_off"}
    # Reuse f50e's active-pair selector (scanner:active_pairs, bounded).
    try:
        from ml.candle_online_trainer import _active_pairs
        pairs = _active_pairs(limit=max_pairs)
    except Exception:
        pairs = []
    if not pairs:
        return {"status": "no_pairs"}
    if _load_pipeline() is None:
        return {"status": "model_unavailable"}

    ok = none = 0
    for pair in pairs:
        try:
            if predict(pair) is not None:
                ok += 1
            else:
                none += 1
        except Exception as exc:
            none += 1
            log.debug("foundation_sweep_item_failed", pair=pair,
                      error=str(exc)[:160])
    try:
        r.set("foundation:sweep_ok", ok)
        r.set("foundation:sweep_none", none)
        r.set("foundation:last_sweep_ts", int(time.time()))
        r.incrby("foundation:sweep_total", ok)
    except Exception:
        pass
    log.info("foundation_forecast_sweep", pairs=len(pairs), ok=ok, none=none)
    return {"status": "ok", "pairs": len(pairs), "ok": ok, "none": none}


# ── Download helper (called by pretrainer/main.py) ──────────────────────────

def download_weights() -> dict:
    """Download Chronos-Bolt base model from Hugging Face. Idempotent —
    skips when the local dir already has model files. Called once by the
    pretrainer at step_12 (cont. 54)."""
    if not _chronos_available():
        return {"status": "skip_chronos_unavailable",
                "hint": "add chronos-forecasting to requirements.txt"}
    try:
        from huggingface_hub import snapshot_download
        path = snapshot_download(repo_id="amazon/chronos-bolt-base",
                                 local_dir=str(_MODEL_DIR),
                                 local_dir_use_symlinks=False)
        # Probe load to verify weights are usable
        from chronos import BaseChronosPipeline
        BaseChronosPipeline.from_pretrained(path, device_map="cpu",
                                            torch_dtype="float32")
        log.info("chronos_downloaded", path=path)
        return {"status": "ok", "path": path}
    except Exception as exc:
        log.warning("chronos_download_failed", error=str(exc)[:200])
        return {"status": "fail", "error": str(exc)[:200]}


# ── F30 governance registration ─────────────────────────────────────────────

def _register_governance():
    try:
        from feature_governance.registry import register
        register(_FG_ID, "F50g — Chronos-2 Foundation Forecaster",
                 activation_phase=0)
    except Exception as exc:
        log.warning("foundation_registry_failed", error=str(exc)[:200])


_register_governance()
