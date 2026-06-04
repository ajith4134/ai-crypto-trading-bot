"""cont. 60b — In-process Binance OI-based liquidation cluster estimator.

Algorithm adapted from `minchillo4/btc-liquidation-heatmap` (MIT). Uses
Binance USDT-M `openInterestHist` (free, no auth) to project liquidation
prices across leverage tiers, bins them into per-price density clusters,
and zeroes bins where price has already traded through (presumed liquidated).

Inputs (all free):
  * https://fapi.binance.com/futures/data/openInterestHist
    ?symbol={pair}&period=5m&limit=60         (5 hours of 5-min OI history)
  * Existing `{pair}:1m:candles` in Redis     (for VWAP estimates per hour)
  * Existing `{pair}:mark_price`              (current price for nearest-cluster picks)

Outputs (replaces missing Coinglass / Coinalyze):
  * `liquidation:nearest_above:{pair}`   JSON {top, bottom, density}
  * `liquidation:nearest_below:{pair}`   JSON {top, bottom, density}
  * `liquidation:source:{pair}` = "binance_oi_estimator"
  * `liquidation:last_refresh:{pair}` UNIX ts

Leverage distribution (Glassnode 2024 perp-trader survey approximations):
   5×: 30%  10×: 30%  25×: 20%  50×: 10%  100×: 10%

The 5-hour rolling window is intentionally short. Liquidation clusters are
the ones forming RIGHT NOW from recently opened leveraged positions, not
historical residual ones.

Rate limit: Binance USDT-M futures-data weight=1 per call. 80 active pairs
every 60s = 80 req/min, well under the 1200 weight/min IP cap.

Source: Glassnode "Inferring Leveraged Positioning from Price and OI",
minchillo4/btc-liquidation-heatmap, vsching/liquidation-heatmap.
"""
from __future__ import annotations
import json
import math
import time
import requests
import structlog

import redis_client
import redis_keys

log = structlog.get_logger()

_BASE = "https://fapi.binance.com/futures/data/openInterestHist"
_TIMEOUT = 8
_OI_LIMIT = 60          # 60 5-min buckets = 5 hours
_OI_PERIOD = "5m"
_BIN_TOLERANCE = 0.005  # 0.5% — adjacent prices within this fold into one cluster
_CACHE_TTL = 600        # 10 min — let downstream consumers see stale-but-usable

# (leverage, weight) — sums to 1.0
_LEVERAGE_TIERS = [(5, 0.30), (10, 0.30), (25, 0.20), (50, 0.10), (100, 0.10)]


def _fetch_oi_history(pair: str) -> list[dict] | None:
    """Returns list of {timestamp, sumOpenInterest, sumOpenInterestValue}
    sorted oldest → newest, or None on failure."""
    try:
        resp = requests.get(_BASE,
                            params={"symbol": pair, "period": _OI_PERIOD,
                                    "limit": _OI_LIMIT},
                            timeout=_TIMEOUT)
    except Exception as exc:
        log.debug("binance_oi_fetch_failed", pair=pair, error=str(exc)[:120])
        return None
    if resp.status_code != 200:
        if resp.status_code == 400:
            # symbol not on USDT-M (alt-quote pair) — silent skip
            return None
        log.debug("binance_oi_http_error", pair=pair, code=resp.status_code)
        return None
    try:
        rows = resp.json()
        if not isinstance(rows, list) or not rows:
            return None
        # Newest-first from Binance; reverse to oldest-first for delta walk.
        return list(reversed(rows))
    except Exception:
        return None


def _candle_vwap_window(r, pair: str, n_minutes: int) -> list[float]:
    """Return a list of recent close prices from {pair}:1m:candles (newest-first
    in Redis → reversed to oldest-first here). We use close as VWAP proxy."""
    key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1m")
    raw = r.lrange(key, 0, n_minutes + 5)
    closes: list[float] = []
    for c in raw:
        try:
            cd = json.loads(c)
            closes.append(float(cd.get("c") or 0))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return list(reversed(closes))   # oldest-first


