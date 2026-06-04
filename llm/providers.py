"""
Shared cloud LLM provider chain — used by BOTH:
  - llm/researcher.py (background research / OPRO / AI Scientist / DGM) — sync caller
  - llm/decision.py:decide (real-time debate council moderator) — async caller

Extracted from llm/researcher.py so both consumers share the same provider list,
the same per-provider Redis cooldown state, and the same call mechanics. The
two consumers differ only in how they invoke the chain (sync vs async via
asyncio.to_thread).

Per-provider Redis cooldown: when a provider returns 429/402/403 we set
`llm:provider_cooldown:<name>` with a 5-minute TTL so the rest of the chain
doesn't keep hitting an exhausted provider. 404 (model not found) gets a 24h
cooldown since the model name is wrong and won't recover until config change.

The blueprint deviation (cloud LLM instead of local AirLLM/llama.cpp) is
documented as D-01 in BLUEPRINT_COMPLIANCE_AUDIT.md. Wiring this same chain
into decide() extends D-01 to the real-time path — see PROGRESS.md cont. 7.
"""
import json
import os
import random
import time as _time_mod
import requests
import structlog

import config

log = structlog.get_logger()

_TIMEOUT = 60
# cont. 52: 300 → 75 s. Rate-limit windows for the free tiers are 60 s; the
# old 300 s pinned the provider out for 4× the window and meant a single
# burst kept the whole chain in cooldown long after limits had reset. 75 s
# is "1 window + 15 s safety". Jitter below adds 0-20 s on each set so
# providers exit cooldown at staggered times rather than as a synchronised
# wave (the LiteLLM Router community pattern).
_COOLDOWN_SECONDS = 75
_COOLDOWN_JITTER_MAX = 20
# Proactive RPM headroom: skip a provider before sending when its recent
# call rate within the rolling 60 s window is ≥ this fraction of free_rpm.
# Burning a 429 to discover we're out of budget costs a full cooldown TTL;
# this avoids that cost when the budget signal is already in hand.
_RPM_HEADROOM_FRAC = 0.80


