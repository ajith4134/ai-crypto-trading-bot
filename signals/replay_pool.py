"""Hysteresis Replay Pool — cont. 63 (2026-05-29).

Phase 1 of the signal-monitor remediation. Rejected signals with strength
>= T_low and recoverable reasons get pushed into a Redis sorted set; when
an open-trade slot frees, the engine consumes the best fresh-and-valid
entry from the pool rather than waiting for a fresh fresh-signal sweep.

Stale-invalidation rules (production-validated, Florinelchis 2026):
    * age > REPLAY_POOL_TTL_SECONDS  -> drop, stale_age_count++
    * |mark_now - mark_then| / mark_then > drift_pct_max -> drop, stale_drift_count++
    * regime changed since push -> drop, stale_regime_count++
    * pair removed from scanner active set -> drop, stale_pair_count++

Kill switch: signals:replay_pool:enabled = "0" disables both producer
and consumer cleanly; existing entries are then drained by stale rules.
"""
from __future__ import annotations

import json
import time
from typing import Iterable, Optional

import structlog

import redis_client
import redis_keys

log = structlog.get_logger()


# Reasons that are *recoverable* — the underlying signal could still be
# valid moments later. Hard rejections (pair suspension, bot-confidence
# floor, turbulence cap, regime whitelist deadlock) are NOT replayable.
#
# IMPORTANT: many reject reasons are *dynamically formatted* by the engine
# (e.g. "sentiment_0.45_blocks_short", "btc_pump_0.95pct_blocks_short",
# "memrl_low_winrate_14%_n30", "metacog_low_confidence_18.4",
# "world_model_uncertain_0.82"). We therefore match by PREFIX / SUFFIX
# patterns rather than exact strings. Verified against actual DB rows
# 2026-05-29: btc_*_blocks_*, sentiment_*_blocks_*, marl_minute_skip,
# marl_minute_hold, signal_too_weak, conformal_uncertain, memrl_low_*.
_RECOVERABLE_EXACT = frozenset({
    "signal_too_weak",
    "marl_minute_hold",
    "marl_minute_skip",
    "conformal_uncertain",
    "liquidity_below_floor",
    # cont. 69s — F37 deterministic-verdict skip is a SOFT/advisory veto, not a
    # hard structural block. Live audit 2026-06-02: debate_skip_risk rejected 88
    # signals in 6h up to str 46.6. Make it recoverable so it is eligible for
    # re-admission (replay consumer / EV override) instead of a silent kill.
    "debate_skip_risk",
})
_RECOVERABLE_PREFIXES: tuple[str, ...] = (
    "sentiment_",        # sentiment_0.45_blocks_short etc.
    "btc_pump_",         # btc_pump_0.95pct_blocks_short
    "btc_dump_",         # btc_dump_-0.55pct_blocks_long
    "memrl_low_",        # memrl_low_winrate_14%_n30
    "metacog_low_",      # metacog_low_confidence_18.4
    "world_model_",      # world_model_uncertain_0.82
)


def _is_recoverable(reason: str) -> bool:
    """Match a reject reason against the recoverable whitelist by exact
    string OR by prefix (covers dynamically-formatted reasons)."""
    if not reason:
        return False
    if reason in _RECOVERABLE_EXACT:
        return True
    return any(reason.startswith(p) for p in _RECOVERABLE_PREFIXES)

# Conservative default floor — overridden by bayes_threshold:t_low when
# the Bayesian module has converged (>= 30 samples per bucket).
_DEFAULT_T_LOW = 30.0
_DEFAULT_TTL = 900               # 15 minutes
_DEFAULT_DRIFT_PCT_MAX = 2.0     # %
_DEFAULT_MAX_ENTRIES = 100


def _enabled() -> bool:
    """Producer + consumer master switch. Default ON."""
    try:
        v = redis_client.get().get(redis_keys.REPLAY_POOL_ENABLED)
        if v is None:
            return True
        return v in ("1", b"1")
    except Exception:
        return True


