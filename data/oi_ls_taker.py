"""
Priority 2 (cont. 74, 2026-06-07) — Open Interest velocity + Long/Short ratio +
Taker buy/sell ratio producer.

Closes the audit's biggest data gap. The existing stack has ZERO of:
  - OI CHANGE RATE — BOT_BLUEPRINT.md:2706 "Rising OI + rising price = long
    strength; rising OI + falling price = short strength" (audit Gate 3).
  - LONG/SHORT crowding — >70-75% one side precedes a squeeze (audit Gate 4).
  - TAKER buy/sell — who is the aggressor (5m momentum).

All three are FREE, production `/futures/data` Binance endpoints (verified live:
testnet=False; these endpoints don't exist on testnet). Each returns a 30-sample
ascending time series, so z-scores are computed INLINE — no Redis rolling window.

Design mirrors data/onchain_netflow.py (proven producer template):
  - is_disabled() deadlock kill-switch (`oils:disabled`, 80% reject over 50 calls).
  - _bump_call / _bump_reject(reason) — Rule 12 silent-failure counters.
  - refresh_one(pair) → 4 client calls (OI hist, global LS, top LS, taker).
  - refresh_all() celery-beat entrypoint, top-N active pairs, weight-bounded.
  - get_oi_bonus / get_ls_bonus — engine consumer helpers (Step C; live & 0-safe).

I/O-bound (REST), not CPU-bound — the ceiling is Binance request-weight, not the
box's 10 vCPU; a small thread pool fans out across pairs without starving cores.

This module is ADDITIVE: it only writes new Redis keys. It does NOT touch
FEATURE_COLUMNS or any trade decision (those are Steps B/C, gated on Rule 14).
"""
from __future__ import annotations

import time
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

import structlog

import redis_client
import redis_keys

log = structlog.get_logger()

# 30 × 5m = 2.5h of history → enough for a stable z-score, one call per endpoint.
_PERIOD = "5m"
_LIMIT = 30
_TTL_S = 600

# Deadlock detector (shared convention with netflow / liq producers).
_DEADLOCK_MIN_CALLS = 50
_DEADLOCK_REJECT_FRAC = 0.80

# Weight-bounded fan-out. The bot is fapi-weight-sensitive (cont.69x micro-ws
# work). 4 calls/pair × N pairs every 5 min. Redis-tunable `oils:max_pairs`.
_DEFAULT_MAX_PAIRS = 50
# Modest concurrency: I/O-bound, but keep well under the 1200/min weight budget
# and the box's 10 vCPU. _track_weight() in the client serialises the rate cap.
_MAX_WORKERS = 8

_ANCHORS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def _r():
    return redis_client.get()


# ---- health / deadlock ------------------------------------------------------

def is_disabled() -> bool:
    """Kill switch + deadlock-detector. Consumers (Step C) must call this."""
    r = _r()
    if _as_str(r.get(redis_keys.OILS_DISABLED), "0") == "1":
        return True
    calls = int(r.get(redis_keys.OILS_CALL_COUNT) or 0)
    rejects = int(r.get(redis_keys.OILS_REJECT_COUNT) or 0)
    if calls >= _DEADLOCK_MIN_CALLS and rejects / max(calls, 1) >= _DEADLOCK_REJECT_FRAC:
        r.set(redis_keys.OILS_DISABLED, "1")
        log.error("oils_auto_disabled_deadlock", calls=calls, rejects=rejects)
        return True
    return False


def _as_str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="ignore")
    return str(v)


def _bump_call() -> None:
    _r().incr(redis_keys.OILS_CALL_COUNT)


def _bump_reject(reason: str) -> None:
    r = _r()
    r.incr(redis_keys.OILS_REJECT_COUNT)
    r.incr(f"oils:reject:{reason}")


# ---- math helpers -----------------------------------------------------------

def _zscore(series: list[float]) -> float:
    """z-score of the LAST element vs the distribution of the whole series.
    Returns 0.0 when too short or degenerate — honest signal-of-absence."""
    vals = [v for v in series if v is not None and math.isfinite(v)]
    if len(vals) < 10:
        return 0.0
    mean = sum(vals) / len(vals)
    var = sum((x - mean) ** 2 for x in vals) / len(vals)
    std = var ** 0.5
    if std <= 1e-12:
        return 0.0
    return max(-5.0, min(5.0, (vals[-1] - mean) / std))


def _sign(x: float) -> float:
    if x > 1e-12:
        return 1.0
    if x < -1e-12:
        return -1.0
    return 0.0


# ---- per-pair refresh -------------------------------------------------------

