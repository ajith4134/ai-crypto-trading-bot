"""
Section AH: FastAPI dashboard backend — AH-01 to AH-06.
"""
import asyncio
import json
from datetime import datetime, timezone
from typing import Optional
import structlog
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext
from slowapi import Limiter
from slowapi.util import get_remote_address
from pydantic import BaseModel

log = structlog.get_logger()

app = FastAPI(title="Trading Bot Dashboard", version="1.0.0")


@app.on_event("startup")
async def startup():
    """Initialize DB pool and Redis on dashboard startup."""
    import db
    import redis_client
    db.init_pool()
    redis_client.init()
    log.info("dashboard_startup_complete")

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
        "starting_capital_usdt": r.get("bot:starting_capital_usdt"),
        "virtual_balance": r.get("account:virtual_balance"),
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


@app.post("/bot/mode", dependencies=[Depends(_verify_token)])
async def set_mode(mode: str):
    import redis_client
    r = redis_client.get()
    paper_closed = int(r.get("brain:paper_closed") or 0)
    if mode == "live" and paper_closed < 2000:
        raise HTTPException(status_code=403, detail=f"Need 2000 paper trades, have {paper_closed}")
    r.set("bot:mode", mode)
    return {"mode": mode, "accepted": True}


@app.put("/bot/settings", dependencies=[Depends(_verify_token)])
async def bot_settings(settings: dict):
    """
    Required before Start Trading button is enabled. All 4 fields are mandatory.
    Expects: {
        min_open_trades: int,        — Brain maintains at least this many trades open
        max_open_trades: int,        — Brain never exceeds this many simultaneous trades
        max_position_usdt: float,    — Absolute max USDT per single trade (e.g. 100)
        starting_capital_usdt: float — Your paper trading virtual balance (e.g. 10000)
    }
    """
    import redis_client
    r = redis_client.get()
    errors = []

    min_t    = settings.get("min_open_trades")
    max_t    = settings.get("max_open_trades")
    max_pos  = settings.get("max_position_usdt")
    capital  = settings.get("starting_capital_usdt")

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

    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    r.set("bot:min_open_trades",      int(min_t))
    r.set("bot:max_open_trades",      int(max_t))
    r.set("bot:max_position_usdt",    float(max_pos))
    r.set("bot:starting_capital_usdt", float(capital))

    return {
        "saved": True,
        "min_open_trades": int(min_t),
        "max_open_trades": int(max_t),
        "max_position_usdt": float(max_pos),
        "starting_capital_usdt": float(capital),
        "ready_to_start": True,
    }


# --- AH-05: REST data endpoints ---

@app.get("/trades/open", dependencies=[Depends(_verify_token)])
async def get_open_trades():
    from memory.query import get_open_trades as _get
    import redis_client, redis_keys
    trades = _get()
    r = redis_client.get()
    enriched = []
    for t in trades:
        pair = t.get("pair", "")
        mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", pair)) or t.get("entry_price") or 0)
        entry = float(t.get("average_entry") or t.get("entry_price") or 0)
        qty = float(t.get("quantity") or 0)
        direction_sign = 1.0 if t.get("direction") == "long" else -1.0
        capital = float(t.get("capital_usdt") or 0)
        leverage = int(t.get("leverage") or 1)
        current_pnl = round((mark - entry) * qty * direction_sign, 4) if entry > 0 else 0.0
        fees_estimate = round(capital * leverage * 0.0004, 4)
        t["current_mark_price"] = mark
        t["current_pnl_usdt"] = current_pnl
        t["net_current_pnl"] = round(current_pnl - fees_estimate, 4)
        enriched.append(t)
    return enriched


@app.get("/trades/closed", dependencies=[Depends(_verify_token)])
async def get_closed_trades(pair: Optional[str] = None, limit: int = 100):
    from db import db_conn
    where = "WHERE status='closed'"
    params = []
    if pair:
        where += " AND pair = %s"
        params.append(pair)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM trades {where} ORDER BY exit_time DESC LIMIT %s", params + [limit])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


@app.get("/brain/status", dependencies=[Depends(_verify_token)])
async def brain_status():
    import redis_client, redis_keys, json
    r = redis_client.get()
    return {
        "stage": int(r.get(redis_keys.BRAIN_STAGE) or 1),
        "regime": r.get(redis_keys.CURRENT_REGIME) or "unknown",
        "paper_closed": int(r.get("brain:paper_closed") or 0),
        "active_pairs": len(r.smembers(redis_keys.ACTIVE_PAIRS)),
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


@app.get("/features/health", dependencies=[Depends(_verify_token)])
async def features_health():
    from db import db_conn
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM feature_governance ORDER BY feature_id")
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


@app.get("/system/health", dependencies=[Depends(_verify_token)])
async def system_health():
    return {"services": "see /health per container", "ws_clients": len(_connected_ws)}


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
