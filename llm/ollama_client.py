"""Local Ollama HTTP client — cont. 38 LLM redesign.

Blueprint §8.10 (revised 2026-05-22): all background LLM tasks are
local-primary via Ollama; cloud providers are an opt-in burst escape only.
This module wraps the Ollama HTTP chat endpoint and exposes the same
text-in / text-out contract as the prior cloud-chain call_chain so the
caller (llm/researcher.py::research) can fall back transparently.

Why a separate file: keeps the cloud chain in llm/providers.py untouched
so the fallback path remains intact when local fails.
"""
from __future__ import annotations

import os
import time
import structlog
import requests

log = structlog.get_logger()

_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
_DEFAULT_MODEL = os.environ.get("OLLAMA_RESEARCH_MODEL", "mistral:7b")
# Mistral 7B on a 4-core CPU runs at ~5 tok/s. A 512-token generation needs
# ~100s steady-state. Cold-start adds ~18s for model load on first call.
# Set client timeout to 300s so a cold-start + full generation still fits.
# The server-side Ollama request can also exceed 2 min for long generations;
# our token cap below keeps generations within Ollama's own limits.
_DEFAULT_TIMEOUT_S = 300


def chat_ollama(prompt: str,
                model: str | None = None,
                max_tokens: int = 512,
                timeout_s: int | None = None) -> str:
    """Synchronous Ollama chat call. Returns the raw model text.

    Raises RuntimeError on transport/HTTP failure — caller handles by
    falling back to cloud or returning a sentinel.

    `model` defaults to `OLLAMA_RESEARCH_MODEL` env (mistral:7b by default).
    Pass `model="phi3:mini"` for faster, less capable inference (e.g.
    decoder classifications)."""
    mdl = model or _DEFAULT_MODEL
    # Cap token budget to keep generation under the Ollama request window.
    # Tasks that legitimately need >384 tokens should split the call.
    num_predict = min(int(max_tokens), 384)
    t0 = time.time()
    try:
        resp = requests.post(
            f"{_OLLAMA_HOST}/api/chat",
            json={
                "model": mdl,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"num_predict": num_predict,
                            "temperature": 0.7},
                # keep_alive=10m so the model stays warm across calls and
                # we don't pay the 18s cold-start penalty repeatedly.
                "keep_alive": "10m",
            },
            timeout=timeout_s or _DEFAULT_TIMEOUT_S,
        )
    except Exception as exc:
        raise RuntimeError(f"ollama_transport_failed: {str(exc)[:160]}")

    if resp.status_code != 200:
        raise RuntimeError(
            f"ollama_status_{resp.status_code}: {resp.text[:160]}")

    payload = resp.json()
    text = (payload.get("message") or {}).get("content") or ""
    elapsed = round(time.time() - t0, 1)
    log.info("ollama_chat_ok",
             model=mdl, elapsed_s=elapsed,
             prompt_chars=len(prompt), reply_chars=len(text))
    # Bump health counter for the dashboard / governance.
    try:
        import redis_client as _rc
        r = _rc.get()
        r.incr("llm:ollama:success_count")
        r.set("llm:ollama:last_success_ts", int(time.time()))
        r.set("llm:ollama:last_elapsed_s", elapsed)
    except Exception:
        pass
    return text
