"""cont. 70e — Binance→DB reconciler. The exchange is the SOURCE OF TRUTH.

Runs every `interval_s` (live mode only), launched as a background task from
`risk.manager.monitor_trailing_sl`:

  1. GHOSTS — any is_paper=false trade still OPEN in the DB whose Binance
     positionAmt is 0 closed on the exchange (e.g. the exchange-native stop
     fired and the bot's reduce-only close was rejected) but was never recorded.
     Close the DB row with the REAL realized PnL + commission + funding pulled
     from futures_income_history. This is the only mutation the reconciler makes.

  2. DRIFT — compares Binance net (realized+commission+funding) over the last
     24h against the DB net of is_paper=false closed trades in the same window,
     and logs a warning + stores the numbers in Redis when they diverge. This
     surfaces trades that executed on Binance but were never written to the DB
     (e.g. opens that placed an order then failed the DB write). DRIFT never
     mutates — report only; we do NOT fabricate entry data for unknown trades.

Toggles (Redis):
  reconciler:enabled              "0" disables the whole loop (default on)
  reconciler:grace_s              skip trades opened < N s ago (default 90)
  reconciler:drift_check_enabled  "0" disables part 2 (default on)
  reconciler:drift_alert_usdt     drift warning threshold (default 1.0)
Counters/state: reconciler:ghosts_closed_count, reconciler:{binance,db}_net_24h,
  reconciler:drift_24h, reconciler:last_run_ts.
"""
import asyncio
import time
from datetime import datetime, timezone

import structlog

import redis_client
from db import db_conn
from memory.write import write_trade_close

log = structlog.get_logger()


def _income_by_type(client, pair: str, start_ms: int):
    """Sum REALIZED_PNL / COMMISSION / FUNDING_FEE (USDT) for a symbol since
    start_ms. COMMISSION income is returned NEGATIVE by Binance."""
    realized = commission = funding = 0.0
    try:
        inc = client._client.futures_income_history(
            symbol=pair, startTime=int(start_ms), limit=500)
        for i in inc:
            t = i.get("incomeType")
            a = float(i.get("income") or 0)
            if t == "REALIZED_PNL":
                realized += a
            elif t == "COMMISSION":
                commission += a
            elif t == "FUNDING_FEE":
                funding += a
    except Exception as exc:
        log.debug("reconciler_income_failed", pair=pair, error=str(exc)[:120])
    return realized, commission, funding


def _close_ghost(client, r, row) -> None:
    tid, pair, direction, avg_entry, entry_price, qty, t0_ms = row
    entry = float(avg_entry or entry_price or 0)
    qty = float(qty or 0)
    start_ms = int(t0_ms) if t0_ms else int((time.time() - 24 * 3600) * 1000)

    realized, commission, funding = _income_by_type(client, pair, start_ms)
    fees = abs(commission)
    net = realized + commission + funding   # commission already negative

    # Derive an exit price consistent with the realized PnL (display only —
    # the realized/fees/net above are the authoritative figures).
    exit_price = 0.0
    if qty > 0 and entry > 0:
        sign = 1.0 if direction == "long" else -1.0
        exit_price = entry + sign * realized / qty
    if exit_price <= 0:
        exit_price = entry

    hold_s = int(time.time() - (start_ms / 1000.0)) if t0_ms else 0
    write_trade_close(tid, {
        "exit_price": round(exit_price, 8),
        "exit_time": datetime.now(timezone.utc),
        "exit_reason": "reconciled_exchange",
        "hold_time_seconds": max(hold_s, 0),
        "final_pnl_usdt": round(realized, 4),
        "fees_usdt": round(fees, 4),
        "net_pnl_usdt": round(net, 4),
    })
    try:
        r.incr("reconciler:ghosts_closed_count")
        r.delete(f"trade:{tid}:closing", f"trade:{tid}:sl_order_id",
                 f"trade:{tid}:entry_fee_usdt")
    except Exception:
        pass
    log.warning("reconciler_ghost_closed", trade_id=tid, pair=pair,
                direction=direction, realized=round(realized, 4),
                fees=round(fees, 4), net=round(net, 4))


