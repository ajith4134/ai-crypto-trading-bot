"""Smoke test for the cloud-fallback decide() path.

Forces Ollama into circuit-open via the existing failure counter, then issues
a real JSON-output prompt and verifies the response was served by a cloud
provider. Cleans up the cooldown marker afterwards so production state is
unchanged.

Run inside the brain container:
    docker exec trading-bot-brain-1 python -m tools._decide_smoke
"""
import asyncio
import sys


async def main() -> int:
    import redis_client
    from llm.decision import decide
    from llm.fallback import _COOLDOWN_KEY, _FAIL_KEY

    r = redis_client.get()

    # Snapshot prior state so we can restore it.
    prior_cooldown = r.get(_COOLDOWN_KEY.format(task="decide"))
    prior_fails = r.get(_FAIL_KEY.format(task="decide"))
    prior_provider = r.get("llm:decision_last_provider")
    prior_count = r.get("llm:decision_calls_count") or "0"

    # Force Ollama into cooldown for this test so we exercise the cloud branch.
    r.setex(_COOLDOWN_KEY.format(task="decide"), 60, "smoke_test")
    print("forced ollama 'decide' circuit open for smoke test (60s)")

    prompt = (
        "You are a test responder. Output exactly this JSON and nothing else: "
        "{\"ok\": true, \"value\": 42}"
    )
    try:
        result = await decide(prompt, timeout=30)
        print(f"decide() returned: {result}")
        assert isinstance(result, dict), f"expected dict got {type(result).__name__}"
        provider = r.get("llm:decision_last_provider")
        count = r.get("llm:decision_calls_count") or "0"
        print(f"last provider:  {provider}")
        print(f"calls before:   {prior_count}")
        print(f"calls after:    {count}")
        assert provider != "ollama", f"expected non-ollama provider, got {provider}"
        assert int(count) > int(prior_count), "calls_count did not increment"
        print("PASS: decide() served from cloud provider")
        ok = 0
    except Exception as exc:
        print(f"FAIL: decide() raised {type(exc).__name__}: {exc}")
        ok = 1
    finally:
        # Restore prior state — don't pollute live counters.
        r.delete(_COOLDOWN_KEY.format(task="decide"))
        if prior_cooldown:
            r.set(_COOLDOWN_KEY.format(task="decide"), prior_cooldown)
        if prior_fails:
            r.set(_FAIL_KEY.format(task="decide"), prior_fails)
        print("smoke test cleaned up cooldown marker")
    return ok


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
