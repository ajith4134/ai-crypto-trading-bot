"""F50e (cont. 64, 2026-05-29) — Self-supervised continuous candle training.

Self-supervised online training stream for `prediction/online_predictor.py`.
Every closed 1m/5m/15m candle on every active pair becomes one training
sample, independent of trade outcomes. Solves the "train on live data of
symbols we are going to place, not closed-trade data" requirement.

Pipeline (per beat tick, 60s cadence):

  1. ENQUEUE — for each active pair × TF in {1m, 5m, 15m}:
       * Read `{pair}:{tf}:candles[0]` (newest closed candle, JSON
         `{"t","o","h","l","c","v"}` per data/feed.py:_fetch_one_short).
       * Compare candle close ts vs watermark `f50e:last_seen_ts:{pair}:{tf}`.
       * If newer: snapshot live_features(pair) + ref_close, ZADD to
         `f50e:pending:{pair}:{tf}` with score = ready_at = close_ts +
         tf_seconds.  Then advance the watermark.

  2. DRAIN — for each (pair, tf), ZRANGEBYSCORE pending entries whose
     score ≤ now. For each:
       * Look up the candle in `{pair}:{tf}:candles` whose close_ts is the
         FIRST one after ref close_ts.
       * direction_label = 1 iff close > snap.ref_close
       * confidence_label = 1 iff abs(close - ref_close) > 0.5 × ref_atr
       * Call `online_predictor.learn_from_candle_close(features, dir, conf)`.
       * ZREM the entry. Increment counters.

  3. THROTTLE — at most `f50e:throttle_per_tick` (default 200) drain calls
     per beat invocation across all (pair, tf) buckets.

Kill switches:
  * `f50e:disabled = "1"`  → train_tick returns immediately with status "disabled"
  * `f50e:throttle_per_tick`  → tune work per tick (5..2000)

Never raises (caught at the train_tick() boundary).
"""
from __future__ import annotations

import json
import time
from typing import Optional

import structlog

import redis_client
import redis_keys

log = structlog.get_logger()


_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900}
_PENDING_KEY = "f50e:pending:{pair}:{tf}"
_WATERMARK_KEY = "f50e:last_seen_ts:{pair}:{tf}"
_DEFAULT_THROTTLE = 200
_MAX_THROTTLE = 2000


def _is_disabled() -> bool:
    try:
        v = redis_client.get().get("f50e:disabled")
        return v in ("1", b"1")
    except Exception:
        return False


def _throttle() -> int:
    try:
        v = redis_client.get().get("f50e:throttle_per_tick")
        if v is None:
            return _DEFAULT_THROTTLE
        return max(5, min(_MAX_THROTTLE, int(v)))
    except (TypeError, ValueError):
        return _DEFAULT_THROTTLE


def _active_pairs(limit: int = 60) -> list[str]:
    """Pairs to train on. Reuses the scanner active set (same source as the
    refresh loop) so the trainer stays in lockstep with what's tradeable."""
    r = redis_client.get()
    out: list[str] = []
    try:
        active = r.smembers(redis_keys.ACTIVE_PAIRS) or set()
        for p in active:
            sym = p.decode("utf-8") if isinstance(p, bytes) else p
            if sym and sym not in out:
                out.append(sym)
                if len(out) >= limit:
                    return out
    except Exception:
        pass
    return out


def _read_candles(pair: str, tf: str, n: int = 50) -> list[dict]:
    """Return up to `n` candles for (pair, tf), NEWEST first (matches
    data/feed.py LPUSH order). Each entry is the parsed JSON dict
    {"t","o","h","l","c","v"}; bad entries skipped."""
    r = redis_client.get()
    key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", tf)
    try:
        raw = r.lrange(key, 0, max(1, n) - 1) or []
    except Exception:
        return []
    out: list[dict] = []
    for item in raw:
        try:
            if isinstance(item, bytes):
                item = item.decode("utf-8", errors="ignore")
            obj = json.loads(item)
            if not isinstance(obj, dict):
                continue
            t = int(obj.get("t") or 0)
            c = float(obj.get("c") or 0)
            o = float(obj.get("o") or 0)
            if t <= 0 or c <= 0:
                continue
            obj["_t_s"] = t // 1000 if t > 10_000_000_000 else t
            obj["_c"] = c
            obj["_o"] = o
            out.append(obj)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return out


