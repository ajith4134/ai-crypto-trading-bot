"""L-12: 3-tier MARL hierarchy. Activates at 300 closed trades.

Day Agent (strategic direction + risk budget),
Hour Agent (pair selection — reserved for future use),
Minute Agent (execution timing — enter / hold / skip).

Without trained PPO checkpoints at models/marl_<level>_agent.zip, the getters
fall back to neutral defaults so signal flow is unaffected. Once checkpoints
exist they're picked up by `load_agents` (called from brain startup) and the
agents start informing decisions.
"""
from pathlib import Path
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()

_agents: dict = {}
_loaded = False


def _get_regime() -> str:
    return redis_client.get().get(redis_keys.CURRENT_REGIME) or "bull"


def load_agents(paper_closed_count: int) -> dict:
    """Load Day/Hour/Minute agents from checkpoint when available. Idempotent.

    Below 300 trades: no-op (MARL not active per blueprint Phase 3).
    Missing checkpoints: log + continue (agents will be trained externally;
    once a .zip lands, the next call picks it up).
    """
    global _loaded
    try:
        from feature_governance.registry import is_active
        if not is_active("F21"):
            return {}
    except Exception:
        pass

    if paper_closed_count < 300:
        if not _loaded:
            log.info("marl_not_active_yet", trades=paper_closed_count, needed=300)
            _loaded = True
        return {}

    try:
        from stable_baselines3 import PPO

        loaded = {}
        for level in ("day", "hour", "minute"):
            path = Path(f"models/marl_{level}_agent.zip")
            if path.exists():
                # Only reload if mtime changed
                mtime = path.stat().st_mtime
                cached = _agents.get(f"{level}_mtime")
                if cached == mtime:
                    loaded[level] = _agents[level]
                else:
                    loaded[level] = PPO.load(str(path))
                    _agents[f"{level}_mtime"] = mtime
                    log.info("marl_agent_loaded", level=level)
            elif not _loaded:
                log.info("marl_agent_not_trained_yet", level=level)
        _agents.update(loaded)
        _loaded = True
        return loaded
    except Exception as exc:
        log.error("marl_load_failed", error=str(exc))
        return {}


def _mark_called(level: str, active: bool, decision: str) -> None:
    """Durable evidence — every MARL agent call increments a Redis counter
    and stamps a timestamp. Distinguishes 'active' (real checkpoint loaded)
    from default-passthrough so the checker can report both states."""
    try:
        import time as _t
        import redis_client as _rc
        r = _rc.get()
        r.incr(f"marl:{level}:call_count")
        r.set(f"marl:{level}:last_call_ts", str(int(_t.time())))
        r.set(f"marl:{level}:last_active", "1" if active else "0")
        r.set(f"marl:{level}:last_decision", str(decision)[:50])
    except Exception:
        pass


def get_day_agent_decision(obs: dict) -> dict:
    """Day Agent: strategic direction and risk budget. Returns neutral defaults
    when the agent is not loaded — never blocks trading."""
    if "day" not in _agents:
        _mark_called("day", False, "neutral")
        return {"direction": "neutral", "risk_budget_pct": 10.0, "active": False}
    try:
        import numpy as np
        regime = _get_regime()
        obs_vec = np.array([
            float(obs.get("sentiment", 0.5)),
            float(obs.get("turbulence", 0)),
            {"bull": 1, "bear": -1, "turbulent": 0}.get(regime, 0),
        ], dtype=np.float32)
        action, _ = _agents["day"].predict(obs_vec, deterministic=True)
        a_int = int(action) if hasattr(action, "__int__") else 0
        direction = ["long", "short", "neutral"][a_int % 3]
        _mark_called("day", True, direction)
        return {
            "direction": direction,
            "risk_budget_pct": float(5 + abs(a_int) * 2),
            "active": True,
        }
    except Exception as exc:
        log.warning("marl_day_failed", error=str(exc)[:120])
        _mark_called("day", False, "neutral")
        return {"direction": "neutral", "risk_budget_pct": 10.0, "active": False}


def get_minute_agent_action(obs: dict) -> str:
    """Minute Agent: execution timing — enter, hold, or skip. Returns 'enter'
    when not loaded so signal flow is unaffected at training-warmup."""
    if "minute" not in _agents:
        _mark_called("minute", False, "enter")
        return "enter"
    try:
        import numpy as np
        obs_vec = np.array([
            float(obs.get("ofi", 0)),
            float(obs.get("vpin", 0)),
            float(obs.get("spread", 0)),
        ], dtype=np.float32)
        action, _ = _agents["minute"].predict(obs_vec, deterministic=True)
        a_int = int(action) if hasattr(action, "__int__") else 0
        decision = ["enter", "hold", "skip"][a_int % 3]
        _mark_called("minute", True, decision)
        return decision
    except Exception as exc:
        log.warning("marl_minute_failed", error=str(exc)[:120])
        _mark_called("minute", False, "enter")
        return "enter"


def has_loaded_agents() -> bool:
    """Used by health checks / governance to confirm wiring is reachable."""
    return any(k in _agents for k in ("day", "hour", "minute"))
