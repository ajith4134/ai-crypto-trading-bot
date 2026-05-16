"""I-02: Phi-3 Mini via Ollama — market regime routing and situation classification."""
import asyncio
import json
import aiohttp
import structlog
from llm.guard import assert_no_reflection
import config

log = structlog.get_logger()


async def classify(prompt: str, timeout: int = 10) -> dict:
    """Send prompt to Phi-3 Mini; return parsed JSON response."""
    assert_no_reflection(prompt)
    payload = {
        "model": config.llm.router_model,
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