def _watermark(pair: str, tf: str) -> int:
    r = redis_client.get()
    try:
        v = r.get(_WATERMARK_KEY.format(pair=pair, tf=tf))
        if v is None:
            return 0
        if isinstance(v, bytes):
            v = v.decode("utf-8", errors="ignore")
        return int(v)
    except (TypeError, ValueError):
        return 0


def _set_watermark(pair: str, tf: str, ts_s: int) -> None:
    try:
        redis_client.get().set(_WATERMARK_KEY.format(pair=pair, tf=tf),
                                int(ts_s))
    except Exception:
        pass


def _enqueue_new(pair: str, tf: str, candles_newest_first: list[dict]) -> int:
    """For every candle newer than the watermark, snapshot features + ZADD
    a pending training entry. Returns count enqueued."""
    if not candles_newest_first:
        return 0
    r = redis_client.get()
    wm = _watermark(pair, tf)
    tf_s = _TF_SECONDS.get(tf, 60)
    # Process oldest-newer-than-wm first so the watermark advances monotonically.
    new_candles = [c for c in candles_newest_first if c["_t_s"] > wm]
    new_candles.sort(key=lambda c: c["_t_s"])
    if not new_candles:
        return 0

    # Feature snapshot — taken ONCE per tick (cheap; current Redis state ≈
    # state at most-recent candle close). Same dict used for every enqueue
    # this tick because live state isn't bar-aligned at sub-minute precision.
    try:
        from prediction.features import live_features
        feats = live_features(pair)
    except Exception as exc:
        log.debug("f50e_features_snapshot_failed",
                  pair=pair, tf=tf, error=str(exc)[:200])
        try:
            r.incr("f50e:enqueue_error_count")
        except Exception:
            pass
        return 0

    try:
        atr = float(r.get(f"{pair}:atr") or 0.0)
    except (TypeError, ValueError):
        atr = 0.0

    n = 0
    pkey = _PENDING_KEY.format(pair=pair, tf=tf)
    try:
        pipe = r.pipeline()
        max_ts = wm
        for c in new_candles:
            ready_at = c["_t_s"] + tf_s
            payload = json.dumps({
                "snap_ts": c["_t_s"],
                "ref_close": c["_c"],
                "ref_atr": atr,
                "features": feats,
                "pair": pair,
                "tf": tf,
            })
            pipe.zadd(pkey, {payload: ready_at})
            if c["_t_s"] > max_ts:
                max_ts = c["_t_s"]
            n += 1
        # Bound pending set size; keep only the 500 most recent entries.
        pipe.zremrangebyrank(pkey, 0, -501)
        pipe.execute()
        _set_watermark(pair, tf, max_ts)
    except Exception as exc:
        log.warning("f50e_enqueue_failed",
                    pair=pair, tf=tf, error=str(exc)[:200])
        try:
            r.incr("f50e:enqueue_error_count")
        except Exception:
            pass
        return 0
    return n


def _resolve_label(candles_newest_first: list[dict],
                    ref_close_ts: int,
                    ref_close: float,
                    ref_atr: float) -> Optional[tuple[int, int]]:
    """Find the first candle whose close_ts > ref_close_ts and return
    (direction_label, confidence_label). None if no realized candle yet."""
    # Candles are newest-first; the realized candle is the LAST one (oldest
    # walking forward) whose t > ref_close_ts — i.e. the smallest t > ref.
    realized: Optional[dict] = None
    for c in reversed(candles_newest_first):
        if c["_t_s"] > ref_close_ts:
            realized = c
            break
    if realized is None:
        return None
    realized_close = realized["_c"]
    move = realized_close - ref_close
    dir_lbl = 1 if move > 0 else 0
    if ref_atr > 0:
        conf_lbl = 1 if abs(move) > 0.5 * ref_atr else 0
    else:
        # No ATR → fall back to "decisive iff > 25 bps move"
        rel = abs(move) / max(ref_close, 1e-9)
        conf_lbl = 1 if rel > 0.0025 else 0
    return dir_lbl, conf_lbl


