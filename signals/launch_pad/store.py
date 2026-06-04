"""Launch-Pad storage layer (cont. 70).

Single owner of all launch_pad / launch_pad_history I/O so the shadow tracker
(P2), qualify gate (P3) and maintainer loop (P4) never duplicate SQL or Redis
mirror logic.

Source of truth = Postgres table `launch_pad` (N fixed slots, default 10).
Hot mirror = Redis HASH `launchpad:slots` (slot -> JSON) for fast reads by the
engine open-hook (P5) and the dashboard panel (P6).

All functions take an explicit psycopg2 `conn` and/or redis client `r` so the
caller controls transaction scope (the maintainer borrows one connection per
loop iteration via db.db_conn). Nothing here opens real trades.
"""
from __future__ import annotations

import datetime as _dt
import json
from decimal import Decimal
from typing import Any, Optional

import structlog

import redis_keys

log = structlog.get_logger()

# Columns the caller may update on a slot (slot/table_entry_ts are immutable here).
_UPDATABLE = (
    "symbol", "direction", "table_entry_price", "table_entry_ts", "last_mark",
    "shadow_pnl_pct", "peak_profit_pct", "peak_loss_pct",
    "mv_candlenet", "mv_predicted", "mv_realized",
    "qualified", "flips_count", "state", "regime", "ttl_expires_at",
)

_HISTORY_COLS = (
    "symbol", "direction", "table_entry_price", "table_entry_ts", "exit_reason",
    "shadow_pnl_pct", "peak_profit_pct", "peak_loss_pct",
    "mv_candlenet", "mv_predicted", "mv_realized", "regime", "flips_count", "trade_id",
)


def _jsonable(v: Any) -> Any:
    """Make Decimals / datetimes JSON-serialisable for the Redis mirror."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    return v


def depth(r) -> int:
    """Configured buffer depth (number of slots). Default 10."""
    try:
        return int(r.get(redis_keys.LAUNCHPAD_DEPTH) or 10)
    except (TypeError, ValueError):
        return 10


def read_slots(conn) -> list[dict]:
    """All slots ordered by slot id, as plain dicts (numerics -> float)."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM launch_pad ORDER BY slot")
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    for row in rows:
        for k, v in list(row.items()):
            if isinstance(v, Decimal):
                row[k] = float(v)
    return rows


def read_occupied(conn) -> list[dict]:
    """Slots that currently hold a symbol (state != 'empty')."""
    return [s for s in read_slots(conn) if s.get("state") != "empty" and s.get("symbol")]


def occupied_symbols(conn) -> set[str]:
    return {s["symbol"] for s in read_occupied(conn)}


def update_slot(conn, r, slot: int, **fields) -> None:
    """Patch the given columns on a slot, bump updated_at, and refresh the
    Redis mirror. Unknown columns are ignored (defensive)."""
    sets, vals = [], []
    for col, val in fields.items():
        if col not in _UPDATABLE:
            continue
        sets.append(f"{col} = %s")
        vals.append(val)
    if not sets:
        return
    sets.append("updated_at = now()")
    vals.append(slot)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE launch_pad SET {', '.join(sets)} WHERE slot = %s", vals)
    _mirror_slot(conn, r, slot)


def assign_slot(conn, r, slot: int, *, symbol: str, direction: str,
                table_entry_price: float, last_mark: float,
                mv_candlenet: Optional[float] = None,
                mv_predicted: Optional[float] = None,
                mv_realized: Optional[float] = None,
                regime: Optional[str] = None,
                ttl_expires_at: Optional[_dt.datetime] = None,
                state: str = "staged") -> None:
    """Place a fresh candidate into a slot (resets shadow + flips, sets
    table_entry_ts = now)."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE launch_pad SET
                 symbol=%s, direction=%s, table_entry_price=%s, table_entry_ts=now(),
                 last_mark=%s, shadow_pnl_pct=0, peak_profit_pct=0, peak_loss_pct=0,
                 mv_candlenet=%s, mv_predicted=%s, mv_realized=%s,
                 qualified=false, flips_count=0, state=%s, regime=%s,
                 ttl_expires_at=%s, updated_at=now()
               WHERE slot=%s""",
            (symbol, direction, table_entry_price, last_mark,
             mv_candlenet, mv_predicted, mv_realized, state, regime,
             ttl_expires_at, slot),
        )
    _mirror_slot(conn, r, slot)


