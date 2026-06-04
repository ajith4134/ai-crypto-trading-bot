"""
Section AG: Self-Healing & Auto-Restart Watchdog — AG-01 to AG-06.
"""
import asyncio
import json
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
    """AG-03: Auto-restart via Docker SDK (docker socket mounted at /var/run/docker.sock)."""
    log.warning("restarting_service", service=service)
    try:
        import docker as docker_sdk
        client = docker_sdk.from_env()
        containers = client.containers.list(all=True, filters={"name": f"trading-bot-{service}-1"})
        if containers:
            containers[0].restart()
            log.info("service_restarted", service=service)
        else:
            log.warning("container_not_found", service=service)
    except Exception as exc:
        log.error("restart_failed", service=service, error=str(exc))
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


def _apply_mode_change(payload: dict) -> dict:
    """cont. 47 — Execute a paper↔live mode switch requested via Redis.

    Steps:
      1. Validate the bot is stopped + flat
      2. Rewrite .env (TRADING_MODE + BINANCE_TESTNET)
      3. Restart brain, celery_worker, data_feed
      4. Wait for brain to come back up
      5. Initialise virtual_balance via the CORRECT redis key
      6. Set bot:mode_change_result + bot:mode_change_status='complete'
    """
    import os, time as _t
    import redis_client as _rc
    import redis_keys as _rk
    r = _rc.get()
    result: dict = {"steps": []}

    # Sanity — bot stopped
    if r.get("bot:running") == "1":
        result["error"] = "bot_running"
        return result

    # Sanity — flat (DB open trades = 0). Watchdog's DB pool may not be inited
    # in all paths; guard with try/except.
    try:
        from memory.query import get_open_trades
        ot = get_open_trades()
        if ot:
            result["error"] = f"open_trades={len(ot)} — close all first"
            return result
        result["steps"].append("flat_verified")
    except Exception as exc:
        result["steps"].append(f"flat_check_skipped: {str(exc)[:120]}")

    # 1) Rewrite .env
    env_path = "/app/.env"  # mounted from host /opt/trading-bot/.env
    if not os.path.exists(env_path):
        result["error"] = f".env not mounted at {env_path}"
        return result

    target_trading = payload.get("trading_mode", "paper")
    target_testnet = payload.get("binance_testnet", "true")
    try:
        with open(env_path, "r") as fh:
            lines = fh.readlines()
        new_lines = []
        replaced_mode = replaced_tn = False
        for line in lines:
            if line.startswith("TRADING_MODE="):
                new_lines.append(f"TRADING_MODE={target_trading}\n")
                replaced_mode = True
            elif line.startswith("BINANCE_TESTNET="):
                new_lines.append(f"BINANCE_TESTNET={target_testnet}\n")
                replaced_tn = True
            else:
                new_lines.append(line)
        if not replaced_mode:
            new_lines.append(f"TRADING_MODE={target_trading}\n")
        if not replaced_tn:
            new_lines.append(f"BINANCE_TESTNET={target_testnet}\n")
        with open(env_path, "w") as fh:
            fh.writelines(new_lines)
        result["steps"].append("env_rewritten")
    except Exception as exc:
        result["error"] = f"env_write_failed: {str(exc)[:200]}"
        return result

    # 2) RECREATE containers via docker SDK so they pick up the new .env.
    # `container.restart()` is NOT enough — env_file vars are loaded at
    # container CREATION time, so a soft restart keeps the old env. We must
    # `remove + create` (equivalent to `docker compose up -d --force-recreate`).
    # docker-py doesn't expose compose semantics directly; instead we shell
    # out to the docker CLI via subprocess (the watchdog has docker socket
    # access via group_add 1001).
    try:
        import subprocess
        # cont. 69m FIX — the watchdog runs inside a container, so compose's
        # relative bind-mount sources (`./memory`, `./signals`, …) must resolve
        # against the HOST project path, NOT the container cwd. The old call used
        # cwd=/app with no --project-directory → compose resolved `./memory` to
        # `/app/memory` on the HOST (empty) → every bind-mount broke → the brain
        # crash-looped on `ModuleNotFoundError: memory.query` (cont.69 live-switch
        # incident). `--project-directory /opt/trading-bot` makes ./ resolve to the
        # real host paths. `-f /app/docker-compose.yml` reads the (now-mounted,
        # current) compose. Naming only the 3 services + current compose also stops
        # the accidental redis/ollama recreation seen before.
        cmd = ["docker", "compose",
               "--project-directory", "/opt/trading-bot",
               "-f", "/app/docker-compose.yml",
               "-p", "trading-bot",
               "up", "-d", "--force-recreate",
               "brain", "celery_worker", "data_feed"]
        proc = subprocess.run(cmd, cwd="/app",
                              capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            result["steps"].append("recreated_brain_celery_data_feed")
        else:
            # Fallback: do soft restart of each via docker-py — won't pick up
            # env changes, but better than nothing.
            log.error("compose_recreate_failed",
                      stderr=proc.stderr[:300],
                      stdout=proc.stdout[:300])
            import docker as docker_sdk
            client = docker_sdk.from_env()
            for svc in ("brain", "celery_worker", "data_feed"):
                try:
                    conts = client.containers.list(
                        all=True, filters={"name": f"trading-bot-{svc}-1"})
                    if conts:
                        conts[0].restart()
                        result["steps"].append(f"fallback_restart_{svc}")
                except Exception as exc:
                    result["steps"].append(
                        f"fallback_restart_{svc}_failed: {str(exc)[:120]}")
    except Exception as exc:
        result["error"] = f"recreate_failed: {str(exc)[:200]}"
        return result

    # 3) Wait for brain health (poll its /health, max 60s)
    waited = 0
    while waited < 60:
        _t.sleep(2)
        waited += 2
        try:
            import httpx
            resp = httpx.get("http://brain:8000/health", timeout=3)
            if resp.status_code == 200:
                result["steps"].append(f"brain_healthy_after_{waited}s")
                break
        except Exception:
            continue
    else:
        result["steps"].append("brain_health_timeout")

    # 4) Initialise virtual_balance via the CORRECT redis key
    starting_capital = payload.get("starting_capital")
    if starting_capital is not None and starting_capital > 0:
        try:
            r.set(_rk.VIRTUAL_BALANCE, float(starting_capital))
            result["steps"].append(
                f"virtual_balance_set={float(starting_capital)}")
        except Exception as exc:
            result["steps"].append(f"vb_set_failed: {str(exc)[:120]}")

    # 5) Mark complete
    import json as _j
    result["completed_at"] = int(_t.time())
    r.set("bot:mode_change_status", "complete")
    r.set("bot:mode_change_result", _j.dumps(result))
    r.delete("bot:mode_change_pending")

    log.warning("mode_change_complete", **{k: v for k, v in result.items()
                                            if k != "steps"})
    try:
        from notifications.telegram import send_critical
        send_critical(
            f"Mode switch complete → {payload.get('target')}.\n"
            f"Brain restarted, virtual_balance set to {starting_capital}.")
    except Exception:
        pass
    return result


async def _mode_change_watcher() -> None:
    """cont. 47 — Watch for /bot/mode_switch requests from the dashboard
    and execute them. Runs forever alongside the health watchdog loop."""
    import redis_client as _rc
    import json as _j
    r = _rc.get()
    while True:
        try:
            raw = r.get("bot:mode_change_pending")
            if raw:
                try:
                    payload = _j.loads(raw)
                except Exception:
                    payload = None
                if payload:
                    # cont. 66 — process each request EXACTLY ONCE. Previously the
                    # pending key was deleted only on the SUCCESS path (_apply_mode_
                    # change line 245), so ANY failure (e.g. bot_running, open_trades,
                    # recreate_failed) returned early WITHOUT clearing it → this watcher
                    # reprocessed the same request every 5s forever (infinite error
                    # loop that also wedged all future switches). Delete up-front;
                    # failures now report via bot:mode_change_status and the user
                    # re-submits after fixing the precondition.
                    r.delete("bot:mode_change_pending")
                    r.set("bot:mode_change_status", "in_progress")
                    log.warning("mode_change_picked_up", **payload)
                    try:
                        result = _apply_mode_change(payload)
                        if result.get("error"):
                            r.set("bot:mode_change_status", "failed")
                            r.set("bot:mode_change_result",
                                  _j.dumps(result))
                            log.error("mode_change_failed",
                                      error=result["error"])
                    except Exception as exc:
                        r.set("bot:mode_change_status", "failed")
                        r.set("bot:mode_change_result",
                              _j.dumps({"error": str(exc)[:300]}))
                        log.error("mode_change_exception",
                                  error=str(exc)[:300])
        except Exception as exc:
            log.warning("mode_change_watcher_iter_failed",
                        error=str(exc)[:200])
        await asyncio.sleep(5)


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

        # Heartbeat — 120s TTL so key expires automatically if watchdog dies
        try:
            import redis_client as _rc
            _rc.get().setex("watchdog:alive", 120, "1")
        except Exception:
            pass
        await asyncio.sleep(_POLL_INTERVAL)


_RETRAIN_POLL_INTERVAL = 3600   # check once per hour
_RETRAIN_BACKOFF_SEC   = 6 * 3600   # don't refire a retrain more than 1×/6h
_MODELS_DIR = "/app/models"

# Per-model spec: (file_name, max_age_hours, celery_task_name, redis_dedupe_tag)
# max_age_hours mirrors what dashboard/api.py uses to mark "stale" so the
# watchdog and dashboard never disagree about what "fresh" means.
_RETRAIN_SPECS = [
    ("hmm_regime.pkl", 24,  "celery_app.retrain_hmm",          "hmm"),
    ("tft.pth",        48,  "celery_app.retrain_tft",          "tft"),
    ("patchtst.pth",   72,  "celery_app.retrain_patchtst",     "patchtst"),
    ("gnn.pth",        48,  "celery_app.retrain_gnn",          "gnn"),
    # Weekly retrains — refire only if the file is older than ~10 days (168h × 1.5)
    # so the cron path remains primary and the watchdog only kicks in after a
    # missed run.
    ("candlenet_1m.pth",  240, "celery_app.retrain_candlenet_1m",  "candlenet_1m"),
    ("candlenet_5m.pth",  240, "celery_app.retrain_candlenet_5m",  "candlenet_5m"),
    ("candlenet_15m.pth", 240, "celery_app.retrain_candlenet_15m", "candlenet_15m"),
    ("marl_day_agent.zip",    240, "celery_app.retrain_marl_day",    "marl_day"),
    ("marl_minute_agent.zip", 240, "celery_app.retrain_marl_minute", "marl_minute"),
]


def _file_age_hours(file_name: str) -> float | None:
    import os
    path = os.path.join(_MODELS_DIR, file_name)
    try:
        st = os.stat(path)
    except OSError:
        return None
    import time
    return (time.time() - st.st_mtime) / 3600.0


def _dispatch_retrain(task_name: str, tag: str) -> tuple[bool, str]:
    """Send the retrain task via celery. Returns (fired, reason)."""
    try:
        from celery_app import app as celery_app
        celery_app.send_task(task_name, queue="default")
        return True, "dispatched"
    except Exception as exc:
        return False, f"send_failed:{str(exc)[:120]}"


async def _retrain_watchdog_loop() -> None:
    """Check ML model file mtimes every hour. Trigger retrains for any model
    whose file age exceeds its max_age_hours threshold. 6h backoff per model
    so a stuck retrain doesn't loop. Emits counters per
    [[feedback_silent_rejection]] so the never-firing path is observable.

    Closes cont. 61 finding: cron-based retrains miss their window when
    celery_beat restarts before the scheduled minute. This watchdog catches
    up stale models regardless of beat history.
    """
    import time
    import redis_client as _rc
    r = _rc.get()
    log.info("retrain_watchdog_started",
             poll_interval=_RETRAIN_POLL_INTERVAL,
             tracked_models=len(_RETRAIN_SPECS))
    while True:
        try:
            now = int(time.time())
            for file_name, max_age, task_name, tag in _RETRAIN_SPECS:
                age_h = _file_age_hours(file_name)
                if age_h is None:
                    # Missing file — fire once, then back off so a model that
                    # legitimately has no producer doesn't spam the worker.
                    last_fire = r.get(f"retrain_watchdog:{tag}:last_fire_ts")
                    if last_fire and (now - int(last_fire)) < _RETRAIN_BACKOFF_SEC:
                        r.incr(f"retrain_watchdog:{tag}:skipped_count")
                        r.set(f"retrain_watchdog:{tag}:last_skip_reason",
                              "missing_but_backoff_active")
                        continue
                    fired, reason = _dispatch_retrain(task_name, tag)
                    if fired:
                        r.setex(f"retrain_watchdog:{tag}:last_fire_ts",
                                _RETRAIN_BACKOFF_SEC, now)
                        r.incr(f"retrain_watchdog:{tag}:fire_count")
                        r.set(f"retrain_watchdog:{tag}:last_fire_reason",
                              "file_missing")
                        log.info("retrain_watchdog_fire", tag=tag,
                                 reason="file_missing", task=task_name)
                    else:
                        r.incr(f"retrain_watchdog:{tag}:dispatch_error_count")
                        r.set(f"retrain_watchdog:{tag}:last_skip_reason",
                              reason)
                    continue

                if age_h <= max_age:
                    r.incr(f"retrain_watchdog:{tag}:fresh_count")
                    continue

                # Stale — check backoff
                last_fire = r.get(f"retrain_watchdog:{tag}:last_fire_ts")
                if last_fire and (now - int(last_fire)) < _RETRAIN_BACKOFF_SEC:
                    r.incr(f"retrain_watchdog:{tag}:skipped_count")
                    r.set(f"retrain_watchdog:{tag}:last_skip_reason",
                          f"backoff_active:age={round(age_h,1)}h")
                    continue

                fired, reason = _dispatch_retrain(task_name, tag)
                if fired:
                    r.setex(f"retrain_watchdog:{tag}:last_fire_ts",
                            _RETRAIN_BACKOFF_SEC, now)
                    r.incr(f"retrain_watchdog:{tag}:fire_count")
                    r.set(f"retrain_watchdog:{tag}:last_fire_reason",
                          f"stale:age={round(age_h,1)}h>threshold={max_age}h")
                    log.info("retrain_watchdog_fire", tag=tag,
                             age_hours=round(age_h, 1),
                             threshold=max_age, task=task_name)
                else:
                    r.incr(f"retrain_watchdog:{tag}:dispatch_error_count")
                    r.set(f"retrain_watchdog:{tag}:last_skip_reason", reason)
                    log.warning("retrain_watchdog_dispatch_failed",
                                tag=tag, reason=reason)
        except Exception as exc:
            log.warning("retrain_watchdog_iter_failed",
                        error=str(exc)[:200])
        # Heartbeat key with 2×poll TTL so dashboards can detect a stuck loop.
        try:
            r.setex("retrain_watchdog:alive", 2 * _RETRAIN_POLL_INTERVAL, "1")
        except Exception:
            pass
        await asyncio.sleep(_RETRAIN_POLL_INTERVAL)


async def _main() -> None:
    """Run health watchdog + mode-change watcher + retrain watchdog concurrently."""
    await asyncio.gather(
        watchdog_loop(),
        _mode_change_watcher(),
        _retrain_watchdog_loop(),
        return_exceptions=True,
    )


if __name__ == "__main__":
    from db import init_pool
    import redis_client
    init_pool()
    redis_client.init()
    asyncio.run(_main())
