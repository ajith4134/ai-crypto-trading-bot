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


def handle_ollama_failure(exc: Exception, task_name: str) -> dict:
    """Log failure, alert Redis, return sentinel so Brain uses ML-only this cycle."""
    log.error("ollama_failure", task=task_name, error=str(exc))
    try:
        r = redis_client.get()
        r.publish(
            redis_keys.CH_SYSTEM_EVENT,
            f'{{"event": "ollama_failure", "task": "{task_name}"}}',
        )
    except Exception:
        pass
    return ML_ONLY_SENTINEL


def handle_airllm_failure(exc: Exception, task_name: str) -> None:
    """Log AirLLM failure. Caller (Celery task) handles retry via Celery retry mechanism."""
    log.warning("airllm_failure", task=task_name, error=str(exc))
