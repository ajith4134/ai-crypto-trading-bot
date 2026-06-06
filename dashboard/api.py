"""
Section AH: FastAPI dashboard backend — AH-01 to AH-06.
"""
import asyncio
import json
from datetime import datetime, timezone
from typing import Optional
import structlog
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext
from slowapi import Limiter
from slowapi.util import get_remote_address
from pydantic import BaseModel

log = structlog.get_logger()

app = FastAPI(title="Trading Bot Dashboard", version="1.0.0", redirect_slashes=False)


async def _fapi_probe_loop():
    """Background task: probe fapi.binance.com/fapi/v1/ping every 30s.

    Writes Redis keys:
      fapi:ban_status       — "ok" | "banned"
      fapi:last_check_ts    — epoch of last probe
      fapi:first_banned_ts  — epoch when ban was first detected (not overwritten)
      fapi:last_ok_ts       — epoch of last successful probe
    Sends a Telegram alert when the ban lifts.
    """
    import time as _t
    import redis_client as _rc
    import requests as _requests

    def _probe() -> int:
        try:
            resp = _requests.get("https://fapi.binance.com/fapi/v1/ping", timeout=5)
            return resp.status_code
        except Exception:
            return 0

    loop = asyncio.get_event_loop()
    while True:
        try:
            r = _rc.get()
            now = int(_t.time())
            status_code = await loop.run_in_executor(None, _probe)

            if status_code == 200:
                was_banned = r.get("fapi:ban_status") == "banned"
                r.set("fapi:ban_status", "ok")
                r.set("fapi:last_ok_ts", now)
                r.set("fapi:last_check_ts", now)
                if was_banned:
                    log.warning("fapi_ban_lifted", recovered_at=now)
                    try:
                        from notifications.telegram import send_critical
                        send_critical(
                            "fapi.binance.com RECOVERED ✅\n"
                            "Live trading is now reachable from this IP.\n"
                            "Go to dashboard → Switch to LIVE Trading."
                        )
                    except Exception:
                        pass
            else:
                r.set("fapi:ban_status", "banned")
                r.set("fapi:last_check_ts", now)
                if not r.get("fapi:first_banned_ts"):
                    r.set("fapi:first_banned_ts", now)
                    log.warning("fapi_ban_detected", http_status=status_code, ts=now)
        except Exception as exc:
            log.warning("fapi_probe_error", error=str(exc)[:100])
        await asyncio.sleep(30)


@app.on_event("startup")
async def startup():
    """Initialize DB pool and Redis on dashboard startup."""
    import db
    import redis_client
    db.init_pool()
    redis_client.init()
    log.info("dashboard_startup_complete")
    asyncio.create_task(_fapi_probe_loop())

# CORS — restrict to VPS domain only (AP-19)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Replace with actual domain in production
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)

_SECRET = "change-me-strong-jwt-secret-32-chars-min"
_ALGORITHM = "HS256"
_TOKEN_EXPIRE_HOURS = 24
_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_bearer = HTTPBearer()
_limiter = Limiter(key_func=get_remote_address)

_connected_ws: list[WebSocket] = []


# --- Auth helpers ---

def _make_token(username: str) -> str:
    import time
    payload = {"sub": username, "exp": int(time.time()) + _TOKEN_EXPIRE_HOURS * 3600}
    return jwt.encode(payload, _SECRET, algorithm=_ALGORITHM)


def _verify_token(creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> str:
    try:
        payload = jwt.decode(creds.credentials, _SECRET, algorithms=[_ALGORITHM])
        return payload["sub"]
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


# --- AH-03: Health endpoint (no auth, used by watchdog) ---

@app.get("/health")
async def health():
    from db import db_conn
    import redis_client
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        redis_client.get().ping()
        status = "ok"
    except Exception as exc:
        status = f"degraded: {exc}"
    return {
        "status": status,
        "uptime": "running",
        "last_processed": datetime.now(timezone.utc).isoformat(),
    }


# --- AH-02: Auth login (rate limited: 5/min per IP) ---

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
async def login(req: LoginRequest):
    import hashlib, os
    # Password stored as SHA-256 hash in Redis for simplicity
    # Set via: docker exec trading-bot-redis-1 redis-cli SET dashboard:password_hash <hash>
    import redis_client
    r = redis_client.get()
    stored_hash = r.get("dashboard:password_hash")
    if not stored_hash:
        # Default password on first run: TradingBot2026!
        # Change it via dashboard or Redis directly
        default_hash = hashlib.sha256("TradingBot2026!".encode()).hexdigest()
        r.set("dashboard:password_hash", default_hash)
        stored_hash = default_hash

    input_hash = hashlib.sha256(req.password.encode()).hexdigest()
    if req.username != "admin" or input_hash != stored_hash:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"access_token": _make_token(req.username), "token_type": "bearer"}


# --- AH-04: Bot control ---

def _settings_configured(r) -> bool:
    """Return True only if user has set all required pre-start values."""
    return (
        r.get("bot:min_open_trades") is not None and
        r.get("bot:max_open_trades") is not None and
        r.get("bot:max_position_usdt") is not None and
        r.get("bot:starting_capital_usdt") is not None
    )


@app.get("/bot/status", dependencies=[Depends(_verify_token)])
async def bot_status():
    import redis_client, redis_keys
    r = redis_client.get()
    configured = _settings_configured(r)
    return {
        "stage": int(r.get(redis_keys.BRAIN_STAGE) or 1),
        "mode": r.get("bot:mode") or "paper",
        "running": r.get("bot:running") == "1",
        "paper_closed": int(r.get("brain:paper_closed") or 0),
        "settings_configured": configured,
        "min_open_trades": r.get("bot:min_open_trades"),
        "max_open_trades": r.get("bot:max_open_trades"),
        "max_position_usdt": r.get("bot:max_position_usdt"),
        "min_position_usdt": r.get("bot:min_position_usdt"),
        "starting_capital_usdt": r.get("bot:starting_capital_usdt"),
        "virtual_balance": r.get("account:virtual_balance"),
        # cont. 66 — real Binance futures balance (account_risk.monitor writes
        # redis_keys.ACCOUNT_BALANCE = account:balance_usdt every 30s in live
        # mode). Shown ALONGSIDE virtual_balance in live so the dashboard
        # surfaces both the actual account total and the bot's trading budget.
        # Null in paper mode (no live account) → frontend hides the real-balance line.
        "real_balance": r.get(redis_keys.ACCOUNT_BALANCE),
        "leverage": int(r.get("bot:leverage") or 5),
    }


@app.post("/bot/start", dependencies=[Depends(_verify_token)])
async def bot_start():
    import redis_client, redis_keys
    r = redis_client.get()
    if not _settings_configured(r):
        raise HTTPException(
            status_code=400,
            detail="Cannot start: set min/max trades, max position USDT, and starting capital first via PUT /bot/settings"
        )
    # Initialise virtual balance from user-set starting capital (only if not already set)
    if not r.get(redis_keys.VIRTUAL_BALANCE):
        capital = float(r.get("bot:starting_capital_usdt"))
        r.set(redis_keys.VIRTUAL_BALANCE, capital)
    r.set("bot:running", "1")
    return {"started": True, "virtual_balance": float(r.get(redis_keys.VIRTUAL_BALANCE))}


@app.post("/bot/stop", dependencies=[Depends(_verify_token)])
async def bot_stop():
    import redis_client
    redis_client.get().set("bot:running", "0")
    return {"stopped": True}


# ── cont. 31 — Session markers (operator-driven dashboard filter) ─────────
# Lets the operator stop the bot, reset settings, click "Start New Session"
# and have the dashboard's closed-trades / equity-curve / PnL views show
# ONLY trades from this session. Old trades stay in DB for ML/analytics.

class SessionStartRequest(BaseModel):
    label: Optional[str] = None
    reset_virtual_balance: bool = False  # if True, reset virtual_balance to bot:starting_capital_usdt


@app.post("/sessions/start", dependencies=[Depends(_verify_token)])
async def session_start(req: SessionStartRequest):
    """Mark a new dashboard session starting NOW. Old trades stay in DB
    but dashboard views (closed trades, equity curve, session P&L) filter
    to only trades with exit_time >= session_start_ts. Open trades are
    always shown regardless of session boundary (they need management).

    `reset_virtual_balance=True` also resets the paper virtual balance to
    `bot:starting_capital_usdt` — use when starting fresh after closing
    all open trades and adjusting capital settings."""
    import time
    import redis_client, redis_keys
    r = redis_client.get()
    now = int(time.time())

    if req.reset_virtual_balance:
        starting_capital = r.get("bot:starting_capital_usdt")
        if starting_capital:
            r.set(redis_keys.VIRTUAL_BALANCE, float(starting_capital))

    start_balance = float(r.get(redis_keys.VIRTUAL_BALANCE) or 0)
    r.set("session:start_ts", now)
    r.set("session:start_balance", start_balance)
    if req.label:
        r.set("session:label", req.label[:80])
    else:
        r.delete("session:label")
    return {
        "started": True, "session_start_ts": now,
        "session_start_balance": start_balance,
        "label": req.label,
        "reset_virtual_balance": req.reset_virtual_balance,
    }


