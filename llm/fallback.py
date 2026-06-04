"""
I-06: LLM failure handlers.

Ollama failure → ML-only mode for this cycle; trading continues.
AirLLM busy/error → background task queues and retries; trading unaffected.
"""
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()

# Sentinel returned when Ollama is unavailable
ML_ONLY_SENTINEL = {"mode": "ml_only", "llm_available": False}

# Circuit breaker: after this many back-to-back failures, skip ollama for
# _COOLDOWN_SECONDS so the brain loop stops hammering an unhealthy backend
# every 14s. Cooldown is keyed by task_name so router and decision are tracked
# independently.
_FAIL_THRESHOLD = 3
_COOLDOWN_SECONDS = 300
_FAIL_KEY = "ollama:consecutive_failures:{task}"
_COOLDOWN_KEY = "ollama:cooldown_until:{task}"


def _fmt_exc(exc: Exception) -> str:
    """str(exc) is empty for some exceptions (asyncio.TimeoutError). Include type."""
    s = str(exc)
    return f"{type(exc).__name__}: {s}" if s else type(exc).__name__


def ollama_in_cooldown(task_name: str) -> bool:
    """Caller (brain) should check before attempting an ollama request."""
    try:
        return bool(redis_client.get().get(_COOLDOWN_KEY.format(task=task_name)))
    except Exception:
        return False


def reset_ollama_health(task_name: str) -> None:
    """Call on a successful ollama response to clear the failure counter."""
    try:
        r = redis_client.get()
        r.delete(_FAIL_KEY.format(task=task_name))
        cooldown_key = _COOLDOWN_KEY.format(task=task_name)
        if r.get(cooldown_key):
            r.delete(cooldown_key)
            log.info("ollama_recovered", task=task_name)
    except Exception:
        pass


def handle_ollama_failure(exc: Exception, task_name: str) -> dict:
    """Log failure, alert Redis, return sentinel so Brain uses ML-only this cycle.
    After _FAIL_THRESHOLD consecutive failures, opens a _COOLDOWN_SECONDS circuit
    breaker so the caller can skip ollama entirely and stop log spam."""
    try:
        r = redis_client.get()
        fail_key = _FAIL_KEY.format(task=task_name)
        cooldown_key = _COOLDOWN_KEY.format(task=task_name)
        fails = r.incr(fail_key)
        r.expire(fail_key, _COOLDOWN_SECONDS * 2)

        if fails >= _FAIL_THRESHOLD and not r.get(cooldown_key):
            r.setex(cooldown_key, _COOLDOWN_SECONDS, "1")
            log.error("ollama_circuit_open",
                      task=task_name, error=_fmt_exc(exc),
                      cooldown_seconds=_COOLDOWN_SECONDS,
                      consecutive_failures=int(fails))
            r.publish(
                redis_keys.CH_SYSTEM_EVENT,
                f'{{"event": "ollama_circuit_open", "task": "{task_name}"}}',
            )
        elif fails < _FAIL_THRESHOLD:
            log.error("ollama_failure", task=task_name, error=_fmt_exc(exc),
                      consecutive_failures=int(fails))
            r.publish(
                redis_keys.CH_SYSTEM_EVENT,
                f'{{"event": "ollama_failure", "task": "{task_name}"}}',
            )
        # else: circuit already open → suppress per-failure log
    except Exception:
        log.error("ollama_failure", task=task_name, error=_fmt_exc(exc))
    return ML_ONLY_SENTINEL


def handle_airllm_failure(exc: Exception, task_name: str) -> None:
    """Log AirLLM failure. Caller (Celery task) handles retry via Celery retry mechanism."""
    log.warning("airllm_failure", task=task_name, error=_fmt_exc(exc))
