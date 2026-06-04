"""
F58 — Liquidation Cascade Level Producer (cont. 56, 2026-05-28).

Polls liquidation-heatmap data (Coinglass / Coinalyze when API keys are
present, falls back to a degraded OI × funding proxy otherwise) and writes
per-pair sorted liquidation-density arrays + the nearest-cluster distance
to Redis. The cascade-imminent predictor in `ml/cascade_predictor.py`
consumes these.

Research basis (next_impl/f58_liquidation_cascade_alpha.md):
  - Tigro Blanc Feb 2026 backtest — +299% return / Sharpe 3.58 (raw)
    using liquidation-heatmap proximity. Beta-adjusted Sharpe ≈ 1.5.

Design:
  - Per active pair, build a sorted (price, est_notional_usd) list of
    liquidation clusters within ±10% of current price.
  - Identify the nearest cluster ABOVE and BELOW current price.
  - Cascade-direction = sign(notional_above - notional_below). When one
    side dominates by ≥2× the other AND the price is within 1.5% of that
    cluster, mark `cascade_imminent=1` with proximity prob.
  - Writes:
      {pair}:liq_clusters_json       — JSON [{price, notional, side}, ...]
      {pair}:liq_nearest_above_pct   — float (distance to nearest above)
      {pair}:liq_nearest_below_pct   — float (distance to nearest below)
      {pair}:liq_cascade_direction   — "long" | "short" | "neutral"
      {pair}:liq_cascade_prob        — float [0,1]

Falls back gracefully:
  - Coinglass key absent → use a proxy derived from open_interest / funding.
    Crude but better than nothing, and clearly marked
    (`{pair}:liq_source=proxy` vs `="coinglass"`).
  - All-providers-fail → bump `liq:reject_count` and exit; cold-start safe.
"""
from __future__ import annotations

import json
import math
import os
import time
from typing import Any

import requests
import structlog

import redis_client
import redis_keys
from feature_governance.registry import register

log = structlog.get_logger()

_FG_ID = "F58"
try:
    register(_FG_ID, "Liquidation Cascade Alpha", activation_phase=0)
except Exception as _exc:
    log.debug("f58_self_register_deferred", err=str(_exc))


# ---- Provider config -------------------------------------------------------

_COINGLASS_KEY = os.environ.get("COINGLASS_API_KEY", "").strip()
_COINALYZE_KEY = os.environ.get("COINALYZE_API_KEY", "").strip()

_PROVIDER_TIMEOUT_S = 8
_PROXIMITY_THRESHOLD = 0.015   # within 1.5% of nearest cluster = imminent
_DOMINANCE_RATIO = 2.0         # one side ≥2× the other
_DEADLOCK_MIN_CALLS = 30
_DEADLOCK_REJECT_FRAC = 0.85


def _r():
    return redis_client.get()


def is_disabled() -> bool:
    if (_r().get("liq:disabled") or b"0") in (b"1", "1"):
        return True
    calls = int(_r().get("liq:call_count") or 0)
    rejs = int(_r().get("liq:reject_count") or 0)
    if calls >= _DEADLOCK_MIN_CALLS and rejs / max(calls, 1) >= _DEADLOCK_REJECT_FRAC:
        _r().set("liq:disabled", "1")
        log.error("f58_liq_auto_disabled_deadlock", calls=calls, rejects=rejs)
        return True
    return False


def _bump_call(): _r().incr("liq:call_count")
def _bump_reject(reason: str):
    _r().incr("liq:reject_count")
    _r().incr(f"liq:reject:{reason}")


# ---- Native providers ------------------------------------------------------

def _fetch_coinglass(pair: str) -> list[dict] | None:
    """Coinglass liquidation-heatmap. Free tier rate-limit is low, so we
    only call for the highest-weight pairs and let the rest use the proxy."""
    if not _COINGLASS_KEY: return None
    sym = pair.replace("USDT", "")
    url = (f"https://open-api-v3.coinglass.com/public/v2/liquidation_chart"
           f"?symbol={sym}&time_type=h12")
    try:
        _bump_call()
        resp = requests.get(url, headers={"coinglassSecret": _COINGLASS_KEY},
                            timeout=_PROVIDER_TIMEOUT_S)
        if resp.status_code != 200:
            _bump_reject(f"coinglass_http_{resp.status_code}")
            return None
        payload = resp.json()
        data = payload.get("data") or {}
        rows = data.get("liquidationDetail") or data.get("data") or []
        clusters = []
        for row in rows:
            price = float(row.get("price") or row.get("Price") or 0)
            notional = float(row.get("longVolUsd", 0)) + \
                       float(row.get("shortVolUsd", 0))
            if price > 0 and notional > 0:
                clusters.append({"price": price, "notional": notional})
        return clusters or None
    except Exception as exc:
        _bump_reject("coinglass_transport")
        log.debug("coinglass_fetch_fail", pair=pair, err=str(exc)[:120])
        return None


