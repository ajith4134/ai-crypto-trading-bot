"""SciBrain per-module Information-Coefficient tracker (Phase 3, Task 3).

The Information Coefficient (IC) is the canonical quant measure of an alpha's skill: the
correlation between a signal's *prediction* and the *realized* forward return. Here we
measure it per module, in SHADOW, over the whole scored universe — no real trade needed,
so the window fills fast and every expert is graded continuously:

  record(r, sym, ref_price, outputs)   ← at score time: stash each module's signed vote +
                                          the reference price, maturing IC_HORIZON_MIN later.
  settle(r)                            ← once per funnel cycle (main process): for every
                                          matured observation, read the realized forward
                                          return, push (vote, return) into each module's
                                          rolling window, and recompute its IC.
  get_ic_map(r)                        ← {module: IC} the Meta-Router folds into its gains.

IC = Pearson(votes, realized_returns) over the last IC_WINDOW samples; below IC_MIN_SAMPLES
the module's IC is reported as None (untrusted → router multiplier 1.0). Recording happens
inside the parallel scorer workers (concurrent ZADD is safe); settlement is centralized in
the gate's main process so a matured observation is folded exactly once. Never raises.
"""
from __future__ import annotations

import json
import math
import time

import structlog

from . import keys as K
from .contracts import ModuleOutput

log = structlog.get_logger()

_DEF_HORIZON_MIN = 30
_DEF_WINDOW = 400
_DEF_MIN_SAMPLES = 30
_PRICE_TF = "5m"          # timeframe whose latest close is the price reference / settlement
_SETTLE_BATCH = 500       # max observations folded per settle() call (bounds cycle cost)
_MAX_PENDING = 5000       # hard cap on the pending ZSET (drop oldest beyond this)


def _enabled(r) -> bool:
    try:
        return (r.get(K.IC_ENABLED) or "1") != "0"
    except Exception:
        return False


