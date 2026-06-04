"""cont. 60 — Coinglass liquidation heatmap fetcher.

Coinglass provides aggregated liquidation density per pair. We fetch the
nearest cluster above and below current mark every 1 minute.

Free-tier endpoint requires `COINGLASS_API_KEY` env var. Falls back silently
when key is missing — the dark-side SL placement returns `no_cluster_data`
and the original SL distance is used.

Endpoint: https://open-api-v3.coinglass.com/api/futures/liquidation/heatmap
  ?exchange=Binance&symbol=BTCUSDT&interval=h1

Schedule: every 60s for top 20 most-active pairs (resp time ~200ms each).

Writes:
  `liquidation:nearest_above:{pair}`   JSON {top, bottom, density}
  `liquidation:nearest_below:{pair}`   JSON {top, bottom, density}
  `liquidation:last_refresh:{pair}`    UNIX ts
"""
from __future__ import annotations
import os
import json
import time
import requests
import structlog

import redis_client

log = structlog.get_logger()

_TIMEOUT = 8
_BASE = "https://open-api-v3.coinglass.com/api/futures/liquidation/heatmap"


def _find_clusters(heatmap: list, current_price: float) -> tuple[dict | None, dict | None]:
    """Given a heatmap response (list of {price, density} or similar) and the
    current price, find the nearest density-cluster above and below."""
    if not heatmap or current_price <= 0:
        return None, None
    # Sort by price. Densities above some threshold form "clusters".
    levels = []
    for row in heatmap:
        try:
            p = float(row.get("price"))
            d = float(row.get("density") or row.get("liquidation_amount") or 0)
            if p > 0 and d > 0:
                levels.append((p, d))
        except (TypeError, ValueError, KeyError):
            continue
    if not levels:
        return None, None
    levels.sort()
    # Aggregate adjacent levels into clusters using a 0.5% bandwidth.
    clusters: list[dict] = []
    cur_bottom = None
    cur_top    = None
    cur_d      = 0.0
    for p, d in levels:
        if cur_bottom is None or (p - cur_top) / cur_top > 0.005:
            if cur_bottom is not None:
                clusters.append({"top": cur_top, "bottom": cur_bottom, "density": cur_d})
            cur_bottom = p
            cur_top    = p
            cur_d      = d
        else:
            cur_top = p
            cur_d  += d
    if cur_bottom is not None:
        clusters.append({"top": cur_top, "bottom": cur_bottom, "density": cur_d})

    # Normalise density to 0-1 range
    max_d = max(c["density"] for c in clusters) if clusters else 1.0
    if max_d > 0:
        for c in clusters:
            c["density"] = round(c["density"] / max_d, 4)

    above = next((c for c in clusters if c["bottom"] > current_price), None)
    below = None
    for c in reversed(clusters):
        if c["top"] < current_price:
            below = c
            break
    return above, below


def refresh_pair(pair: str, current_mark: float) -> dict:
    """Refresh nearest-cluster keys for a single pair. Returns status dict."""
    api_key = os.environ.get("COINGLASS_API_KEY")
    if not api_key:
        return {"status": "no_api_key"}
    r = redis_client.get()
    try:
        resp = requests.get(_BASE,
                            params={"exchange": "Binance", "symbol": pair,
                                    "interval": "h1"},
                            headers={"coinglassSecret": api_key},
                            timeout=_TIMEOUT)
    except Exception as exc:
        return {"status": "error", "error": str(exc)[:120]}
    if resp.status_code != 200:
        return {"status": "http_error", "code": resp.status_code}
    try:
        payload = resp.json()
        heatmap = payload.get("data") or []
    except Exception:
        return {"status": "parse_error"}
    above, below = _find_clusters(heatmap, current_mark)
    if above:
        r.setex(f"liquidation:nearest_above:{pair}", 300, json.dumps(above))
    if below:
        r.setex(f"liquidation:nearest_below:{pair}", 300, json.dumps(below))
    r.set(f"liquidation:last_refresh:{pair}", int(time.time()))
    return {"status": "ok", "above": bool(above), "below": bool(below)}


def refresh_all_active() -> dict:
    """cont. 60b — Refresh liquidation clusters via the fallback chain:

      1. Coinglass    (if `COINGLASS_API_KEY` set)
      2. Coinalyze    (if `COINALYZE_API_KEY` set — 40 calls/min/key free)
      3. Binance OI estimator (always-on, no auth — in-process minchillo4-style)

    Each tier writes to the same Redis keys (`liquidation:nearest_above/below:{pair}`)
    so the dark-side SL consumer is source-agnostic. The Binance estimator
    runs LAST and skips pairs already covered by a higher-priority source
    within the last 90s (see `binance_liq_estimator.estimate_all_active`).

    Also kicks off the Moon Dev cross-check at the end as an advisory layer
    bumping per-pair `liquidation:cross_check_score:{pair}`.
    """
    summary = {"coinglass": None, "coinalyze": None, "binance": None, "moondev": None}
    r = redis_client.get()
    pairs = list(r.smembers("scanner:active_pairs"))[:20]
    if not pairs:
        return {"status": "no_pairs", "summary": summary}

    # ─── Tier 1: Coinglass (paid, premium quality when available) ─────────
    if os.environ.get("COINGLASS_API_KEY"):
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
                r.set(f"liquidation:source:{p}", "coinglass")
                ok += 1
            else:
                err += 1
            time.sleep(0.2)
        summary["coinglass"] = {"ok": ok, "err": err}
        log.info("liq_tier_coinglass", ok=ok, err=err, total=len(pairs))

    # ─── Tier 2: Coinalyze (free, multi-exchange aggregated) ──────────────
    if os.environ.get("COINALYZE_API_KEY"):
        try:
            from data.external.coinalyze_liq import refresh_all_active as _calyz
            summary["coinalyze"] = _calyz()
        except Exception as exc:
            log.warning("liq_tier_coinalyze_failed", error=str(exc)[:120])

    # ─── Tier 3: Binance OI estimator (always-on, in-process) ─────────────
    try:
        from data.external.binance_liq_estimator import estimate_all_active
        summary["binance"] = estimate_all_active()
    except Exception as exc:
        log.warning("liq_tier_binance_failed", error=str(exc)[:120])

    # ─── Advisory: Moon Dev cross-check ────────────────────────────────────
    try:
        from data.external.moondev_liq import refresh_all as _md_refresh
        summary["moondev"] = _md_refresh()
    except Exception as exc:
        log.warning("liq_cross_check_failed", error=str(exc)[:120])

    return {"status": "ok", "summary": summary}
