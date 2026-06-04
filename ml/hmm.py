"""L-01: Market regime classifier.

cont. 70 (2026-06-03) REDESIGN — the previous Viterbi-only path produced a stuck "bull"
label in a market that was down ~5%. Three root bugs:
  1. INPUT: data:returns:recent is a CROSS-SECTION (latest tick of ~100 different pairs)
     but was Viterbi-decoded as a single asset's TIME SERIES; the model was TRAINED on
     pooled *daily* returns ⇒ train/serve scale mismatch ⇒ live data collapsed into the
     near-zero state. The instantaneous cross-section is also far too noisy (its breadth
     flips up/down minute-to-minute) to be a regime signal.
  2. LABELS: `_REGIMES = {0:bull,1:bear,2:turbulent}` was positional, never tied to each
     state's mean return ⇒ the flat state was labelled "bull", the positive one "turbulent".
  3. UNMAPPED 4th state + bull defaults: n_components=4 with 3 labels ⇒ the truly-bearish
     state fell through `.get(3,"bull")`; plus a degenerate state (mean ~39) from an
     unfiltered bad daily return.

NEW DESIGN — regime is driven by a STABLE MARKET PROXY (BTC multi-hour trend), which is
what "the market is up/down" actually means and matches external reality:
  • PRIMARY: BTC 24h trend (BTCUSDT:change_24h, falling back to a computed return from the
    1h candle buffer) + hourly realized vol for the turbulent overlay. All thresholds
    Redis-tunable.
  • TIE-BREAK: when BTC is in the flat band, the cross-section breadth nudges the call.
  • OPTIONAL HMM: a *calibrated* HMM (labels assigned by sorting means_, degenerate models
    rejected) is used only if regime:use_hmm="1" AND the model is healthy. Off by default
    because the current model is degenerate and mis-fed; retrain before trusting it.
  • NO "bull" defaults — undecided stays "unknown" (symmetric gate thresholds).
"""
import json
import pickle
from pathlib import Path

import numpy as np
import structlog

import redis_client
import redis_keys

log = structlog.get_logger()

_MODEL_PATH = Path("models/hmm_regime.pkl")
_model = None
_label_map: dict | None = None
_model_healthy: bool | None = None

_MARKET_PROXY = "BTCUSDT"
_DEGENERATE_MEAN = 0.5     # |state mean| > 50% ⇒ garbage HMM component
_OUTLIER_RET = 0.5         # drop |r| > 50% from breadth input


def _f(r, key, default):
    try:
        return float(r.get(key) or default)
    except (TypeError, ValueError):
        return default


# ── market-proxy trend (primary signal) ───────────────────────────────────────

def _btc_trend(r) -> dict:
    """BTC trend stats from Redis. Prefers the published 24h change; computes 24h /
    12h returns + hourly vol from the 1h candle buffer when available."""
    out = {"ret_24h": None, "ret_12h": None, "hourly_std": None, "source": None}
    # published 24h change (fast path)
    try:
        ch = r.get(f"{_MARKET_PROXY}:change_24h")
        if ch is not None:
            out["ret_24h"] = float(ch)
            out["source"] = "change_24h"
    except (TypeError, ValueError):
        pass
    # compute from 1h candles (gives 12h + vol, and a fallback for 24h)
    try:
        raw = r.lrange(f"{_MARKET_PROXY}:1h:candles", 0, 47)
        candles = [json.loads(x) for x in raw]
        closes = [float(c["c"]) for c in candles]
        if len(closes) >= 2:
            # detect ordering; normalise to newest-first
            if candles[0]["t"] < candles[1]["t"]:
                closes = closes[::-1]
            now = closes[0]
            if out["ret_24h"] is None and len(closes) > 24:
                out["ret_24h"] = (now - closes[24]) / closes[24] * 100.0
                out["source"] = "candles"
            if len(closes) > 12:
                out["ret_12h"] = (now - closes[12]) / closes[12] * 100.0
            hourly = [(closes[i] - closes[i + 1]) / closes[i + 1]
                      for i in range(min(24, len(closes) - 1)) if closes[i + 1] > 0]
            if hourly:
                out["hourly_std"] = float(np.std(hourly))
    except Exception:
        pass
    return out


# Stable / pegged quotes carry no directional regime signal — exclude from breadth.
_STABLES = {"USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "USDPUSDT", "DAIUSDT", "BUSDUSDT",
            "USDEUSDT", "EURUSDT", "USD1USDT"}


def _universe_trend(r) -> dict:
    """Broad-market read from the WHOLE active alt universe's stable 24h change
    (the pairs we actually trade), NOT a one-tick snapshot. Returns median/mean 24h
    change, declining breadth, dispersion, and count. This is the alt-inclusive
    signal the user asked for — alts (~30% of the market) routinely diverge from BTC."""
    out = {"alt_median": None, "alt_mean": None, "alt_breadth_down": None,
           "alt_dispersion": None, "alt_n": 0}
    try:
        pairs = r.smembers(redis_keys.ACTIVE_PAIRS) or set()
    except Exception:
        pairs = set()
    vals = []
    for p in pairs:
        if p == _MARKET_PROXY or p in _STABLES:
            continue
        v = r.get(f"{p}:change_24h")
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if abs(f) <= 100:                # drop obviously-bad ticks
            vals.append(f)
    if len(vals) >= 20:
        arr = np.array(vals, dtype=float)
        down = int((arr < 0).sum())
        out.update(alt_median=float(np.median(arr)), alt_mean=float(arr.mean()),
                   alt_breadth_down=down / len(arr), alt_dispersion=float(arr.std()),
                   alt_n=len(arr))
    return out


