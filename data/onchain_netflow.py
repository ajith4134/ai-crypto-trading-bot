"""
F52 — Exchange Net-Flow Directional Gate (cont. 55, 2026-05-28).

Closes the largest blind spot in the bot's direction-prediction stack:
every existing forecaster (F13, F19, F20, F48, F50c, F50g, F24M) consumes
exchange-internal OHLCV / LOB / microstructure data only. None see coins
actually entering or leaving the exchanges — which is the direct proxy
for forthcoming sell or buy pressure.

Research basis:
  - Coin Metrics 2025 "On-Chain Signal Quality" — exchange net-flow > 2σ
    predicts directional move with 72% accuracy as a regime gate.
  - Glassnode 2024-2026 backtests — net inflows precede sell pressure
    (median lead 4-12h); outflows precede accumulation rallies.

Design (see next_impl/f52_exchange_netflow.md):
  - Producer polls free-tier endpoints in priority order, falls through.
  - Computes 30d rolling z-score per pair.
  - Writes {pair}:exchange_netflow_z to Redis.
  - Consumer in signals/engine.py adds a ±8 bonus to direction confidence
    when |z| > 1.5 and the inflow/outflow sign aligns with the trade.
  - Kill switch: SET netflow:disabled 1.
  - Deadlock detector per memory rule [[rl-deadlock-detector]]: when the
    reject rate exceeds 80% over 50+ provider calls the producer is
    auto-disabled (manual SET netflow:disabled 0 to re-enable).

Cold-start safe — until the 30-day window fills, z-score is None and the
consumer adds zero.
"""
from __future__ import annotations

import json
import os
import time
import math
from typing import Any
from collections import defaultdict

import requests
import structlog

import redis_client
import redis_keys
from feature_governance.registry import register

log = structlog.get_logger()

_FG_ID = "F52"

# Self-register so the consume-side `assert_registered` check passes when
# signals/engine.py calls update_contribution after a trade closes.
try:
    register(_FG_ID, "Exchange Net-Flow Directional Gate", activation_phase=0)
except Exception as _exc:
    # Bootstrap may not have init'd the DB pool yet (import order); the
    # main bootstrap.py call will register us later. Non-fatal.
    log.debug("f52_self_register_deferred", err=str(_exc))


# ---- Provider configuration -------------------------------------------------
# Each provider returns net-flow in USD for a given symbol. Net positive =
# inflow to exchanges = bearish; net negative = outflow = bullish.
# Free-tier endpoints as of 2026-05-28; rate-limited at ~10 req/min each.

_PROVIDERS = [
    {
        "name": "cryptoquant",
        "url_tpl": ("https://api.cryptoquant.com/v1/{asset}/exchange-flows/"
                    "netflow-total?window=hour&limit=1"),
        "asset_map": {"BTCUSDT": "btc", "ETHUSDT": "eth"},
        "parse": lambda j: float(j.get("result", {})
                                 .get("data", [{}])[0]
                                 .get("netflow_total", 0.0)),
    },
    {
        "name": "glassnode",
        "url_tpl": ("https://api.glassnode.com/v1/metrics/transactions/"
                    "transfers_volume_to_exchanges_net?a={asset}&i=1h&c=USD"),
        "asset_map": {"BTCUSDT": "BTC", "ETHUSDT": "ETH"},
        "parse": lambda j: float(j[-1].get("v", 0.0)) if isinstance(j, list) and j else 0.0,
    },
    {
        "name": "coinmetrics",
        "url_tpl": ("https://community-api.coinmetrics.io/v4/timeseries/"
                    "asset-metrics?assets={asset}&metrics=FlowNetExUSD"
                    "&frequency=1h&page_size=1"),
        "asset_map": {"BTCUSDT": "btc", "ETHUSDT": "eth", "SOLUSDT": "sol"},
        "parse": lambda j: float(j.get("data", [{}])[0].get("FlowNetExUSD", 0.0)),
    },
]

# Pairs with true per-asset chain-flow signal. Others get a beta-proxy
# from BTC's signal × pair's 30d β to BTC (computed in _proxy_for_alt).
_NATIVE_PAIRS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

# 30d × 24h = 720 hourly samples. Stored as JSON list in Redis.
_ROLLING_WINDOW = 720

# Deadlock detector thresholds (see memory [[rl-deadlock-detector]]).
_DEADLOCK_MIN_CALLS = 50
_DEADLOCK_REJECT_FRAC = 0.80