def get_providers() -> list[tuple[str, str, str, str]]:
    """Return [(name, url, model, api_key), ...] in priority order, skipping any
    with unset API keys. Order chosen by observed reliability + free-tier
    headroom — Groq has lowest latency, NVIDIA + Mistral have the largest free
    daily budgets, SambaNova as last resort.

    Combined free-tier budget at full chain (all 5 keys set):
      Groq          : 30 RPM / ~1k RPD     llama-3.3-70b-versatile
      Cerebras      : 30 RPM               qwen-3-235b-a22b-instruct-2507
      NVIDIA NIM    : ~40 RPM              meta/llama-3.3-70b-instruct
      Mistral       : ~60 RPM, 1B tok/mo   mistral-large-latest
      SambaNova     : ~30 RPM persistent   Meta-Llama-3.3-70B-Instruct
      ────────────────────────────────
      Total         : ~190 RPM, ~5k+ RPD

    Free-tier signup links (no payment required):
      NVIDIA NIM   : build.nvidia.com   (sign in, copy "API Key")
      Mistral      : console.mistral.ai (free workspace, La Plateforme tab)
    Set NVIDIA_API_KEY + MISTRAL_API_KEY in .env then restart brain to pick up.
    """
    providers: list[tuple[str, str, str, str]] = []
    if config.GROQ_API_KEY:
        providers.append((
            "groq",
            "https://api.groq.com/openai/v1/chat/completions",
            "llama-3.3-70b-versatile",
            config.GROQ_API_KEY,
        ))
    if config.CEREBRAS_API_KEY:
        providers.append((
            "cerebras",
            "https://api.cerebras.ai/v1/chat/completions",
            "qwen-3-235b-a22b-instruct-2507",
            config.CEREBRAS_API_KEY,
        ))
    if config.NVIDIA_API_KEY:
        # NVIDIA NIM serves Llama-3.3-70B via its OpenAI-compatible endpoint.
        # Free tier is ~40 RPM, no daily cap on most models — the largest
        # 70B-class headroom in the chain.
        providers.append((
            "nvidia",
            "https://integrate.api.nvidia.com/v1/chat/completions",
            "meta/llama-3.3-70b-instruct",
            config.NVIDIA_API_KEY,
        ))
    if config.MISTRAL_API_KEY:
        # Mistral La Plateforme — mistral-large-latest (~123B). Free tier
        # offers 1 req/sec (~60 RPM) and 500K tokens/min, with a monthly
        # cap around 1B tokens. The largest model in the chain.
        providers.append((
            "mistral",
            "https://api.mistral.ai/v1/chat/completions",
            "mistral-large-latest",
            config.MISTRAL_API_KEY,
        ))
    if config.SAMBANOVA_API_KEY:
        providers.append((
            "sambanova",
            "https://api.sambanova.ai/v1/chat/completions",
            "Meta-Llama-3.3-70B-Instruct",
            config.SAMBANOVA_API_KEY,
        ))

    # ─── cont. 58: 12-provider expansion ──────────────────────────────────
    # Local OpenAI-compatible servers come FIRST in the chain (free, fast,
    # unmetered). Each is gated on the URL env var being set so the chain
    # silently skips providers the user hasn't deployed. Auth header uses
    # "Bearer EMPTY" or "Bearer none" by convention for local servers that
    # don't require keys; call_provider_sync accepts any non-empty string.
    if config.LLAMACPP_URL:
        providers.append((
            "llamacpp",
            config.LLAMACPP_URL.rstrip("/") + "/v1/chat/completions",
            config.LLAMACPP_MODEL,
            "EMPTY",
        ))
    if config.LMSTUDIO_URL:
        providers.append((
            "lmstudio",
            config.LMSTUDIO_URL.rstrip("/") + "/v1/chat/completions",
            config.LMSTUDIO_MODEL,
            "EMPTY",
        ))
    if config.JANAI_URL:
        providers.append((
            "janai",
            config.JANAI_URL.rstrip("/") + "/v1/chat/completions",
            config.JANAI_MODEL,
            "EMPTY",
        ))
    if config.TEXTGEN_URL:
        # text-generation-webui (oobabooga). Needs the openai api extension
        # enabled in webui (--api or --extensions openai).
        providers.append((
            "textgen",
            config.TEXTGEN_URL.rstrip("/") + "/v1/chat/completions",
            config.TEXTGEN_MODEL,
            "EMPTY",
        ))
    if config.GPT4ALL_URL:
        providers.append((
            "gpt4all",
            config.GPT4ALL_URL.rstrip("/") + "/v1/chat/completions",
            config.GPT4ALL_MODEL,
            "EMPTY",
        ))

    # Cloud OpenAI-compatible — added after the existing 5 cloud providers.
    if config.OPENROUTER_API_KEY:
        # OpenRouter free-tier: many `:free` suffixed models. Llama-3.3-70B
        # is the strongest free option (~50 RPM with login bonus, ~10 RPM
        # baseline). Caller can override model via OPENROUTER_MODEL.
        providers.append((
            "openrouter",
            "https://openrouter.ai/api/v1/chat/completions",
            os.getenv("OPENROUTER_MODEL",
                      "meta-llama/llama-3.3-70b-instruct:free"),
            config.OPENROUTER_API_KEY,
        ))
    if config.TOGETHER_API_KEY:
        # Together AI free serverless tier — Llama-3.3-70B-Instruct-Turbo-Free.
        # Generous free RPM (~60), occasional model rotation.
        providers.append((
            "together",
            "https://api.together.xyz/v1/chat/completions",
            os.getenv("TOGETHER_MODEL",
                      "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"),
            config.TOGETHER_API_KEY,
        ))
    if config.DEEPINFRA_API_KEY:
        # DeepInfra OpenAI-compatible endpoint (free initial credits, then
        # cheap pay-per-token — still in the chain for headroom).
        providers.append((
            "deepinfra",
            "https://api.deepinfra.com/v1/openai/chat/completions",
            os.getenv("DEEPINFRA_MODEL",
                      "meta-llama/Meta-Llama-3.3-70B-Instruct"),
            config.DEEPINFRA_API_KEY,
        ))
    if config.FIREWORKS_API_KEY:
        # Fireworks AI serverless — DeepSeek V4 Pro (confirmed available on this account).
        providers.append((
            "fireworks",
            "https://api.fireworks.ai/inference/v1/chat/completions",
            os.getenv("FIREWORKS_MODEL",
                      "accounts/fireworks/models/deepseek-v4-pro"),
            config.FIREWORKS_API_KEY,
        ))
    if config.HUGGINGFACE_API_KEY:
        # HuggingFace Inference router — OpenAI-compatible endpoint that
        # routes to whichever inference provider is cheapest/available.
        providers.append((
            "huggingface",
            "https://router.huggingface.co/v1/chat/completions",
            os.getenv("HUGGINGFACE_MODEL",
                      "meta-llama/Llama-3.3-70B-Instruct"),
            config.HUGGINGFACE_API_KEY,
        ))
    if config.GOOGLE_AI_STUDIO_API_KEY:
        # Google AI Studio (Gemini) OpenAI-compatible endpoint. Gemini 2.0
        # Flash is the free-tier sweet spot (~15 RPM, 1M tokens/day).
        providers.append((
            "google_ai_studio",
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            os.getenv("GOOGLE_AI_STUDIO_MODEL", "gemini-2.0-flash"),
            config.GOOGLE_AI_STUDIO_API_KEY,
        ))
    if config.CLOUDFLARE_API_KEY and config.CLOUDFLARE_ACCOUNT_ID:
        # Cloudflare Workers AI OpenAI-compatible endpoint. Free daily
        # neuron quota; Llama 3.3 70B Instruct FP8 fast variant is the
        # strongest model under the free allowance.
        providers.append((
            "cloudflare",
            f"https://api.cloudflare.com/client/v4/accounts/{config.CLOUDFLARE_ACCOUNT_ID}/ai/v1/chat/completions",
            os.getenv("CLOUDFLARE_MODEL",
                      "@cf/meta/llama-3.3-70b-instruct-fp8-fast"),
            config.CLOUDFLARE_API_KEY,
        ))

    return providers


