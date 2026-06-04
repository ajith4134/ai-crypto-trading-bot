"""Launch-Pad P2 — shadow MAE/MFE tracker (cont. 70).

Every buffered symbol is "shadow-traded" from the price at which it ENTERED the
slot (`table_entry_price`). No real orders — this is a signal-quality measure.

Per slot, each tick we update:
  shadow_pnl_pct  = direction-adjusted % move from table_entry_price
  peak_profit_pct = running max favourable excursion (MFE, >= 0)
  peak_loss_pct   = running max adverse excursion (MAE, <= 0)  ← drives the D8 flip rule

PnL is expressed as a raw PRICE move % (not leveraged): the buffer is a
direction-quality gauge, not a position. A long that drifts to peak_loss_pct
below -launchpad:flip_mae_pct is a flip candidate (handled in P4 maintainer).
"""
from __future__ import annotations

import structlog

import redis_keys
from . import store

log = structlog.get_logger()


def direction_sign(direction: str | None) -> int:
    return 1 if direction == "long" else -1 if direction == "short" else 0


def mark_price(r, symbol: str) -> float | None:
    """Latest mark price for a symbol, or None if unavailable/invalid."""
    try:
        raw = r.get(redis_keys.MARK_PRICE.replace("{pair}", symbol))
        if raw is None:
            return None
        px = float(raw)
        return px if px > 0 else None
    except (TypeError, ValueError):
        return None


def shadow_pnl_pct(entry: float, mark: float, direction: str | None) -> float | None:
    """Direction-adjusted % move from table-entry price. None if inputs invalid."""
    sign = direction_sign(direction)
    if sign == 0 or not entry or entry <= 0 or not mark or mark <= 0:
        return None
    return sign * (mark - entry) / entry * 100.0


def update_fields(slot: dict, mark: float) -> dict | None:
    """Pure helper: given a slot row + current mark, return the new shadow
    fields (last_mark, shadow_pnl_pct, peak_profit_pct, peak_loss_pct), or None
    if the slot can't be evaluated."""
    pnl = shadow_pnl_pct(
        float(slot.get("table_entry_price") or 0),
        mark,
        slot.get("direction"),
    )
    if pnl is None:
        return None
    prev_peak_profit = float(slot.get("peak_profit_pct") or 0.0)
    prev_peak_loss = float(slot.get("peak_loss_pct") or 0.0)
    return {
        "last_mark": mark,
        "shadow_pnl_pct": round(pnl, 6),
        "peak_profit_pct": round(max(prev_peak_profit, pnl, 0.0), 6),
        "peak_loss_pct": round(min(prev_peak_loss, pnl, 0.0), 6),
    }


def refresh(conn, r) -> int:
    """Update shadow PnL/MFE/MAE for every occupied slot. Returns the number of
    slots updated. Never raises — a single bad slot is skipped, not fatal."""
    updated = 0
    for slot in store.read_occupied(conn):
        try:
            mark = mark_price(r, slot["symbol"])
            if mark is None:
                continue
            fields = update_fields(slot, mark)
            if fields is None:
                continue
            store.update_slot(conn, r, slot["slot"], **fields)
            updated += 1
        except Exception as exc:
            log.debug("launchpad_shadow_slot_failed",
                      slot=slot.get("slot"), symbol=slot.get("symbol"),
                      error=str(exc)[:120])
    return updated