def _resolve_t_low() -> float:
    """Replay-pool admission floor. Reads Bayesian module first; falls
    back to the conservative default if Bayesian hasn't converged."""
    try:
        r = redis_client.get()
        v = r.get(redis_keys.BAYES_THRESHOLD_T_LOW)
        if v is not None:
            return max(15.0, min(50.0, float(v)))
    except Exception:
        pass
    return _DEFAULT_T_LOW


def _resolve_ttl_seconds() -> int:
    try:
        v = redis_client.get().get(redis_keys.REPLAY_POOL_TTL_SECONDS)
        if v is not None:
            return max(60, min(7200, int(v)))
    except Exception:
        pass
    return _DEFAULT_TTL


def _resolve_drift_pct_max() -> float:
    try:
        v = redis_client.get().get(redis_keys.REPLAY_POOL_DRIFT_PCT_MAX)
        if v is not None:
            return max(0.2, min(10.0, float(v)))
    except Exception:
        pass
    return _DEFAULT_DRIFT_PCT_MAX


def _resolve_max_entries() -> int:
    try:
        v = redis_client.get().get(redis_keys.REPLAY_POOL_MAX_ENTRIES)
        if v is not None:
            return max(10, min(1000, int(v)))
    except Exception:
        pass
    return _DEFAULT_MAX_ENTRIES


def push(signal: dict, rejection_reason: str, signal_id: Optional[str] = None) -> bool:
    """Producer. Called from signals/engine.py right after a rejection.

    Returns True if pushed, False if filtered out (disabled, ineligible
    reason, strength too low, missing fields).
    """
    if not _enabled():
        return False
    if not _is_recoverable(rejection_reason):
        return False
    try:
        strength = float(signal.get("signal_strength") or 0)
    except (TypeError, ValueError):
        return False
    t_low = _resolve_t_low()
    if strength < t_low:
        return False
    pair = signal.get("pair")
    direction = signal.get("direction")
    if not pair or direction not in ("long", "short"):
        return False
    try:
        mark = float(signal.get("mark_price") or 0)
    except (TypeError, ValueError):
        mark = 0.0
    if mark <= 0:
        try:
            mark = float(redis_client.get().get(
                redis_keys.MARK_PRICE.replace("{pair}", pair)) or 0)
        except (TypeError, ValueError):
            mark = 0.0
    if mark <= 0:
        return False
    regime = signal.get("market_regime") or "unknown"
    push_ts = int(time.time())
    entry = {
        "signal_id": str(signal_id) if signal_id else None,
        "pair": pair,
        "direction": direction,
        "strength": round(strength, 2),
        "mark_at_push": mark,
        "regime_at_push": regime,
        "push_ts": push_ts,
        "rejection_reason": rejection_reason,
        "trade_potential": signal.get("trade_potential"),
        "direction_confidence": signal.get("direction_confidence"),
    }
    try:
        r = redis_client.get()
        r.zadd(redis_keys.REPLAY_POOL, {json.dumps(entry): push_ts})
        max_n = _resolve_max_entries()
        # LRU cap: keep the newest max_n entries (highest scores)
        r.zremrangebyrank(redis_keys.REPLAY_POOL, 0, -max_n - 1)
        r.incr(redis_keys.REPLAY_PRODUCE_COUNT)
        r.set(redis_keys.REPLAY_LAST_PRODUCE_TS, push_ts)
        log.info("replay_pool_pushed",
                 pair=pair, direction=direction,
                 strength=round(strength, 2),
                 rejection_reason=rejection_reason)
        return True
    except Exception as exc:
        log.warning("replay_pool_push_failed",
                    pair=pair, error=str(exc)[:120])
        return False