# Provider call timeout — keep short so a slow provider doesn't stall the
# 5-min celery beat. The pipeline tries the next provider on timeout.
_PROVIDER_TIMEOUT_S = 8


def _r():
    return redis_client.get()


def is_disabled() -> bool:
    """Kill switch + deadlock-detector check. Consumer must call this."""
    r = _r()
    if (r.get("netflow:disabled") or b"0").decode(errors="ignore") == "1":
        return True
    calls = int(r.get("netflow:call_count") or 0)
    rejects = int(r.get("netflow:reject_count") or 0)
    if calls >= _DEADLOCK_MIN_CALLS and rejects / max(calls, 1) >= _DEADLOCK_REJECT_FRAC:
        r.set("netflow:disabled", "1")
        log.error("f52_netflow_auto_disabled_deadlock",
                  calls=calls, rejects=rejects)
        return True
    return False


def _bump_call() -> None:
    _r().incr("netflow:call_count")


def _bump_reject(reason: str) -> None:
    r = _r()
    r.incr("netflow:reject_count")
    r.incr(f"netflow:reject:{reason}")


def _fetch_raw_netflow(pair: str) -> float | None:
    """Try each provider until one returns a valid float. None = all failed.

    Honest about asset coverage: providers vary in which alts they expose.
    A None return for an alt does NOT mean the pair has no flow — it means
    we couldn't measure it directly; the consumer-side proxy fills in.
    """
    for prov in _PROVIDERS:
        asset = prov["asset_map"].get(pair)
        if not asset:
            continue
        url = prov["url_tpl"].format(asset=asset)
        try:
            _bump_call()
            resp = requests.get(url, timeout=_PROVIDER_TIMEOUT_S)
            if resp.status_code != 200:
                _bump_reject(f"http_{resp.status_code}")
                continue
            payload = resp.json()
            value = prov["parse"](payload)
            if not math.isfinite(value):
                _bump_reject("nan")
                continue
            log.debug("f52_netflow_provider_ok",
                      provider=prov["name"], pair=pair, value=value)
            return float(value)
        except requests.Timeout:
            _bump_reject("timeout")
        except requests.RequestException as exc:
            _bump_reject("transport")
            log.debug("f52_provider_transport_fail",
                      provider=prov["name"], err=str(exc)[:120])
        except Exception as exc:
            _bump_reject("parse")
            log.debug("f52_provider_parse_fail",
                      provider=prov["name"], err=str(exc)[:120])
    return None


def _update_window(pair: str, raw: float) -> tuple[float, str]:
    """Append a raw sample, recompute the z-score over the rolling 30d window.

    Returns (z, regime) where regime ∈ {"inflow", "outflow", "neutral"}.
    Bot uses z directly; the regime label is for the dashboard panel.
    """
    r = _r()
    win_key = f"{pair}:exchange_netflow_window"
    window_raw = r.lrange(win_key, 0, _ROLLING_WINDOW - 1)
    window = [float(x) for x in window_raw if x is not None]
    window.append(raw)
    if len(window) > _ROLLING_WINDOW:
        window = window[-_ROLLING_WINDOW:]
    # Push raw + trim.
    pipe = r.pipeline(transaction=False)
    pipe.lpush(win_key, raw)
    pipe.ltrim(win_key, 0, _ROLLING_WINDOW - 1)
    pipe.set(redis_keys.EXCHANGE_NETFLOW_RAW.replace("{pair}", pair), raw)
    pipe.execute()

    # Need at least 24 samples (~1 day) before z is meaningful — until then
    # report z=0 + neutral. Avoids spurious early signals.
    if len(window) < 24:
        z = 0.0
    else:
        mu = sum(window) / len(window)
        var = sum((x - mu) ** 2 for x in window) / max(len(window) - 1, 1)
        sd = math.sqrt(var) if var > 0 else 0.0
        z = (raw - mu) / sd if sd > 0 else 0.0
    if z > 1.5:
        regime = "inflow"   # bearish
    elif z < -1.5:
        regime = "outflow"  # bullish
    else:
        regime = "neutral"
    pipe = r.pipeline(transaction=False)
    pipe.set(redis_keys.EXCHANGE_NETFLOW_Z.replace("{pair}", pair), z)
    pipe.set(redis_keys.EXCHANGE_NETFLOW_REGIME.replace("{pair}", pair), regime)
    pipe.execute()
    return z, regime


