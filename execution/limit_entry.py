"""
Predicted-entry-offset LIMIT entry — cont. 69 (2026-06-02).

Optionally replace the market entry with a LIMIT entry placed at the current
mark adjusted by the model's `predicted_entry_offset_bps` (on the favourable
side: a long fills LOWER, a short fills HIGHER). Waits up to a TTL for the
mark to reach the target; on expiry it either market-fills or skips.

DEFAULT OFF (`entry:limit_entry_enabled` != "1").
Knobs (all Redis-tunable):
  entry:limit_entry_enabled   "1" to enable                       (default off)
  entry:limit_min_bps         min |offset| to bother limiting     (default 5)
  entry:limit_max_bps         clamp on |offset|                   (default 60)
  entry:limit_ttl_s           seconds to wait for the fill        (default 120)
  entry:limit_expire_action   "market" | "skip" on TTL expiry     (default market)

IMPORTANT caveats (see PROGRESS cont.69d):
  * The current predict-all model emits a DEGENERATE constant offset (~-0.12 bps,
    identical across pairs), which is below limit_min_bps → this module is INERT
    (every entry stays market) until the predictor is retrained to emit real
    per-pair offsets. That is intentional: the clamp makes it fail-safe.
  * Limit-below-market on longs in a strong uptrend often won't fill (price runs
    away) → you miss movers. The TTL→market fallback bounds that miss; keep TTL
    short and prefer expire_action="market" in trending regimes.
"""
import json
import time
import uuid
import asyncio

import structlog

import redis_client
import redis_keys

log = structlog.get_logger()

_ENABLED = "entry:limit_entry_enabled"
_MIN_BPS = "entry:limit_min_bps"
_MAX_BPS = "entry:limit_max_bps"
_TTL_S = "entry:limit_ttl_s"
_EXPIRE = "entry:limit_expire_action"
_PFX = "pending_entry:"


def _mark(r, pair: str) -> float:
    try:
        return float(r.get(redis_keys.MARK_PRICE.replace("{pair}", pair)) or 0)
    except Exception:
        return 0.0


def compute_target_entry(r, pair: str, direction: str, mark: float):
    """Return a favourable limit price, or None to fall back to market entry."""
    if (r.get(_ENABLED) or "0") != "1":
        return None
    if not mark or mark <= 0:
        return None
    raw = r.get(f"predictions:{pair}")
    if not raw:
        return None
    try:
        pred = json.loads(raw)
    except Exception:
        return None
    # Only limit when the model AGREES with the trade direction; never place a
    # limit on the wrong side of a disagreeing prediction.
    if str(pred.get("predicted_direction", "")).lower() != direction:
        return None
    try:
        off = abs(float(pred.get("predicted_entry_offset_bps") or 0))
    except (TypeError, ValueError):
        return None
    try:
        lo = float(r.get(_MIN_BPS) or 5)
        hi = float(r.get(_MAX_BPS) or 60)
    except Exception:
        lo, hi = 5.0, 60.0
    if off < lo:          # too small to matter → market entry
        return None
    off = min(off, hi)
    tgt = mark * (1 - off / 1e4) if direction == "long" else mark * (1 + off / 1e4)
    return round(tgt, 10)


def queue_pending_entry(r, open_params: dict, signal_id, target_price: float) -> str:
    try:
        ttl = int(float(r.get(_TTL_S) or 120))
    except Exception:
        ttl = 120
    pid = uuid.uuid4().hex
    rec = {
        "params": open_params,
        "signal_id": str(signal_id) if signal_id is not None else None,
        "target": float(target_price),
        "pair": open_params["pair"],
        "direction": open_params["direction"],
        "expiry": int(time.time()) + ttl,
        "created": int(time.time()),
    }
    # Key TTL = wait window + 60s grace so the loop always sees the expiry.
    r.setex(_PFX + pid, ttl + 60, json.dumps(rec))
    try:
        r.incr("limit_entry:queued_count")
    except Exception:
        pass
    log.info("limit_entry_queued", pair=open_params["pair"],
             direction=open_params["direction"], target=target_price, ttl_s=ttl)
    return pid