@app.get("/sessions/current", dependencies=[Depends(_verify_token)])
async def session_current():
    """Return current session info + session-scoped P&L."""
    import redis_client, redis_keys
    from db import db_conn
    r = redis_client.get()
    ts_raw = r.get("session:start_ts")
    if not ts_raw:
        return {"active": False}
    try:
        ts = int(ts_raw)
    except (TypeError, ValueError):
        return {"active": False}
    start_balance = float(r.get("session:start_balance") or 0)
    label = r.get("session:label")
    current_balance = float(r.get(redis_keys.VIRTUAL_BALANCE) or 0)

    # Session-scoped stats from DB
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  COUNT(*)                                              AS closed_n,
                  COALESCE(SUM(net_pnl_usdt), 0)                        AS realised_pnl,
                  COUNT(*) FILTER(WHERE net_pnl_usdt > 0)               AS wins,
                  COUNT(*) FILTER(WHERE net_pnl_usdt < 0)               AS losses,
                  COALESCE(AVG(net_pnl_usdt) FILTER(WHERE net_pnl_usdt > 0), 0) AS avg_win,
                  COALESCE(AVG(net_pnl_usdt) FILTER(WHERE net_pnl_usdt < 0), 0) AS avg_loss
                FROM trades
                WHERE status='closed' AND is_paper=true
                  AND exit_time >= to_timestamp(%s)
            """, (ts,))
            row = cur.fetchone() or (0, 0, 0, 0, 0, 0)
    closed_n, realised_pnl, wins, losses, avg_win, avg_loss = row
    win_rate = (100.0 * wins / closed_n) if closed_n > 0 else None

    return {
        "active": True,
        "session_start_ts": ts,
        "session_start_balance": start_balance,
        "current_balance": current_balance,
        "label": label,
        "closed_in_session": closed_n,
        "realised_pnl_usdt": round(float(realised_pnl), 4),
        "wins": wins, "losses": losses,
        "win_rate_pct": round(win_rate, 1) if win_rate is not None else None,
        "avg_win_usdt": round(float(avg_win), 4),
        "avg_loss_usdt": round(float(avg_loss), 4),
        "pnl_pct_of_start": (round(100.0 * float(realised_pnl) / start_balance, 2)
                             if start_balance > 0 else None),
    }


@app.post("/sessions/clear", dependencies=[Depends(_verify_token)])
async def session_clear():
    """Clear the current session marker. Dashboard reverts to all-time view."""
    import redis_client
    r = redis_client.get()
    r.delete("session:start_ts", "session:start_balance", "session:label")
    return {"cleared": True}


def _session_start_ts() -> int | None:
    """Helper: returns active session start_ts (epoch int) or None."""
    import redis_client
    raw = redis_client.get().get("session:start_ts")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class CloseAllRequest(BaseModel):
    confirm: bool = False


@app.post("/bot/close_all_trades", dependencies=[Depends(_verify_token)])
async def close_all_trades(req: CloseAllRequest):
    """Force-close every open trade at current mark price. Lets the user reset
    state before restarting with new capital / settings. Requires explicit
    confirm=true to prevent accidental triggering."""
    if not req.confirm:
        raise HTTPException(status_code=400,
                            detail="confirm=true required to close all trades")

    from memory.query import get_open_trades
    from execution.factory import get_engine

    engine = get_engine()
    open_trades = get_open_trades()

    closed_ids = []
    errors = []
    for trade in open_trades:
        trade_id = trade.get("id")
        try:
            engine.close_trade(trade_id, reason="manual_close_all")
            closed_ids.append(str(trade_id))
        except Exception as exc:
            log.error("close_all_trade_failed", trade_id=str(trade_id),
                      error=f"{type(exc).__name__}: {exc}")
            errors.append({"trade_id": str(trade_id), "error": str(exc)[:200]})

    log.warning("close_all_trades_complete",
                requested=len(open_trades),
                closed=len(closed_ids),
                failed=len(errors))

    # One-shot Telegram alert — explicit user action, worth notifying.
    try:
        from notifications.telegram import send_critical
        send_critical(
            f"Manual close-all-trades fired by dashboard.\n"
            f"Requested: {len(open_trades)}\n"
            f"Closed: {len(closed_ids)}\n"
            f"Failed: {len(errors)}"
        )
    except Exception:
        pass

    return {
        "requested": len(open_trades),
        "closed": len(closed_ids),
        "failed": len(errors),
        "errors": errors[:10],   # cap response size
    }


@app.post("/bot/mode", dependencies=[Depends(_verify_token)])
async def set_mode(mode: str):
    import redis_client
    r = redis_client.get()
    paper_closed = int(r.get("brain:paper_closed") or 0)
    if mode == "live" and paper_closed < 2000:
        raise HTTPException(status_code=403, detail=f"Need 2000 paper trades, have {paper_closed}")
    r.set("bot:mode", mode)
    return {"mode": mode, "accepted": True}


# cont. 47 — Real mode switch endpoint that actually changes the execution
# engine + Binance endpoint, not just the Redis cosmetic flag.

class ModeSwitchRequest(BaseModel):
    target: str                                # "live" | "paper"
    max_position_usdt: float | None = None     # required for live
    starting_capital_usdt: float | None = None # required for live
    leverage: int | None = 5                   # default 5
    close_open_trades: bool = True             # auto-close existing trades
    confirm: bool = False                      # explicit ack of irreversibility


@app.post("/bot/mode_switch", dependencies=[Depends(_verify_token)])
async def mode_switch(req: ModeSwitchRequest):
    """cont. 47 — Full paper↔live transition. Does ALL the steps required
    so the dashboard button works end-to-end:

      1. Validates intent (requires confirm=true for irreversibility)
      2. Stops the bot (bot:running=0)
      3. Closes any open positions via the CURRENT engine
      4. Writes the desired Redis settings (using CORRECT keys)
      5. Signals watchdog via bot:mode_change_pending Redis key
      6. Watchdog rewrites .env (TRADING_MODE + BINANCE_TESTNET) and
         restarts brain + celery_worker + data_feed containers
      7. Returns "pending" — caller polls /bot/mode_change_status

    For live mode, this also REQUIRES the operator has MAINNET API keys
    in .env (sanity check only — actual auth happens on container restart).
    """
    import redis_client
    import json as _j
    r = redis_client.get()
    target = req.target.lower()
    if target not in ("live", "paper"):
        raise HTTPException(status_code=400,
                            detail="target must be 'live' or 'paper'")
    if not req.confirm:
        raise HTTPException(
            status_code=400,
            detail=("confirm=true required — switching modes restarts the "
                    "brain + celery + data_feed and (for live) routes orders "
                    "to REAL Binance"))

    # Gate live mode on the 2000-paper-trade floor
    paper_closed = int(r.get("brain:paper_closed") or 0)
    if target == "live" and paper_closed < 2000:
        raise HTTPException(
            status_code=403,
            detail=f"Need 2000 paper trades to enable live mode "
                   f"(currently {paper_closed})")

    if target == "live":
        if req.max_position_usdt is None or req.starting_capital_usdt is None:
            raise HTTPException(
                status_code=400,
                detail="max_position_usdt and starting_capital_usdt required for live")
        if req.starting_capital_usdt < 5:
            raise HTTPException(
                status_code=400, detail="starting_capital_usdt must be ≥ 5")
        if req.max_position_usdt < 5:
            raise HTTPException(
                status_code=400, detail="max_position_usdt must be ≥ 5")
        if req.max_position_usdt > req.starting_capital_usdt:
            raise HTTPException(
                status_code=400,
                detail="max_position_usdt cannot exceed starting_capital_usdt")

    # Step 1 — stop bot
    r.set("bot:running", "0")

    # Step 2 — close open trades via current engine
    from memory.query import get_open_trades
    from execution.factory import get_engine
    closed_ids: list[str] = []
    errors: list[dict] = []
    if req.close_open_trades:
        try:
            engine = get_engine()
            for trade in get_open_trades():
                tid = trade.get("id")
                try:
                    engine.close_trade(tid, reason="manual_close_all")
                    closed_ids.append(str(tid))
                except Exception as exc:
                    errors.append({"trade_id": str(tid),
                                   "error": str(exc)[:200]})
        except Exception as exc:
            errors.append({"error": f"engine_unavailable: {str(exc)[:200]}"})

    # Step 3 — write the desired settings (correct Redis keys)
    import redis_keys as _rk
    if target == "live":
        r.set("bot:max_position_usdt",     float(req.max_position_usdt))
        r.set("bot:starting_capital_usdt", float(req.starting_capital_usdt))
        r.set("bot:leverage",              int(req.leverage or 5))
        r.set("bot:min_open_trades",       1)
        r.set("bot:max_open_trades",       1)
        # CORRECT key — fixes the cont. 47 sizing bug.
        r.set(_rk.VIRTUAL_BALANCE,         float(req.starting_capital_usdt))
    # Cosmetic mode flag (also used by dashboard /bot/status UI)
    r.set("bot:mode", target)

    # Step 4 — signal watchdog to do the env rewrite + container restart
    payload = {
        "target":            target,
        "trading_mode":      "live" if target == "live" else "paper",
        "binance_testnet":   "false" if target == "live" else "true",
        "requested_at":      int(time.time()) if False else None,  # set below
        "starting_capital":  req.starting_capital_usdt,
        "max_position":      req.max_position_usdt,
    }
    import time as _t
    payload["requested_at"] = int(_t.time())
    r.set("bot:mode_change_pending", _j.dumps(payload))
    r.set("bot:mode_change_status",  "pending")

    log.warning("mode_switch_requested",
                target=target,
                closed=len(closed_ids), failed=len(errors))

    try:
        from notifications.telegram import send_critical
        send_critical(
            f"Mode switch requested → {target.upper()}\n"
            f"Closed open trades: {len(closed_ids)} (failed: {len(errors)})\n"
            f"Watchdog will rewrite .env and restart brain in ~10s."
        )
    except Exception:
        pass

    return {
        "status":              "pending",
        "target":              target,
        "closed_trades":       len(closed_ids),
        "failed_closes":       errors,
        "next":                "Poll /bot/mode_change_status for completion.",
        "estimated_seconds":   30,
    }


@app.get("/bot/mode_change_status", dependencies=[Depends(_verify_token)])
async def mode_change_status():
    """Poll the in-flight mode change. Returns one of:
      - "complete"  — watchdog finished; brain + celery + data_feed are up
      - "pending"   — still in flight (env rewrite or container restart)
      - "failed"    — watchdog reported failure (manual reconciliation needed)
      - "idle"      — no change in flight
    """
    import redis_client
    r = redis_client.get()
    status = r.get("bot:mode_change_status") or "idle"
    pending = r.get("bot:mode_change_pending")
    result  = r.get("bot:mode_change_result")
    out = {"status": status, "mode": r.get("bot:mode") or "paper"}
    if pending:
        import json as _j
        try:
            out["pending_payload"] = _j.loads(pending)
        except Exception:
            pass
    if result:
        import json as _j
        try:
            out["result"] = _j.loads(result)
        except Exception:
            out["result"] = result
    return out


@app.put("/bot/settings", dependencies=[Depends(_verify_token)])
async def bot_settings(settings: dict):
    """
    Supports partial updates — only send the fields you want to change.
    Unset fields keep their current Redis values.
    All 4 fields required on first setup (before Start Trading is pressed).
    {
        min_open_trades: int,         — Brain maintains at least this many trades open
        max_open_trades: int,         — Brain never exceeds this many simultaneous trades
        max_position_usdt: float,     — Absolute max USDT per single trade
        starting_capital_usdt: float  — Virtual balance (only set before first Start)
    }
    """
    import redis_client
    r = redis_client.get()

    # Merge with current Redis values so partial updates work (e.g. only change max_open_trades)
    current_min     = r.get("bot:min_open_trades")
    current_max     = r.get("bot:max_open_trades")
    current_pos     = r.get("bot:max_position_usdt")
    current_pos_min = r.get("bot:min_position_usdt")
    current_capital = r.get("bot:starting_capital_usdt")

    current_lev = r.get("bot:leverage")

    min_t    = settings.get("min_open_trades",      int(current_min)     if current_min     else None)
    max_t    = settings.get("max_open_trades",      int(current_max)     if current_max     else None)
    max_pos  = settings.get("max_position_usdt",    float(current_pos)   if current_pos     else None)
    # min_position_usdt is OPTIONAL (a per-trade floor). None/0 = no floor (engine keeps its $5 default).
    min_pos  = settings.get("min_position_usdt",    float(current_pos_min) if current_pos_min else None)
    capital  = settings.get("starting_capital_usdt",float(current_capital) if current_capital else None)
    leverage = settings.get("leverage",             int(current_lev)     if current_lev     else 5)

    errors = []
    if min_t is None or int(min_t) < 1:
        errors.append("min_open_trades must be ≥ 1")
    if max_t is None or int(max_t) < 1:
        errors.append("max_open_trades must be ≥ 1")
    if min_t and max_t and int(min_t) > int(max_t):
        errors.append("min_open_trades cannot exceed max_open_trades")
    if max_pos is None or float(max_pos) <= 0:
        errors.append("max_position_usdt must be > 0 USDT")
    if capital is None or float(capital) < 100:
        errors.append("starting_capital_usdt must be ≥ 100 USDT")
    if max_pos and capital and float(max_pos) > float(capital):
        errors.append("max_position_usdt cannot exceed starting_capital_usdt")
    if min_pos is not None and float(min_pos) > 0:
        if float(min_pos) < 1:
            errors.append("min_position_usdt must be ≥ 1 USDT")
        if max_pos and float(min_pos) > float(max_pos):
            errors.append("min_position_usdt cannot exceed max_position_usdt")
    if not (1 <= int(leverage) <= 20):
        errors.append("leverage must be between 1 and 20")
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    r.set("bot:min_open_trades",      int(min_t))
    r.set("bot:max_open_trades",      int(max_t))
    r.set("bot:max_position_usdt",    float(max_pos))
    # 0 / unset clears the floor (engine falls back to its built-in $5 minimum).
    if min_pos is not None:
        r.set("bot:min_position_usdt", float(min_pos))
    r.set("bot:starting_capital_usdt", float(capital))
    r.set("bot:leverage",             int(leverage))

    # If the user explicitly changed starting_capital AND there are no open
    # trades, sync the live virtual_balance to match — otherwise the new
    # capital sits unused (only consumed at first /bot/start). Skip when
    # trades are open: live balance reflects PnL changes that the user has
    # already accumulated; we don't silently wipe them.
    import redis_keys as _rk
    balance_reset = False
    new_capital_requested = "starting_capital_usdt" in settings
    if new_capital_requested:
        from memory.query import get_open_trades as _get_open
        open_n = len(_get_open())
        if open_n == 0:
            r.set(_rk.VIRTUAL_BALANCE, float(capital))
            balance_reset = True
            log.info("virtual_balance_reset_via_settings",
                     new_capital=float(capital), open_trades=0)

    return {
        "saved": True,
        "min_open_trades": int(min_t),
        "max_open_trades": int(max_t),
        "max_position_usdt": float(max_pos),
        "min_position_usdt": float(min_pos) if min_pos is not None else None,
        "starting_capital_usdt": float(capital),
        "leverage": int(leverage),
        "virtual_balance_reset": balance_reset,
        "takes_effect": (
            "immediately on next new trade — existing open trades unchanged. "
            + ("Virtual balance reset to new starting_capital."
               if balance_reset
               else "Virtual balance NOT reset (open trades present — close all first).")
        ),
    }


# --- AH-05: REST data endpoints ---

@app.get("/trades/open", dependencies=[Depends(_verify_token)])
async def get_open_trades():
    from memory.query import get_open_trades as _get
    import redis_client, redis_keys
    from db import db_conn as _db_conn
    trades = _get()
    r = redis_client.get()
    # Cont. 41: resolve strategy_id → human-readable name in one batched query
    # (avoid an N+1 select per trade). The dashboard renders the resolved name.
    strategy_name_map: dict = {}
    sids = [t.get("strategy_id") for t in trades if t.get("strategy_id")]
    if sids:
        try:
            with _db_conn() as _conn:
                with _conn.cursor() as _cur:
                    _cur.execute(
                        "SELECT id, name FROM strategies WHERE id = ANY(%s::uuid[])",
                        (list({str(s) for s in sids}),),
                    )
                    strategy_name_map = {str(sid): name for sid, name in _cur.fetchall()}
        except Exception:
            strategy_name_map = {}
    enriched = []
    for t in trades:
        t["strategy_name"] = strategy_name_map.get(str(t.get("strategy_id") or ""), "—")
        pair = t.get("pair", "")
        mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", pair)) or t.get("entry_price") or 0)
        entry = float(t.get("average_entry") or t.get("entry_price") or 0)
        direction_sign = 1.0 if t.get("direction") == "long" else -1.0
        capital = float(t.get("capital_usdt") or 0)
        leverage = int(t.get("leverage") or 1)
        # Binance USDT-M PnL formula: capital × leverage × price_change_pct × direction
        # This correctly handles tiny-priced altcoins regardless of qty
        if entry > 0 and mark > 0:
            pct_change = (mark - entry) / entry
            position_size = capital * leverage
            current_pnl = round(position_size * pct_change * direction_sign, 4)
        else:
            current_pnl = 0.0
        fees = round(capital * leverage * 0.0004, 4)  # 0.04% taker fee estimate
        # SL locked-in PnL: what the trade would close at if the trailing SL fires now
        sl_level = float(t.get("trailing_sl_level") or 0)
        if sl_level > 0 and entry > 0:
            sl_pct = (sl_level - entry) / entry
            sl_raw_pnl = round(capital * leverage * sl_pct * direction_sign, 4)
            sl_net_pnl = round(sl_raw_pnl - fees, 4)
        else:
            sl_net_pnl = 0.0

        t["current_mark_price"] = mark
        t["current_pnl_usdt"] = current_pnl
        t["net_current_pnl"] = round(current_pnl - fees, 4)
        t["sl_locked_pnl_usdt"] = sl_net_pnl   # + means locked profit, - means locked loss
        t["pct_change"] = round((mark - entry) / entry * 100, 4) if entry > 0 else 0
        # F48 §Idea B — surface the tp1_fired Redis flag so the open-trades
        # table can show a ✓ next to TP1 once the partial has executed.
        try:
            t["tp1_fired"] = (r.get(f"trade:{t.get('id')}:tp1_fired") == "1")
            # Phase A write-through (cont. 55): expose tp_fired too.
            t["tp_fired"]  = (r.get(f"trade:{t.get('id')}:tp_fired") == "1") or t["tp1_fired"]
        except Exception:
            t["tp1_fired"] = False
            t["tp_fired"]  = False
        # Live hold time
        if t.get("entry_time"):
            try:
                from datetime import datetime, timezone
                et = t["entry_time"]
                if hasattr(et, "replace"):
                    elapsed = (datetime.now(timezone.utc) - et.replace(tzinfo=timezone.utc)).total_seconds()
                else:
                    elapsed = 0
                t["hold_time_seconds"] = int(elapsed)
                t["hold_hours"] = round(elapsed / 3600, 2)
            except Exception:
                pass
        enriched.append(t)
    total_pnl = round(sum(t.get("net_current_pnl", 0) for t in enriched), 4)
    return {"trades": enriched, "total_open_pnl": total_pnl}


@app.get("/trades/closed", dependencies=[Depends(_verify_token)])
async def get_closed_trades(pair: Optional[str] = None, limit: int = 100,
                            all_history: bool = False):
    """List closed trades. cont. 31: when a dashboard session is active
    (session:start_ts in Redis), only trades with exit_time >= session_start_ts
    are returned AND the summary is computed over the session window.
    Pass `?all_history=true` to bypass session filter."""
    from db import db_conn
    where = "WHERE status='closed'"
    params: list = []
    session_ts = None if all_history else _session_start_ts()
    if session_ts is not None:
        where += " AND exit_time >= to_timestamp(%s)"
        params.append(session_ts)
    if pair:
        where += " AND pair = %s"
        params.append(pair)
    # cont. 65 — JOIN signals so closed trades carry the candle-cascade
    # decision that fired them: the picked direction, the per-TF candle
    # forecast votes that backed it, and the cascade confidence. Lets the
    # operator see, per closed trade, "what the candle prediction said".
    # Old trades pre-back-link will show NULL fields — frontend handles "n/a".
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT t.*,
                  s.direction        AS predicted_direction,
                  s.signal_strength  AS cascade_confidence,
                  s.feature_vector   AS signal_feature_vector
                FROM trades t
                LEFT JOIN signals s ON s.trade_id = t.id
                {where.replace('WHERE ', 'WHERE t.')}
                ORDER BY t.exit_time DESC
                LIMIT %s
            """, params + [limit])
            cols = [d[0] for d in cur.description]
            trades = []
            for row in cur.fetchall():
                rec = dict(zip(cols, row))
                # Surface the candle-prediction details as flat fields so
                # the React table can render them directly. cn_*_dir3 are
                # P(up) [0,1] from the corresponding-TF CandleNet — below
                # 0.5 = short bias, above 0.5 = long bias.
                fv = rec.pop("signal_feature_vector", None)
                rec["predicted_direction"] = rec.get("predicted_direction")
                rec["cascade_confidence"] = (
                    float(rec["cascade_confidence"])
                    if rec.get("cascade_confidence") is not None else None)
                rec["candle_followed"] = (
                    rec.get("predicted_direction") is not None
                    and rec.get("predicted_direction") == rec.get("direction"))
                if fv:
                    try:
                        import json as _json
                        fv_d = fv if isinstance(fv, dict) else _json.loads(fv)
                    except Exception:
                        fv_d = {}
                else:
                    fv_d = {}
                rec["cn_5m_dir3"]  = fv_d.get("cn_5m_dir3")
                rec["cn_15m_dir3"] = fv_d.get("cn_15m_dir3")
                rec["cn_1h_dir3"]  = fv_d.get("cn_1h_dir3")
                rec["cn_5m_mag3"]  = fv_d.get("cn_5m_mag3")
                rec["cn_15m_mag3"] = fv_d.get("cn_15m_mag3")
                rec["cn_1h_mag3"]  = fv_d.get("cn_1h_mag3")
                trades.append(rec)

            # Summary stats over the SAME filter (session-scoped when active).
            # candle_pred_followed_rate: of closed trades that HAVE a linked
            # signal, fraction whose realized direction matched the cascade
            # pick. Tells the operator "is the candle prediction actually
            # driving entry decisions, or is OFI fallback dominating?"
            cur.execute(f"""
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE net_pnl_usdt > 0) as wins,
                    COUNT(*) FILTER (WHERE net_pnl_usdt <= 0) as losses,
                    COALESCE(ROUND(SUM(net_pnl_usdt)::numeric,4), 0) as total_pnl,
                    COALESCE(ROUND(AVG(net_pnl_usdt)::numeric,4), 0) as avg_pnl,
                    COALESCE(ROUND(SUM(fees_usdt)::numeric,4), 0) as total_fees
                FROM trades t {where.replace('WHERE ', 'WHERE t.')}
            """, params)
            s = cur.fetchone()
            cur.execute(f"""
                SELECT
                  COUNT(*)                                  AS linked,
                  COUNT(*) FILTER (WHERE s.direction = t.direction) AS matched,
                  COUNT(*) FILTER (WHERE s.direction = t.direction AND t.net_pnl_usdt > 0) AS matched_wins
                FROM trades t
                JOIN signals s ON s.trade_id = t.id
                {where.replace('WHERE ', 'WHERE t.')}
            """, params)
            c = cur.fetchone()
            summary = {
                "total": s[0], "wins": s[1], "losses": s[2],
                "total_pnl": float(s[3]), "avg_pnl": float(s[4]),
                "total_fees": float(s[5]),
                "win_rate_pct": round(s[1] / s[0] * 100, 2) if s[0] else 0,
                # cont. 65 — candle-prediction observability rollup.
                "candle_linked": c[0],
                "candle_followed": c[1],
                "candle_followed_wins": c[2],
                "candle_follow_rate_pct": (
                    round(c[1] / c[0] * 100, 2) if c[0] else None),
                "candle_followed_win_rate_pct": (
                    round(c[2] / c[1] * 100, 2) if c[1] else None),
                "session_scoped": session_ts is not None,
                "session_start_ts": session_ts,
            }

    return {"trades": trades, "summary": summary}