def _drain_and_train(pair: str, tf: str, budget: int,
                      candles_newest_first: list[dict]) -> tuple[int, int, int]:
    """Drain pending entries whose ready_at ≤ now. Returns (trained, skipped, errors).
    `budget` is the max number of training steps allowed this call."""
    if budget <= 0:
        return 0, 0, 0
    r = redis_client.get()
    pkey = _PENDING_KEY.format(pair=pair, tf=tf)
    now = int(time.time())
    try:
        members = r.zrangebyscore(pkey, 0, now, start=0, num=budget,
                                    withscores=False) or []
    except Exception as exc:
        log.warning("f50e_zrange_failed",
                    pair=pair, tf=tf, error=str(exc)[:200])
        return 0, 0, 1
    if not members:
        return 0, 0, 0

    from prediction.online_predictor import learn_from_candle_close

    trained = 0
    skipped = 0
    errors = 0
    to_remove: list[bytes | str] = []
    for raw in members:
        try:
            entry = json.loads(raw if isinstance(raw, str)
                                else raw.decode("utf-8"))
            ref_close_ts = int(entry.get("snap_ts") or 0)
            ref_close = float(entry.get("ref_close") or 0)
            ref_atr = float(entry.get("ref_atr") or 0)
            feats = entry.get("features") or {}
            if ref_close_ts <= 0 or ref_close <= 0 or not feats:
                to_remove.append(raw)
                skipped += 1
                continue
            label = _resolve_label(candles_newest_first, ref_close_ts,
                                     ref_close, ref_atr)
            if label is None:
                # Realization not yet available — leave in queue.
                continue
            dir_lbl, conf_lbl = label
            learn_from_candle_close(feats, dir_lbl, conf_lbl)
            to_remove.append(raw)
            trained += 1
        except Exception as exc:
            log.debug("f50e_drain_item_failed",
                      pair=pair, tf=tf, error=str(exc)[:200])
            to_remove.append(raw)
            errors += 1
    if to_remove:
        try:
            r.zrem(pkey, *to_remove)
        except Exception:
            pass
    return trained, skipped, errors


def train_tick() -> dict:
    """One beat invocation. Enqueues new candle observations + drains any
    pending entries that now have realized labels. Returns telemetry dict."""
    if _is_disabled():
        try:
            redis_client.get().incr("f50e:disabled_skip_count")
        except Exception:
            pass
        return {"status": "disabled"}

    r = redis_client.get()
    budget_total = _throttle()
    pairs = _active_pairs()
    if not pairs:
        return {"status": "no_pairs"}

    total_enqueued = 0
    total_trained = 0
    total_skipped = 0
    total_errors = 0

    for pair in pairs:
        for tf in ("1m", "5m", "15m"):
            if budget_total <= 0:
                break
            candles = _read_candles(pair, tf, n=50)
            if not candles:
                continue
            try:
                enq = _enqueue_new(pair, tf, candles)
                total_enqueued += enq
            except Exception as exc:
                log.warning("f50e_enqueue_outer_failed",
                            pair=pair, tf=tf, error=str(exc)[:200])
                total_errors += 1
                continue
            try:
                trained, skipped, errors = _drain_and_train(
                    pair, tf, min(50, budget_total), candles)
            except Exception as exc:
                log.warning("f50e_drain_outer_failed",
                            pair=pair, tf=tf, error=str(exc)[:200])
                trained, skipped, errors = 0, 0, 1
            total_trained += trained
            total_skipped += skipped
            total_errors += errors
            budget_total -= trained
        if budget_total <= 0:
            break

    try:
        r.incrby("f50e:samples_total", total_trained)
        r.set("f50e:last_step_ts", int(time.time()))
        r.set("f50e:last_tick_summary", json.dumps({
            "enqueued": total_enqueued,
            "trained":  total_trained,
            "skipped":  total_skipped,
            "errors":   total_errors,
            "pairs":    len(pairs),
        }))
        if total_errors:
            r.incrby("f50e:train_error_count", total_errors)
    except Exception:
        pass

    log.info("f50e_train_tick",
             pairs=len(pairs),
             enqueued=total_enqueued,
             trained=total_trained,
             skipped=total_skipped,
             errors=total_errors)
    return {
        "status":    "ok",
        "pairs":     len(pairs),
        "enqueued":  total_enqueued,
        "trained":   total_trained,
        "skipped":   total_skipped,
        "errors":    total_errors,
    }