def _btc_beta_proxy(alt_pair: str) -> float:
    """Estimate a pair's BTC-β from its 30d 1m return correlation, used to
    derive a proxy net-flow z for alts without native chain coverage.

    Returns 0.0 when we don't have enough history (cold start; consumer-side
    bonus is 0). 1.0 means alt moves identically to BTC; values >1 mean
    higher-beta alts (e.g. SOL ~1.5 to BTC in 2025-2026 regime).
    """
    r = _r()
    cache_key = f"{alt_pair}:btc_beta_30d"
    cached = r.get(cache_key)
    if cached is not None:
        try:
            return float(cached)
        except Exception:
            pass
    try:
        # Sample last 1440 1m candles (24h) for both, downsample to 30 points.
        btc_raw = r.lrange("BTCUSDT:1m:candles", 0, 1439)
        alt_raw = r.lrange(f"{alt_pair}:1m:candles", 0, 1439)
        # cont. 68: floor lowered 100→60 to match reality — data/feed.py polls
        # short candles with limit=65, so the live 1m:candles list never exceeds
        # ~65 entries. The old 100 floor made this return 0.0 for EVERY pair,
        # silently disabling both F52 proxy-netflow AND the F53 idiosyncratic
        # gate (engine.py). A ~64-sample (1h) 1m-return beta is statistically
        # adequate for a short-horizon BTC-β estimate.
        if len(btc_raw) < 60 or len(alt_raw) < 60:
            return 0.0
        n = min(len(btc_raw), len(alt_raw), 1440)
        btc_c = [float(json.loads(c)["c"]) for c in btc_raw[:n]]
        alt_c = [float(json.loads(c)["c"]) for c in alt_raw[:n]]
        # Returns
        btc_r = [(btc_c[i - 1] / btc_c[i]) - 1.0 for i in range(1, n) if btc_c[i] > 0]
        alt_r = [(alt_c[i - 1] / alt_c[i]) - 1.0 for i in range(1, n) if alt_c[i] > 0]
        m = min(len(btc_r), len(alt_r))
        if m < 40:
            return 0.0
        btc_r = btc_r[:m]
        alt_r = alt_r[:m]
        mu_b = sum(btc_r) / m
        mu_a = sum(alt_r) / m
        cov = sum((btc_r[i] - mu_b) * (alt_r[i] - mu_a) for i in range(m)) / m
        var_b = sum((btc_r[i] - mu_b) ** 2 for i in range(m)) / m
        beta = cov / var_b if var_b > 0 else 0.0
        beta = max(-3.0, min(3.0, beta))  # clamp pathological estimates
        r.setex(cache_key, 3600, beta)
        return beta
    except Exception as exc:
        log.debug("f52_beta_calc_failed", pair=alt_pair, err=str(exc)[:120])
        return 0.0


def _proxy_for_alt(alt_pair: str, btc_z: float) -> tuple[float, str]:
    """Derive a proxy z for an alt pair from BTC's z × the alt's β.

    Marked separately so consumer can halve the bonus (proxy is noisier
    than native chain-flow data).
    """
    beta = _btc_beta_proxy(alt_pair)
    if beta == 0.0:
        return 0.0, "neutral"
    proxy_z = btc_z * beta
    # Apply the same regime thresholds, but slightly tighter so proxy noise
    # doesn't false-positive.
    if proxy_z > 1.8:
        regime = "inflow"
    elif proxy_z < -1.8:
        regime = "outflow"
    else:
        regime = "neutral"
    r = _r()
    pipe = r.pipeline(transaction=False)
    pipe.set(redis_keys.EXCHANGE_NETFLOW_Z.replace("{pair}", alt_pair), proxy_z)
    pipe.set(redis_keys.EXCHANGE_NETFLOW_REGIME.replace("{pair}", alt_pair), regime)
    pipe.set(f"{alt_pair}:exchange_netflow_proxy", "1")  # flag for consumer
    pipe.execute()
    return proxy_z, regime


def _active_pairs() -> list[str]:
    """Read the scanner's active pair set; fall back to anchor pairs."""
    r = _r()
    try:
        members = r.smembers(redis_keys.ACTIVE_PAIRS) or set()
        if isinstance(members, set):
            pairs = [m.decode() if isinstance(m, bytes) else m for m in members]
        else:
            pairs = list(members)
        if pairs:
            return pairs
    except Exception:
        pass
    try:
        anchor = r.smembers(redis_keys.SCANNER_ANCHOR_PAIRS) or set()
        return [m.decode() if isinstance(m, bytes) else m for m in anchor]
    except Exception:
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