@app.get("/brain/status", dependencies=[Depends(_verify_token)])
async def brain_status():
    import redis_client, redis_keys, json
    r = redis_client.get()
    # Compute overall directional accuracy across all pairs
    acc_keys = r.keys("brain:directional_accuracy:*")
    total_dir, correct_dir = 0, 0
    for k in acc_keys:
        raw = r.get(k)
        if raw:
            d = json.loads(raw)
            total_dir += d.get("total", 0)
            correct_dir += d.get("correct", 0)
    dir_accuracy = round(correct_dir / total_dir * 100, 1) if total_dir > 0 else 0.0
    return {
        "stage": int(r.get(redis_keys.BRAIN_STAGE) or 1),
        "regime": r.get(redis_keys.CURRENT_REGIME) or "unknown",
        "paper_closed": int(r.get("brain:paper_closed") or 0),
        "active_pairs": len(r.smembers(redis_keys.ACTIVE_PAIRS)),
        "directional_accuracy": dir_accuracy,
        "opro_counter": int(r.get("opro:window_counter") or 0),
    }


@app.get("/brain/advanced", dependencies=[Depends(_verify_token)])
async def brain_advanced():
    """Advanced brain metrics: curiosity, self-play, research engine, world model, competence."""
    import redis_client, json
    r = redis_client.get()

    cmap_raw = r.get("brain:competence_map")
    cmap = {}
    if cmap_raw:
        raw = json.loads(cmap_raw)
        # Compute domain scores: average of last N entries
        for domain, history in raw.items():
            if history:
                cmap[domain] = round(sum(history) / len(history), 3)

    worst_domain = min(cmap, key=cmap.get) if cmap else None
    best_domain  = max(cmap, key=cmap.get) if cmap else None

    return {
        "curiosity_score":       float(r.get("brain:curiosity_score") or 0),
        "exploration_budget":    float(r.get("brain:exploration_budget") or 10),
        "self_play_win_rate":    float(r.get("brain:self_play_win_rate") or 0),
        "self_play_games":       int(r.get("brain:self_play_games") or 0),
        "self_play_last_pnl":    float(r.get("brain:self_play_last_pnl") or 0),
        "research_queue_length": int(r.llen("research:hypothesis_queue") or 0),
        "worst_learning_domain": worst_domain,
        "best_learning_domain":  best_domain,
        "competence_domains":    len(cmap),
        "priority_gap":          (r.get("brain:priority_learning_gap") or "").strip() or None,
    }


@app.get("/analytics/metrics", dependencies=[Depends(_verify_token)])
async def analytics_metrics():
    import redis_client, json
    r = redis_client.get()
    result = {}
    for window in [50, 100, 500]:
        raw = r.get(f"analytics:metrics:all:{window}")
        result[str(window)] = json.loads(raw) if raw else {}
    return result


@app.get("/system/feature_health", dependencies=[Depends(_verify_token)])
async def system_feature_health():
    """Per-feature firing status — runs the same checks as tools/feature_health.py
    but as an HTTP endpoint with a 60s in-memory cache. Used by the Feature
    Health dashboard panel to render the per-feature evidence table."""
    import time
    global _feature_health_cache
    try:
        _feature_health_cache
    except NameError:
        _feature_health_cache = {"ts": 0, "data": None}

    # 60s cache — running all 40 checks each request would be wasteful and
    # would also re-load the world model on the F22 check.
    if (time.time() - _feature_health_cache["ts"] < 60
            and _feature_health_cache["data"] is not None):
        return _feature_health_cache["data"]

    try:
        from tools.feature_health import run_all
        from collections import Counter
        results = run_all()
        summary = Counter(r.status for r in results)
        payload = {
            "summary": {
                "total": len(results),
                "firing": summary.get("firing", 0),
                "stale": summary.get("stale", 0),
                "dead": summary.get("dead", 0),
                "disabled": summary.get("disabled", 0),
                "no_check": summary.get("no_check", 0),
                "error": summary.get("error", 0),
            },
            "results": [
                {
                    "feature_id": r.feature_id,
                    "name": r.name,
                    "status": r.status,
                    "evidence": r.evidence,
                    "last_seen_age_seconds": r.last_seen_age_seconds,
                } for r in results
            ],
            "generated_at_ts": int(time.time()),
        }
        _feature_health_cache = {"ts": time.time(), "data": payload}
        return payload
    except Exception as exc:
        log.error("feature_health_endpoint_failed", error=str(exc)[:200])
        raise HTTPException(status_code=500, detail=f"check failed: {exc}")