def clear_slot(conn, r, slot: int, *, exit_reason: Optional[str] = None,
               trade_id: Optional[int] = None) -> None:
    """Free a slot back to 'empty'. If it held a symbol, log a history row
    first (records the 3 movement metrics + final shadow outcome)."""
    occupant = None
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM launch_pad WHERE slot=%s", (slot,))
        row = cur.fetchone()
        if row:
            cols = [c.name for c in cur.description]
            occupant = dict(zip(cols, row))
    if occupant and occupant.get("symbol") and exit_reason:
        insert_history(conn, occupant, exit_reason=exit_reason, trade_id=trade_id)
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE launch_pad SET
                 symbol=NULL, direction=NULL, table_entry_price=NULL,
                 last_mark=NULL, shadow_pnl_pct=0, peak_profit_pct=0, peak_loss_pct=0,
                 mv_candlenet=NULL, mv_predicted=NULL, mv_realized=NULL,
                 qualified=false, flips_count=0, state='empty', regime=NULL,
                 ttl_expires_at=NULL, updated_at=now()
               WHERE slot=%s""",
            (slot,),
        )
    _mirror_slot(conn, r, slot)


def insert_history(conn, occupant: dict, *, exit_reason: str,
                   trade_id: Optional[int] = None) -> None:
    """Append a launch_pad_history row capturing the candidate's lifecycle.
    The mv_* values stored here are AS AT ENTRY (the slot keeps them fixed for
    the occupant's lifetime) so the reliability study (owner Q4) is honest."""
    vals = [
        occupant.get("symbol"), occupant.get("direction"),
        occupant.get("table_entry_price"), occupant.get("table_entry_ts"),
        exit_reason, occupant.get("shadow_pnl_pct"), occupant.get("peak_profit_pct"),
        occupant.get("peak_loss_pct"), occupant.get("mv_candlenet"),
        occupant.get("mv_predicted"), occupant.get("mv_realized"),
        occupant.get("regime"), occupant.get("flips_count"), trade_id,
    ]
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO launch_pad_history ({', '.join(_HISTORY_COLS)}) "
            f"VALUES ({', '.join(['%s'] * len(_HISTORY_COLS))})",
            vals,
        )


def _mirror_slot(conn, r, slot: int) -> None:
    """Refresh one slot in the Redis hot-mirror hash (best-effort)."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM launch_pad WHERE slot=%s", (slot,))
            row = cur.fetchone()
            if not row:
                return
            cols = [c.name for c in cur.description]
            d = {c: _jsonable(v) for c, v in zip(cols, row)}
        r.hset(redis_keys.LAUNCHPAD_SLOTS, str(slot), json.dumps(d))
    except Exception as exc:  # mirror is advisory; never break the DB write
        log.debug("launchpad_mirror_failed", slot=slot, error=str(exc)[:120])


def mirror_all(conn, r) -> None:
    """Rebuild the entire Redis mirror from Postgres (used on startup/repair)."""
    try:
        slots = read_slots(conn)
        pipe = r.pipeline()
        pipe.delete(redis_keys.LAUNCHPAD_SLOTS)
        for s in slots:
            pipe.hset(redis_keys.LAUNCHPAD_SLOTS, str(s["slot"]),
                      json.dumps({k: _jsonable(v) for k, v in s.items()}))
        pipe.execute()
    except Exception as exc:
        log.warning("launchpad_mirror_all_failed", error=str(exc)[:120])
