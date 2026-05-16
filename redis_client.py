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
    if _client is None:
        raise RuntimeError("Redis not initialised — call init() at startup")
    return _client
