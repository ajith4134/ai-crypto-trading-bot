"""Launch-Pad P4 — maintainer loop (cont. 70).

Runs on a short cadence (celery beat → predict_all queue → candlenet worker).
Keeps the 10-deep buffer full of the best pre-qualified candidates and applies
the owner-locked rules:

  • Shadow refresh  — update MAE/MFE for every occupied slot (P2).
  • Flip rule (D8)  — flip a staged candidate's direction ONLY when its MAE has
                      exceeded launchpad:flip_mae_pct AND CandleNet + the last
                      closed candle now confirm the OPPOSITE direction. Capped at
                      launchpad:flip_cap flips; past the cap the symbol → cooldown.
  • Expiry          — stale, never-qualified candidates (ttl) → cooldown.
  • Fill            — empty slots filled from scanner:active_pairs, qualified
                      candidates first, then best-by-movement (so the buffer is
                      always full and we keep watching the rest).
  • Displace        — an outside candidate whose movement beats the weakest
                      NON-qualified occupant by launchpad:displace_margin takes
                      its slot.

This loop NEVER opens real trades — that is P5 in signals/engine.py, gated by
launchpad:enabled (default 0). The maintainer runs whenever
launchpad:maintainer_enabled != "0" (default ON) so shadow data accumulates with
zero trading impact until the owner flips the system live.
"""
from __future__ import annotations

import datetime as _dt
import time

import structlog

import redis_client
import redis_keys
from . import qualify, shadow, store

log = structlog.get_logger()

_DEFAULT_TTL_S = 1800
_DEFAULT_COOLDOWN_S = 900
_DEFAULT_FLIP_MAE = 1.5
_DEFAULT_FLIP_CAP = 2
_DEFAULT_DISPLACE_MARGIN = 0.10
_DEFAULT_RANK_METRIC = "mv_realized"


# ── config helpers ───────────────────────────────────────────────────────────

def _maintainer_enabled(r) -> bool:
    return (r.get("launchpad:maintainer_enabled") or "1") != "0"


def _int(r, key, default) -> int:
    try:
        return int(r.get(key) or default)
    except (TypeError, ValueError):
        return default


def _float(r, key, default) -> float:
    try:
        return float(r.get(key) or default)
    except (TypeError, ValueError):
        return default


def _rank_metric(r) -> str:
    m = r.get(redis_keys.LAUNCHPAD_RANK_METRIC) or _DEFAULT_RANK_METRIC
    return m if m in ("mv_candlenet", "mv_predicted", "mv_realized") else _DEFAULT_RANK_METRIC


def _score(d: dict, metric: str) -> float:
    """Absolute movement score on the configured metric; missing → -1 (worst)."""
    v = d.get(metric)
    try:
        return abs(float(v)) if v is not None else -1.0
    except (TypeError, ValueError):
        return -1.0


# ── cooldown set ─────────────────────────────────────────────────────────────

def _cooldown_active(r) -> set[str]:
    now = time.time()
    try:
        r.zremrangebyscore(redis_keys.LAUNCHPAD_COOLDOWN, 0, now)
        return set(r.zrangebyscore(redis_keys.LAUNCHPAD_COOLDOWN, now, "+inf"))
    except Exception:
        return set()


def _add_cooldown(r, symbol: str) -> None:
    try:
        secs = _int(r, redis_keys.LAUNCHPAD_COOLDOWN_SECONDS, _DEFAULT_COOLDOWN_S)
        r.zadd(redis_keys.LAUNCHPAD_COOLDOWN, {symbol: time.time() + secs})
    except Exception:
        pass


def _universe(r) -> list[str]:
    try:
        return sorted(r.smembers(redis_keys.ACTIVE_PAIRS) or set())
    except Exception:
        return []


def _ttl_at(r) -> _dt.datetime:
    secs = _int(r, redis_keys.LAUNCHPAD_TTL_SECONDS, _DEFAULT_TTL_S)
    return _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=secs)