@app.get("/analytics/direction_win_rate", dependencies=[Depends(_verify_token)])
async def analytics_direction_win_rate():
    """Per-direction win rate across multiple time windows. Used by the
    Direction Win Rate dashboard panel to visualise recovery from the
    sentiment-gate bias bug (see D-03 in BLUEPRINT_COMPLIANCE_AUDIT.md)."""
    from db import db_conn
    out = {}
    for label, interval in [("1h", "1 hour"), ("24h", "24 hours"), ("7d", "7 days")]:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                      direction,
                      COUNT(*) AS n,
                      COUNT(*) FILTER (WHERE net_pnl_usdt > 0) AS wins,
                      COUNT(*) FILTER (WHERE net_pnl_usdt < 0) AS losses,
                      COALESCE(ROUND(AVG(net_pnl_usdt)::numeric, 2), 0) AS avg_pnl,
                      COALESCE(ROUND(SUM(net_pnl_usdt)::numeric, 2), 0) AS total_pnl
                    FROM trades
                    WHERE status='closed' AND is_paper=true
                      AND exit_time > NOW() - INTERVAL '{interval}'
                    GROUP BY direction
                """)
                rows = cur.fetchall()
        long_row = next((r for r in rows if r[0] == "long"), None)
        short_row = next((r for r in rows if r[0] == "short"), None)

        def fmt(r):
            if r is None:
                return {"n": 0, "wins": 0, "losses": 0, "win_pct": None,
                        "avg_pnl": 0.0, "total_pnl": 0.0}
            n, wins, losses, avg_pnl, total_pnl = r[1], r[2], r[3], r[4], r[5]
            return {
                "n": n, "wins": wins, "losses": losses,
                "win_pct": round(100.0 * wins / n, 1) if n else None,
                "avg_pnl": float(avg_pnl),
                "total_pnl": float(total_pnl),
            }

        long_d, short_d = fmt(long_row), fmt(short_row)
        total_n = long_d["n"] + short_d["n"]
        total_wins = long_d["wins"] + short_d["wins"]
        out[label] = {
            "long": long_d,
            "short": short_d,
            "total": {
                "n": total_n,
                "wins": total_wins,
                "win_pct": round(100.0 * total_wins / total_n, 1) if total_n else None,
                "total_pnl": round(long_d["total_pnl"] + short_d["total_pnl"], 2),
            },
        }
    # Currently-open positions split so the user can see the live posture
    from db import db_conn as _dbc
    with _dbc() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT direction, COUNT(*) FROM trades WHERE status='open' GROUP BY direction
            """)
            open_rows = dict(cur.fetchall())
    out["open_now"] = {
        "long": int(open_rows.get("long", 0)),
        "short": int(open_rows.get("short", 0)),
    }
    return out


@app.get("/analytics/equity_curve", dependencies=[Depends(_verify_token)])
async def analytics_equity_curve(all_history: bool = False):
    """Equity curve. cont. 31: when a session is active, filter points to
    those with ts >= session_start_ts so the curve shows session P&L only.
    Pass `?all_history=true` to bypass."""
    import redis_client, json
    from datetime import datetime, timezone
    r = redis_client.get()
    raw = r.lrange("analytics:equity_curve", 0, 999)
    points = [json.loads(item) for item in raw] if raw else []
    points = list(reversed(points))  # oldest first
    session_ts = None if all_history else _session_start_ts()
    if session_ts is not None:
        session_dt = datetime.fromtimestamp(session_ts, tz=timezone.utc)
        filtered = []
        for p in points:
            try:
                pt_ts = datetime.fromisoformat(p["ts"].replace("Z", "+00:00"))
                if pt_ts >= session_dt:
                    filtered.append(p)
            except Exception:
                continue
        points = filtered
    return points


# cont. 70e2 — price-movement columns for the scanner + launch-pad tables.
def _trailing_move(r, pair: str) -> dict:
    """Live TRAILING price movement % over the last 15m/30m/1h, computed from the
    1m candles in Redis (`{pair}:1m:candles`, newest-first: index 0 = latest).
    Returns {trail_15m, trail_30m, trail_60m} (omits a window with no data)."""
    import json as _json
    out: dict = {}
    try:
        raw = r.lrange(f"{pair}:1m:candles", 0, 61)
    except Exception:
        return out
    if not raw or len(raw) < 2:
        return out

    def _close(i: int) -> float:
        try:
            return float(_json.loads(raw[i]).get("c") or 0)
        except Exception:
            return 0.0

    now_px = _close(0)
    if now_px <= 0:
        return out
    for field, idx in (("trail_15m", 15), ("trail_30m", 30), ("trail_60m", 60)):
        if len(raw) > idx:
            then = _close(idx)
            if then > 0:
                out[field] = round((now_px - then) / then * 100.0, 3)
    return out


@app.get("/pairs/active", dependencies=[Depends(_verify_token)])
async def pairs_active():
    import redis_client
    from db import db_conn
    r = redis_client.get()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, is_active, composite_score, volume_score,
                       volatility_score, spread_score, win_rate_score,
                       win_rate, trade_count, last_scanned_at
                FROM pairs WHERE is_active = true
                ORDER BY composite_score DESC NULLS LAST
            """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    for p in rows:
        sym = p.get("symbol") or ""
        p.update(_trailing_move(r, sym))
    return rows


@app.get("/launchpad", dependencies=[Depends(_verify_token)])
async def launchpad():
    """cont. 70 — Launch-Pad: the 10-deep on-deck buffer (P6 panel) that is the SOLE
    funnel for opens when launchpad:enabled=1. Source of truth = Postgres launch_pad;
    returns every slot ordered by slot id with the 3 movement metrics, shadow MAE/MFE
    and state, plus the live enabled flag + open counter."""
    import redis_client
    import redis_keys
    r = redis_client.get()
    from db import db_conn
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT slot, symbol, direction, state, qualified,
                       table_entry_price, last_mark, shadow_pnl_pct,
                       peak_profit_pct, peak_loss_pct, mv_candlenet,
                       mv_predicted, mv_realized, flips_count, regime,
                       ttl_expires_at
                FROM launch_pad ORDER BY slot
            """)
            cols = [d[0] for d in cur.description]
            slots = [dict(zip(cols, row)) for row in cur.fetchall()]
    # cont. 70e2 — attach trailing (live) + since-entry (maintainer-captured)
    # movement % to each occupied slot.
    for s in slots:
        sym = s.get("symbol")
        if sym:
            s.update(_trailing_move(r, sym))
    return {
        "enabled": r.get(redis_keys.LAUNCHPAD_ENABLED) == "1",
        "depth": int(r.get(redis_keys.LAUNCHPAD_DEPTH) or 10),
        "open_count": int(r.get(redis_keys.LAUNCHPAD_OPEN_COUNT) or 0),
        "regime": r.get(redis_keys.CURRENT_REGIME) or "unknown",
        "slots": slots,
    }


@app.get("/signals/shadow_win_rate", dependencies=[Depends(_verify_token)])
async def shadow_win_rate():
    """Blueprint 15.6: Shadow Win Rate panel element.

    cont. 70g (2026-06-05) — computed LIVE from the counterfactuals table, not the
    incremental Redis counter (which had drifted: counter said 41.8%/44912 while the
    table is ~36.7%/94036). The table is the single source of truth.

    NOTE: the underlying `would_have_won` is a POINT-IN-TIME check at the 72h
    checkpoint with NO stop-loss path modelling (peak_loss is structurally 0), so
    this rate is an OPTIMISTIC upper bound — a path-aware win rate would be lower.
    The `basis`/`note` fields tell the UI to label it as such.
    """
    from db import db_conn
    won = total = 0
    sample_oldest = sample_newest = None
    pending_eval = 0
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT count(*) FILTER (WHERE c.would_have_won)          AS won,
                           count(*) FILTER (WHERE c.would_have_won IS NOT NULL) AS total,
                           MIN(s.generated_at), MAX(s.generated_at)
                    FROM counterfactuals c
                    JOIN signals s ON s.id = c.signal_id
                """)
                row = cur.fetchone()
                if row:
                    won   = int(row[0] or 0)
                    total = int(row[1] or 0)
                    sample_oldest = row[2].isoformat() if row[2] else None
                    sample_newest = row[3].isoformat() if row[3] else None
                # Real backlog = rejected signals already PAST the 72h+slack maturity
                # window that still have no counterfactual row. (Signals < ~84h old are
                # not "stale" — they simply haven't matured yet, which is by design.)
                cur.execute("""
                    SELECT COUNT(*)
                    FROM signals s
                    LEFT JOIN counterfactuals c ON c.signal_id = s.id
                    WHERE s.accepted = FALSE
                      AND s.generated_at < NOW() - INTERVAL '84 hours'
                      AND c.signal_id IS NULL
                """)
                pending_eval = int(cur.fetchone()[0] or 0)
    except Exception:
        pass

    return {
        "won": won,
        "total": total,
        "rate": round(100.0 * won / total, 2) if total else 0.0,
        "sample_oldest": sample_oldest,
        "sample_newest": sample_newest,
        "pending_eval": pending_eval,
        # cont. 70g — honesty metadata for the UI.
        "basis": "point_in_time_72h",
        "optimistic": True,
        "note": "Point-in-time at the 72h checkpoint; excludes stop-loss path → optimistic upper bound.",
    }


@app.get("/signals/acceptance_rate", dependencies=[Depends(_verify_token)])
async def signal_acceptance_rate(hours: int = 24):
    """Blueprint 15.6: Signal Acceptance Rate panel element.

    Returns total/accepted/rejected/rate over a rolling window (default 24h).
    Per blueprint 10.7: abnormally low acceptance rate is a flag that the
    rejection filter is over-active.
    """
    from db import db_conn
    h = max(1, min(int(hours), 720))
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN accepted THEN 1 ELSE 0 END) AS accepted
                FROM signals
                WHERE generated_at > NOW() - INTERVAL '{h} hours'
            """)
            row = cur.fetchone()
    total    = int(row[0] or 0)
    accepted = int(row[1] or 0)
    return {
        "window_hours": h,
        "total":        total,
        "accepted":     accepted,
        "rejected":     total - accepted,
        "rate":         round(100.0 * accepted / total, 2) if total else 0.0,
    }


@app.get("/signals/replay_pool", dependencies=[Depends(_verify_token)])
async def signal_replay_pool(limit: int = 50):
    """Signal Monitor Phase 1 (cont. 63, 2026-05-29). Current replay-pool
    contents + producer/consumer counters. Read-only view."""
    from signals.replay_pool import snapshot, stats
    lim = max(1, min(int(limit), 200))
    return {"entries": snapshot(lim), "stats": stats()}


@app.get("/signals/threshold_state", dependencies=[Depends(_verify_token)])
async def signal_threshold_state():
    """Signal Monitor Phase 2 (cont. 63). Per-bucket Bayesian Beta posterior
    + currently-published T_high / T_low + utility-calibration anchor."""
    from signals.bayes_threshold import state
    return state()


@app.get("/signals/bandit_state", dependencies=[Depends(_verify_token)])
async def signal_bandit_state(pair: str | None = None):
    """Signal Monitor Phase 3 (cont. 63). Thompson sampling slot selector
    posterior counts. Pass `?pair=BTCUSDT,ETHUSDT` to see per-pair Beta state."""
    from signals.slot_selector import state
    top = [p.strip() for p in pair.split(",")] if pair else None
    return state(top_pairs=top)


@app.get("/signals/util_calib_state", dependencies=[Depends(_verify_token)])
async def signal_util_calib_state():
    """Signal Monitor Phase 4 (cont. 63). Nightly utility-weighted walk-forward
    calibration last-run report."""
    from signals.utility_calibration import state
    return state()


# ─────────────────────────────────────────────────────────────────────────
# cont. 69s — F9/F12 §4.3 EV-override (SHADOW) control surface
# ─────────────────────────────────────────────────────────────────────────
class EvOverrideToggle(BaseModel):
    live: bool          # True = take trades at reduced size; False = shadow only
    enabled: bool | None = None   # optional master switch (shadow eval on/off)


@app.get("/signals/ev_override", dependencies=[Depends(_verify_token)])
async def ev_override_state():
    """F9/F12 §4.3 EV-override state: shadow/live flags, counters, recent audit."""
    from signals.ev_override import state
    return state()


@app.post("/signals/ev_override/toggle", dependencies=[Depends(_verify_token)])
async def ev_override_toggle(req: EvOverrideToggle):
    """Flip the EV-override mode. `live=false` → SHADOW (measure only, no trade);
    `live=true` → LIVE (re-admit recoverable rejects at reduced size). Optional
    `enabled` toggles the master shadow-eval switch."""
    import redis_client
    r = redis_client.get()
    r.set("ev_override:live", "1" if req.live else "0")
    if req.enabled is not None:
        r.set("ev_override:enabled", "1" if req.enabled else "0")
    from signals.ev_override import state
    return {"ok": True, "state": state()}


@app.get("/signals/ev_override/panel", response_class=HTMLResponse)
async def ev_override_panel():
    """Self-contained control page (no React rebuild). Open it via the dashboard
    host at /signals/ev_override/panel, log in once, then flip SHADOW⇄LIVE."""
    return HTMLResponse(_EV_OVERRIDE_PANEL_HTML)