def _fetch_coinalyze(pair: str) -> list[dict] | None:
    """Coinalyze liquidation aggregator. Has a free tier; simpler endpoint."""
    if not _COINALYZE_KEY: return None
    url = (f"https://api.coinalyze.net/v1/liquidations?symbols={pair}.A"
           f"&interval=1hour&from={int(time.time()) - 86400}"
           f"&to={int(time.time())}")
    try:
        _bump_call()
        resp = requests.get(url, headers={"api_key": _COINALYZE_KEY},
                            timeout=_PROVIDER_TIMEOUT_S)
        if resp.status_code != 200:
            _bump_reject(f"coinalyze_http_{resp.status_code}")
            return None
        payload = resp.json()
        entries = payload[0].get("history", []) if isinstance(payload, list) and payload else []
        clusters = []
        for e in entries:
            price = float(e.get("h", 0))   # h = high during the bucket
            notional = float(e.get("l", 0)) + float(e.get("s", 0))
            if price > 0 and notional > 0:
                clusters.append({"price": price, "notional": notional})
        return clusters or None
    except Exception as exc:
        _bump_reject("coinalyze_transport")
        log.debug("coinalyze_fetch_fail", pair=pair, err=str(exc)[:120])
        return None


# ---- Proxy (no API key) ----------------------------------------------------

def _fetch_proxy(pair: str, current_price: float) -> list[dict] | None:
    """No-API-key fallback. Build a synthetic two-cluster heatmap from:
      - funding_rate sign → which side is crowded
      - open_interest size → notional weight
      - chandelier-distance bands → typical liquidation prices

    Clearly NOT as accurate as the real heatmap; the consumer flags the
    pair as `liq_source=proxy` and halves the bonus weight.
    """
    r = _r()
    fund_raw = r.get(redis_keys.FUNDING_RATE.replace("{pair}", pair))
    if fund_raw is None:
        return None
    try:
        funding = float(fund_raw)
    except Exception:
        return None
    # Rough ATR proxy: 1m range over last 20 candles.
    candles_raw = r.lrange(
        redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1m"),
        0, 19)
    if not candles_raw:
        return None
    try:
        ranges = []
        for c in candles_raw:
            d = json.loads(c)
            hi = float(d.get("h", 0)); lo = float(d.get("l", 0))
            if hi > 0 and lo > 0: ranges.append(hi - lo)
        if not ranges: return None
        atr = sum(ranges) / len(ranges)
    except Exception:
        return None
    # Build two synthetic clusters: one above and one below, each at
    # ±(3 × ATR) from current price. Notional split by funding sign.
    band = max(3.0 * atr, current_price * 0.01)
    crowd_long = funding > 0     # positive funding → longs paying → crowded long
    above_notional = 1.0 if crowd_long else 0.5
    below_notional = 0.5 if crowd_long else 1.0
    return [
        {"price": current_price + band, "notional": above_notional},
        {"price": current_price - band, "notional": below_notional},
    ]


# ---- Cluster analysis ------------------------------------------------------