# ── per-occupied-slot processing: flip / expiry / re-qualify ─────────────────

def _process_occupied(conn, r, slot: dict) -> None:
    symbol = slot["symbol"]
    direction = slot["direction"]
    flip_mae = _float(r, redis_keys.LAUNCHPAD_FLIP_MAE_PCT, _DEFAULT_FLIP_MAE)
    flip_cap = _int(r, redis_keys.LAUNCHPAD_FLIP_CAP, _DEFAULT_FLIP_CAP)

    mm = qualify.movement_metrics(r, symbol)
    mae = float(slot.get("peak_loss_pct") or 0.0)   # <= 0

    # --- D8 flip rule: sustained adverse AND momentum reversal confirmed ---
    if -mae >= flip_mae:
        opposite = "short" if direction == "long" else "long"
        consensus = qualify.decide_direction(mm["forecasts"])
        reversal = (consensus == opposite and
                    qualify._candle_close_confirms(r, symbol, opposite))
        if reversal:
            if slot.get("flips_count", 0) + 1 > flip_cap:
                # exhausted the flip budget → drop to cooldown
                store.clear_slot(conn, r, slot["slot"], exit_reason="flipped")
                _add_cooldown(r, symbol)
                return
            mark = shadow.mark_price(r, symbol)
            if mark is not None:
                store.update_slot(
                    conn, r, slot["slot"],
                    direction=opposite, table_entry_price=mark,
                    table_entry_ts=_dt.datetime.now(_dt.timezone.utc),
                    last_mark=mark, shadow_pnl_pct=0, peak_profit_pct=0,
                    peak_loss_pct=0, flips_count=slot.get("flips_count", 0) + 1,
                    state="flipped", qualified=False, ttl_expires_at=_ttl_at(r),
                    mv_candlenet=mm["mv_candlenet"], mv_predicted=mm["mv_predicted"],
                    mv_realized=mm["mv_realized"],
                )
                try:
                    r.incr(redis_keys.LAUNCHPAD_FLIP_COUNT)
                except Exception:
                    pass
                log.info("launchpad_flip", symbol=symbol, from_dir=direction,
                         to_dir=opposite, mae=round(mae, 3),
                         flips=slot.get("flips_count", 0) + 1)
                return

    # --- expiry: stale + never confirmed green → cooldown ---
    ttl = slot.get("ttl_expires_at")
    now = _dt.datetime.now(_dt.timezone.utc)
    if ttl is not None and not slot.get("qualified"):
        ttl_aware = ttl if ttl.tzinfo else ttl.replace(tzinfo=_dt.timezone.utc)
        if ttl_aware < now:
            store.clear_slot(conn, r, slot["slot"], exit_reason="expired")
            _add_cooldown(r, symbol)
            return

    # --- re-qualify (refresh the open-green flag + movement columns) ---
    ok, _reason = qualify.qualify(r, symbol, direction)
    store.update_slot(
        conn, r, slot["slot"], qualified=ok,
        state=("confirmed_green" if ok else "staged"),
        mv_candlenet=mm["mv_candlenet"], mv_predicted=mm["mv_predicted"],
        mv_realized=mm["mv_realized"],
    )


# ── outside-candidate evaluation ─────────────────────────────────────────────

def _eval_candidates(r, in_buffer: set[str], cooldown: set[str],
                     metric: str) -> list[dict]:
    """Evaluate the outside universe; return [{symbol, direction, qualified,
    mv_*, score}] sorted best-first (qualified, then movement)."""
    out = []
    for sym in _universe(r):
        if sym in in_buffer or sym in cooldown:
            continue
        if shadow.mark_price(r, sym) is None:   # need a price to shadow-track
            continue
        ev = qualify.evaluate(r, sym)
        if ev["direction"] is None:
            continue
        ev["symbol"] = sym
        ev["score"] = _score(ev, metric)
        out.append(ev)
    out.sort(key=lambda e: (e["qualified"], e["score"]), reverse=True)
    return out


# ── main loop ────────────────────────────────────────────────────────────────