def _project_clusters(oi_rows: list[dict], closes: list[float],
                      current_price: float) -> list[dict]:
    """Project per-bucket OI deltas into liquidation price clusters.

    Walks oi_rows oldest→newest. For each bucket where OI INCREASED, treats
    the delta as new leveraged positions opening at the bucket's VWAP. Splits
    50/50 long/short (we don't know the actual mix without per-trade data —
    real liquidations rarely deviate >60/40). Projects liquidation prices
    across the leverage tiers + weights.

    Returns sorted-by-price list of {top, bottom, density} clusters with
    density normalised to [0, 1] by the largest cluster.
    """
    if len(oi_rows) < 2 or len(closes) < 1:
        return []
    # bins[price_rounded] = notional_usd
    bins: dict[float, float] = {}
    # Approximate per-bucket close: align by index from the close list.
    # We need at least one close per oi bucket. If counts mismatch, use the
    # last available close for older buckets.
    for i in range(1, len(oi_rows)):
        try:
            prev_oi = float(oi_rows[i - 1].get("sumOpenInterest") or 0)
            cur_oi  = float(oi_rows[i].get("sumOpenInterest") or 0)
            delta_contracts = cur_oi - prev_oi
            if delta_contracts <= 0:
                continue  # only new-position buckets feed the heatmap
            # Convert contracts → notional USD using the per-bucket value
            prev_val = float(oi_rows[i - 1].get("sumOpenInterestValue") or 0)
            cur_val  = float(oi_rows[i].get("sumOpenInterestValue") or 0)
            delta_value = max(0.0, cur_val - prev_val)
            if delta_value <= 0:
                continue
        except (TypeError, ValueError):
            continue
        # Pick the closest close as bucket entry price
        idx = min(len(closes) - 1, int(i * len(closes) / len(oi_rows)))
        entry = closes[idx] if idx < len(closes) else current_price
        if entry <= 0:
            continue
        # Split into long/short halves
        half = 0.5 * delta_value
        for L, weight in _LEVERAGE_TIERS:
            liq_long  = entry * (1.0 - 1.0 / L)   # long stops below
            liq_short = entry * (1.0 + 1.0 / L)   # short stops above
            nl = round(liq_long,  8)
            ns = round(liq_short, 8)
            bins[nl] = bins.get(nl, 0.0) + half * weight
            bins[ns] = bins.get(ns, 0.0) + half * weight

    if not bins:
        return []

    # Zero out bins between recent low/high (already liquidated by trade-through)
    if len(closes) >= 5:
        recent_low = min(closes[-5:])
        recent_high = max(closes[-5:])
        for p in list(bins.keys()):
            if recent_low <= p <= recent_high:
                bins[p] = 0.0

    # Cluster adjacent bins (within _BIN_TOLERANCE)
    sorted_items = sorted((p, n) for p, n in bins.items() if n > 0)
    if not sorted_items:
        return []
    clusters: list[dict] = []
    cur = None
    for p, n in sorted_items:
        if cur is None:
            cur = {"top": p, "bottom": p, "density": n}
            continue
        if (p - cur["top"]) / cur["top"] <= _BIN_TOLERANCE:
            cur["top"] = p
            cur["density"] += n
        else:
            clusters.append(cur)
            cur = {"top": p, "bottom": p, "density": n}
    if cur is not None:
        clusters.append(cur)

    # Normalise density to [0, 1]
    max_d = max(c["density"] for c in clusters)
    if max_d > 0:
        for c in clusters:
            c["density"] = round(c["density"] / max_d, 4)
    return clusters


def estimate_pair(pair: str) -> dict:
    """Run the full estimator for one pair. Returns status dict."""
    r = redis_client.get()
    mark_raw = r.get(f"{pair}:mark_price")
    if mark_raw is None:
        return {"status": "no_mark"}
    try:
        mark = float(mark_raw)
    except (TypeError, ValueError):
        return {"status": "parse_mark"}
    oi_rows = _fetch_oi_history(pair)
    if not oi_rows or len(oi_rows) < 10:
        return {"status": "no_oi_data"}
    closes = _candle_vwap_window(r, pair, _OI_LIMIT)
    if not closes:
        return {"status": "no_candles"}
    clusters = _project_clusters(oi_rows, closes, mark)
    if not clusters:
        return {"status": "no_clusters"}
    above = next((c for c in clusters if c["bottom"] > mark), None)
    below = None
    for c in reversed(clusters):
        if c["top"] < mark:
            below = c
            break
    pipe = r.pipeline()
    if above:
        pipe.setex(f"liquidation:nearest_above:{pair}", _CACHE_TTL, json.dumps(above))
    if below:
        pipe.setex(f"liquidation:nearest_below:{pair}", _CACHE_TTL, json.dumps(below))
    pipe.set(f"liquidation:source:{pair}", "binance_oi_estimator")
    pipe.set(f"liquidation:last_refresh:{pair}", int(time.time()))
    pipe.execute()
    return {"status": "ok", "clusters": len(clusters),
            "above": bool(above), "below": bool(below)}


def estimate_all_active() -> dict:
    """Run estimator for every active pair that doesn't already have fresh
    cluster data from a higher-priority source (Coinglass/Coinalyze)."""
    r = redis_client.get()
    try:
        pairs = sorted(r.smembers("scanner:active_pairs"))
    except Exception:
        return {"status": "no_pairs"}
    ok = err = skipped = 0
    for p in pairs:
        # Skip if already covered by a higher-priority source within the last 90s
        src = r.get(f"liquidation:source:{p}")
        ts_raw = r.get(f"liquidation:last_refresh:{p}")
        if src in ("coinglass", "coinalyze") and ts_raw:
            try:
                age = time.time() - float(ts_raw)
                if age < 90:
                    skipped += 1
                    continue
            except (TypeError, ValueError):
                pass
        try:
            result = estimate_pair(p)
            if result.get("status") == "ok":
                ok += 1
            else:
                err += 1
        except Exception as exc:
            err += 1
            log.debug("binance_liq_estimator_failed",
                      pair=p, error=str(exc)[:120])
        # Throttle to stay well within Binance weight limit
        time.sleep(0.05)
    log.info("binance_liq_estimator_run", ok=ok, err=err, skipped=skipped,
             total=len(pairs))
    return {"status": "ok", "ok": ok, "err": err, "skipped": skipped}