_EV_OVERRIDE_PANEL_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>EV-Override Control</title><style>
body{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#0f1117;color:#e6e6e6;margin:0;padding:24px}
.card{max-width:640px;margin:0 auto;background:#171a21;border:1px solid #262b36;border-radius:12px;padding:24px}
h1{font-size:20px;margin:0 0 4px}.sub{color:#8a93a3;font-size:13px;margin-bottom:20px}
input{background:#0f1117;border:1px solid #2a3140;color:#e6e6e6;border-radius:8px;padding:10px;width:100%;box-sizing:border-box;margin:6px 0 14px}
button{border:0;border-radius:10px;padding:12px 18px;font-size:15px;font-weight:600;cursor:pointer;margin-right:8px}
.shadow{background:#2a3140;color:#cbd3e1}.live{background:#e0453a;color:#fff}.go{background:#2d8a4e;color:#fff}
.badge{display:inline-block;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:700}
.b-shadow{background:#33415a;color:#9fc1ff}.b-live{background:#5a2330;color:#ff9b9b}
.row{display:flex;gap:18px;flex-wrap:wrap;margin:14px 0}.stat{background:#0f1117;border:1px solid #232936;border-radius:8px;padding:10px 14px;min-width:120px}
.stat .k{color:#8a93a3;font-size:11px}.stat .v{font-size:18px;font-weight:700}
.warn{background:#3a2a14;border:1px solid #5a431c;color:#f0c674;padding:10px 12px;border-radius:8px;font-size:13px;margin-top:14px}
small{color:#6b7383}</style></head><body><div class=card>
<h1>F9/F12 EV-Override</h1><div class=sub>Re-admit recoverable rejects at reduced size when positive-EV. cont. 69s.</div>
<div id=login>
  <input id=pw type=password placeholder="dashboard password" />
  <button class=go onclick=login()>Log in</button>
</div>
<div id=ctl style=display:none>
  <div>Mode: <span id=mode class="badge b-shadow">SHADOW</span></div>
  <div class=row>
    <div class=stat><div class=k>evaluated</div><div class=v id=s_eval>–</div></div>
    <div class=stat><div class=k>would-override</div><div class=v id=s_would>–</div></div>
    <div class=stat><div class=k>live-taken</div><div class=v id=s_taken>–</div></div>
    <div class=stat><div class=k>cold-start skips</div><div class=v id=s_cold>–</div></div>
  </div>
  <button class=shadow onclick=setLive(false)>Set SHADOW (no trades)</button>
  <button class=live onclick=setLive(true)>Set LIVE (take trades)</button>
  <div class=warn id=warn style=display:none>⚠ LIVE re-admits real trades at reduced size. Under full_deploy_mode this carves capital from the main allocation. Watch the would-override stream first.</div>
  <p><small>Refreshes every 5s. Token stored in this browser only.</small></p>
</div>
<script>
let T=localStorage.getItem('ev_tok')||'';
function hdr(){return {'Authorization':'Bearer '+T,'Content-Type':'application/json'}}
async function login(){
  const pw=document.getElementById('pw').value;
  const res=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:'admin',password:pw})});
  if(!res.ok){alert('login failed');return}
  const j=await res.json();T=j.access_token;localStorage.setItem('ev_tok',T);show()}
function show(){document.getElementById('login').style.display='none';
  document.getElementById('ctl').style.display='block';refresh()}
async function refresh(){
  const res=await fetch('/signals/ev_override',{headers:hdr()});
  if(res.status===401){localStorage.removeItem('ev_tok');location.reload();return}
  const s=await res.json();
  document.getElementById('s_eval').textContent=s.evaluated;
  document.getElementById('s_would').textContent=s.would_override;
  document.getElementById('s_taken').textContent=s.live_taken;
  document.getElementById('s_cold').textContent=s.coldstart_skips;
  const m=document.getElementById('mode');
  if(s.live){m.textContent='LIVE';m.className='badge b-live';document.getElementById('warn').style.display='block'}
  else{m.textContent='SHADOW';m.className='badge b-shadow';document.getElementById('warn').style.display='none'}}
async function setLive(v){
  if(v&&!confirm('Switch to LIVE? The bot will start taking reduced-size override trades.'))return;
  await fetch('/signals/ev_override/toggle',{method:'POST',headers:hdr(),body:JSON.stringify({live:v})});
  refresh()}
if(T)show();setInterval(()=>{if(T)refresh()},5000);
</script></div></body></html>"""


# cont. 70g — path-aware counterfactual metrics for the Missed-Opportunities panel.
# The stored `peak_profit_pct` is the point-in-time move at the 72h checkpoint (NOT a
# true peak, no drawdown/stop path). Here we walk the actual 15m candle path over the
# 72h window to surface the TRUE max favourable excursion AND the drawdown you'd have
# had to sit through to reach it (so a "+19% miss" that first dipped -12% is exposed).
# On-demand + Redis-cached (24h) + fapi-ban-guarded + best-effort → never breaks the
# panel; only the ~10 displayed rows are ever computed, each once.
_EXCH_CLIENT = None


def _exchange_client():
    global _EXCH_CLIENT
    if _EXCH_CLIENT is None:
        from exchange.client import BinanceClient
        _EXCH_CLIENT = BinanceClient()
    return _EXCH_CLIENT


def _cf_path_metrics(pair: str, direction: str, signal_dt) -> dict | None:
    """True MFE + drawdown-to-peak + MAE over the 72h window from 15m candles.
    Returns None on any failure (caller falls back to the point-in-time numbers)."""
    import redis_client, json as _json
    if signal_dt is None or not pair or direction not in ("long", "short"):
        return None
    r = redis_client.get()
    sig_ms = int(signal_dt.timestamp() * 1000)
    ckey = f"cf:path:{pair}:{sig_ms}"
    try:
        cached = r.get(ckey)
        if cached:
            return _json.loads(cached)
    except Exception:
        pass
    try:                                   # don't poke a banned fapi endpoint
        if (r.get("fapi:ban_status") or "ok") not in ("ok", "none", "", None):
            return None
    except Exception:
        pass
    try:
        kl = _exchange_client().get_historical_klines(
            pair, "15m", sig_ms, sig_ms + 72 * 3600 * 1000)
    except Exception:
        return None
    if not kl or len(kl) < 2:
        return None
    try:
        entry = float(kl[0][1])            # first bar open ≈ price at signal time
        if entry <= 0:
            return None
        peak = 0.0; dd_to_peak = 0.0; mae = 0.0; running_adv = 0.0
        for k in kl:
            hi, lo = float(k[2]), float(k[3])
            if direction == "long":
                fav = (hi - entry) / entry * 100.0
                adv = (lo - entry) / entry * 100.0
            else:
                fav = (entry - lo) / entry * 100.0
                adv = (entry - hi) / entry * 100.0
            running_adv = min(running_adv, adv)
            if fav > peak:
                peak = fav
                dd_to_peak = running_adv   # worst drawdown endured up to the new peak
            mae = min(mae, adv)
        out = {
            "path_peak_pct": round(peak, 3),          # true max favourable excursion
            "path_dd_to_peak_pct": round(dd_to_peak, 3),  # drawdown to reach that peak
            "path_mae_pct": round(mae, 3),            # worst adverse over the window
        }
    except Exception:
        return None
    try:
        r.setex(ckey, 86400, _json.dumps(out))
    except Exception:
        pass
    return out


@app.get("/signals/missed_opportunities", dependencies=[Depends(_verify_token)])
async def signal_missed_opportunities(limit: int = 10):
    """Blueprint 15.6: Recent Missed Opportunities panel element.

    Returns rejected signals that were counterfactually profitable AND have a
    decoded explanation from F9 Miss Decoder.

    cont. 70g — each row is enriched (best-effort) with PATH-AWARE metrics from the
    real candle path so the optimistic point-in-time `peak_profit_pct` can be shown
    next to the true peak + the drawdown you'd have endured to reach it.
    """
    from db import db_conn
    lim = max(1, min(int(limit), 100))
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.pair, s.direction, s.signal_strength,
                       s.rejection_reason, s.market_regime, s.generated_at,
                       c.peak_profit_pct, c.peak_loss_pct,
                       c.miss_decode_reason, c.created_at AS evaluated_at
                FROM counterfactuals c
                JOIN signals s ON s.id = c.signal_id
                WHERE c.would_have_won = TRUE
                  AND c.miss_decoded   = TRUE
                ORDER BY c.created_at DESC
                LIMIT %s
            """, (lim,))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    for row in rows:
        try:
            pm = _cf_path_metrics(row.get("pair"), row.get("direction"),
                                  row.get("generated_at"))
            if pm:
                row.update(pm)
        except Exception:
            pass
    return rows


@app.get("/trades/closed/export", dependencies=[Depends(_verify_token)])
async def closed_trades_export():
    import csv, io
    from db import db_conn
    from fastapi.responses import StreamingResponse
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, pair, direction, strategy_id, brain_stage,
                       entry_price, exit_price, entry_time, exit_time,
                       quantity, capital_usdt, leverage, timeframe,
                       market_regime, net_pnl_usdt, final_pnl_usdt, fees_usdt,
                       hold_time_seconds, failure_type, trade_quality_score,
                       peak_pnl_usdt, peak_loss_usdt,
                       tp1_target, tp2_target, mag1_pct, mag3_pct,
                       tp_target,
                       exit_reason
                FROM trades WHERE status = 'closed'
                ORDER BY exit_time DESC
            """)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(cols)
    for row in rows:
        writer.writerow([str(v) if v is not None else '' for v in row])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=closed_trades.csv"},
    )


@app.get("/account/risk", dependencies=[Depends(_verify_token)])
async def account_risk():
    import redis_client, redis_keys
    r = redis_client.get()
    return {
        "margin_ratio": float(r.get(redis_keys.MARGIN_RATIO) or 0),
        "free_margin": float(r.get(redis_keys.ACCOUNT_BALANCE) or 0),
        "total_exposure": float(r.get(redis_keys.TOTAL_EXPOSURE) or 0),
        "unrealised_pnl": float(r.get(redis_keys.UNREALISED_PNL) or 0),
    }


@app.get("/hedge/learned_params", dependencies=[Depends(_verify_token)])
async def hedge_learned_params():
    """F44 — per-parameter learned-vs-default snapshot for the dashboard.

    Reads from risk.hedge_params.get_all_learned() which exposes each scalar's
    blueprint default, current learned value, n_samples, bounds, last update
    timestamp, and a `trusted` flag (True iff n_samples >= MIN_SAMPLES).
    """
    import time as _t
    import redis_client
    from risk.hedge_params import get_all_learned

    rows = get_all_learned()
    now = int(_t.time())

    r = redis_client.get()
    updates_count = int(r.get("hedge_params:updates_count") or 0)
    last_update_ts = r.get("hedge_params:last_update_ts")
    last_reward = r.get("hedge_params:last_reward")

    # Enrich each row with last-update age + drift-from-default %.
    out_rows = []
    for row in rows:
        age = None
        if row.get("last_update_ts"):
            try:
                age = now - int(row["last_update_ts"])
            except Exception:
                pass
        default = float(row["default"])
        current = float(row["current"])
        drift_pct = round((current - default) / default * 100, 2) if default else 0.0
        out_rows.append({
            **row,
            "last_update_age_seconds": age,
            "drift_pct_from_default": drift_pct,
        })

    trusted_count = sum(1 for r in out_rows if r.get("trusted"))

    return {
        "params": out_rows,
        "summary": {
            "total_params": len(out_rows),
            "trusted_params": trusted_count,
            "updates_count": updates_count,
            "last_update_age_seconds":
                (now - int(last_update_ts)) if last_update_ts else None,
            "last_reward": float(last_reward) if last_reward else None,
        },
        "generated_at_ts": now,
    }


@app.get("/llm/providers", dependencies=[Depends(_verify_token)])
async def llm_providers():
    """Per-provider LLM chain status — surfaces calls served + rate-limit hits
    so the user can see whether the current 5-provider chain has enough free-tier
    headroom or if more providers should be added.

    Reads cumulative counters written by llm/providers.py:
      - llm:provider_success:{name}          → total successful calls
      - llm:provider_success:{name}:last_ts  → last successful call ts
      - llm:provider_hits:{name}             → total 429/402/403/404 hits
      - llm:provider_hits:{name}:last_ts     → last hit ts
      - llm:provider_hits:{name}:last_status → last HTTP status
      - llm:provider_cooldown:{name}         → present iff currently in cooldown
    """
    import time as _t
    import redis_client
    import config
    from llm.providers import PROVIDER_CATALOG

    r = redis_client.get()
    now = int(_t.time())

    rows: list[dict] = []
    for entry in PROVIDER_CATALOG:
        name = entry["name"]
        key_env = entry["config_env"]
        configured = bool(getattr(config, key_env, ""))

        cooldown_ttl = -2
        try:
            cooldown_ttl = int(r.ttl(f"llm:provider_cooldown:{name}"))
        except Exception:
            cooldown_ttl = -2
        in_cooldown = cooldown_ttl > 0

        succ = int(r.get(f"llm:provider_success:{name}") or 0)
        succ_last_ts = r.get(f"llm:provider_success:{name}:last_ts")
        hits = int(r.get(f"llm:provider_hits:{name}") or 0)
        hits_last_ts = r.get(f"llm:provider_hits:{name}:last_ts")
        last_status = r.get(f"llm:provider_hits:{name}:last_status")

        rows.append({
            "name": name,
            "model": entry["model"],
            "free_rpm": entry["free_rpm"],
            "configured": configured,
            "in_cooldown": in_cooldown,
            "cooldown_ttl_seconds": cooldown_ttl if cooldown_ttl > 0 else 0,
            "successful_calls": succ,
            "rate_limit_hits": hits,
            "last_success_age_seconds": (now - int(succ_last_ts)) if succ_last_ts else None,
            "last_hit_age_seconds":     (now - int(hits_last_ts)) if hits_last_ts else None,
            "last_hit_status_code":     int(last_status) if last_status else None,
        })

    # Local Ollama row — cont. 40. Post-cont.38 redesign moved background
    # LLM tasks to local-primary Ollama; this row surfaces the new primary
    # alongside the 5 cloud-burst providers so the operator sees the full
    # chain in one view. Keys written by llm/ollama_client.py + researcher.
    import os as _os
    ollama_succ = int(r.get("llm:ollama:success_count") or 0)
    ollama_fail = int(r.get("llm:ollama:fail_count") or 0)
    ollama_last_ts = r.get("llm:ollama:last_success_ts")
    ollama_last_elapsed = r.get("llm:ollama:last_elapsed_s")
    ollama_age = (now - int(ollama_last_ts)) if ollama_last_ts else None
    # "in cooldown" doesn't apply to local; report a degraded flag if no
    # success in the last 10 min instead.
    ollama_degraded = ollama_age is None or ollama_age > 600
    ollama_row = {
        "name": "ollama_local",
        "model": _os.environ.get("OLLAMA_RESEARCH_MODEL", "mistral:7b"),
        "free_rpm": None,          # no rate limit — local CPU bound
        "kind": "local",            # frontend can render differently
        "configured": True,         # always — built into the stack
        "in_cooldown": False,       # cooldown is cloud-rate-limit semantics
        "degraded": ollama_degraded,
        "cooldown_ttl_seconds": 0,
        "successful_calls": ollama_succ,
        "rate_limit_hits": ollama_fail,   # repurposed: transport/timeout fails
        "last_success_age_seconds": ollama_age,
        "last_hit_age_seconds":     None,
        "last_hit_status_code":     None,
        "last_elapsed_seconds":     float(ollama_last_elapsed) if ollama_last_elapsed else None,
    }
    # Tag rows with kind so the dashboard can group local vs cloud.
    # cont. 58: llamacpp / lmstudio / janai / textgen / gpt4all are local
    # OpenAI-compatible servers; same `kind="local"` semantics as the
    # built-in Ollama row (degraded if no success in last 10 min).
    _LOCAL_PROVIDERS = {"llamacpp", "lmstudio", "janai", "textgen", "gpt4all"}
    for row in rows:
        if row["name"] in _LOCAL_PROVIDERS:
            row["kind"] = "local"
            # Local servers don't have cloud-style rate-limit semantics;
            # surface a degraded flag when configured but no success in the
            # last 10 min (parallels the Ollama row's logic).
            _age = row.get("last_success_age_seconds")
            row["degraded"] = row["configured"] and (_age is None or _age > 600)
        else:
            row["kind"] = "cloud"
    rows.insert(0, ollama_row)   # primary first

    # Headline totals so the dashboard can show "are we hitting the ceiling?"
    total_succ = sum(r["successful_calls"] for r in rows)
    total_hits = sum(r["rate_limit_hits"] for r in rows)
    total_configured = sum(1 for r in rows if r["configured"])
    in_cooldown_count = sum(1 for r in rows if r["in_cooldown"])

    return {
        "providers": rows,
        "summary": {
            "primary_provider": "ollama_local",
            "primary_healthy": not ollama_degraded,
            "total_configured": total_configured,
            "total_in_chain": len(rows),
            "total_successful_calls": total_succ,
            "total_rate_limit_hits": total_hits,
            "currently_in_cooldown": in_cooldown_count,
            # Hit ratio — if this trends > 5-10% over a day, free-tier headroom
            # is getting tight and more providers (Gemini Flash, OpenRouter)
            # are worth adding.
            "hit_ratio_pct": (round(total_hits / max(1, total_hits + total_succ) * 100, 2)),
        },
        "generated_at_ts": now,
    }


@app.get("/features/health", dependencies=[Depends(_verify_token)])
async def features_health():
    from db import db_conn
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM feature_governance ORDER BY feature_id")
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


@app.get("/strategies", dependencies=[Depends(_verify_token)])
async def get_strategies():
    from db import db_conn
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, status, source, generation, created_at,
                       win_rate, avg_pnl_usdt, sharpe_ratio, trade_count,
                       paper_trade_count, active_open_trades
                FROM strategies ORDER BY created_at DESC
            """)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


@app.get("/signals/recent", dependencies=[Depends(_verify_token)])
async def signals_recent(limit: int = 50):
    from db import db_conn
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT pair, direction, accepted, rejection_reason,
                       signal_strength, brain_stage, generated_at
                FROM signals ORDER BY generated_at DESC LIMIT %s
            """, (limit,))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


@app.get("/brain/decisions", dependencies=[Depends(_verify_token)])
async def brain_decisions(limit: int = 20):
    """Blueprint 14.3: recent Brain decision log entries.
    Reads brain:decisions_log Redis list (written by brain/soar.py:_decide
    per the producer-side fix). Returns newest-first to match list semantics."""
    import redis_client as _rc
    raw = _rc.get().lrange("brain:decisions_log", 0, max(1, min(limit, 200)) - 1)
    out = []
    for item in raw:
        try:
            out.append(json.loads(item))
        except Exception:
            pass
    return out


@app.get("/models", dependencies=[Depends(_verify_token)])
async def models_status():
    """Blueprint 14.3: ML/RL models with REAL status (cont. 28).

    Pre-cont.28 this returned raw file mtimes + a few metrics; the frontend
    panel was hardcoded React with placeholder "Active" labels for every model.
    Now returns a per-model row with status derived from file presence, mtime
    vs expected retrain interval, and live training counters. Frontend just
    renders the rows.

    Status semantics:
      active      — file present AND mtime within max_age_hours (recently trained)
      stale       — file present BUT mtime exceeds max_age_hours (overdue)
      frozen      — file present, no retrain pipeline (e.g. external pretrain)
      pending     — checkpoint not present, gated on a future condition
      pretrained  — externally pretrained, no in-bot retrain by design (HF models)
      missing     — expected file absent
    """
    import time
    from pathlib import Path
    import redis_client as _rc
    r = _rc.get()
    now = int(time.time())
    models_dir = Path("/app/models")

    def _file_info(filename: str) -> dict:
        p = models_dir / filename
        if not p.exists():
            return {"exists": False, "mtime": None, "age_hours": None, "size_kb": None}
        st = p.stat()
        return {
            "exists": True,
            "mtime": int(st.st_mtime),
            "age_hours": round((now - st.st_mtime) / 3600, 1),
            "size_kb": round(st.st_size / 1024, 1),
        }

    def _redis_float(key: str) -> float | None:
        v = r.get(key)
        try:
            return round(float(v), 4) if v else None
        except (TypeError, ValueError):
            return None

    def _redis_int(key: str) -> int | None:
        v = r.get(key)
        try:
            return int(v) if v else None
        except (TypeError, ValueError):
            return None

    paper_closed = _redis_int("brain:paper_closed") or 0

    # Each model's spec: (display_name, purpose, file_or_None, max_age_hours_or_None,
    #                    pending_condition_text_or_None, metric_label, metric_value_provider)
    rows: list[dict] = []

    def _status(f: dict, max_age_hours: int | None,
                pending_text: str | None, kind: str = "trained") -> str:
        if pending_text is not None and not f["exists"]:
            return "pending"
        if not f["exists"]:
            return "missing"
        if kind == "pretrained":
            return "pretrained"
        if max_age_hours is None:
            return "frozen"
        if f["age_hours"] is not None and f["age_hours"] > max_age_hours:
            return "stale"
        return "active"

    # HMM Regime
    f = _file_info("hmm_regime.pkl")
    rows.append({
        "name": "HMM Regime", "purpose": "Regime Detection",
        "file": "hmm_regime.pkl", **f,
        "status": _status(f, max_age_hours=24, pending_text=None),
        "metric_label": "States",
        "metric_value": "3 (bull/bear/turbulent)" if f["exists"] else None,
        "progress_pct": None,
    })

    # TFT
    f = _file_info("tft.pth")
    train_acc_tft = _redis_float("ml:tft:last_train_loss")
    rows.append({
        "name": "TFT Forecast", "purpose": "Price Prediction (1h)",
        "file": "tft.pth", **f,
        "status": _status(f, max_age_hours=48, pending_text=None),
        "metric_label": "Last train loss",
        "metric_value": train_acc_tft,
        "progress_pct": None,
    })

    # PatchTST
    f = _file_info("patchtst.pth")
    train_acc_ptst = _redis_float("ml:patchtst:last_train_loss")
    rows.append({
        "name": "PatchTST", "purpose": "Long Sequence (16h)",
        "file": "patchtst.pth", **f,
        "status": _status(f, max_age_hours=72, pending_text=None),
        "metric_label": "Last train loss",
        "metric_value": train_acc_ptst,
        "progress_pct": None,
    })

    # GNN
    f = _file_info("gnn.pth")
    rows.append({
        "name": "GNN Correlation", "purpose": "Inter-Asset Lead-Lag",
        "file": "gnn.pth", **f,
        "status": _status(f, max_age_hours=48, pending_text=None),
        "metric_label": "Params",
        "metric_value": "GAT-init" if f["exists"] else None,
        "progress_pct": None,
    })

    # World Model
    f = _file_info("world_model.pth")
    wm_loss = _redis_float("brain:world_model_last_loss")
    rows.append({
        "name": "World Model", "purpose": "Latent Dynamics",
        "file": "world_model.pth", **f,
        "status": _status(f, max_age_hours=12, pending_text=None),
        "metric_label": "Last loss",
        "metric_value": wm_loss,
        "progress_pct": None,
    })

    # CandleNet 1m (F48)
    cn1_f       = _file_info("candlenet_1m.pth")
    cn1_auc     = _redis_float("brain:candlenet_1m_val_auc")
    cn1_lift    = _redis_float("brain:candlenet_1m_top_decile_lift")
    cn1_calib   = _redis_float("brain:candlenet_1m_dir_calib_err")
    cn1_samples = _redis_int("brain:candlenet_1m_samples")
    cn1_accepted = (r.get("brain:candlenet_1m_accepted") == "1")
    cn1_reject  = r.get("brain:candlenet_1m_rejection_reason")
    if cn1_auc is not None and cn1_samples:
        if cn1_accepted:
            cn1_metric = (f"auc {cn1_auc:.3f}  lift {cn1_lift:.2f}x"
                          f"  calib {cn1_calib:.3f}  ({cn1_samples:,}n)")
        else:
            cn1_metric = (f"REJECTED ({cn1_reject})"
                          if cn1_reject else f"REJECTED ({cn1_samples}n)")
    else:
        cn1_metric = None
    rows.append({
        "name": "CandleNet 1m", "purpose": "Next-Candle Direction / Magnitude (F48)",
        "file": "candlenet_1m.pth", **cn1_f,
        "status": _status(cn1_f, max_age_hours=168, pending_text=None),   # retrained weekly
        "metric_label": "val_auc / lift / calib / samples",
        "metric_value": cn1_metric,
        "progress_pct": None,
    })

    # CandleNet 5m (F48)
    cn5_f       = _file_info("candlenet_5m.pth")
    cn5_auc     = _redis_float("brain:candlenet_5m_val_auc")
    cn5_lift    = _redis_float("brain:candlenet_5m_top_decile_lift")
    cn5_calib   = _redis_float("brain:candlenet_5m_dir_calib_err")
    cn5_samples = _redis_int("brain:candlenet_5m_samples")
    cn5_accepted = (r.get("brain:candlenet_5m_accepted") == "1")
    cn5_reject  = r.get("brain:candlenet_5m_rejection_reason")
    if cn5_auc is not None and cn5_samples:
        if cn5_accepted:
            cn5_metric = (f"auc {cn5_auc:.3f}  lift {cn5_lift:.2f}x"
                          f"  calib {cn5_calib:.3f}  ({cn5_samples:,}n)")
        else:
            cn5_metric = (f"REJECTED ({cn5_reject})"
                          if cn5_reject else f"REJECTED ({cn5_samples}n)")
    else:
        cn5_metric = None
    rows.append({
        "name": "CandleNet 5m", "purpose": "Next-Candle Direction / Magnitude (F48)",
        "file": "candlenet_5m.pth", **cn5_f,
        "status": _status(cn5_f, max_age_hours=168, pending_text=None),   # retrained weekly
        "metric_label": "val_auc / lift / calib / samples",
        "metric_value": cn5_metric,
        "progress_pct": None,
    })

    # CandleNet 15m (F48 §Idea D — 4-TF hierarchy, cont. 46)
    cn15_f       = _file_info("candlenet_15m.pth")
    cn15_auc     = _redis_float("brain:candlenet_15m_val_auc")
    cn15_lift    = _redis_float("brain:candlenet_15m_top_decile_lift")
    cn15_calib   = _redis_float("brain:candlenet_15m_dir_calib_err")
    cn15_samples = _redis_int("brain:candlenet_15m_samples")
    cn15_accepted = (r.get("brain:candlenet_15m_accepted") == "1")
    cn15_reject  = r.get("brain:candlenet_15m_rejection_reason")
    if cn15_auc is not None and cn15_samples:
        if cn15_accepted:
            cn15_metric = (f"auc {cn15_auc:.3f}  lift {cn15_lift:.2f}x"
                           f"  calib {cn15_calib:.3f}  ({cn15_samples:,}n)")
        else:
            cn15_metric = (f"REJECTED ({cn15_reject})"
                           if cn15_reject else f"REJECTED ({cn15_samples}n)")
    else:
        cn15_metric = None
    rows.append({
        "name": "CandleNet 15m", "purpose": "Next-Candle Direction / Magnitude (F48 §Idea D)",
        "file": "candlenet_15m.pth", **cn15_f,
        "status": _status(cn15_f, max_age_hours=168, pending_text=None),
        "metric_label": "val_auc / lift / calib / samples",
        "metric_value": cn15_metric,
        "progress_pct": None,
    })

    # Entry Timing Agent (F48 §Idea C — PPO wait/enter/skip, cont. 46)
    et_f          = _file_info("entry_timing_agent.zip")
    et_decisions  = _redis_int("brain:entry_timing:decisions_count")
    et_skipped    = _redis_int("brain:entry_timing:skipped_count")
    et_last       = r.get("brain:entry_timing:last_decision")
    if et_f["exists"]:
        et_metric = (f"decisions={et_decisions or 0}  skipped={et_skipped or 0}"
                     f"  last={et_last or '-'}")
    else:
        et_metric = "training pending (needs ≥1000 paper trades)"
    rows.append({
        "name": "Entry Timing Agent", "purpose": "PPO wait/enter/skip (F48 §Idea C)",
        "file": "entry_timing_agent.zip", **et_f,
        "status": ("active" if et_f["exists"] else "pending"),
        "metric_label": "decisions / skipped / last",
        "metric_value": et_metric,
        "progress_pct": None,
    })

    # Direction Model — bidirectional, retrained every 50 trades (cont. 25).
    # cont. 37: dashboard now reports the HELD-OUT test metrics, not train.
    # Pre-cont.37 the model published train_acc=89% that was overfit; the
    # honest test_acc / AUC / top-decile lift make it visible when a model
    # fails the validation gates so the operator knows F13 is off for real
    # reasons.
    f = _file_info("direction_model.pkl")
    dir_test_acc = _redis_float("brain:direction_model_test_acc")
    dir_auc      = _redis_float("brain:direction_model_test_auc")
    dir_lift     = _redis_float("brain:direction_model_top_decile_lift")
    dir_samples  = _redis_int("brain:direction_model_samples")
    dir_accepted = (r.get("brain:direction_model_accepted") == "1")
    dir_reject_reason = r.get("brain:direction_model_rejection_reason")
    if dir_test_acc is not None and dir_samples:
        if dir_accepted:
            metric = (f"test_acc {dir_test_acc:.1%} "
                      f"auc {dir_auc:.2f} lift {dir_lift:.2f}x "
                      f"({dir_samples}n)")
        else:
            metric = (f"REJECTED ({dir_reject_reason})"
                      if dir_reject_reason
                      else f"REJECTED ({dir_samples}n)")
    else:
        metric = None
    rows.append({
        "name": "Direction Model", "purpose": "Direction Picker (P(win|long) vs P(win|short))",
        "file": "direction_model.pkl", **f,
        "status": _status(f, max_age_hours=24, pending_text=None),
        "metric_label": "Test acc / AUC / lift / samples",
        "metric_value": metric,
        "progress_pct": None,
    })

    # Kelly Sizer — runtime calc, no model file
    rows.append({
        "name": "Kelly Sizer", "purpose": "Position Sizing",
        "file": None, "exists": True, "mtime": None, "age_hours": None, "size_kb": None,
        "status": "active" if paper_closed >= 50 else "pending",
        "metric_label": "Active threshold",
        "metric_value": f"{paper_closed}/50 trades" if paper_closed < 50 else "active",
        "progress_pct": min(100, round(100 * paper_closed / 50)) if paper_closed < 50 else 100,
    })

    # MARL (Day + Minute) — cont. 35.
    # Hour Agent is intentionally NOT trained: no production code path
    # consumes get_hour_agent_*. Status reports the two trained agents only,
    # so a clean 2/2 reads "active" instead of being dragged down to
    # "missing" by an absent hour checkpoint that nothing would read.
    marl_day_f = _file_info("marl_day_agent.zip")
    marl_min_f = _file_info("marl_minute_agent.zip")
    marl_consumed_loaded = marl_day_f["exists"] and marl_min_f["exists"]
    marl_mtimes = [x["mtime"] for x in (marl_day_f, marl_min_f) if x["mtime"]]
    marl_ages   = [x["age_hours"] for x in (marl_day_f, marl_min_f) if x["age_hours"] is not None]
    marl_sizes  = [(x["size_kb"] or 0) for x in (marl_day_f, marl_min_f)]
    rows.append({
        "name": "MARL (Day + Minute)",
        "purpose": "Strategic Direction + Execution Timing",
        "file": "marl_{day,minute}_agent.zip",
        "exists": marl_consumed_loaded,
        "mtime":     max(marl_mtimes) if marl_mtimes else None,
        "age_hours": min(marl_ages) if marl_ages else None,
        "size_kb":   sum(marl_sizes) or None,
        "status": ("active" if marl_consumed_loaded
                   else ("pending" if paper_closed < 300 else "missing")),
        "metric_label": "Activation",
        "metric_value": (
            f"{paper_closed}/300 trades" if paper_closed < 300
            else ("day+minute loaded" if marl_consumed_loaded
                  else "checkpoints missing")),
        "progress_pct": (min(100, round(100 * paper_closed / 300))
                         if paper_closed < 300
                         else (100 if marl_consumed_loaded else 0)),
    })

    # MAML — adapt counter in redis indicates it's running
    maml_adapt = _redis_int("ml:maml:adapt_count")
    maml_loss = _redis_float("ml:maml:last_mean_loss")
    rows.append({
        "name": "MAML", "purpose": "Regime Adaptation",
        "file": None, "exists": maml_adapt is not None,
        "mtime": _redis_int("ml:maml:last_adapt_ts"),
        "age_hours": (round((now - _redis_int("ml:maml:last_adapt_ts")) / 3600, 1)
                      if _redis_int("ml:maml:last_adapt_ts") else None),
        "size_kb": None,
        "status": ("active" if maml_adapt and maml_adapt > 0
                   else ("pending" if paper_closed < 500 else "missing")),
        "metric_label": "Adaptations / mean loss",
        "metric_value": (f"{maml_adapt} / {maml_loss:.4f}" if maml_adapt and maml_loss
                         else (f"{paper_closed}/500 trades" if paper_closed < 500 else None)),
        "progress_pct": None,
    })

    # CryptoBERT — pretrained from HuggingFace, never retrained in-bot
    crypto_count = _redis_int("ml:sentiment:cryptobert_inference_count") or 0
    rows.append({
        "name": "CryptoBERT", "purpose": "Crypto-tweet Sentiment",
        "file": None, "exists": crypto_count > 0, "mtime": None, "age_hours": None, "size_kb": None,
        "status": "pretrained" if crypto_count > 0 else "missing",
        "metric_label": "Inferences",
        "metric_value": crypto_count if crypto_count else None,
        "progress_pct": None,
    })

    # FinBERT — pretrained from HuggingFace, never retrained in-bot
    finbert_count = _redis_int("ml:sentiment:finbert_inference_count") or 0
    rows.append({
        "name": "FinBERT", "purpose": "Financial-news Sentiment",
        "file": None, "exists": finbert_count > 0, "mtime": None, "age_hours": None, "size_kb": None,
        "status": "pretrained" if finbert_count > 0 else "missing",
        "metric_label": "Inferences",
        "metric_value": finbert_count if finbert_count else None,
        "progress_pct": None,
    })

    return {"models": rows, "generated_at": now}


@app.get("/autonomous_training", dependencies=[Depends(_verify_token)])
async def autonomous_training():
    """F49 — Autonomous Self-Training Orchestrator dashboard panel.

    Returns per-model autonomy state + recent orchestrator decisions + HPO
    cache. Read-only view of everything Redis tracks for F49.
    """
    import time as _t
    import json as _j
    import redis_client as _rc
    r = _rc.get()
    now = int(_t.time())

    tracked_models = [
        "candlenet_1m", "candlenet_5m", "candlenet_15m",
        "tft", "patchtst", "direction_model",
    ]

    def _ri(k):
        v = r.get(k)
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _rf(k):
        v = r.get(k)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _rs(k):
        return r.get(k)

    model_rows = []
    for name in tracked_models:
        last_at = _ri(f"model:{name}:last_trained_at")
        active_v = _rs(f"model:{name}:active_version")
        drift_flag = (_rs(f"model:{name}:drift_detected") == "1")
        drift_score = _rf(f"model:{name}:drift_score")
        drift_at = _ri(f"model:{name}:drift_detected_at")
        drift_features_raw = _rs(f"model:{name}:drift_features")
        try:
            drift_features = _j.loads(drift_features_raw) if drift_features_raw else []
        except Exception:
            drift_features = []
        retrain_needed = (_rs(f"model:{name}:retrain_needed") == "1")
        rolling_auc = _rf(f"model:{name}:rolling_auc")
        peak_auc = _rf(f"model:{name}:peak_auc")
        rolling_lift = _rf(f"model:{name}:rolling_lift")
        rolling_n = _ri(f"model:{name}:rolling_n")
        rollback_count = _ri(f"model:{name}:rollback_count")
        last_rollback_at = _ri(f"model:{name}:last_rollback_at")
        hpo_params_raw = _rs(f"model:{name}:hpo_best_params")
        try:
            hpo_params = _j.loads(hpo_params_raw) if hpo_params_raw else None
        except Exception:
            hpo_params = None
        hpo_best_value = _rf(f"model:{name}:hpo_best_value")
        hpo_trials = _ri(f"model:{name}:hpo_trials")
        online_updates = _ri(f"model:{name}:online_updates")

        age_h = ((now - last_at) / 3600.0) if last_at else None

        model_rows.append({
            "name":             name,
            "last_trained_at":  last_at,
            "age_hours":        round(age_h, 2) if age_h is not None else None,
            "active_version":   active_v,
            "drift_detected":   drift_flag,
            "drift_score":      drift_score,
            "drift_features":   drift_features,
            "drift_detected_at": drift_at,
            "retrain_needed":   retrain_needed,
            "rolling_auc":      rolling_auc,
            "peak_auc":         peak_auc,
            "rolling_lift":     rolling_lift,
            "rolling_n":        rolling_n,
            "rollback_count":   rollback_count,
            "last_rollback_at": last_rollback_at,
            "hpo_params":       hpo_params,
            "hpo_best_value":   hpo_best_value,
            "hpo_trials":       hpo_trials,
            "online_updates":   online_updates,
        })

    # Recent orchestrator decisions
    try:
        raws = r.lrange("orchestrator:decisions", 0, 19)
        decisions = []
        for x in raws:
            try:
                decisions.append(_j.loads(x))
            except Exception:
                continue
    except Exception:
        decisions = []

    orch_active = _rs("orchestrator:active_training")
    orch_last_at = _ri("orchestrator:last_decision_at")

    return {
        "models":                model_rows,
        "decisions":             decisions,
        "active_training":       orch_active or None,
        "last_decision_at":      orch_last_at,
        "generated_at":          now,
    }


@app.get("/account/positions", dependencies=[Depends(_verify_token)])
async def account_positions():
    """Blueprint 14.3: per-position liquidation distance + risk level.
    Computed from open trades + live mark prices. For each open trade:
      - liquidation_distance_pct: how far current price is from the liquidation
        threshold (derived from leverage; ~1/leverage for cross margin)
      - risk_level: 'low' / 'medium' / 'high' / 'critical' band
    """
    from db import db_conn
    import redis_client as _rc, redis_keys as _rk
    r = _rc.get()
    positions = []
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, pair, direction, average_entry, quantity, leverage,
                       capital_usdt, entry_time
                FROM trades WHERE status='open'
            """)
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                t = dict(zip(cols, row))
                pair = t["pair"]
                mark = float(r.get(_rk.MARK_PRICE.replace("{pair}", pair)) or 0)
                avg_entry = float(t.get("average_entry") or 0)
                lev = float(t.get("leverage") or 1)
                if mark <= 0 or avg_entry <= 0 or lev <= 0:
                    continue
                # Approx liquidation distance for cross margin: 1/leverage in the
                # losing direction. E.g., 5× lev → liquidation ≈ 20% adverse move.
                liq_margin = 1.0 / lev
                if t["direction"] == "long":
                    adverse_pct = max(0.0, (avg_entry - mark) / avg_entry)
                else:
                    adverse_pct = max(0.0, (mark - avg_entry) / avg_entry)
                distance_to_liq_pct = max(0.0, liq_margin - adverse_pct) * 100
                # Risk bands
                if distance_to_liq_pct < 2.0:
                    risk = "critical"
                elif distance_to_liq_pct < 5.0:
                    risk = "high"
                elif distance_to_liq_pct < 10.0:
                    risk = "medium"
                else:
                    risk = "low"
                positions.append({
                    "trade_id": str(t["id"]),
                    "pair": pair,
                    "direction": t["direction"],
                    "mark": mark,
                    "average_entry": avg_entry,
                    "leverage": int(lev),
                    "capital_usdt": float(t.get("capital_usdt") or 0),
                    "distance_to_liq_pct": round(distance_to_liq_pct, 2),
                    "risk_level": risk,
                })
    # Sort most-at-risk first so UI can highlight them
    positions.sort(key=lambda x: x["distance_to_liq_pct"])
    return positions


@app.get("/web_intel/feed", dependencies=[Depends(_verify_token)])
async def web_intel_feed(limit: int = 50):
    """Blueprint 14.3: recent web intelligence signals."""
    from db import db_conn
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, fetched_at, source, source_url, signal_type, sentiment,
                       confidence, summary, credibility_score, brain_weight,
                       outcome_tracked, outcome_correct, pairs_affected
                FROM web_intelligence
                ORDER BY fetched_at DESC LIMIT %s
            """, (max(1, min(limit, 200)),))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


@app.get("/web_intel/sources", dependencies=[Depends(_verify_token)])
async def web_intel_sources():
    """Blueprint 14.3: source credibility leaderboard.
    Aggregates per-source from web_intelligence — uses the credibility_score
    computed by web_intel/collector.update_source_credibility (Issue #5 final fix).
    Sources start at neutral 0.5 until ≥5 tracked outcomes."""
    from db import db_conn
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  source,
                  COUNT(*) AS total_signals,
                  COUNT(*) FILTER (WHERE outcome_tracked) AS tracked,
                  COUNT(*) FILTER (WHERE outcome_correct) AS correct,
                  ROUND(MAX(credibility_score)::numeric, 4) AS credibility_score,
                  ROUND(MAX(brain_weight)::numeric, 4) AS brain_weight,
                  MAX(fetched_at) AS most_recent
                FROM web_intelligence
                GROUP BY source
                ORDER BY credibility_score DESC NULLS LAST, total_signals DESC
            """)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


@app.get("/web_intel/strategies", dependencies=[Depends(_verify_token)])
async def web_intel_strategies():
    """Blueprint 14.3: strategy extraction queue with trial status.
    Strategies created from web intel research (source IN 'research', 'web_intel')."""
    from db import db_conn
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, status, source, trade_count, win_rate,
                       avg_pnl_usdt, created_at, retired_at, retirement_reason
                FROM strategies
                WHERE source IN ('research', 'web_intel', 'self_play')
                ORDER BY created_at DESC LIMIT 200
            """)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


@app.get("/system/fapi_recovery", dependencies=[Depends(_verify_token)])
async def fapi_recovery():
    """fapi.binance.com IP ban recovery monitor.

    The background task _fapi_probe_loop() probes every 30s and writes
    Redis keys. This endpoint returns the current snapshot so the dashboard
    can show a live/banned indicator with elapsed time near the live button.
    """
    import redis_client as _rc, time as _t
    r = _rc.get()
    now = int(_t.time())

    status     = r.get("fapi:ban_status") or "unknown"
    last_check = r.get("fapi:last_check_ts")
    first_ban  = r.get("fapi:first_banned_ts")
    last_ok    = r.get("fapi:last_ok_ts")

    banned_for = None
    if first_ban and status == "banned":
        banned_for = now - int(first_ban)

    return {
        "status":           status,            # "ok" | "banned" | "unknown"
        "live_blocked":     status == "banned",
        "last_checked_ts":  int(last_check) if last_check else None,
        "first_banned_ts":  int(first_ban)  if first_ban  else None,
        "last_ok_ts":       int(last_ok)    if last_ok    else None,
        "banned_for_seconds": banned_for,
        "generated_at":     now,
    }


@app.get("/system/health", dependencies=[Depends(_verify_token)])
async def system_health():
    import redis_client as _rc, redis_keys as _rk
    import requests as _req
    from db import db_conn
    r = _rc.get()
    svc = {}

    # Redis — already connected if we reach this code
    try:
        r.ping()
        svc["redis"] = {"status": "ok"}
    except Exception:
        svc["redis"] = {"status": "down"}

    # Postgres
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        svc["postgres"] = {"status": "ok"}
    except Exception:
        svc["postgres"] = {"status": "down"}

    # Brain — writes brain:stage on startup
    svc["brain"] = {"status": "ok" if r.exists("brain:stage") else "down"}

    # Data feed — writes BTCUSDT:mark_price every 5s
    svc["data_feed"] = {"status": "ok" if r.exists("BTCUSDT:mark_price") else "down"}

    # Scanner — active pairs set should have members
    try:
        svc["scanner"] = {"status": "ok" if r.scard(_rk.ACTIVE_PAIRS) > 0 else "degraded"}
    except Exception:
        svc["scanner"] = {"status": "degraded"}

    # Ollama
    try:
        resp = _req.get("http://ollama:11434/api/tags", timeout=2)
        svc["ollama"] = {"status": "ok" if resp.status_code == 200 else "degraded"}
    except Exception:
        svc["ollama"] = {"status": "down"}

    # Cloud Llama 3.3 70B providers (Groq → Cerebras → SambaNova). Replaces
    # the local llama_cpp service removed 2026-05-20. Report "ok" if at least
    # one provider key is configured; "down" if all are missing.
    import config as _cfg
    _keys = [_cfg.GROQ_API_KEY, _cfg.CEREBRAS_API_KEY, _cfg.SAMBANOVA_API_KEY]
    svc["llm_70b_cloud"] = {
        "status": "ok" if any(_keys) else "down",
        "providers_configured": sum(1 for k in _keys if k),
    }

    # Celery worker — check Redis for active celery keys
    try:
        celery_keys = r.keys("celery*") or r.keys("_kombu*")
        svc["celery_worker"] = {"status": "ok" if celery_keys else "degraded"}
    except Exception:
        svc["celery_worker"] = {"status": "degraded"}

    # Web intel — heartbeat key written every cycle (TTL 2× sleep interval)
    svc["web_intel"] = {"status": "ok" if r.exists("web_intel:alive") else "degraded"}

    # Watchdog — heartbeat key written every poll cycle (TTL 120s)
    svc["watchdog"] = {"status": "ok" if r.exists("watchdog:alive") else "degraded"}

    # Dashboard — we are running
    svc["dashboard"] = {"status": "ok"}

    return {"services": svc, "ws_clients": len(_connected_ws)}


# ────────────────────────────────────────────────────────────────────────────
# 2026-05-21 dashboard surfaces for today's new instrumentation:
#   /memory/clusters   — F35 Fast→Slow cluster table
#   /decoders/recent   — F9 + F12 LLM postmortems
#   /self_play/episodes — F41 LOB episode history
# ────────────────────────────────────────────────────────────────────────────

@app.get("/memory/clusters", dependencies=[Depends(_verify_token)])
async def get_memory_clusters():
    """F35: latest consolidated memory clusters (Fast→Slow output)."""
    from db import db_conn
    rows = []
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cluster_key, market_regime, pair_class, direction, "
                "       outcome_class, n_trades, win_rate, avg_pnl_usdt, "
                "       avg_hold_seconds, "
                "       EXTRACT(EPOCH FROM (NOW() - last_updated))::int AS age_s "
                "FROM memory_clusters ORDER BY n_trades DESC"
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    # Cast Decimals to floats for JSON
    for r in rows:
        for k in ("win_rate", "avg_pnl_usdt"):
            if r.get(k) is not None:
                r[k] = float(r[k])
    import redis_client, time
    rd = redis_client.get()
    summary = {
        "count":            len(rows),
        "consolidations":   int(rd.get("memrl:consolidation_count") or 0),
        "last_updated_clusters": int(rd.get("memrl:consolidation_last_clusters_updated") or 0),
        "last_created_clusters": int(rd.get("memrl:consolidation_last_clusters_created") or 0),
        "last_run_age_seconds": (
            int(time.time() - float(rd.get("memrl:consolidation_last_ts") or 0))
            if rd.get("memrl:consolidation_last_ts") else None
        ),
    }
    return {"clusters": rows, "summary": summary}


@app.get("/decoders/recent", dependencies=[Depends(_verify_token)])
async def get_recent_decoders(limit: int = 15):
    """F9 Miss Decoder + F12 Mismatch Decoder — recent LLM postmortems."""
    from db import db_conn
    misses = []
    mismatches = []
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.id, s.pair, s.direction, s.rejection_reason, "
                "       c.peak_profit_pct, c.miss_decode_reason, c.created_at "
                "FROM counterfactuals c "
                "JOIN signals s ON s.id = c.signal_id "
                "WHERE c.miss_decoded = TRUE "
                "ORDER BY c.created_at DESC LIMIT %s",
                (limit,),
            )
            cols = [d[0] for d in cur.description]
            misses = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.execute(
                "SELECT m.id, lt.pair AS loser_pair, lt.direction AS loser_dir, "
                "       lt.trade_potential_score AS loser_pot, m.loser_pnl_usdt, "
                "       wt.pair AS winner_pair, wt.direction AS winner_dir, "
                "       wt.trade_potential_score AS winner_pot, m.winner_pnl_usdt, "
                "       m.decode_reason, m.decoded_at "
                "FROM mismatches m "
                "JOIN trades lt ON lt.id = m.loser_trade_id "
                "JOIN trades wt ON wt.id = m.winner_trade_id "
                "ORDER BY m.decoded_at DESC LIMIT %s",
                (limit,),
            )
            cols = [d[0] for d in cur.description]
            mismatches = [dict(zip(cols, r)) for r in cur.fetchall()]
    for row in misses + mismatches:
        for k, v in list(row.items()):
            if hasattr(v, "isoformat"):
                row[k] = v.isoformat()
            elif type(v).__name__ == "Decimal":
                row[k] = float(v)
    import redis_client, time
    rd = redis_client.get()
    summary = {
        "miss_decoded_count":     int(rd.get("decoders:miss_decoded_count") or 0),
        "mismatch_decoded_count": int(rd.get("decoders:mismatch_decoded_count") or 0),
        "miss_last_age_seconds":  (
            int(time.time() - float(rd.get("decoders:miss_last_ts") or 0))
            if rd.get("decoders:miss_last_ts") else None
        ),
        "mismatch_last_age_seconds": (
            int(time.time() - float(rd.get("decoders:mismatch_last_ts") or 0))
            if rd.get("decoders:mismatch_last_ts") else None
        ),
    }
    return {"misses": misses, "mismatches": mismatches, "summary": summary}


@app.get("/self_play/episodes", dependencies=[Depends(_verify_token)])
async def get_self_play_episodes():
    """F41: rolling self-play episode history. Latest 50 episodes from the
    self_play:episode_history Redis list."""
    import redis_client, json, time
    rd = redis_client.get()
    raw = rd.lrange("self_play:episode_history", 0, 49)
    episodes = []
    for item in raw:
        try:
            episodes.append(json.loads(item))
        except Exception:
            continue
    summary = {
        "win_rate":    float(rd.get("brain:self_play_win_rate") or 0),
        "games":       int(rd.get("brain:self_play_games") or 0),
        "last_pnl":    float(rd.get("brain:self_play_last_pnl") or 0),
        "last_spread": float(rd.get("self_play:last_mean_spread") or 0),
        "last_slip_bps": float(rd.get("self_play:last_mean_slippage_bps") or 0),
        "last_fills":  int(rd.get("self_play:last_fills") or 0),
        "last_age_seconds": (
            int(time.time() - float(rd.get("self_play:last_episode_ts") or 0))
            if rd.get("self_play:last_episode_ts") else None
        ),
    }
    return {"episodes": episodes, "summary": summary}


# --- AH-06: WebSocket push endpoint ---

@app.websocket("/ws/dashboard")
async def ws_dashboard(websocket: WebSocket):
    await websocket.accept()
    _connected_ws.append(websocket)
    try:
        import redis_client, redis_keys
        r = redis_client.get()
        pubsub = r.pubsub()
        channels = [
            redis_keys.CH_PRICE_UPDATE, redis_keys.CH_TRADE_OPENED,
            redis_keys.CH_TRADE_CLOSED, redis_keys.CH_SL_MOVED,
            redis_keys.CH_DCA_TRIGGERED, redis_keys.CH_BRAIN_DECISION,
            redis_keys.CH_SIGNAL_GENERATED, redis_keys.CH_ALERT,
            redis_keys.CH_FEATURE_EVENT, redis_keys.CH_SYSTEM_EVENT,
            redis_keys.CH_BRAIN_METRICS,
        ]
        pubsub.subscribe(*channels)

        while True:
            message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message and message.get("data"):
                try:
                    await websocket.send_text(json.dumps({
                        "channel": message["channel"],
                        "data": message["data"],
                    }))
                except Exception:
                    break
            await asyncio.sleep(0.05)

    except WebSocketDisconnect:
        pass
    finally:
        _connected_ws.remove(websocket)
        try:
            pubsub.unsubscribe()
        except Exception:
            pass