def _check_drift(client, r) -> None:
    """Account-level DB↔Binance net drift over the last 24h (report only)."""
    if (r.get("reconciler:drift_check_enabled") or "1") == "0":
        return
    start_ms = int((time.time() - 24 * 3600) * 1000)
    try:
        inc = client._client.futures_income_history(startTime=start_ms, limit=1000)
    except Exception as exc:
        log.debug("reconciler_drift_income_failed", error=str(exc)[:120])
        return
    binance_net = sum(
        float(i.get("income") or 0) for i in inc
        if i.get("incomeType") in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"))
    try:
        with db_conn() as cx:
            cur = cx.cursor()
            cur.execute(
                "SELECT COALESCE(SUM(net_pnl_usdt),0) FROM trades "
                "WHERE is_paper=false AND status='closed' "
                "AND exit_time > now() - interval '24 hours'")
            db_net = float(cur.fetchone()[0] or 0)
    except Exception as exc:
        log.debug("reconciler_drift_db_failed", error=str(exc)[:120])
        return

    drift = db_net - binance_net
    try:
        r.set("reconciler:binance_net_24h", round(binance_net, 4))
        r.set("reconciler:db_net_24h", round(db_net, 4))
        r.set("reconciler:drift_24h", round(drift, 4))
    except Exception:
        pass
    try:
        thresh = float(r.get("reconciler:drift_alert_usdt") or 1.0)
    except (TypeError, ValueError):
        thresh = 1.0
    if abs(drift) > thresh:
        log.warning("reconciler_drift_detected",
                    db_net=round(db_net, 4), binance_net=round(binance_net, 4),
                    drift=round(drift, 4),
                    note="DB closed-trade net diverges from Binance — likely "
                         "live trades executed on Binance but missing from DB")


def reconcile_once(client) -> None:
    r = redis_client.get()
    if (r.get("reconciler:enabled") or "1") == "0":
        return
    try:
        grace_s = float(r.get("reconciler:grace_s") or 90)
    except (TypeError, ValueError):
        grace_s = 90.0

    # Snapshot Binance positions once (one weighted call).
    try:
        positions = {p["symbol"]: float(p.get("positionAmt") or 0)
                     for p in client._client.futures_position_information()}
    except Exception as exc:
        log.error("reconciler_positions_failed", error=str(exc)[:160])
        return

    try:
        with db_conn() as cx:
            cur = cx.cursor()
            cur.execute(
                "SELECT id,pair,direction,average_entry,entry_price,quantity,"
                "EXTRACT(EPOCH FROM entry_time)*1000 "
                "FROM trades WHERE status='open' AND is_paper=false")
            open_rows = cur.fetchall()
    except Exception as exc:
        log.error("reconciler_open_query_failed", error=str(exc)[:160])
        return

    now_ms = time.time() * 1000
    ghosts = 0
    for row in open_rows:
        tid, pair, direction, avg_entry, entry_price, qty, t0_ms = row
        if abs(positions.get(pair, 0.0)) > 0:
            continue   # still open on Binance — healthy
        if t0_ms and (now_ms - float(t0_ms)) < grace_s * 1000:
            continue   # too fresh — don't race a just-placed order
        try:
            _close_ghost(client, r, row)
            ghosts += 1
        except Exception as exc:
            log.error("reconciler_ghost_close_failed", trade_id=str(tid),
                      pair=pair, error=str(exc)[:160])

    try:
        r.set("reconciler:last_run_ts", int(time.time()))
    except Exception:
        pass
    if ghosts:
        log.info("reconciler_pass", ghosts_closed=ghosts,
                 open_checked=len(open_rows))

    _check_drift(client, r)


async def reconcile_loop(engine, interval_s: int = 120) -> None:
    """Background loop. No-op in paper mode (engine has no Binance client)."""
    client = getattr(engine, "_client", None)
    if client is None:
        log.info("reconciler_disabled_paper_mode")
        return
    log.info("reconciler_started", interval_s=interval_s)
    while True:
        try:
            reconcile_once(client)
        except Exception as exc:
            log.error("reconciler_error", error=str(exc)[:200])
        await asyncio.sleep(interval_s)