def refresh_one(pair: str, client) -> dict[str, Any]:
    """Fetch OI/LS/taker for one pair, compute features, write Redis. Each
    endpoint failure is isolated (telemetry counter + continue) so a single
    bad symbol never zero-fills the others."""
    r = _r()
    out: dict[str, Any] = {"pair": pair, "ok": False}
    wrote = 0

    # 1) OI history → oi_now, oi_change_5m, oi_change_z, oi_price_div.
    #    price_t = sumOpenInterestValue / sumOpenInterest (derived from same call).
    try:
        _bump_call()
        rows = client.get_open_interest_hist(pair, _PERIOD, _LIMIT)
        oi = [float(x["sumOpenInterest"]) for x in rows
              if float(x.get("sumOpenInterest", 0)) > 0]
        val = [float(x["sumOpenInterestValue"]) for x in rows
               if float(x.get("sumOpenInterest", 0)) > 0]
        if len(oi) >= 2:
            oi_now = oi[-1]
            oi_change_5m = (oi[-1] - oi[-2]) / oi[-2] if oi[-2] > 0 else 0.0
            pct_changes = [(oi[i] - oi[i - 1]) / oi[i - 1]
                           for i in range(1, len(oi)) if oi[i - 1] > 0]
            oi_change_z = _zscore(pct_changes) if pct_changes else 0.0
            # Gate 3: sign(Δoi) · sign(Δprice) over the same last step.
            price_prev = val[-2] / oi[-2] if oi[-2] > 0 else 0.0
            price_now = val[-1] / oi[-1] if oi[-1] > 0 else 0.0
            d_price = (price_now - price_prev) if price_prev > 0 else 0.0
            oi_price_div = _sign(oi[-1] - oi[-2]) * _sign(d_price)
            r.setex(redis_keys.OI_NOW.replace("{pair}", pair), _TTL_S, oi_now)
            r.setex(redis_keys.OI_CHANGE_5M.replace("{pair}", pair), _TTL_S,
                    round(oi_change_5m, 6))
            r.setex(redis_keys.OI_CHANGE_Z.replace("{pair}", pair), _TTL_S,
                    round(oi_change_z, 4))
            r.setex(redis_keys.OI_PRICE_DIV.replace("{pair}", pair), _TTL_S,
                    oi_price_div)
            wrote += 1
    except Exception as exc:
        _bump_reject("oi_" + _reason(exc))

    # 2) Global LS account ratio (retail crowd) → ls_global_ratio.
    try:
        _bump_call()
        rows = client.get_longshort_ratio(pair, _PERIOD, _LIMIT, top=False)
        gr = [float(x["longShortRatio"]) for x in rows]
        if gr:
            r.setex(redis_keys.LS_GLOBAL_RATIO.replace("{pair}", pair), _TTL_S,
                    round(gr[-1], 4))
            wrote += 1
    except Exception as exc:
        _bump_reject("lsg_" + _reason(exc))

    # 3) Top-trader LS position ratio (smart money) → ls_top_ratio, ls_crowd_z.
    try:
        _bump_call()
        rows = client.get_longshort_ratio(pair, _PERIOD, _LIMIT, top=True)
        tr = [float(x["longShortRatio"]) for x in rows]
        if tr:
            r.setex(redis_keys.LS_TOP_RATIO.replace("{pair}", pair), _TTL_S,
                    round(tr[-1], 4))
            r.setex(redis_keys.LS_CROWD_Z.replace("{pair}", pair), _TTL_S,
                    round(_zscore(tr), 4))
            wrote += 1
    except Exception as exc:
        _bump_reject("lst_" + _reason(exc))

    # 4) Taker buy/sell volume ratio (aggressor) → taker_ratio, taker_ratio_z.
    try:
        _bump_call()
        rows = client.get_taker_ratio(pair, _PERIOD, _LIMIT)
        tk = [float(x["buySellRatio"]) for x in rows]
        if tk:
            r.setex(redis_keys.TAKER_RATIO.replace("{pair}", pair), _TTL_S,
                    round(tk[-1], 4))
            r.setex(redis_keys.TAKER_RATIO_Z.replace("{pair}", pair), _TTL_S,
                    round(_zscore(tk), 4))
            wrote += 1
    except Exception as exc:
        _bump_reject("taker_" + _reason(exc))

    out["ok"] = wrote > 0
    out["wrote"] = wrote
    return out


def _reason(exc: Exception) -> str:
    msg = str(exc)
    if "429" in msg or "Too Many" in msg:
        return "429"
    if "Invalid symbol" in msg or "-1121" in msg:
        return "bad_symbol"
    if "timeout" in msg.lower():
        return "timeout"
    return "other"


# ---- pair selection ---------------------------------------------------------

