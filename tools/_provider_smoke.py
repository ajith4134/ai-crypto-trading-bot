"""Smoke test for the two newly-added providers (NVIDIA NIM + Mistral).

Forces the OTHER 3 providers into a brief cooldown so we KNOW NVIDIA/Mistral
are the ones that served the test prompt. Cleans up after.

Run inside the brain container:
    docker exec trading-bot-brain-1 python -m tools._provider_smoke
"""
import sys
import redis_client
from llm.providers import call_provider_sync, get_providers


def main() -> int:
    r = redis_client.get()
    prior = {}
    # Force the OTHER 3 into cooldown so the chain has to pick the new ones.
    for n in ("groq", "cerebras", "sambanova"):
        prior[n] = r.get(f"llm:provider_cooldown:{n}")
        r.setex(f"llm:provider_cooldown:{n}", 120, "smoke_skip")

    ok = 0
    try:
        for name, url, model, key in get_providers():
            if name not in ("nvidia", "mistral"):
                continue
            try:
                text = call_provider_sync(
                    name, url, model, key,
                    'Reply with this JSON only: {"r":"ok"}',
                    max_tokens=20, json_mode=True, timeout=30,
                )
                print(f"PASS {name}: model={model}")
                print(f"  response: {text[:120]!r}")
            except Exception as exc:
                ok = 1
                print(f"FAIL {name}: {type(exc).__name__}: {str(exc)[:200]}")
    finally:
        # Restore prior state — don't leave forced cooldowns lying around.
        for n in ("groq", "cerebras", "sambanova"):
            r.delete(f"llm:provider_cooldown:{n}")
            if prior[n]:
                r.set(f"llm:provider_cooldown:{n}", prior[n])
        print("cleanup done")
    return ok


if __name__ == "__main__":
    sys.exit(main())