def classify(r=None, breadth_returns=None) -> tuple[str, dict]:
    """Broad-market regime: a BTC-anchored, alt-weighted composite trend + alt breadth.
    We trade alts, so the alt universe dominates; BTC is the beta anchor. Thresholds:
      regime:btc_weight (0.35)          — weight on BTC in the composite (rest = alts)
      regime:bear_pct (1.5)             — composite 24h <= -X% ⇒ bear
      regime:bull_pct (1.5)             — composite 24h >= +X% ⇒ bull
      regime:bear_breadth (0.58)        — alt declining-fraction at/above ⇒ bear
      regime:turbulent_dispersion (8.0) — alt 24h-change std at/above ⇒ turbulent
      regime:turbulent_hourly_std (0.015) — BTC hourly vol at/above ⇒ turbulent
    """
    if r is None:
        r = redis_client.get()
    btc = _btc_trend(r)
    uni = _universe_trend(r)
    btc_w = _f(r, "regime:btc_weight", 0.35)
    bear_pct = _f(r, "regime:bear_pct", 1.5)
    bull_pct = _f(r, "regime:bull_pct", 1.5)
    bear_b = _f(r, "regime:bear_breadth", 0.58)
    turb_disp = _f(r, "regime:turbulent_dispersion", 8.0)
    turb_std = _f(r, "regime:turbulent_hourly_std", 0.015)

    btc24 = btc["ret_24h"]
    alt = uni["alt_median"]
    bd = uni["alt_breadth_down"]

    # composite 24h trend: alt-weighted, BTC-anchored (use whichever side exists)
    if btc24 is not None and alt is not None:
        composite = btc_w * btc24 + (1.0 - btc_w) * alt
    else:
        composite = btc24 if btc24 is not None else alt
    stats = {"composite": round(composite, 3) if composite is not None else None,
             "btc_24h": round(btc24, 2) if btc24 is not None else None,
             "alt_median": round(alt, 2) if alt is not None else None,
             "alt_breadth_down": round(bd, 3) if bd is not None else None,
             "alt_n": uni["alt_n"]}

    # turbulent overlay: extreme cross-alt dispersion OR high BTC realised vol
    if uni["alt_dispersion"] is not None and uni["alt_dispersion"] >= turb_disp:
        return "turbulent", {**stats, "alt_dispersion": round(uni["alt_dispersion"], 2),
                             "reason": "alt_high_dispersion"}
    if btc["hourly_std"] is not None and btc["hourly_std"] >= turb_std:
        return "turbulent", {**stats, "reason": "btc_high_vol"}

    if composite is None:
        return "unknown", {**stats, "reason": "no_data"}

    # decisive trend OR decisive alt breadth ⇒ regime
    if composite <= -bear_pct or (bd is not None and bd >= bear_b):
        return "bear", {**stats, "reason": "down_trend_or_breadth"}
    if composite >= bull_pct or (bd is not None and (1.0 - bd) >= bear_b):
        return "bull", {**stats, "reason": "up_trend_or_breadth"}
    return "unknown", {**stats, "reason": "mixed"}


# ── calibrated HMM (optional, off unless regime:use_hmm=1 AND model healthy) ───

def _load():
    global _model
    if _model is None and _MODEL_PATH.exists():
        with open(_MODEL_PATH, "rb") as f:
            _model = pickle.load(f)
    return _model


def _calibrate(model):
    try:
        means = model.means_.flatten()
    except Exception:
        return None, False
    if np.any(np.abs(means) > _DEGENERATE_MEAN):
        return None, False
    order = np.argsort(means)
    label_map = {int(order[0]): "bear", int(order[-1]): "bull"}
    for s in order[1:-1]:
        label_map[int(s)] = "unknown"
    try:
        label_map[int(np.argmax(model.covars_.flatten()))] = "turbulent"
    except Exception:
        pass
    return label_map, True


def _hmm_regime(r, returns) -> str | None:
    global _label_map, _model_healthy
    if r.get("regime:use_hmm") != "1":
        return None
    model = _load()
    if model is None:
        return None
    if _model_healthy is None:
        _label_map, _model_healthy = _calibrate(model)
    if not _model_healthy or not _label_map:
        return None
    vals = [x for x in (returns or []) if x is not None and abs(x) <= _OUTLIER_RET]
    if len(vals) < 10:
        return None
    try:
        return _label_map.get(int(model.predict(np.array(vals).reshape(-1, 1))[-1]))
    except Exception:
        return None


# ── public API ────────────────────────────────────────────────────────────────

def get_current_regime() -> str:
    r = redis_client.get()
    return r.get(redis_keys.CURRENT_REGIME) or "unknown"


def update_regime(price_returns: list[float] | None = None) -> str:
    """Classify and persist the current regime from the BTC trend (+ breadth tie-break).
    A healthy calibrated HMM overrides only when regime:use_hmm=1. Never defaults to bull."""
    try:
        r = redis_client.get()
        regime, stats = classify(r, breadth_returns=price_returns)
        hmm_regime = _hmm_regime(r, price_returns)
        if hmm_regime:
            regime = hmm_regime
            stats["source"] = "hmm"
        if regime == "unknown":
            prior = r.get(redis_keys.CURRENT_REGIME)
            if prior:
                return prior
        r.set(redis_keys.CURRENT_REGIME, regime)
        log.info("regime_updated", regime=regime, **{k: v for k, v in stats.items()
                                                     if v is not None})
        return regime
    except Exception as exc:
        log.error("regime_update_failed", error=str(exc))
        return get_current_regime()
