"""cont. 60b — Coinalyze liquidation history fetcher.

Free tier (40 calls/min/key, ~57k/day). Returns time-bucketed liquidation
totals (long vs short) per pair across multiple exchanges. We bin the
aggregated long-side volume into a single "above-current" cluster and
short-side into "below-current" cluster (Coinalyze does not expose per-price
density — only time-bucketed totals).

Endpoint:
  https://api.coinalyze.net/v1/liquidations
    ?symbols={pair}.A&interval=5min&from={ts}&to={ts}

The `.A` suffix targets aggregated multi-exchange. For Binance-only use
{pair}.A_PERP_USDT.

Auth: `api_key` header.

Writes (same keys consumed by risk/frontier/exit_placement.apply_liquidation_dark_side):
  `liquidation:nearest_above:{pair}`   JSON {top, bottom, density}
  `liquidation:nearest_below:{pair}`   JSON {top, bottom, density}
  `liquidation:source:{pair}` = "coinalyze"
  `liquidation:last_refresh:{pair}` UNIX ts

Source: https://api.coinalyze.net/v1/doc/
"""
from __future__ import annotations
import os
import json
import time
import requests
import structlog

import redis_client

log = structlog.get_logger()

_BASE = "https://api.coinalyze.net/v1/liquidation-history"
_TIMEOUT = 8
_CACHE_TTL = 300


def _aggregate_to_clusters(entries: list[dict], current_mark: float) -> dict:
    """Coinalyze returns rows with `t, l, s` (timestamp, long_liq_value,
    short_liq_value). We aggregate the last hour of longs into one "below"
    cluster (longs liquidate when price falls) and shorts into "above"
    (shorts liquidate when price rises).

    Cluster shape mirrors what the dark-side SL consumer expects.
    """
    long_total = 0.0
    short_total = 0.0
    for row in entries:
        try:
            long_total  += float(row.get("l") or 0)
            short_total += float(row.get("s") or 0)
        except (TypeError, ValueError):
            continue
    total = long_total + short_total
    if total <= 0:
        return {"above": None, "below": None}
    # Density is the side's share of total liquidation flow in the window.
    # Width = 1% below for longs (typical leverage cluster), 1% above for shorts.
    out = {"above": None, "below": None}
    if short_total > 0:
        center = current_mark * 1.005
        out["above"] = {
            "top":     round(center * 1.005, 8),
            "bottom":  round(center * 0.995, 8),
            "density": round(short_total / total, 4),
        }
    if long_total > 0:
        center = current_mark * 0.995
        out["below"] = {
            "top":     round(center * 1.005, 8),
            "bottom":  round(center * 0.995, 8),
            "density": round(long_total / total, 4),
        }
    return out


def refresh_pair(pair: str, current_mark: float) -> dict:
    api_key = os.environ.get("COINALYZE_API_KEY")
    if not api_key:
        return {"status": "no_api_key"}
    r = redis_client.get()
    end = int(time.time())
    start = end - 3600  # last 1h
    # Coinalyze aggregated multi-exchange perp symbol format: e.g. BTCUSDT_PERP.A
    symbol = f"{pair}_PERP.A"
    try:
        resp = requests.get(_BASE,
                            params={"symbols": symbol, "interval": "5min",
                                    "from": start, "to": end},
                            headers={"api_key": api_key},
                            timeout=_TIMEOUT)
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:120]}
    if resp.status_code != 200:
        return {"status": "http_error", "code": resp.status_code,
                "body": resp.text[:120]}
    try:
        payload = resp.json()
    except Exception:
        return {"status": "parse_error"}
    # Response is list of {symbol, history: [{t,l,s},...]}
    if not isinstance(payload, list) or not payload:
        return {"status": "empty"}
    history = payload[0].get("history") or []
    if not history:
        return {"status": "no_history"}
    clusters = _aggregate_to_clusters(history, current_mark)
    pipe = r.pipeline()
    if clusters.get("above"):
        pipe.setex(f"liquidation:nearest_above:{pair}", _CACHE_TTL,
                   json.dumps(clusters["above"]))
    if clusters.get("below"):
        pipe.setex(f"liquidation:nearest_below:{pair}", _CACHE_TTL,
                   json.dumps(clusters["below"]))
    pipe.set(f"liquidation:source:{pair}", "coinalyze")
    pipe.set(f"liquidation:last_refresh:{pair}", int(time.time()))
    pipe.execute()
    return {"status": "ok",
            "above": bool(clusters.get("above")),
            "below": bool(clusters.get("below"))}


def refresh_all_active() -> dict:
    """Refresh per-pair via Coinalyze for the top-20 active pairs.

    Rate limit awareness: 40 calls/min free. With 20 pairs every 60s we use
    half the budget — leaves room for other Coinalyze callers (the existing
    `data/liquidation_levels.py` may also use it).
    """
    r = redis_client.get()
    try:
        pairs = sorted(r.smembers("scanner:active_pairs"))[:20]
    except Exception:
        return {"status": "no_pairs"}
    ok = err = 0
    for p in pairs:
        mark_raw = r.get(f"{p}:mark_price")
        if mark_raw is None:
            err += 1
            continue
        try:
            mark = float(mark_raw)
        except (TypeError, ValueError):
            err += 1
            continue
        result = refresh_pair(p, mark)
        if result.get("status") == "ok":
            ok += 1
        else:
            err += 1
        time.sleep(1.6)   # ~37 calls/min, under the 40/min cap
    log.info("coinalyze_liq_refreshed", ok=ok, err=err, total=len(pairs))
    return {"status": "ok", "ok": ok, "err": err}
