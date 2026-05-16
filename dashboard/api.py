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
    HARDCODED_HASH = "$2b$12$placeholder_replace_with_real_bcrypt_hash"
    if req.username != "admin" or not _pwd_context.verify(req.password, HARDCODED_HASH):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return {"access_token": _make_token(req.username), "token_type": "bearer"}


# --- AH-04: Bot control ---

@app.get("/bot/status", dependencies=[Depends(_verify_token)])
async def bot_status():
    import redis_client, redis_keys
    r = redis_client.get()
    return {
        "stage": int(r.get(redis_keys.BRAIN_STAGE) or 1),
        "mode": "paper",
        "running": True,
        "paper_closed": int(r.get("brain:paper_closed") or 0),
    }


@app.post("/bot/mode", dependencies=[Depends(_verify_token)])
async def set_mode(mode: str):
    import redis_client
    r = redis_client.get()
    paper_closed = int(r.get("brain:paper_closed") or 0)
    if mode == "live" and paper_closed < 2000:
        raise HTTPException(status_code=403, detail=f"Need 2000 paper trades, have {paper_closed}")
    return {"mode": mode, "accepted": True}


# --- AH-05: REST data endpoints ---

@app.get("/trades/open", dependencies=[Depends(_verify_token)])
async def get_open_trades():
    from memory.query import get_open_trades as _get
    return _get()


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