def run() -> dict:
    """One maintainer iteration. Returns a small status dict for telemetry."""
    r = redis_client.get()
    if not _maintainer_enabled(r):
        return {"status": "disabled"}

    from db import db_conn
    metric = _rank_metric(r)
    flips0 = int(r.get(redis_keys.LAUNCHPAD_FLIP_COUNT) or 0)
    filled = displaced = 0

    with db_conn() as conn:
        shadow.refresh(conn, r)

        for slot in store.read_occupied(conn):
            try:
                _process_occupied(conn, r, slot)
            except Exception as exc:
                log.debug("launchpad_process_occupied_failed",
                          slot=slot.get("slot"), error=str(exc)[:160])

        # reload after mutations
        slots = store.read_slots(conn)
        empties = [s for s in slots if s.get("state") == "empty"]
        occupied = [s for s in slots if s.get("state") != "empty" and s.get("symbol")]
        in_buffer = {s["symbol"] for s in occupied}
        cooldown = _cooldown_active(r)

        candidates = _eval_candidates(r, in_buffer, cooldown, metric)

        # fill empties (best-first)
        ci = 0
        for slot in empties:
            if ci >= len(candidates):
                break
            ev = candidates[ci]; ci += 1
            mark = shadow.mark_price(r, ev["symbol"])
            if mark is None:
                continue
            store.assign_slot(
                conn, r, slot["slot"], symbol=ev["symbol"], direction=ev["direction"],
                table_entry_price=mark, last_mark=mark,
                mv_candlenet=ev["mv_candlenet"], mv_predicted=ev["mv_predicted"],
                mv_realized=ev["mv_realized"],
                regime=(r.get(redis_keys.CURRENT_REGIME) or None),
                ttl_expires_at=_ttl_at(r),
                state=("confirmed_green" if ev["qualified"] else "staged"),
            )
            in_buffer.add(ev["symbol"]); filled += 1

        # displacement: remaining candidates vs weakest NON-qualified occupants
        margin = _float(r, redis_keys.LAUNCHPAD_DISPLACE_MARGIN, _DEFAULT_DISPLACE_MARGIN)
        remaining = candidates[ci:]
        occ = [s for s in store.read_occupied(conn) if not s.get("qualified")]
        for ev in remaining:
            if not occ:
                break
            weakest = min(occ, key=lambda s: _score(s, metric))
            if ev["score"] > _score(weakest, metric) * (1.0 + margin):
                mark = shadow.mark_price(r, ev["symbol"])
                if mark is None:
                    continue
                store.clear_slot(conn, r, weakest["slot"], exit_reason="displaced")
                store.assign_slot(
                    conn, r, weakest["slot"], symbol=ev["symbol"],
                    direction=ev["direction"], table_entry_price=mark, last_mark=mark,
                    mv_candlenet=ev["mv_candlenet"], mv_predicted=ev["mv_predicted"],
                    mv_realized=ev["mv_realized"],
                    regime=(r.get(redis_keys.CURRENT_REGIME) or None),
                    ttl_expires_at=_ttl_at(r),
                    state=("confirmed_green" if ev["qualified"] else "staged"),
                )
                occ.remove(weakest); displaced += 1
            else:
                break   # candidates are sorted; none after this beats the weakest

    # telemetry
    try:
        r.incr(redis_keys.LAUNCHPAD_MAINTAIN_COUNT)
        if filled:
            r.incrby(redis_keys.LAUNCHPAD_REFILL_COUNT, filled)
        if displaced:
            r.incrby(redis_keys.LAUNCHPAD_DISPLACE_COUNT, displaced)
        r.set(redis_keys.LAUNCHPAD_LAST_RUN_TS, str(int(time.time())))
    except Exception:
        pass

    flips = int(r.get(redis_keys.LAUNCHPAD_FLIP_COUNT) or 0) - flips0
    return {"status": "ok", "filled": filled, "displaced": displaced,
            "flips": flips, "metric": metric}
