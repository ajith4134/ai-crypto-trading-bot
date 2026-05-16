"""
I-04: Llama 3.1 70B via AirLLM — background intelligence tasks ONLY.

MUST only be called from Celery background workers — never from the live trading loop.
"""
import json
import structlog
from llm.guard import assert_no_reflection
import config

log = structlog.get_logger()


def research(prompt: str, max_new_tokens: int = 512) -> str:
    """
    Run inference via AirLLM (layer-by-layer, 30-120 sec).
    Returns raw text response — caller is responsible for parsing.
    Only call from Celery background workers.
    """
    assert_no_reflection(prompt)

    from airllm import AutoModel
    model = AutoModel.from_pretrained(config.llm.airllm_model_path)
    input_text = f"[INST] {prompt} [/INST]"
    output = model.generate(
        input_text,
        max_new_tokens=max_new_tokens,
        use_cache=True,
    )
    log.info("airllm_inference_complete", tokens=max_new_tokens)
    return output
