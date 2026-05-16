"""L-12: 3-tier MARL hierarchy. Activates training at 300 closed trades."""
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()

_agents: dict = {}


def _get_regime() -> str:
    return redis_client.get().get(redis_keys.CURRENT_REGIME) or "bull"


def load_agents(paper_closed_count: int) -> dict:
    """Load or initialize Day/Hour/Minute agents from checkpoint if available."""
    if paper_closed_count < 300:
        log.info("marl_not_active_yet", trades=paper_closed_count, needed=300)
        return {}
    try:
        from stable_baselines3 import PPO
        from pathlib import Path

        agents = {}
        for level in ("day", "hour", "minute"):
            path = Path(f"models/marl_{level}_agent.zip")
            if path.exists():
                agents[level] = PPO.load(str(path))
                log.info("marl_agent_loaded", level=level)
            else:
                log.info("marl_agent_not_trained_yet", level=level)
        return agents
    except Exception as exc:
        log.error("marl_load_failed", error=str(exc))
        return {}


def get_day_agent_decision(obs: dict) -> dict:
    """Day Agent: strategic direction and risk budget."""
    if "day" not in _agents:
        return {"direction": "neutral", "risk_budget_pct": 10}
    try:
        import numpy as np
        regime = _get_regime()
        obs_vec = np.array([obs.get("sentiment", 0.5), obs.get("turbulence", 0),
                            {"bull": 1, "bear": -1, "turbulent": 0}.get(regime, 0)])
        action, _ = _agents["day"].predict(obs_vec)
        return {"direction": ["long", "short", "neutral"][int(action) % 3],
                "risk_budget_pct": float(5 + abs(action) * 2)}
    except Exception:
        return {"direction": "neutral", "risk_budget_pct": 10}


def get_minute_agent_action(obs: dict) -> str:
    """Minute Agent: execution timing — enter, hold, or skip."""
    if "minute" not in _agents:
        return "enter"
    try:
        import numpy as np
        obs_vec = np.array([obs.get("ofi", 0), obs.get("vpin", 0), obs.get("spread", 0)])
        action, _ = _agents["minute"].predict(obs_vec)
        return ["enter", "hold", "skip"][int(action) % 3]
    except Exception:
        return "enter"
