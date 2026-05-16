"""I-03: Mistral 7B via Ollama — Debate Council moderator and final trade decision."""
import asyncio
import json
import aiohttp
import structlog
from llm.guard import assert_no_reflection
import config

log = structlog.get_logger()


async def decide(prompt: str, timeout: int = 30) -> dict:
    """Send prompt to Mistral 7B; return parsed JSON response."""
    assert_no_reflection(prompt)
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