def _is_stale(entry: dict, current_regime: str,
              active_pairs: Optional[Iterable[str]],
              drift_pct_max: float, ttl: int) -> Optional[str]:
    """Return a stale-reason string ('age', 'drift', 'regime', 'pair')
    if the entry should be dropped, else None."""
    now = int(time.time())
    push_ts = int(entry.get("push_ts") or 0)
    if now - push_ts > ttl:
        return "age"
    pair = entry.get("pair")
    if active_pairs is not None and pair not in active_pairs:
        return "pair"
    if entry.get("regime_at_push") not in (None, "unknown", current_regime):
        return "regime"
    try:
        mark_then = float(entry.get("mark_at_push") or 0)
        if mark_then <= 0:
            return "drift"
        mark_now = float(redis_client.get().get(
            redis_keys.MARK_PRICE.replace("{pair}", pair)) or 0)
        if mark_now <= 0:
            return "drift"
        drift_pct = abs(mark_now - mark_then) / mark_then * 100.0
        if drift_pct > drift_pct_max:
            return "drift"
    except (TypeError, ValueError):
        return "drift"
    return None


def _bump_stale(reason: str) -> None:
    key = {
        "age":    redis_keys.REPLAY_STALE_AGE_COUNT,
        "drift":  redis_keys.REPLAY_STALE_DRIFT_COUNT,
        "regime": redis_keys.REPLAY_STALE_REGIME_COUNT,
        "pair":   redis_keys.REPLAY_STALE_PAIR_COUNT,
    }.get(reason)
    if not key:
        return
    try:
        redis_client.get().incr(key)
    except Exception:
        pass


def fetch_fresh_entries(limit: int = 25) -> list[dict]:
    """Consumer helper. Returns up-to-limit non-stale entries from the
    pool, newest-first. Stale entries are atomically removed as a side
    effect (the consumer never sees a stale row twice)."""
    if not _enabled():
        return []
    r = redis_client.get()
    try:
        current_regime = r.get(redis_keys.CURRENT_REGIME) or "unknown"
    except Exception:
        current_regime = "unknown"
    try:
        active_pairs = set(r.smembers(redis_keys.ACTIVE_PAIRS) or set())
        # Union with anchors (matches scanner behaviour)
        anchors = r.smembers(redis_keys.SCANNER_ANCHOR_PAIRS) or set()
        active_pairs |= set(anchors)
        if not active_pairs:
            active_pairs = None  # don't filter when scanner not ready
    except Exception:
        active_pairs = None
    ttl = _resolve_ttl_seconds()
    drift_pct_max = _resolve_drift_pct_max()
    try:
        raw_entries = r.zrevrange(redis_keys.REPLAY_POOL, 0, max(limit * 2, 50))
    except Exception:
        return []
    fresh: list[dict] = []
    for raw in raw_entries:
        if isinstance(raw, bytes):
            raw_str = raw.decode("utf-8", errors="ignore")
        else:
            raw_str = raw
        try:
            entry = json.loads(raw_str)
        except (TypeError, ValueError, json.JSONDecodeError):
            try:
                r.zrem(redis_keys.REPLAY_POOL, raw)
            except Exception:
                pass
            continue
        stale_reason = _is_stale(entry, current_regime, active_pairs,
                                 drift_pct_max, ttl)
        if stale_reason:
            try:
                r.zrem(redis_keys.REPLAY_POOL, raw)
            except Exception:
                pass
            _bump_stale(stale_reason)
            continue
        entry["_raw"] = raw_str  # caller needs this to ZREM after consume
        fresh.append(entry)
        if len(fresh) >= limit:
            break
    return fresh


def remove_entry(raw: str) -> None:
    """Consumer commits removal after a successful re-acceptance + open."""
    try:
        redis_client.get().zrem(redis_keys.REPLAY_POOL, raw)
    except Exception:
        pass


def mark_consumed() -> None:
    """Counter bump after a replay entry is actually opened as a trade."""
    try:
        r = redis_client.get()
        r.incr(redis_keys.REPLAY_CONSUME_COUNT)
        r.set(redis_keys.REPLAY_LAST_CONSUME_TS, int(time.time()))
    except Exception:
        pass