def _icfg(r, key: str, default: int) -> int:
    try:
        v = r.get(key)
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _latest_close(r, symbol: str) -> float | None:
    """Newest closed-candle close for `symbol` on the price TF, or None."""
    try:
        raw = r.lrange(f"{symbol}:{_PRICE_TF}:candles", 0, 0)   # newest-first → index 0
    except Exception:
        return None
    if not raw:
        return None
    try:
        c = float(json.loads(raw[0])["c"])
        return c if c > 0 else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def record(r, symbol: str, ref_price: float | None,
           outputs: list[ModuleOutput], now: float | None = None) -> None:
    """Stash this cycle's per-module votes for `symbol`, to be graded IC_HORIZON_MIN later.

    Deduped to one observation per symbol per horizon bucket (so a symbol scored every 20s
    doesn't flood the queue). Best-effort; visibility/learning must never break scoring."""
    if not _enabled(r):
        return
    if ref_price is None or ref_price <= 0:
        return
    now = now or time.time()
    horizon_s = _icfg(r, K.IC_HORIZON_MIN, _DEF_HORIZON_MIN) * 60
    if horizon_s <= 0:
        return
    # only the directional experts carry an IC (a 0-vote gate has nothing to grade)
    votes = {o.module: round(float(o.direction), 6)
             for o in outputs if o.ok and o.direction != 0.0}
    if not votes:
        return
    bucket = int(now // horizon_s)
    # DEDUP (the documented intent): record exactly ONE observation per symbol per horizon bucket.
    # Without this, every ~5s cycle re-records all ~486 pairs (the member JSON varies by price/ts so
    # it never collapses), the pending ZSET pins at its cap, and zremrangebyrank evicts every vote
    # long BEFORE its maturity → settlement starves and the IC map freezes at stale values. An O(1)
    # NX marker (auto-expiring just past maturity) lets only the first cycle in each bucket record,
    # so inflow ≈ one/symbol/bucket and every recorded vote survives to settle. (cont. — Rule-19 audit)
    try:
        if not r.set(K.IC_SEEN.replace("{key}", f"{symbol}|{bucket}"), "1",
                     nx=True, ex=int(horizon_s) + 300):
            return                              # this symbol already recorded in this bucket
    except Exception:
        pass                                    # marker unavailable → fall through and still record
    member = json.dumps({"s": symbol, "b": bucket, "p": round(float(ref_price), 10),
                         "v": votes, "t": round(now, 3)}, separators=(",", ":"))
    maturity = now + horizon_s
    try:
        pipe = r.pipeline()
        pipe.zadd(K.IC_PENDING, {member: maturity})
        # safety bound only (dedup keeps this far from saturation): drop oldest beyond the cap
        pipe.zremrangebyrank(K.IC_PENDING, 0, -(_MAX_PENDING + 1))
        pipe.execute()
    except Exception as exc:
        log.debug("scibrain_ic_record_failed", symbol=symbol, error=str(exc)[:120])


def settle(r, now: float | None = None) -> int:
    """Fold every matured observation into the per-module IC windows + recompute IC.

    Runs once per funnel cycle in the main process. Returns the number of observations
    settled. Atomic-ish: each matured member is ZREM'd as it is consumed so a crash can
    at worst lose (not double-count) a sample. Never raises."""
    if not _enabled(r):
        return 0
    now = now or time.time()
    try:
        matured = r.zrangebyscore(K.IC_PENDING, "-inf", now, start=0, num=_SETTLE_BATCH)
    except Exception:
        return 0
    if not matured:
        return 0

    window = _icfg(r, K.IC_WINDOW, _DEF_WINDOW)
    touched: set[str] = set()
    settled = 0

    for member in matured:
        try:
            obs = json.loads(member)
        except (TypeError, ValueError, json.JSONDecodeError):
            r.zrem(K.IC_PENDING, member)         # unparseable → drop it
            continue
        sym = obs.get("s")
        ref = obs.get("p")
        votes = obs.get("v") or {}
        cur = _latest_close(r, sym) if sym else None
        if not sym or not ref or ref <= 0 or cur is None:
            # can't grade (price gone) → discard the observation, don't poison the window
            r.zrem(K.IC_PENDING, member)
            continue
        fwd_ret = math.log(cur / ref)            # realized forward log-return over the horizon
        if not math.isfinite(fwd_ret):
            r.zrem(K.IC_PENDING, member)
            continue
        try:
            pipe = r.pipeline()
            for mod, vote in votes.items():
                pipe.lpush(K.mod_key(K.IC_WIN, mod), f"{float(vote):.6f},{fwd_ret:.8f}")
                pipe.ltrim(K.mod_key(K.IC_WIN, mod), 0, window - 1)
                touched.add(mod)
            pipe.zrem(K.IC_PENDING, member)
            pipe.execute()
            settled += 1
        except Exception as exc:
            log.debug("scibrain_ic_settle_item_failed", symbol=sym, error=str(exc)[:120])

    # recompute IC for the modules that gained samples this pass
    min_samples = _icfg(r, K.IC_MIN_SAMPLES, _DEF_MIN_SAMPLES)
    for mod in touched:
        ic, n = _recompute_ic(r, mod, min_samples)
        try:
            pipe = r.pipeline()
            if ic is None:
                pipe.hdel(K.IC_MAP, mod)
            else:
                pipe.hset(K.IC_MAP, mod, round(ic, 6))
            pipe.hset(K.IC_SAMPLES, mod, n)
            pipe.execute()
        except Exception:
            pass

    if settled:
        try:
            r.incrby(K.IC_SETTLED, settled)
        except Exception:
            pass
        log.info("scibrain_ic_settled", settled=settled, modules=len(touched))
    return settled


def _recompute_ic(r, module: str, min_samples: int) -> tuple[float | None, int]:
    """Pearson IC of (vote, realized_return) over the module's rolling window."""
    try:
        rows = r.lrange(K.mod_key(K.IC_WIN, module), 0, -1)
    except Exception:
        return None, 0
    votes: list[float] = []
    rets: list[float] = []
    for row in rows:
        try:
            v_s, ret_s = row.split(",")
            votes.append(float(v_s))
            rets.append(float(ret_s))
        except (ValueError, AttributeError):
            continue
    n = len(votes)
    if n < min_samples:
        return None, n
    # Pearson with zero-variance guards (a module that always votes the same sign, or a
    # flat market, has no measurable IC → report None rather than a divide-by-zero/NaN).
    import numpy as np
    a = np.asarray(votes, dtype=float)
    b = np.asarray(rets, dtype=float)
    if a.std() == 0.0 or b.std() == 0.0:
        return None, n
    ic = float(np.corrcoef(a, b)[0, 1])
    return (ic if math.isfinite(ic) else None), n


def get_ic_map(r) -> dict:
    """{module: IC} for the router. Empty when IC is disabled or nothing settled yet."""
    if not _enabled(r):
        return {}
    try:
        raw = r.hgetall(K.IC_MAP) or {}
    except Exception:
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    return out
