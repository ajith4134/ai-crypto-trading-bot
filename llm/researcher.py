"""
I-04: Background-intelligence LLM entry point.

Blueprint §8.10 (revised cont. 38, 2026-05-22): local-primary via Ollama
with cloud burst as fallback. Order:

  1. Ollama  (mistral:7b)        — DEFAULT. Local, no rate limits.
  2. Cloud chain (llm/providers.py)   — only when local fails AND the
                                       caller hasn't disabled it.

Historical record of prior designs (DO NOT revive without re-reading
PROGRESS.md cont. 38 and the blueprint redesign note):
  - First spec: Llama 3.1 70B via AirLLM (local, layer-by-layer).
    Never deployed.
  - Second attempt: llama-cpp-python with 42.5 GB GGUF. Failed to load
    on the 16 GB host (1007 container restarts observed).
  - Third attempt: 5-provider cloud chain (groq/cerebras/nvidia/
    sambanova/mistral). Hit cascading rate-limit 429s daily, killing
    research / decode / OPRO loops.
  - Cont. 38 redesign: local Ollama mistral:7b (already deployed, ~5-
    30s per call) becomes the primary path. Cloud chain retained as
    fallback only.
  - Cont. 65k redesign: CLOUD-PRIMARY (this file). The cloud chain now
    has mature cooldown-aware rotation (staggered TTLs + proactive RPM
    headroom skip) that survives the 429s that killed attempt #3, and
    Ollama is kept as the guaranteed local fallback so jobs never stall.
    Motivation: free ~5 GB host RAM by keeping Ollama idle/unloaded.

MUST only be called from Celery background workers — never from the live
trading loop.
"""
import structlog
from llm.guard import assert_no_reflection
from llm.ollama_client import chat_ollama
from llm.providers import call_chain
from llm.fallback import handle_airllm_failure

log = structlog.get_logger()


def research(prompt: str, max_new_tokens: int = 512,
             allow_cloud_fallback: bool = True) -> str:
    """Background LLM call — cloud-primary, Ollama fallback (cont. 65k).

    Tries the cloud chain first (cooldown-aware rotation across 6 providers).
    Falls back to local Ollama (mistral:7b) only when every cloud provider is
    in cooldown or failed — so jobs never stall, and Ollama stays mostly idle
    (unloads after KEEP_ALIVE, freeing host RAM).

    Reversible: set Redis `llm:cloud_primary = "0"` to restore the cont. 38
    local-first order.

    `allow_cloud_fallback=False` is honoured as LOCAL-ONLY: high-volume
    quota-sensitive loops (e.g. decoders) go straight to Ollama and never
    touch cloud, in either order mode — this preserves the cont. 38 guard
    that kept those loops from burning the free tiers out in minutes.

    On total failure, raises — Celery's retry semantics apply at the
    task layer. Trading is never gated on this call.
    """
    assert_no_reflection(prompt)

    def _incr(key: str) -> None:
        try:
            import redis_client as _rc
            _rc.get().incr(key)
        except Exception:
            pass

    # Caller forbids cloud → local Ollama only (high-volume quota-sensitive
    # loops). Fails loud on Ollama outage; never silently routes to cloud.
    if not allow_cloud_fallback:
        return chat_ollama(prompt, max_tokens=max_new_tokens)

    def _cloud_primary() -> bool:
        try:
            import redis_client as _rc
            v = _rc.get().get("llm:cloud_primary")
            v = v.decode() if isinstance(v, bytes) else v
            return (v if v is not None else "1") == "1"   # default cloud-first
        except Exception:
            return True

    if _cloud_primary():
        # Path 1: cloud chain (cooldown-aware rotation across 6 providers).
        try:
            _, text = call_chain(prompt, max_tokens=max_new_tokens,
                                 json_mode=False)
            _incr("llm:cloud:success_count")
            return text
        except Exception as exc:
            # all_llm_providers_in_cooldown (or all failed) → local fallback.
            log.info("research_cloud_exhausted_fallback_ollama",
                     error=str(exc)[:160])
            _incr("llm:cloud:exhausted_count")
        # Path 2: local Ollama — only when every cloud provider is cooled/failed.
        return chat_ollama(prompt, max_tokens=max_new_tokens)

    # Legacy local-primary order (llm:cloud_primary = "0").
    try:
        return chat_ollama(prompt, max_tokens=max_new_tokens)
    except Exception as exc:
        log.warning("research_ollama_failed", error=str(exc)[:160], fallback="cloud")
        _incr("llm:ollama:fail_count")
    try:
        _, text = call_chain(prompt, max_tokens=max_new_tokens, json_mode=False)
        return text
    except Exception as exc:
        handle_airllm_failure(exc, "research")
        raise
