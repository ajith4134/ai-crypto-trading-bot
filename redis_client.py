import redis
from redis.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as _RedisConnError, TimeoutError as _RedisTimeoutError
import structlog
import config

log = structlog.get_logger()

_client: redis.Redis | None = None


def _build() -> redis.Redis:
    # cont. 74 — SELF-HEALING client. The old client had NO reconnection config, so
    # when the redis container was recreated and got a new IP, every pooled connection
    # held a dead socket → ConnectionError → loops that didn't catch it DIED silently
    # (the data_feed zombie: process up, loop dead, Docker shows healthy for 13h).
    #   health_check_interval — PING an idle connection before reuse; a failed ping
    #     discards it and a fresh connection RE-RESOLVES the `redis` hostname → new IP.
    #   socket_keepalive — TCP keepalive surfaces dead peers instead of hanging.
    #   retry + retry_on_error — transparently retry transient connection blips.
    # NOTE: deliberately NO socket_timeout (would break legitimate blocking/slow ops);
    # keepalive + health-check handle dead-connection detection without that risk.
    return redis.Redis(
        host=config.REDIS_HOST,
        port=config.REDIS_PORT,
        decode_responses=True,
        socket_keepalive=True,
        socket_connect_timeout=5,
        health_check_interval=30,
        retry=Retry(ExponentialBackoff(cap=10, base=0.5), retries=3),
        retry_on_error=[_RedisConnError, _RedisTimeoutError],
    )


def init() -> None:
    global _client
    _client = _build()
    _client.ping()
    log.info("redis_ready", host=config.REDIS_HOST, port=config.REDIS_PORT)


def reinit() -> redis.Redis:
    """cont. 74 — force a fresh client + connection pool (new DNS resolution). Called
    by long-running supervisors after a crash so a stale-IP pool can't wedge a loop."""
    global _client
    try:
        if _client is not None:
            _client.close()
    except Exception:
        pass
    _client = _build()
    log.info("redis_reinit", host=config.REDIS_HOST, port=config.REDIS_PORT)
    return _client


def get() -> redis.Redis:
    global _client
    if _client is None:
        # Auto-initialise in Celery worker child processes (they don't run startup code)
        init()
    return _client
