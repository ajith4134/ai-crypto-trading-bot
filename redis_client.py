import redis
import structlog
import config

log = structlog.get_logger()

_client: redis.Redis | None = None


def init() -> None:
    global _client
    _client = redis.Redis(
        host=config.REDIS_HOST,
        port=config.REDIS_PORT,
        decode_responses=True,
    )
    _client.ping()
    log.info("redis_ready", host=config.REDIS_HOST, port=config.REDIS_PORT)


def get() -> redis.Redis:
    global _client
    if _client is None:
        # Auto-initialise in Celery worker child processes (they don't run startup code)
        init()
    return _client
