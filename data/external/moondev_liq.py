"""cont. 60b — Moon Dev liquidation cross-check.

Free public endpoint (no auth — `X-API-Key` recommended but works without).
Fetches the last-10-minute aggregated Binance liquidation events and:

  1. Bins them by price into our own per-price density map.
  2. Compares the resulting clusters to the existing `liquidation:nearest_*`
     keys (written by Coinglass / Coinalyze / Binance-OI-estimator).
  3. Bumps a confidence score per pair when both sources agree on the
     direction of the nearest cluster (above vs below).

The cross-check is ADVISORY only — does not overwrite the primary clusters.
Confidence score is exposed for the dashboard / for the dark-side SL
placement to optionally widen the no-touch zone when confidence is high.

Endpoint:
  https://api.moondev.com/api/binance_liquidations/10m.json
  Returns: list of {timestamp, symbol, side, size, price, value_usd}

Schedule: every 60s via Celery beat.

Writes:
  `liquidation:cross_check_score:{pair}` ∈ [0.0, 1.0]
  `liquidation:cross_check_last_run` UNIX ts
"""
from __future__ import annotations
import os
import json
import time
import requests
import structlog
from collections import defaultdict

import redis_client

log = structlog.get_logger()

_TIMEOUT = 10
_URL = "https://api.moondev.com/api/binance_liquidations/10m.json"
_BIN_BPS = 50          # 0.5% wide bins
_CACHE_TTL = 600


def _aggregate_events(events: list[dict]) -> dict[str, dict]:
    """Aggregate raw events into per-pair {price_bin: notional_usd}."""
    out: dict[str, dict[float, float]] = defaultdict(lambda: defaultdict(float))
    for ev in events:
        try:
            pair = str(ev.get("symbol", "")).upper()
            if not pair.endswith("USDT"):
                continue
            price = float(ev.get("price") or ev.get("p") or 0)
            value = float(ev.get("value_usd")
                          or ev.get("notional")
                          or (float(ev.get("size") or 0) * price))
            if price <= 0 or value <= 0:
                continue
            # Bin by 0.5%
            bin_key = round(price * (1 + (_BIN_BPS * 1e-4) / 2), 8)
            out[pair][bin_key] += value
        except (TypeError, ValueError, KeyError):
            continue
    return out


def _compare_with_primary(pair: str, our_bins: dict, current_mark: float,
                           r) -> float:
    """Compare our binned events vs the existing nearest-above/below clusters.
    Returns confidence ∈ [0, 1]. 1.0 = full agreement.
    """
    primary_above_raw = r.get(f"liquidation:nearest_above:{pair}")
    primary_below_raw = r.get(f"liquidation:nearest_below:{pair}")
    if not primary_above_raw and not primary_below_raw:
        return 0.5  # nothing to compare against — neutral
    # Sum our event mass above vs below mark
    our_above = sum(n for p, n in our_bins.items() if p > current_mark)
    our_below = sum(n for p, n in our_bins.items() if p < current_mark)
    total = our_above + our_below
    if total <= 0:
        return 0.5
    our_above_share = our_above / total
    our_below_share = our_below / total
    # Compare to primary densities
    pa = pb = 0.0
    try:
        if primary_above_raw:
            d = json.loads(primary_above_raw)
            pa = float(d.get("density") or 0)
        if primary_below_raw:
            d = json.loads(primary_below_raw)
            pb = float(d.get("density") or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0.5
    primary_total = pa + pb
    if primary_total <= 0:
        return 0.5
    primary_above_share = pa / primary_total
    primary_below_share = pb / primary_total
    # Agreement = 1 - |above_diff| where diff is in [0, 1]
    diff = abs(our_above_share - primary_above_share)
    return round(max(0.0, 1.0 - diff), 4)


def refresh_all() -> dict:
    """Run cross-check against current Coinalyze/Binance-estimator clusters.

    Requires `MOONDEV_API_KEY` env var (free signup at https://moondev.com).
    Silently skips when missing — cross-check is advisory only and the
    dark-side SL placement works fine without it.
    """
    r = redis_client.get()
    api_key = os.environ.get("MOONDEV_API_KEY")
    if not api_key:
        return {"status": "no_api_key",
                "note": "free signup at moondev.com if you want cross-check"}
    headers = {"X-API-Key": api_key}
    try:
        resp = requests.get(_URL, headers=headers, timeout=_TIMEOUT)
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:120]}
    if resp.status_code != 200:
        return {"status": "http_error", "code": resp.status_code}
    try:
        events = resp.json()
        if isinstance(events, dict):
            events = events.get("data") or events.get("liquidations") or []
        if not isinstance(events, list):
            return {"status": "unexpected_shape"}
    except Exception:
        return {"status": "parse_error"}
    if not events:
        return {"status": "no_events"}
    agg = _aggregate_events(events)
    try:
        active = set(r.smembers("scanner:active_pairs"))
    except Exception:
        active = set()
    n_scored = 0
    high_conf = 0
    pipe = r.pipeline()
    for pair, bins in agg.items():
        if active and pair not in active:
            continue
        mark_raw = r.get(f"{pair}:mark_price")
        if mark_raw is None:
            continue
        try:
            mark = float(mark_raw)
        except (TypeError, ValueError):
            continue
        score = _compare_with_primary(pair, bins, mark, r)
        pipe.setex(f"liquidation:cross_check_score:{pair}", _CACHE_TTL,
                   round(score, 4))
        n_scored += 1
        if score >= 0.75:
            high_conf += 1
    pipe.set("liquidation:cross_check_last_run", int(time.time()))
    pipe.execute()
    log.info("moondev_liq_cross_check",
             events=len(events), pairs_scored=n_scored,
             high_confidence=high_conf)
    return {"status": "ok",
            "events": len(events),
            "pairs_scored": n_scored,
            "high_confidence": high_conf}