def _summarise(clusters: list[dict], current_price: float) -> dict:
    """Compute nearest-above, nearest-below, dominance ratio, cascade prob."""
    above = [c for c in clusters if c["price"] > current_price]
    below = [c for c in clusters if c["price"] < current_price]
    above.sort(key=lambda c: c["price"])
    below.sort(key=lambda c: c["price"], reverse=True)
    n_above_n = above[0]["notional"] if above else 0.0
    n_below_n = below[0]["notional"] if below else 0.0
    if above:
        nearest_above_pct = (above[0]["price"] - current_price) / current_price
    else:
        nearest_above_pct = 0.10
    if below:
        nearest_below_pct = (current_price - below[0]["price"]) / current_price
    else:
        nearest_below_pct = 0.10

    direction = "neutral"
    prob = 0.0
    if max(n_above_n, n_below_n) > 0:
        if n_above_n > n_below_n * _DOMINANCE_RATIO and nearest_above_pct < _PROXIMITY_THRESHOLD:
            # Big stack above → price likely runs UP into longs' liq cluster,
            # which then forces SHORT-side covering. Cascade favors LONG.
            direction = "long"
            prob = min(1.0, (n_above_n / max(n_below_n, 1e-9)) / 5.0)
        elif n_below_n > n_above_n * _DOMINANCE_RATIO and nearest_below_pct < _PROXIMITY_THRESHOLD:
            direction = "short"
            prob = min(1.0, (n_below_n / max(n_above_n, 1e-9)) / 5.0)
    return {
        "nearest_above_pct": float(nearest_above_pct),
        "nearest_below_pct": float(nearest_below_pct),
        "above_notional": float(n_above_n),
        "below_notional": float(n_below_n),
        "direction": direction,
        "prob": float(prob),
    }


# ---- Entry points ----------------------------------------------------------

def refresh_one(pair: str) -> dict:
    if is_disabled():
        return {"ok": False, "reason": "disabled"}
    r = _r()
    price_raw = r.get(redis_keys.MARK_PRICE.replace("{pair}", pair))
    if price_raw is None:
        return {"ok": False, "reason": "no_mark_price", "pair": pair}
    try:
        current_price = float(price_raw)
    except Exception:
        return {"ok": False, "reason": "bad_mark_price", "pair": pair}
    if current_price <= 0:
        return {"ok": False, "reason": "zero_mark_price", "pair": pair}

    clusters = None
    source = "none"
    for fn, name in (
        (_fetch_coinglass, "coinglass"),
        (_fetch_coinalyze, "coinalyze"),
    ):
        clusters = fn(pair)
        if clusters:
            source = name
            break
    if not clusters:
        clusters = _fetch_proxy(pair, current_price)
        source = "proxy" if clusters else "none"
    if not clusters:
        return {"ok": False, "reason": "all_sources_failed", "pair": pair}

    summ = _summarise(clusters, current_price)
    pipe = r.pipeline(transaction=False)
    pipe.setex(f"{pair}:liq_clusters_json", 600,
               json.dumps(clusters[:20]))   # cap size
    pipe.setex(f"{pair}:liq_nearest_above_pct", 600, summ["nearest_above_pct"])
    pipe.setex(f"{pair}:liq_nearest_below_pct", 600, summ["nearest_below_pct"])
    pipe.setex(f"{pair}:liq_cascade_direction", 600, summ["direction"])
    pipe.setex(f"{pair}:liq_cascade_prob", 600, summ["prob"])
    pipe.setex(f"{pair}:liq_source", 600, source)
    pipe.execute()
    return {"ok": True, "pair": pair, "source": source, **summ}


def refresh_all() -> dict:
    if is_disabled():
        return {"ok": False, "reason": "disabled"}
    t0 = time.time()
    r = _r()
    try:
        members = r.smembers(redis_keys.ACTIVE_PAIRS) or set()
        pairs = [m.decode() if isinstance(m, bytes) else m for m in members]
    except Exception:
        pairs = []
    if not pairs:
        pairs = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    ok = 0; failed = 0
    for p in pairs:
        out = refresh_one(p)
        if out.get("ok"):
            ok += 1
        else:
            failed += 1
    elapsed = round(time.time() - t0, 2)
    r.set("liq:updated_at", int(time.time()))
    r.set("liq:last_run_ok", ok)
    r.set("liq:last_run_failed", failed)
    r.set("liq:last_run_elapsed_s", elapsed)
    log.info("f58_liq_refresh_complete", ok=ok, failed=failed,
             elapsed_s=elapsed, n_pairs=len(pairs))
    return {"ok": True, "ok_count": ok, "failed": failed,
            "elapsed_s": elapsed}


# ---- Real-time WS flow blend (cont. 69x item 2) ----------------------------

