"""
I-03: Real-time Decision LLM — Debate Council moderator + final trade decision.

Provider order (Phase D, 2026-05-20):
  1. Ollama (Mistral 7B) — fast local path, used when its circuit breaker is
     not in cooldown. Honors handle_ollama_failure / ollama_in_cooldown.
  2. Cloud chain (Groq → Cerebras → SambaNova) — when Ollama is unhealthy OR
     this individual call fails. Same chain as llm/researcher.py per D-01.

Returns: dict parsed from the LLM's JSON output.
Raises: Exception when both Ollama AND every cloud provider fail / are in
        cooldown. Callers (debate/council.py) wrap with
        `asyncio.gather(..., return_exceptions=True)` and treat exceptions as
        per-agent fallback positions — preserved behaviour from prior version.

Why cloud fallback in the real-time path (extends D-01):
  Phase C (debate rounds 2/3 + verbal reinforcement) was wired correctly but
  could not be observed in production because Ollama had been in 5-minute
  cooldown loops for most of 2026-05-20. The all-failed fallback path
  ('debate_no_llm_full_allocation') triggered on every signal, so no debate
  arguments were persisted and no agent weights ever updated. Adding cloud
  fallback to decide() — the only thing that distinguished it from
  researcher() — closes that observability gap and gives the debate council
  the same provider robustness the background tasks already had.

Cost / rate guard:
  Cloud chain is hit at most 3 times per agent prompt (one per provider).
  Per-provider cooldown (llm/providers.mark_cooldown) prevents a 429-storm
  from any one provider. With 3 agents × up to 3 rounds = 9 calls per signal
  in the worst case, and Groq's 30 RPM free tier as the primary, the
  combined chain handles burst load without issue. Observed during smoke
  test: ~500ms per call to Groq, well within debate's 30s timeout.
"""
import asyncio
import json
import time
import aiohttp
import structlog

from llm.guard import assert_no_reflection
from llm.fallback import (
    handle_ollama_failure, reset_ollama_health, ollama_in_cooldown,
)
from llm.providers import call_chain, extract_json_dict
import config

log = structlog.get_logger()

_TASK_NAME = "decide"


async def _call_ollama(prompt: str, timeout: int) -> dict:
    """Original Ollama path. Mistral 7B with format=json. Raises on network /
    timeout failure so the caller can fall through to the cloud chain."""
    payload = {
        "model": config.llm.decision_model,
        "prompt": prompt,
        "format": "json",
        "stream": False,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{config.llm.ollama_url}/api/generate",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return json.loads(data["response"])


def _mark_evidence(provider: str, elapsed_s: float | None = None) -> None:
    """Durable evidence keys so feature_health and dashboards can see which
    provider is currently serving real-time decision LLM calls."""
    try:
        import time as _t
        import redis_client as _rc
        r = _rc.get()
        r.incr("llm:decision_calls_count")
        r.incr(f"llm:decision_calls_by_provider:{provider}")
        r.set("llm:decision_last_provider", provider)
        r.set("llm:decision_last_ts", str(int(_t.time())))
        if provider == "ollama":
            # dashboard/api.py checks llm:ollama:last_success_ts (written by
            # ollama_client.py for research calls) to determine degraded status.
            # Decision calls use a separate aiohttp path that never wrote this
            # key, causing the dashboard to always show ollama as degraded even
            # when 60+ successful decide() calls had been made.
            r.incr("llm:ollama:success_count")
            r.set("llm:ollama:last_success_ts", str(int(_t.time())))
            if elapsed_s is not None:
                r.set("llm:ollama:last_elapsed_s", str(elapsed_s))
    except Exception:
        pass


async def decide(prompt: str, timeout: int = 120) -> dict:
    """Run a JSON-output LLM call via Ollama → cloud cascade.

    Signature unchanged from prior version so debate/council.py and any other
    callers continue to work without modification.
    """
    assert_no_reflection(prompt)

    # ── 1) Ollama, only if its circuit is healthy ────────────────────────────
    if not ollama_in_cooldown(_TASK_NAME):
        try:
            _t0 = time.time()
            result = await _call_ollama(prompt, timeout)
            if isinstance(result, dict):
                reset_ollama_health(_TASK_NAME)
                _mark_evidence("ollama", elapsed_s=round(time.time() - _t0, 1))
                return result
            # Mistral returned non-dict (e.g., a list) — treat as a parse
            # failure and fall through to cloud.
            raise ValueError(f"ollama_non_dict_response: {type(result).__name__}")
        except (aiohttp.ClientError, asyncio.TimeoutError,
                json.JSONDecodeError, ValueError) as exc:
            # Failure: bump circuit-breaker state but don't return the
            # ML_ONLY_SENTINEL yet — give the cloud chain a chance first.
            handle_ollama_failure(exc, _TASK_NAME)
            log.info("decide_ollama_failed_trying_cloud",
                     error=f"{type(exc).__name__}: {str(exc)[:200]}")
    else:
        log.debug("decide_ollama_circuit_open_using_cloud")

    # ── 2) Cloud chain (sync requests via to_thread to keep this async) ──────
    # Match the per-agent timeout for the cloud call too — debate uses 30s,
    # other callers may use larger. Provider chain still has its own 60s
    # outer cap to bound any single provider request.
    try:
        cloud_timeout = max(10, min(timeout, 60))
        provider, text = await asyncio.to_thread(
            call_chain, prompt, 512, True, cloud_timeout,
        )
        parsed = extract_json_dict(text)
        _mark_evidence(provider)
        return parsed
    except Exception as exc:
        log.warning("decide_cloud_chain_failed",
                    error=f"{type(exc).__name__}: {str(exc)[:200]}")
        raise
