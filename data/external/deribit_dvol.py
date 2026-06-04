"""cont. 60 — Deribit DVOL fetcher (crypto's VIX).

DVOL is Deribit's 30-day implied volatility index for BTC and ETH options.
Free public endpoint, no API key required.

Schedule: every 5 minutes via Celery beat.

Writes:
  `external:dvol:current`         — current DVOL (annualized %)
  `external:dvol:history`         — Redis list, last 252 daily samples
  `external:dvol:median_252d`     — rolling 252-day median (from history)
  `external:dvol:decile_current`  — 0-9 decile of current vs history

Endpoint: https://www.deribit.com/api/v2/public/get_volatility_index_data
  ?currency=BTC&start_timestamp=...&end_timestamp=...&resolution=3600

Source: Deribit DVOL docs (https://insights.deribit.com/...).
"""
from __future__ import annotations
import time
import json
import requests
import structlog

import redis_client

log = structlog.get_logger()

_TIMEOUT = 10
_BASE = "https://www.deribit.com/api/v2/public"
_HISTORY_LEN = 252


def refresh_dvol(currency: str = "BTC") -> dict:
    """Fetch latest DVOL and update Redis state. Returns {status, dvol, decile}."""
    r = redis_client.get()
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 86400 * 1000  # last 24h, hourly resolution
    try:
        resp = requests.get(
            f"{_BASE}/get_volatility_index_data",
            params={"currency": currency, "start_timestamp": start_ms,
                    "end_timestamp": end_ms, "resolution": 3600},
            timeout=_TIMEOUT,
        )
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:120]}
    if resp.status_code != 200:
        return {"status": "http_error", "code": resp.status_code}
    try:
        payload = resp.json()
        data = payload.get("result", {}).get("data") or []
    except Exception:
        return {"status": "parse_error"}
    if not data:
        return {"status": "no_data"}

    # Each row: [timestamp_ms, open, high, low, close]
    latest_close = float(data[-1][4])
    r.set("external:dvol:current", round(latest_close, 4))

    # Maintain rolling 252-day history (use daily close, append last_close once
    # per day — overwrite same-day for idempotence).
    today_key = time.strftime("%Y-%m-%d", time.gmtime())
    last_day_key = r.get("external:dvol:last_history_day")
    if last_day_key != today_key:
        r.lpush("external:dvol:history", latest_close)
        r.ltrim("external:dvol:history", 0, _HISTORY_LEN - 1)
        r.set("external:dvol:last_history_day", today_key)

    # Compute median + decile from history
    history = r.lrange("external:dvol:history", 0, -1)
    if len(history) >= 10:
        try:
            hist_vals = sorted(float(x) for x in history)
            median = hist_vals[len(hist_vals) // 2]
            r.set("external:dvol:median_252d", round(median, 4))
            # Decile of CURRENT value
            n = len(hist_vals)
            rank = sum(1 for v in hist_vals if v <= latest_close)
            decile = min(9, max(0, int(10 * rank / max(n, 1))))
            r.set("external:dvol:decile_current", decile)
        except Exception as exc:
            log.debug("dvol_decile_calc_failed", error=str(exc)[:120])

    log.info("dvol_refreshed", currency=currency, dvol=latest_close,
             history_n=len(history))
    return {"status": "ok", "dvol": latest_close,
            "decile": r.get("external:dvol:decile_current")}
