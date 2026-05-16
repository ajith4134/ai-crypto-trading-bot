"""
I-05: Routes each LLM task to the correct runtime.

Time-critical (Ollama): routing, debate, decision
Background (llama.cpp via Celery): strategy_research, opro_optimization,
    ai_scientist, dgm_code_rewriting, sleep_consolidation, web_intel_interpretation

NOTE: Blueprint specified AirLLM for background tasks. Replaced with llama.cpp
GGUF (Q4_K_M) — same model (70B), same goal, CPU-only, no conversion needed.
"""
import asyncio
import structlog
from llm.guard import assert_no_reflection
import config

log = structlog.get_logger()


async def route(task_name: str, prompt: str, **kwargs):
    """
    Dispatch prompt to correct LLM runtime based on task_name.
    Background tasks must be submitted as Celery tasks by the caller — this
    function only handles real-time Ollama routing.
    """
    assert_no_reflection(prompt)

    if task_name in config.llm.llamacpp_tasks:
        raise ValueError(
            f"Task '{task_name}' is a llama.cpp background task. "
            "Submit it via Celery, not direct route() call."
        )

    if task_name == "routing":
        from llm.router import classify
        return await classify(prompt, **kwargs)
    elif task_name in ("debate", "decision"):
        from llm.decision import decide
        return await decide(prompt, **kwargs)
    else:
        log.warning("unknown_llm_task", task=task_name)
        from llm.router import classify
        return await classify(prompt, **kwargs)
