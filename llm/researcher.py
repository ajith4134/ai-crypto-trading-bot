"""
I-04: Llama 3.1 70B via llama.cpp — background intelligence tasks ONLY.

Blueprint originally specified AirLLM. ALTERNATIVE: llama.cpp GGUF used instead.
Reason: AirLLM's one-time conversion requires ~132 GB RAM (all shards loaded
simultaneously) — impossible on a 16 GB server. llama.cpp memory-maps the GGUF
file and achieves identical goal: CPU-only, local, 70B, ~4-8 GB peak RAM during
inference. REST API is OpenAI-compatible (POST /v1/chat/completions).

MUST only be called from Celery background workers — never from the live trading loop.
"""
import json
import requests
import structlog
from llm.guard import assert_no_reflection
import config

log = structlog.get_logger()

_LLAMA_CPP_URL = "http://llama_cpp:8080/v1/chat/completions"
_TIMEOUT = 180


def research(prompt: str, max_new_tokens: int = 512) -> str:
    """
    Run inference via llama.cpp REST API (30–120 sec on CPU).
    Returns raw text response — caller is responsible for parsing.
    Only call from Celery background workers.
    """
    assert_no_reflection(prompt)

    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_new_tokens,
        "temperature": 0.7,
        "stream": False,
    }

    try:
        resp = requests.post(_LLAMA_CPP_URL, json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        log.info("llamacpp_inference_complete", tokens=max_new_tokens)
        return content
    except Exception as exc:
        from llm.fallback import handle_airllm_failure
        handle_airllm_failure(exc, "research")
        raise