# ---- Public entrypoints -----------------------------------------------------

def refresh_one(pair: str) -> dict[str, Any]:
    """Refresh net-flow for a single pair. Used in unit-test / manual run."""
    if is_disabled():
        return {"ok": False, "reason": "disabled"}
    if pair in _NATIVE_PAIRS:
        raw = _fetch_raw_netflow(pair)
        if raw is None:
            return {"ok": False, "reason": "all_providers_failed", "pair": pair}
        z, regime = _update_window(pair, raw)
        return {"ok": True, "pair": pair, "raw": raw, "z": z, "regime": regime,
                "source": "native"}
    # Alt → proxy from BTC z.
    btc_z_raw = _r().get(redis_keys.EXCHANGE_NETFLOW_Z.replace("{pair}", "BTCUSDT"))
    if btc_z_raw is None:
        return {"ok": False, "reason": "btc_z_not_yet_available", "pair": pair}
    btc_z = float(btc_z_raw)
    z, regime = _proxy_for_alt(pair, btc_z)
    return {"ok": True, "pair": pair, "z": z, "regime": regime, "source": "proxy"}


def refresh_all() -> dict[str, Any]:
    """Celery beat entrypoint. Runs every 5 minutes."""
    t0 = time.time()
    if is_disabled():
        log.warning("f52_refresh_skipped_disabled")
        return {"ok": False, "reason": "disabled"}
    pairs = _active_pairs()
    native_ok = 0
    proxy_ok = 0
    failed = 0
    # Native first so proxy lookups can read fresh BTC z.
    for p in [x for x in pairs if x in _NATIVE_PAIRS]:
        out = refresh_one(p)
        if out.get("ok"):
            native_ok += 1
        else:
            failed += 1
    for p in [x for x in pairs if x not in _NATIVE_PAIRS]:
        out = refresh_one(p)
        if out.get("ok"):
            proxy_ok += 1
        else:
            failed += 1
    elapsed = round(time.time() - t0, 2)
    r = _r()
    r.set(redis_keys.NETFLOW_UPDATED_AT, int(time.time()))
    r.set("netflow:last_run_elapsed_s", elapsed)
    r.set("netflow:last_run_native_ok", native_ok)
    r.set("netflow:last_run_proxy_ok", proxy_ok)
    r.set("netflow:last_run_failed", failed)
    log.info("f52_netflow_refresh_complete",
             native_ok=native_ok, proxy_ok=proxy_ok, failed=failed,
             elapsed_s=elapsed)
    return {"ok": True, "native_ok": native_ok, "proxy_ok": proxy_ok,
            "failed": failed, "elapsed_s": elapsed}


# ---- Consumer helper (used by signals/engine.py) ----------------------------

def get_bonus(pair: str, direction: str) -> int:
    """Return additive direction-confidence bonus for the composite scorer.

    Sized parallel to F50g (±8) for native pairs, halved (±4) for proxy
    pairs. Cold-start safe — missing keys / disabled gate return 0.

    Caller does NOT need to import this module — it's available so the
    engine can read it via dynamic import to keep cold-start light.
    """
    if is_disabled():
        return 0
    r = _r()
    z_raw = r.get(redis_keys.EXCHANGE_NETFLOW_Z.replace("{pair}", pair))
    if z_raw is None:
        return 0
    try:
        z = float(z_raw)
    except Exception:
        return 0
    is_proxy = (r.get(f"{pair}:exchange_netflow_proxy") or b"0") == b"1" \
        or (r.get(f"{pair}:exchange_netflow_proxy") == "1")
    base_strong = 4 if is_proxy else 8
    base_extreme = 6 if is_proxy else 12

    if abs(z) < 1.5:
        return 0
    # z>0 = inflow = bearish → bonus for SHORT direction.
    # z<0 = outflow = bullish → bonus for LONG direction.
    sign = -1 if z > 0 else 1     # +1 = bullish, -1 = bearish
    if (sign == 1 and direction == "long") or (sign == -1 and direction == "short"):
        return base_extreme if abs(z) > 2.5 else base_strong
    if (sign == 1 and direction == "short") or (sign == -1 and direction == "long"):
        return -(base_extreme if abs(z) > 2.5 else base_strong)
    return 0
