"""
Section Z: Curiosity & Exploration Engine — Z-01 to Z-05.
"""
import json
import structlog
import redis_client
import redis_keys
import config

log = structlog.get_logger()

_EXPLORATION_KEY = redis_keys.EXPLORATION_COUNTER


def compute_prediction_error_curiosity(world_model_error: float) -> float:
    """Z-01: High world model prediction error = high novelty = high curiosity."""
    return min(100.0, world_model_error * 10)


async def llm_semantic_curiosity(observation: dict) -> dict:
    """Z-02: Ask Phi-3 Mini if current situation is fundamentally different from known patterns."""
    from llm.router import classify
    from llm.guard import assert_no_reflection
    prompt = (
        f"Market regime: {observation.get('regime')}. "
        f"Turbulence: {observation.get('turbulence', 0):.2f}. "
        f"Sentiment: {observation.get('global_sentiment', 0.5):.2f}. "
        "Is this fundamentally different from known market patterns? "
        "Respond as JSON: {is_novel: bool, novelty_score: int, reason: str}"
    )
    assert_no_reflection(prompt)
    try:
        return await classify(prompt, timeout=8)
    except Exception:
        return {"is_novel": False, "novelty_score": 0}


def get_exploration_budget(paper_closed: int, current_win_rate: float) -> float:
    """Z-03: Return exploration budget as % of capacity."""
    base = config.curiosity.exploration_budget_pct
    if current_win_rate < 40 and paper_closed > 50:
        base = min(20, base + 5)   # increase when plateauing
    elif current_win_rate > 55:
        base = max(8, base - 3)    # decrease when improving
    return base


def increment_exploration_counter(r=None) -> int:
    if r is None:
        r = redis_client.get()
    return r.incr(_EXPLORATION_KEY)


def should_explore(curiosity_score: float, threshold: float = 60.0) -> bool:
    """Z-04: Return True if curiosity score exceeds threshold."""
    return curiosity_score >= threshold


def log_exploration_hypothesis(pair: str, direction: str, reason: str) -> None:
    """Z-04: Log high-curiosity situation to Research Engine queue."""
    r = redis_client.get()
    r.lpush("research:hypothesis_queue", json.dumps({
        "pair": pair, "direction": direction,
        "reason": reason, "source": "curiosity",
    }))
    log.info("exploration_hypothesis_queued", pair=pair, reason=reason)