# Static metadata for the dashboard — same provider IDs as get_providers()
# but also includes providers whose key is currently empty, so the UI can
# show "configured: no" rather than hiding them.
PROVIDER_CATALOG: list[dict] = [
    {"name": "groq",      "model": "llama-3.3-70b-versatile",
     "free_rpm": 30,  "config_env": "GROQ_API_KEY"},
    {"name": "cerebras",  "model": "qwen-3-235b-a22b-instruct-2507",
     "free_rpm": 30,  "config_env": "CEREBRAS_API_KEY"},
    {"name": "nvidia",    "model": "meta/llama-3.3-70b-instruct",
     "free_rpm": 40,  "config_env": "NVIDIA_API_KEY"},
    {"name": "mistral",   "model": "mistral-large-latest",
     "free_rpm": 60,  "config_env": "MISTRAL_API_KEY"},
    {"name": "sambanova", "model": "Meta-Llama-3.3-70B-Instruct",
     "free_rpm": 30,  "config_env": "SAMBANOVA_API_KEY"},
    # cont. 58 expansion — local OpenAI-compatible servers (high RPM as they
    # are localhost; 600 leaves headroom for the proactive 80% gate to clamp
    # any runaway loop instead of saturating the local CPU).
    {"name": "llamacpp",  "model": "<env LLAMACPP_MODEL>",
     "free_rpm": 600, "config_env": "LLAMACPP_URL"},
    {"name": "lmstudio",  "model": "<env LMSTUDIO_MODEL>",
     "free_rpm": 600, "config_env": "LMSTUDIO_URL"},
    {"name": "janai",     "model": "<env JANAI_MODEL>",
     "free_rpm": 600, "config_env": "JANAI_URL"},
    {"name": "textgen",   "model": "<env TEXTGEN_MODEL>",
     "free_rpm": 600, "config_env": "TEXTGEN_URL"},
    {"name": "gpt4all",   "model": "<env GPT4ALL_MODEL>",
     "free_rpm": 600, "config_env": "GPT4ALL_URL"},
    # cont. 58 expansion — cloud OpenAI-compatible. RPM numbers reflect
    # documented free-tier defaults (Oct-2025 snapshot); actual headroom
    # varies by month / model.
    {"name": "openrouter",      "model": "meta-llama/llama-3.3-70b-instruct:free",
     "free_rpm": 20,  "config_env": "OPENROUTER_API_KEY"},
    {"name": "together",        "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
     "free_rpm": 60,  "config_env": "TOGETHER_API_KEY"},
    {"name": "deepinfra",       "model": "meta-llama/Meta-Llama-3.3-70B-Instruct",
     "free_rpm": 60,  "config_env": "DEEPINFRA_API_KEY"},
    {"name": "fireworks",       "model": "accounts/fireworks/models/deepseek-v4-pro",
     "free_rpm": 60,  "config_env": "FIREWORKS_API_KEY"},
    {"name": "huggingface",     "model": "meta-llama/Llama-3.3-70B-Instruct",
     "free_rpm": 30,  "config_env": "HUGGINGFACE_API_KEY"},
    {"name": "google_ai_studio","model": "gemini-2.0-flash",
     "free_rpm": 15,  "config_env": "GOOGLE_AI_STUDIO_API_KEY"},
    {"name": "cloudflare",      "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
     "free_rpm": 30,  "config_env": "CLOUDFLARE_API_KEY"},
]


def is_in_cooldown(name: str) -> bool:
    try:
        import redis_client as _rc
        return bool(_rc.get().get(f"llm:provider_cooldown:{name}"))
    except Exception:
        return False


