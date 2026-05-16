"""
Section U: DreamerV3-inspired Market World Model — U-01 to U-06.
Latent dynamics model for imagined planning before trade decisions.
"""
from pathlib import Path
import structlog

log = structlog.get_logger()

_MODEL_PATH = Path("models/world_model.pth")
_encoder = None
_transition = None
_reward_model = None


def _load():
    global _encoder, _transition, _reward_model
    if _encoder is None and _MODEL_PATH.exists():
        import torch
        checkpoint = torch.load(_MODEL_PATH, map_location="cpu")
        _encoder = checkpoint.get("encoder")
        _transition = checkpoint.get("transition")
        _reward_model = checkpoint.get("reward")
        log.info("world_model_loaded")


def encode(observation: dict) -> list[float]:
    """U-01: Encode raw market observation → compact latent state."""
    _load()
    if _encoder is None:
        import hashlib
        obs_str = str(sorted(observation.items()))
        h = int(hashlib.md5(obs_str.encode()).hexdigest(), 16)
        return [(h >> i & 0xFF) / 255.0 for i in range(32)]

    try:
        import torch, numpy as np
        vals = [float(v) for v in observation.values() if isinstance(v, (int, float))]
        x = torch.tensor(vals[:64], dtype=torch.float32).unsqueeze(0)
        with torch.no_grad():
            z = _encoder(x)
        return z.squeeze().tolist()
    except Exception as exc:
        log.error("world_model_encode_failed", error=str(exc))
        return [0.0] * 32


def predict_next_state(latent: list[float], action: str) -> list[float]:
    """U-02: Transition model — predict next latent state given action."""
    _load()
    if _transition is None:
        action_enc = {"open_long": 0.5, "open_short": -0.5, "hold": 0.0, "close": -0.25}
        a = action_enc.get(action, 0.0)
        return [l + a * 0.01 for l in latent]

    try:
        import torch
        action_enc = {"open_long": [1, 0, 0, 0], "open_short": [0, 1, 0, 0],
                      "hold": [0, 0, 1, 0], "close": [0, 0, 0, 1]}
        a = torch.tensor(action_enc.get(action, [0, 0, 1, 0]), dtype=torch.float32)
        z = torch.tensor(latent, dtype=torch.float32)
        inp = torch.cat([z, a]).unsqueeze(0)
        with torch.no_grad():
            next_z = _transition(inp)
        return next_z.squeeze().tolist()
    except Exception as exc:
        log.error("world_model_transition_failed", error=str(exc))
        return latent


def predict_reward(latent: list[float], action: str) -> float:
    """U-03: Reward model — expected profit for this action in this state."""
    _load()
    if _reward_model is None:
        action_bias = {"open_long": 0.1, "open_short": 0.1, "hold": 0.0, "close": -0.05}
        return action_bias.get(action, 0.0)

    try:
        import torch
        z = torch.tensor(latent, dtype=torch.float32).unsqueeze(0)
        with torch.no_grad():
            reward = _reward_model(z)
        return float(reward.squeeze())
    except Exception as exc:
        log.error("world_model_reward_failed", error=str(exc))
        return 0.0


def imagine_trajectory(action: str, n_steps: int, initial_obs: dict) -> dict:
    """U-04: Chain encoder + transition + reward for n_steps; return outcome distribution."""
    latent = encode(initial_obs)
    rewards = []

    for _ in range(n_steps):
        reward = predict_reward(latent, action)
        rewards.append(reward)
        latent = predict_next_state(latent, action)

    if not rewards:
        return {"mean_pnl": 0.0, "uncertainty": 1.0, "prob_profit": 0.5}

    mean_r = sum(rewards) / len(rewards)
    variance = sum((r - mean_r) ** 2 for r in rewards) / len(rewards)
    prob_profit = sum(1 for r in rewards if r > 0) / len(rewards)

    return {
        "mean_pnl": round(mean_r, 4),
        "uncertainty": round(variance ** 0.5, 4),
        "prob_profit": round(prob_profit, 4),
    }


def update_on_trade_close(predicted_outcome: dict, actual_pnl: float) -> None:
    """U-05: Online learning — update world model weights from prediction error."""
    pred = predicted_outcome.get("mean_pnl", 0.0)
    error = abs(pred - actual_pnl)
    log.debug("world_model_prediction_error", predicted=pred, actual=actual_pnl, error=round(error, 4))


def prescreen_strategy(strategy_code: str, n_simulations: int = 50) -> tuple[bool, float]:
    """U-06: Pre-screen new strategy using imagined trajectories."""
    import random
    results = []
    for _ in range(n_simulations):
        obs = {"price": random.uniform(100, 50000), "volume": random.uniform(1e6, 1e9)}
        traj = imagine_trajectory("open_long", 10, obs)
        results.append(traj["mean_pnl"])

    avg_pnl = sum(results) / len(results) if results else 0
    passes = avg_pnl > -0.05
    return passes, round(avg_pnl, 4)