def prune_stale() -> dict:
    """Celery beat helper. Walks the pool and drops stale entries.
    Useful when the consumer isn't running often enough.

    Returns a dict of {age, drift, regime, pair, kept} counts."""
    if not _enabled():
        return {"age": 0, "drift": 0, "regime": 0, "pair": 0, "kept": 0,
                "disabled": True}
    r = redis_client.get()
    try:
        current_regime = r.get(redis_keys.CURRENT_REGIME) or "unknown"
    except Exception:
        current_regime = "unknown"
    try:
        active_pairs = set(r.smembers(redis_keys.ACTIVE_PAIRS) or set())
        anchors = r.smembers(redis_keys.SCANNER_ANCHOR_PAIRS) or set()
        active_pairs |= set(anchors)
        if not active_pairs:
            active_pairs = None
    except Exception:
        active_pairs = None
    ttl = _resolve_ttl_seconds()
    drift_pct_max = _resolve_drift_pct_max()
    counts = {"age": 0, "drift": 0, "regime": 0, "pair": 0, "kept": 0}
    try:
        raw_entries = r.zrange(redis_keys.REPLAY_POOL, 0, -1)
    except Exception:
        return counts
    for raw in raw_entries:
        if isinstance(raw, bytes):
            raw_str = raw.decode("utf-8", errors="ignore")
        else:
            raw_str = raw
        try:
            entry = json.loads(raw_str)
        except (TypeError, ValueError, json.JSONDecodeError):
            try:
                r.zrem(redis_keys.REPLAY_POOL, raw)
            except Exception:
                pass
            continue
        stale_reason = _is_stale(entry, current_regime, active_pairs,
                                 drift_pct_max, ttl)
        if stale_reason:
            try:
                r.zrem(redis_keys.REPLAY_POOL, raw)
            except Exception:
                pass
            _bump_stale(stale_reason)
            counts[stale_reason] += 1
        else:
            counts["kept"] += 1
    return counts


def snapshot(limit: int = 50) -> list[dict]:
    """Dashboard helper. Read-only view of current pool (newest first)."""
    r = redis_client.get()
    try:
        raw_entries = r.zrevrange(redis_keys.REPLAY_POOL, 0, limit - 1)
    except Exception:
        return []
    out: list[dict] = []
    for raw in raw_entries:
        if isinstance(raw, bytes):
            raw_str = raw.decode("utf-8", errors="ignore")
        else:
            raw_str = raw
        try:
            out.append(json.loads(raw_str))
        except Exception:
            continue
    return out


def stats() -> dict:
    """Dashboard helper. Aggregate counters + current size."""
    r = redis_client.get()
    def _i(k: str) -> int:
        try:
            v = r.get(k)
            return int(v) if v is not None else 0
        except Exception:
            return 0
    try:
        size = int(r.zcard(redis_keys.REPLAY_POOL) or 0)
    except Exception:
        size = 0
    return {
        "size": size,
        "enabled": _enabled(),
        "t_low": _resolve_t_low(),
        "ttl_seconds": _resolve_ttl_seconds(),
        "drift_pct_max": _resolve_drift_pct_max(),
        "max_entries": _resolve_max_entries(),
        "produce_count": _i(redis_keys.REPLAY_PRODUCE_COUNT),
        "consume_count": _i(redis_keys.REPLAY_CONSUME_COUNT),
        "stale_age_count": _i(redis_keys.REPLAY_STALE_AGE_COUNT),
        "stale_drift_count": _i(redis_keys.REPLAY_STALE_DRIFT_COUNT),
        "stale_regime_count": _i(redis_keys.REPLAY_STALE_REGIME_COUNT),
        "stale_pair_count": _i(redis_keys.REPLAY_STALE_PAIR_COUNT),
        "last_produce_ts": _i(redis_keys.REPLAY_LAST_PRODUCE_TS),
        "last_consume_ts": _i(redis_keys.REPLAY_LAST_CONSUME_TS),
    }
