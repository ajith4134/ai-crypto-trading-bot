"""
Section AG: Self-Healing & Auto-Restart Watchdog — AG-01 to AG-06.
"""
import asyncio
import json
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
import httpx
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()

_SERVICES = ["brain", "data_feed", "scanner", "web_intel", "dashboard", "celery_worker"]
_HEALTH_PORT = 8000
_POLL_INTERVAL = 30
_CRASH_WINDOW = 600
_MAX_CRASHES = 3

_crash_times: dict[str, list[float]] = defaultdict(list)
_unstable: set[str] = set()


async def _check_health(service: str) -> tuple[bool, dict]:
    """AG-01/AG-02: Poll /health endpoint of each service."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"http://{service}:{_HEALTH_PORT}/health")
            if r.status_code == 200:
                return True, r.json()
            return False, {}
    except Exception:
        return False, {}


def _restart_service(service: str) -> None:
    """AG-03: Auto-restart via docker compose."""
    log.warning("restarting_service", service=service)
    subprocess.run(
        ["docker", "compose", "restart", service],
        cwd="/opt/trading-bot",
        capture_output=True,
    )
    try:
        from notifications.telegram import send_critical
        send_critical(f"Watchdog restarted service: {service}")
    except Exception:
        pass


def _record_crash(service: str) -> bool:
    """AG-06: Track crash count in rolling 10-minute window. Return True if persistently unstable."""
    import time
    now = time.monotonic()
    cutoff = now - _CRASH_WINDOW
    _crash_times[service] = [t for t in _crash_times[service] if t > cutoff]
    _crash_times[service].append(now)

    if len(_crash_times[service]) >= _MAX_CRASHES:
        _unstable.add(service)
        log.error("service_persistently_unstable", service=service)
        try:
            from notifications.telegram import send_critical
            send_critical(
                f"CRITICAL: {service} crashed {_MAX_CRASHES}+ times in 10 min. "
                "Manual intervention required. Auto-restart suspended."
            )
        except Exception:
            pass
        return True
    return False


async def recover_state() -> None:
    """AG-05: Restore full bot state from PostgreSQL on startup/restart."""
    from memory.query import get_open_trades
    import redis_client as rc
    r = rc.get()

    open_trades = get_open_trades()
    for trade in open_trades:
        pair = trade["pair"]
        r.set(f"trade:{trade['id']}:sl", str(trade.get("trailing_sl_level") or 0))
        r.set(f"trade:{trade['id']}:dca_status", json.dumps(trade.get("dca_status") or {}))

    from db import db_conn
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT evolution_stage, active_feature_flags, feature_weights FROM brain_state WHERE id=1")
            row = cur.fetchone()
            if row:
                r.set(redis_keys.BRAIN_STAGE, row[0])
                r.set(redis_keys.BRAIN_ACTIVE_FLAGS, json.dumps(row[1] or {}))
                r.set(redis_keys.BRAIN_FEATURE_WEIGHTS, json.dumps(row[2] or {}))

    log.info("state_recovered", open_trades=len(open_trades))


async def watchdog_loop() -> None:
    """AG-02/AG-04: Poll all services every 30 seconds."""
    await recover_state()

    while True:
        for service in _SERVICES:
            if service in _unstable:
                continue

            healthy, data = await _check_health(service)

            if not healthy:
                if not _record_crash(service):
                    _restart_service(service)
            else:
                last_processed = data.get("last_processed")
                if last_processed:
                    from datetime import datetime
                    try:
                        ts = datetime.fromisoformat(last_processed)
                        stale_seconds = (datetime.now(timezone.utc) - ts.replace(tzinfo=timezone.utc)).total_seconds()
                        if stale_seconds > 300:
                            log.warning("service_stale", service=service, stale_seconds=stale_seconds)
                            if not _record_crash(service):
                                _restart_service(service)
                    except Exception:
                        pass

        await asyncio.sleep(_POLL_INTERVAL)


if __name__ == "__main__":
    from db import init_pool
    import redis_client
    init_pool()
    redis_client.init()
    asyncio.run(watchdog_loop())