def _filled(direction: str, mark: float, target: float) -> bool:
    return ((direction == "long" and mark <= target) or
            (direction == "short" and mark >= target))


def _open_and_finalize(engine, r, rec: dict, entry_price: float) -> str:
    """Open the trade at `entry_price` and replicate the essential post-open
    bookkeeping (qty recompute, TP targets, signal→trade backlink) that the
    immediate-open path in signals/engine.py does."""
    params = dict(rec["params"])
    params["entry_price"] = entry_price
    params["average_entry"] = entry_price
    # Recompute qty at the actual fill price so notional == capital × leverage.
    try:
        cap = float(params.get("capital_usdt") or 0)
        lev = float(params.get("leverage") or 1)
        if entry_price > 0 and cap > 0:
            params["quantity"] = round((cap * lev) / entry_price, 8)
    except Exception:
        pass

    trade_id = engine.open_trade(params)

    # F48 TP1/TP2 (mirror of signals/engine.py post-open).
    try:
        from risk.manager import compute_tp_targets
        tps = compute_tp_targets(params["pair"], params["direction"],
                                 entry_price=entry_price,
                                 leverage=params.get("leverage", 5))
        if tps:
            r.set(f"trade:{trade_id}:tp1", tps["tp1"])
            r.set(f"trade:{trade_id}:tp2", tps["tp2"])
            r.set(f"trade:{trade_id}:tp", tps.get("tp", tps["tp1"]))
    except Exception as exc:
        log.debug("limit_entry_tp_skipped", trade_id=trade_id, error=str(exc)[:120])

    # cont. 65 signal→trade backlink (training JOINs depend on it).
    try:
        if rec.get("signal_id"):
            from db import db_conn
            with db_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE signals SET trade_id = %s WHERE id = %s",
                                (trade_id, rec["signal_id"]))
    except Exception as exc:
        log.debug("limit_entry_backlink_skipped", error=str(exc)[:120])
    return trade_id


async def process_pending_entries(engine) -> None:
    """Async loop (gathered in main.py): fill or expire pending limit entries."""
    r = redis_client.get()
    log.info("pending_entry_loop_started")
    while True:
        try:
            for key in list(r.scan_iter(match=_PFX + "*", count=200)):
                raw = r.get(key)
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except Exception:
                    r.delete(key)
                    continue
                pair = rec["pair"]
                direction = rec["direction"]
                target = float(rec["target"])
                mark = _mark(r, pair)
                now = int(time.time())

                if mark > 0 and _filled(direction, mark, target):
                    try:
                        _open_and_finalize(engine, r, rec, target)
                        r.incr("limit_entry:filled_count")
                        log.info("limit_entry_filled", pair=pair,
                                 target=target, mark=mark)
                    except Exception as exc:
                        log.warning("limit_entry_fill_failed", pair=pair,
                                    error=str(exc)[:150])
                    r.delete(key)
                elif now >= int(rec.get("expiry", 0)):
                    action = (r.get(_EXPIRE) or "market")
                    if action == "market" and mark > 0:
                        try:
                            _open_and_finalize(engine, r, rec, mark)
                            r.incr("limit_entry:expired_market_count")
                            log.info("limit_entry_expired_market", pair=pair, mark=mark)
                        except Exception as exc:
                            log.warning("limit_entry_expire_open_failed",
                                        pair=pair, error=str(exc)[:150])
                    else:
                        r.incr("limit_entry:expired_skip_count")
                        log.info("limit_entry_expired_skip", pair=pair)
                    r.delete(key)
        except Exception as exc:
            log.warning("pending_entry_loop_error", error=str(exc)[:150])
        await asyncio.sleep(1.0)