def _active_pairs() -> list[str]:
    """Top-N active scanner pairs (anchors always included), bounded by
    `oils:max_pairs`. Mirrors onchain_netflow._active_pairs."""
    r = _r()
    try:
        max_pairs = int(r.get("oils:max_pairs") or _DEFAULT_MAX_PAIRS)
    except (TypeError, ValueError):
        max_pairs = _DEFAULT_MAX_PAIRS
    pairs: list[str] = []
    try:
        members = r.smembers(redis_keys.ACTIVE_PAIRS) or set()
        pairs = [_as_str(m) for m in members]
    except Exception:
        pairs = []
    if not pairs:
        try:
            anchor = r.smembers(redis_keys.SCANNER_ANCHOR_PAIRS) or set()
            pairs = [_as_str(m) for m in anchor]
        except Exception:
            pairs = []
    # Anchors first, dedup, cap.
    ordered = list(dict.fromkeys(_ANCHORS + sorted(pairs)))
    return ordered[:max_pairs] if ordered else list(_ANCHORS)


# ---- beat entrypoint --------------------------------------------------------

def refresh_all() -> dict[str, Any]:
    """Celery beat entrypoint. Every 5 minutes. Weight-bounded, fanned out over
    a small thread pool (I/O-bound; client._track_weight serialises the cap)."""
    t0 = time.time()
    if is_disabled():
        log.warning("oils_refresh_skipped_disabled")
        return {"ok": False, "reason": "disabled"}

    # Local import keeps cold-start light and avoids a hard dep at module load.
    from exchange.client import BinanceClient
    client = BinanceClient()

    pairs = _active_pairs()
    ok = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
        futs = {ex.submit(refresh_one, p, client): p for p in pairs}
        for fut in as_completed(futs):
            try:
                res = fut.result()
                if res.get("ok"):
                    ok += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                _bump_reject("worker_" + _reason(exc))

    elapsed = round(time.time() - t0, 2)
    r = _r()
    r.set(redis_keys.OILS_UPDATED_AT, int(time.time()))
    r.set("oils:last_run_ok", ok)
    r.set("oils:last_run_failed", failed)
    r.set("oils:last_run_elapsed_s", elapsed)
    log.info("oils_refresh_complete", pairs=len(pairs), ok=ok,
             failed=failed, elapsed_s=elapsed)
    return {"ok": True, "pairs": len(pairs), "ok_count": ok,
            "failed": failed, "elapsed_s": elapsed}


# ---- consumer helpers (Step C — wired into engine later, Rule 14) -----------

def get_oi_bonus(pair: str, direction: str) -> int:
    """Audit Gate 3: OI×price divergence as a directional confidence nudge.
    oi_price_div = +1 (OI & price same direction = trend conviction) →
    reward a trade ALIGNED with the price move; -1 (OI up, price down =
    distribution / short conviction). Cold-start & disabled → 0. SOFT (±6).
    Redis-toggleable: SET oils:oi_gate_enabled 0 to disable (default ON)."""
    if is_disabled():
        return 0
    r = _r()
    if _as_str(r.get("oils:oi_gate_enabled"), "1") == "0":
        return 0
    try:
        div = float(r.get(redis_keys.OI_PRICE_DIV.replace("{pair}", pair)) or 0)
        z = float(r.get(redis_keys.OI_CHANGE_Z.replace("{pair}", pair)) or 0)
    except (TypeError, ValueError):
        return 0
    if abs(z) < 1.0 or div == 0.0:
        return 0
    # div=+1 → rising-OI continuation favours the prevailing move (long-biased
    # when price rose). Sign maps to the trade direction at the call site.
    base = 6 if abs(z) >= 2.0 else 4
    want_long = (direction or "").lower() in ("long", "buy")
    aligned = (div > 0 and want_long) or (div < 0 and not want_long)
    return base if aligned else -base


def get_ls_bonus(pair: str, direction: str) -> int:
    """Audit Gate 4: crowding. A heavily one-sided top-trader ratio (high
    |ls_crowd_z|) warns of squeeze risk AGAINST the crowd. Penalise trades that
    pile onto an already-crowded side; small reward for the contrarian. SOFT.
    Redis-toggleable: SET oils:ls_gate_enabled 0 to disable (default ON)."""
    if is_disabled():
        return 0
    r = _r()
    if _as_str(r.get("oils:ls_gate_enabled"), "1") == "0":
        return 0
    try:
        crowd_z = float(r.get(redis_keys.LS_CROWD_Z.replace("{pair}", pair)) or 0)
        top_ratio = float(r.get(redis_keys.LS_TOP_RATIO.replace("{pair}", pair)) or 0)
    except (TypeError, ValueError):
        return 0
    if abs(crowd_z) < 1.5 or top_ratio <= 0:
        return 0
    crowded_long = top_ratio > 1.0
    want_long = (direction or "").lower() in ("long", "buy")
    base = 6 if abs(crowd_z) >= 2.5 else 4
    # Joining the crowded side = squeeze risk → penalise; fading it = reward.
    joining_crowd = (crowded_long and want_long) or (not crowded_long and not want_long)
    return -base if joining_crowd else base


if __name__ == "__main__":  # manual smoke test
    print(refresh_all())
