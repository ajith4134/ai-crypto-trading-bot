"""Launch-Pad P5 — engine open-hook (cont. 70).

When `launchpad:enabled` == "1" the launch-pad becomes the SOLE funnel for opens
(owner decision D1): the engine opens ONLY from buffer slots that are qualified
"green", in movement order, up to `bot:max_open_trades`. If too few qualify it
WAITS (no forcing). On a successful open the slot is retired (fired → cooldown)
and the P4 maintainer refills it on its next tick.

This module is the thin, hot-path-safe surface the engine calls. It reads the
Redis hot-mirror (`launchpad:slots`) for funnel selection — no DB on the read
path — and only touches Postgres on the open-confirmation write (best-effort,
never breaks the open). Every skip path emits a silent-rejection counter.

Kill switch: `launchpad:enabled` (default "0"). When off, funnel_pairs() returns
None and the engine keeps its legacy 160-pair flow verbatim.
"""
from __future__ import annotations

import json
import time

import structlog

import redis_keys

log = structlog.get_logger()

_VALID_METRICS = ("mv_candlenet", "mv_predicted", "mv_realized")
_DEFAULT_RANK_METRIC = "mv_realized"
_DEFAULT_COOLDOWN_S = 900


def enabled(r) -> bool:
    """True when the launch-pad is the live funnel for opens."""
    return r.get(redis_keys.LAUNCHPAD_ENABLED) == "1"


def _rank_metric(r) -> str:
    m = r.get(redis_keys.LAUNCHPAD_RANK_METRIC) or _DEFAULT_RANK_METRIC
    return m if m in _VALID_METRICS else _DEFAULT_RANK_METRIC


def _abs_score(d: dict, metric: str) -> float:
    v = d.get(metric)
    try:
        return abs(float(v)) if v is not None else -1.0
    except (TypeError, ValueError):
        return -1.0


def _read_mirror(r) -> list[dict]:
    """All slot occupants from the Redis hot-mirror hash. Best-effort."""
    out = []
    try:
        raw = r.hgetall(redis_keys.LAUNCHPAD_SLOTS) or {}
    except Exception:
        return out
    for v in raw.values():
        try:
            out.append(json.loads(v))
        except Exception:
            continue
    return out


def funnel_pairs(r) -> tuple[list[str], dict[str, str], dict[str, int]] | None:
    """When the funnel is live, return:
        (ordered_pairs, dir_by_symbol, slot_by_symbol)
    where ordered_pairs are the qualified-green buffer symbols sorted best-first
    by the configured movement metric. Returns None when the kill switch is off
    (engine then keeps its legacy flow). Returns ([], {}, {}) when the funnel is
    on but nothing currently qualifies (engine WAITS — no opens this cycle).
    """
    if not enabled(r):
        return None

    metric = _rank_metric(r)
    qualified = []
    for s in _read_mirror(r):
        sym = s.get("symbol")
        direction = s.get("direction")
        if not sym or direction not in ("long", "short"):
            continue
        # "green" = the maintainer's qualify gate confirmed it favourable now.
        if not s.get("qualified"):
            continue
        qualified.append(s)

    qualified.sort(key=lambda s: _abs_score(s, metric), reverse=True)

    pairs = [s["symbol"] for s in qualified]
    dir_by_symbol = {s["symbol"]: s["direction"] for s in qualified}
    slot_by_symbol = {s["symbol"]: int(s["slot"]) for s in qualified}
    return pairs, dir_by_symbol, slot_by_symbol


def direction_ok(dir_by_symbol: dict[str, str], pair: str, signal_dir: str) -> bool:
    """The launch-pad owns direction: a signal may only fire in the slot's
    staged direction. Unknown pair (shouldn't happen — pairs came from the
    buffer) is rejected closed."""
    want = dir_by_symbol.get(pair)
    return want is not None and want == signal_dir


def on_open(r, pair: str, slot: int, trade_id) -> None:
    """A trade just opened off a buffer slot: retire the slot (fired → history),
    cooldown the symbol so it isn't immediately re-staged, and bump the open
    counter. The P4 maintainer refills the freed slot on its next tick.
    Best-effort: a failure here must never unwind an already-open trade.
    """
    try:
        from db import db_conn
        from . import store
        with db_conn() as conn:
            store.clear_slot(conn, r, slot, exit_reason="fired", trade_id=trade_id)
    except Exception as exc:
        log.warning("launchpad_on_open_clear_failed",
                    pair=pair, slot=slot, error=str(exc)[:160])
    try:
        secs = int(r.get(redis_keys.LAUNCHPAD_COOLDOWN_SECONDS) or _DEFAULT_COOLDOWN_S)
        r.zadd(redis_keys.LAUNCHPAD_COOLDOWN, {pair: time.time() + secs})
        r.incr(redis_keys.LAUNCHPAD_OPEN_COUNT)
        log.info("launchpad_open", pair=pair, slot=slot, trade_id=trade_id)
    except Exception:
        pass