def mark_cooldown(name: str, status_code: int, ttl: int = _COOLDOWN_SECONDS) -> None:
    """Mark provider as cooled-down + record a cumulative hit so the dashboard
    can show the user how often each provider has been rate-limited.

    cont. 52: callers leaving `ttl` defaulted get jitter added so simultaneous
    rate-limit hits across the chain don't expire at the same instant —
    desynchronises the cooldown wavefront across providers.
    """
    try:
        import time as _t
        import redis_client as _rc
        r = _rc.get()
        effective_ttl = ttl
        if ttl == _COOLDOWN_SECONDS:
            effective_ttl = ttl + random.randint(0, _COOLDOWN_JITTER_MAX)
        r.setex(f"llm:provider_cooldown:{name}", effective_ttl, str(status_code))
        # Cumulative rate-limit / quota / not-found hits per provider — read
        # by /llm/providers dashboard endpoint to surface trend over time.
        r.incr(f"llm:provider_hits:{name}")
        r.set(f"llm:provider_hits:{name}:last_ts", str(int(_t.time())))
        r.set(f"llm:provider_hits:{name}:last_status", str(status_code))
    except Exception:
        pass


_PROVIDER_RPM: dict[str, int] = {p["name"]: p["free_rpm"] for p in PROVIDER_CATALOG}


def _record_call_attempt(name: str) -> None:
    """Track per-provider call attempts in a 60 s rolling Redis list for
    proactive RPM headroom checks. Listpush + LTRIM to a small N keeps the
    list bounded; we only ever inspect the last `free_rpm + 5` entries.
    """
    try:
        import redis_client as _rc
        r = _rc.get()
        key = f"llm:provider_calls:{name}"
        now = int(_time_mod.time())
        r.lpush(key, str(now))
        # Bound the list — keep at most 2× the max RPM we'd ever check.
        r.ltrim(key, 0, 199)
        # 90-second TTL: just past the 60 s window we inspect, so stale lists
        # don't accumulate forever for retired providers.
        r.expire(key, 90)
    except Exception:
        pass


def has_rpm_headroom(name: str) -> bool:
    """Return False if the provider's call count in the trailing 60 s window
    is already ≥ 80 % of its free_rpm — avoids burning a 429 to discover
    we're out of budget. Defaults to True on any error or missing data
    (don't fail-closed on a Redis blip; the actual 429 path still protects).
    """
    try:
        import redis_client as _rc
        r = _rc.get()
        key = f"llm:provider_calls:{name}"
        entries = r.lrange(key, 0, 199) or []
        now = int(_time_mod.time())
        within_window = sum(1 for ts in entries if (now - int(ts)) < 60)
        rpm_limit = _PROVIDER_RPM.get(name, 60)
        threshold = int(rpm_limit * _RPM_HEADROOM_FRAC)
        return within_window < threshold
    except Exception:
        return True


def _mark_success(name: str) -> None:
    """Cumulative successful call counter — pairs with mark_cooldown so the
    dashboard can show both sides (calls served vs limits hit) per provider."""
    try:
        import time as _t
        import redis_client as _rc
        r = _rc.get()
        r.incr(f"llm:provider_success:{name}")
        r.set(f"llm:provider_success:{name}:last_ts", str(int(_t.time())))
    except Exception:
        pass


def call_provider_sync(name: str, url: str, model: str, key: str,
                       prompt: str, max_tokens: int = 512,
                       json_mode: bool = False, timeout: int = _TIMEOUT) -> str:
    """Single provider HTTP call. Returns raw response text. Raises on error.

    When json_mode=True, requests `response_format={"type": "json_object"}` per
    OpenAI chat-completions spec. Groq and Cerebras honor this; SambaNova
    ignores unknown keys (returns text). Callers should defensively parse JSON
    from the returned text.

    Rate-limit / quota status codes (429/402/403) mark this provider in Redis
    cooldown via `mark_cooldown` so the chain moves on.
    """
    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    if resp.status_code in (402, 403, 429):
        mark_cooldown(name, resp.status_code)
        raise RuntimeError(f"{name}_rate_limited_status_{resp.status_code}")
    if resp.status_code == 404:
        mark_cooldown(name, 404, ttl=86400)
        raise RuntimeError(f"{name}_model_not_found_404")
    if resp.status_code == 400 and json_mode:
        # Some providers reject json_mode for some models — retry without it
        # so the chain doesn't burn the provider's cooldown over a feature flag.
        payload.pop("response_format", None)
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    _mark_success(name)
    log.info("llm_provider_call_complete", provider=name, model=model,
             json_mode=json_mode, tokens_requested=max_tokens)
    return content


