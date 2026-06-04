"""Pre-Open Candle Gate (PCG) — cont. 65 (2026-05-30).

Hard candle gate: every selected pair must pass live 5m + 15m candle
forecast presence + cascade direction agreement BEFORE the engine emits a
signal. When required forecasts are missing, the signal is HELD for up to
`pcg:max_wait_seconds` (default 30s); each subsequent brain tick re-checks
the forecasts. If they populate within the window the trade executes with
the cascade's direction + cascade_confidence. If the window elapses the
signal is dropped with a "no_forecast_timeout" reason.

This is the production-grade replacement for the implicit OFI fallback the
multi-TF cascade did when forecasts were absent. PCG calls cascade with
strict=True so the cascade refuses to invent a direction, and PCG decides
between "wait for forecasts" vs "drop, too long without."

User mandate (cont. 65, 2026-05-30):
    "for every open trade hold and wait until it give the direction
    long or short and entry price like that and only after getting
    those it trades"

Master switch: `pcg:enabled = "1"`. When off (default), PCG.check returns
("ready", None, 0.0, {...}) and the caller MUST fall through to the legacy
non-strict cascade — so disabled state never forces a guess.

Config (Redis, optional):
    pcg:enabled              master on/off ("1" / "0", default "0")
    pcg:required_tfs         comma-list (default "5m,15m,30m"; 1h optional
                             until candlenet_1h.pth is trained)
    pcg:max_wait_seconds     hold window (default 30, clamped [5, 600])
    pcg:min_cascade_conf     min cascade vote confidence (default 50,
                             clamped [0, 100])

State (Redis):
    signal:pcg_pending       ZSET keyed by pair, score = first-seen epoch.
                             ZADD NX on first wait; ZREM on execute/drop.

Counters (per [[feedback_silent_rejection]] — every path is observable):
    pcg:waiting                          total ticks held
    pcg:waiting:{pair}                   per-pair held ticks
    pcg:ready_first_tick                 signals that found forecasts
                                         on the first PCG check
    pcg:ready_first_tick:{pair}
    pcg:executed_after_wait              signals that waited then fired
    pcg:executed_after_wait:{pair}
    pcg:dropped:no_forecast              signals dropped on timeout
    pcg:dropped:no_forecast:{pair}
    pcg:dropped:cascade_vetoed           cascade said no (4h veto etc.)
    pcg:dropped:low_confidence           cascade confidence below floor
"""
from __future__ import annotations

import time
from typing import Optional

import structlog

import redis_client
from signals.multi_tf_cascade import pick_direction_cascade

log = structlog.get_logger()


_DEFAULTS = {
    "required_tfs":     "5m,15m,30m",
    "max_wait_seconds": 30,
    "min_cascade_conf": 50.0,
}
_PENDING_ZSET = "signal:pcg_pending"


def _decode(v) -> str:
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="ignore")
    return str(v) if v is not None else ""


def _enabled(r) -> bool:
    try:
        return _decode(r.get("pcg:enabled")) == "1"
    except Exception:
        return False


def _read_config(r) -> dict:
    """Read live PCG config. Redis overrides win; defaults apply on miss."""
    cfg = dict(_DEFAULTS)
    try:
        tfs = _decode(r.get("pcg:required_tfs")) or cfg["required_tfs"]
        cfg["required_tfs"] = [t.strip() for t in tfs.split(",") if t.strip()]
    except Exception:
        cfg["required_tfs"] = cfg["required_tfs"].split(",")
    try:
        v = r.get("pcg:max_wait_seconds")
        if v is not None:
            cfg["max_wait_seconds"] = max(5, min(600, int(v)))
    except (TypeError, ValueError):
        pass
    try:
        v = r.get("pcg:min_cascade_conf")
        if v is not None:
            cfg["min_cascade_conf"] = max(0.0, min(100.0, float(v)))
    except (TypeError, ValueError):
        pass
    return cfg


def _missing_forecasts(r, pair: str, required: list[str]) -> list[str]:
    """List of TFs whose `{pair}:{tf}:candle_forecast` key is absent."""
    missing = []
    for tf in required:
        try:
            if not r.exists(f"{pair}:{tf}:candle_forecast"):
                missing.append(tf)
        except Exception:
            # Treat probe failure as missing — we must not falsely claim ready.
            missing.append(tf)
    return missing


def _enqueue_or_get_first_seen(r, pair: str) -> int:
    """Mark pair as pending; return first-seen epoch."""
    now = int(time.time())
    try:
        added = r.zadd(_PENDING_ZSET, {pair: now}, nx=True)
        if added:
            return now
        score = r.zscore(_PENDING_ZSET, pair)
        return int(score) if score else now
    except Exception:
        return now


def _was_pending(r, pair: str) -> bool:
    try:
        return r.zscore(_PENDING_ZSET, pair) is not None
    except Exception:
        return False


def _dequeue(r, pair: str) -> None:
    try:
        r.zrem(_PENDING_ZSET, pair)
    except Exception:
        pass