def realtime_flow(pair: str) -> tuple[str, float]:
    """Live liquidation flow from data/liq_ws.py (!forceOrder@arr).

    Returns (direction, intensity) — ("neutral", 0.0) when the WS keys are
    absent/stale (they carry a short TTL, so absence == no recent cascade).
    Direction convention matches _summarise: BUY-side liqs (shorts liquidated)
    are bullish → "long"; SELL-side (longs liquidated) bearish → "short"."""
    r = _r()
    d_raw = r.get(redis_keys.LIQ_FLOW_DIR.replace("{pair}", pair))
    if d_raw is None:
        return ("neutral", 0.0)
    d = d_raw.decode() if isinstance(d_raw, bytes) else d_raw
    try:
        inten = float(r.get(redis_keys.LIQ_FLOW_INTENSITY.replace("{pair}", pair)) or 0)
    except (TypeError, ValueError):
        inten = 0.0
    return (d, inten)


def _effective_cascade(pair: str) -> tuple[str, float, str]:
    """Blend the scheduled cluster-LEVELS cascade view (coinglass/coinalyze/
    proxy, written by refresh_one) with real-time WS liquidation FLOW.

    Returns (direction, prob, source). The cluster estimate is the baseline;
    live prints sharpen it:
      - clusters neutral + live flow → live flow sets a moderate-prob direction
      - agreement                    → prob boosted toward 1.0
      - conflict                     → cluster direction kept, prob halved
    """
    r = _r()
    # Cluster LEVELS respect the F58 provider deadlock (is_disabled). Real-time
    # WS FLOW is an INDEPENDENT source (data/liq_ws.py) with its own kill switch
    # — a dead coinglass/coinalyze/proxy feed must NOT suppress healthy live
    # liquidation prints, so the cluster contribution is zeroed under deadlock
    # but the WS flow still flows through.
    if is_disabled():
        cl_dir, cl_prob, src = "neutral", 0.0, "none"
    else:
        d_raw = r.get(f"{pair}:liq_cascade_direction")
        cl_dir = (d_raw.decode() if isinstance(d_raw, bytes) else d_raw) if d_raw else "neutral"
        try:
            cl_prob = float(r.get(f"{pair}:liq_cascade_prob") or 0)
        except (TypeError, ValueError):
            cl_prob = 0.0
        src_raw = r.get(f"{pair}:liq_source")
        src = (src_raw.decode() if isinstance(src_raw, bytes) else src_raw) or "none"

    if (r.get("liq:ws:disabled") or "0") in ("1", b"1"):
        rt_dir, rt_inten = "neutral", 0.0
    else:
        rt_dir, rt_inten = realtime_flow(pair)
    if rt_dir == "neutral" or rt_inten <= 0:
        return (cl_dir, cl_prob, src)

    if cl_dir == "neutral":
        # Clusters undecided — live flow alone sets a moderate-confidence call.
        blended_src = "ws" if src in ("none", "proxy") else f"{src}+ws"
        return (rt_dir, min(0.7, 0.3 + 0.5 * rt_inten), blended_src)
    if cl_dir == rt_dir:
        # Agreement — sharpen prob upward.
        return (cl_dir, min(1.0, cl_prob + 0.3 * rt_inten + 0.1), f"{src}+ws")
    # Conflict — clusters and live flow disagree; dampen confidence.
    return (cl_dir, cl_prob * 0.5, f"{src}+ws")


# ---- Consumer helper -------------------------------------------------------

def get_bonus(pair: str, direction: str) -> int:
    """Cascade-imminent additive bonus for signals/engine.py.

    Blends cluster levels with live !forceOrder@arr flow via _effective_cascade.
    Returns:
      +18 when cascade_direction matches `direction` (native cluster and/or
          live-flow confirmed)
      +12 when match but PURE proxy source (no native cluster, no live flow)
      -18 when cascade_direction opposes `direction` at high prob (price about
          to run into a cluster on the OTHER side → trade likely hurt)
      0   when neutral or insufficient data

    Note: the F58 provider deadlock (is_disabled) only suppresses the cluster
    LEVELS half — real-time WS liquidation FLOW still contributes (its own
    `liq:ws:disabled` switch), handled inside _effective_cascade.
    """
    d, prob, src = _effective_cascade(pair)
    if d == "neutral": return 0
    if prob < 0.3:
        return 0
    # Pure proxy (no native cluster, no live prints) stays halved; any signal
    # backed by native clusters or real liquidation flow gets full weight.
    is_proxy = (src == "proxy")
    base = 12 if is_proxy else 18

    if d == direction:
        return base
    # Opposite direction — but only penalise when prob is high (≥0.6).
    if prob >= 0.6:
        return -base
    return 0