def call_chain(prompt: str, max_tokens: int = 512, json_mode: bool = False,
               timeout: int = _TIMEOUT) -> tuple[str, str]:
    """Try each configured provider in priority order, skipping any in cooldown.

    Returns (provider_name, response_text) on first success.
    Raises RuntimeError(no_llm_provider_configured) when no API keys set.
    Raises the last provider's exception when all attempted providers fail.
    Raises RuntimeError(all_llm_providers_in_cooldown) when every provider is
    cooled down and none could be attempted — caller should treat this as a
    transient outage and try again later.
    """
    chain = get_providers()
    if not chain:
        raise RuntimeError("no_llm_provider_configured")

    last_exc: Exception | None = None
    attempted = 0
    for name, url, model, key in chain:
        if is_in_cooldown(name):
            log.info("llm_provider_skipped_cooldown", provider=name)
            continue
        # cont. 52: proactive RPM headroom check. When the rolling 60 s call
        # count is already ≥ 80 % of the provider's free_rpm, skip without
        # sending — sending would have a high probability of returning 429
        # and burning a full cooldown TTL. Counted as "skipped_budget" so
        # the dashboard can distinguish budget-saturation from rate-limit
        # hits.
        if not has_rpm_headroom(name):
            log.info("llm_provider_skipped_budget", provider=name)
            continue
        attempted += 1
        _record_call_attempt(name)
        try:
            text = call_provider_sync(name, url, model, key, prompt,
                                       max_tokens=max_tokens,
                                       json_mode=json_mode, timeout=timeout)
            return name, text
        except Exception as exc:
            last_exc = exc
            log.warning("llm_provider_failed", provider=name,
                        error=f"{type(exc).__name__}: {str(exc)[:200]}")
            continue
    if attempted == 0:
        raise RuntimeError("all_llm_providers_in_cooldown")
    raise last_exc or RuntimeError("all_llm_providers_failed")


# ─────────────────────────────────────────────────────────────────────────────
# JSON parsing helpers — cloud responses often wrap JSON in markdown fences or
# include preamble text. decide() needs strict dict output; this layer is
# defensive enough to handle common formatting variants.
# ─────────────────────────────────────────────────────────────────────────────

def extract_json_dict(text: str) -> dict:
    """Best-effort JSON-dict extraction from arbitrary LLM text output.

    Tries (in order):
      1. Direct json.loads(text)
      2. Strip ```json ... ``` markdown fences
      3. Locate first '{' / last '}' and parse that substring
    Raises ValueError if no JSON object can be recovered.
    """
    if not isinstance(text, str):
        raise ValueError("non_string_response")
    s = text.strip()

    # 1) direct
    try:
        out = json.loads(s)
        if isinstance(out, dict):
            return out
    except json.JSONDecodeError:
        pass

    # 2) markdown fences
    if s.startswith("```"):
        # remove leading ```json or ``` and trailing ```
        lines = s.split("\n")
        # drop first fence + optional language tag
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        # drop trailing fence
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        inner = "\n".join(lines).strip()
        try:
            out = json.loads(inner)
            if isinstance(out, dict):
                return out
        except json.JSONDecodeError:
            pass

    # 3) substring between first '{' and last '}'
    start = s.find("{")
    end = s.rfind("}")
    snippet = s[start:end + 1] if (start >= 0 and end > start) else None
    if snippet:
        try:
            out = json.loads(snippet)
            if isinstance(out, dict):
                return out
        except json.JSONDecodeError:
            pass

    # 4) lenient: small models emit Python-ish pseudo-JSON with unquoted keys
    #    and single-quoted values, e.g. {action:'tighten', conviction:80,
    #    reason:"exhaustion & divergence"}. Strict json.loads rejects all of it,
    #    which previously counted as a decide() FAILURE and tripped the Ollama
    #    "degraded" cooldown even though the model answered correctly (cont. 68d).
    #    Quote bare keys, then ast.literal_eval — a real parser that handles
    #    single quotes and apostrophes inside values without regex fragility.
    if snippet:
        try:
            import ast as _ast
            import re as _re
            # quote unquoted identifier keys: {key:  /  , key:  ->  "key":
            keyed = _re.sub(
                r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)',
                r'\1"\2"\3',
                snippet,
            )
            out = _ast.literal_eval(keyed)
            if isinstance(out, dict):
                return out
        except (ValueError, SyntaxError):
            pass

    raise ValueError(f"no_json_object_in_response: {s[:120]!r}")