def _incr(r, *keys: str) -> None:
    for k in keys:
        try:
            r.incr(k)
        except Exception:
            pass


def check(pair: str, ofi: float, regime: str = "unknown",
          tft_bias: float = 0.0) -> tuple[str, Optional[str], float, dict]:
    """Hard candle gate.

    Returns (verdict, direction, confidence, audit):
      "ready" → forecasts present, cascade decided. `direction` set,
                `confidence` is the cascade vote score (0-100).
                Caller should proceed to write_signal + execute.
                NOTE: when PCG is disabled, this still returns "ready"
                BUT with `direction=None`; the caller must fall through
                to the legacy non-strict cascade in that case.
      "wait"  → required forecasts missing, within max_wait window.
                Caller MUST skip this tick (return [] from compute_signal).
                Brain's next decide cycle will call again.
      "drop"  → either max_wait elapsed without forecasts, OR cascade
                vetoed (4h macro veto, all-neutral in strict mode,
                1-1 split, confidence below floor). Caller should drop.
    """
    r = redis_client.get()

    # Master switch: when off, PCG is invisible — caller falls back to
    # legacy cascade. Direction=None signals "PCG didn't decide."
    if not _enabled(r):
        return "ready", None, 0.0, {"status": "pcg_disabled"}

    cfg = _read_config(r)
    missing = _missing_forecasts(r, pair, cfg["required_tfs"])

    if missing:
        first_seen = _enqueue_or_get_first_seen(r, pair)
        elapsed = int(time.time()) - first_seen
        if elapsed >= cfg["max_wait_seconds"]:
            _dequeue(r, pair)
            _incr(r, "pcg:dropped:no_forecast",
                  f"pcg:dropped:no_forecast:{pair}")
            log.warning("pcg_dropped_timeout",
                        pair=pair, missing_tfs=missing,
                        elapsed_seconds=elapsed,
                        max_wait_seconds=cfg["max_wait_seconds"],
                        note="signal dropped — forecasts never arrived in window")
            return "drop", None, 0.0, {
                "status":  "dropped_timeout",
                "missing": missing,
                "elapsed": elapsed,
            }
        _incr(r, "pcg:waiting", f"pcg:waiting:{pair}")
        log.info("pcg_waiting",
                 pair=pair, missing_tfs=missing,
                 elapsed_seconds=elapsed,
                 max_wait_seconds=cfg["max_wait_seconds"])
        return "wait", None, 0.0, {
            "status":  "waiting",
            "missing": missing,
            "elapsed": elapsed,
        }

    # All required forecasts present — strict cascade vote (no OFI fallback).
    # Thread required_tfs through so cascade only rejects on PCG's set
    # (e.g. 5m+15m), not on 1h being missing while 1h is optional.
    direction, conf, cascade_audit = pick_direction_cascade(
        pair, ofi=ofi, regime=regime, tft_bias=tft_bias,
        strict=True, required_tfs=tuple(cfg["required_tfs"]))

    if direction is None:
        _dequeue(r, pair)
        _incr(r, "pcg:dropped:cascade_vetoed")
        log.info("pcg_cascade_rejected", pair=pair,
                 cascade_status=cascade_audit.get("status"),
                 audit=cascade_audit)
        return "drop", None, 0.0, {
            "status":  "cascade_rejected",
            "cascade": cascade_audit,
        }

    if conf < cfg["min_cascade_conf"]:
        _dequeue(r, pair)
        _incr(r, "pcg:dropped:low_confidence")
        log.info("pcg_low_confidence", pair=pair,
                 cascade_confidence=conf,
                 min_required=cfg["min_cascade_conf"])
        return "drop", None, 0.0, {
            "status":     "low_confidence",
            "confidence": conf,
            "min":        cfg["min_cascade_conf"],
        }

    # READY — distinguish first-tick ready vs after-wait ready for observability.
    after_wait = _was_pending(r, pair)
    _dequeue(r, pair)
    if after_wait:
        _incr(r, "pcg:executed_after_wait", f"pcg:executed_after_wait:{pair}")
    else:
        _incr(r, "pcg:ready_first_tick", f"pcg:ready_first_tick:{pair}")
    log.info("pcg_ready",
             pair=pair, direction=direction, confidence=conf,
             after_wait=after_wait)
    return "ready", direction, conf, {
        "status":     "ready",
        "after_wait": after_wait,
        "cascade":    cascade_audit,
    }


def pending_count() -> int:
    """Operator helper: how many pairs are currently waiting."""
    try:
        return int(redis_client.get().zcard(_PENDING_ZSET) or 0)
    except Exception:
        return 0


def pending_pairs() -> list[tuple[str, int]]:
    """Operator helper: list (pair, first_seen_epoch) pairs."""
    try:
        r = redis_client.get()
        items = r.zrange(_PENDING_ZSET, 0, -1, withscores=True)
        return [(_decode(p), int(s)) for p, s in items]
    except Exception:
        return []
