"""
Section AD: Self-Play Strategy Discovery — AD-01 to AD-07.
MarS market simulator + AlphaZero-style policy/value heads.
"""
import json
import random
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()

_policy_head = None
_value_head = None


# AD-01: MarS adversarial market simulator (simplified CPU version)
class MaRSSimulator:
    """Generates realistic adversarial Binance-like order flows for self-play."""

    def __init__(self, pair: str = "BTCUSDT", initial_price: float = 50000.0):
        self.pair = pair
        self.price = initial_price
        self.step = 0

    def next_tick(self) -> dict:
        """Simulate next market tick with adversarial noise."""
        drift = random.gauss(0, 0.002)
        volatility_shock = random.gauss(0, 0.001) if random.random() < 0.1 else 0
        self.price *= (1 + drift + volatility_shock)
        self.step += 1
        return {
            "price": round(self.price, 2),
            "volume": random.uniform(100, 10000),
            "bid": round(self.price * 0.9999, 2),
            "ask": round(self.price * 1.0001, 2),
            "step": self.step,
        }

    def reset(self, initial_price: float | None = None) -> dict:
        self.price = initial_price or random.uniform(10000, 80000)
        self.step = 0
        return self.next_tick()


# AD-02/AD-03: Policy and value heads (stubs until trained)
def get_action_probabilities(market_state: dict) -> dict:
    """AD-02: Probability distribution over actions."""
    if _policy_head is None:
        return {"open_long": 0.25, "open_short": 0.25, "hold": 0.4, "close": 0.1}
    try:
        import torch, numpy as np
        obs = torch.tensor([
            market_state.get("price", 0) / 100000,
            market_state.get("volume", 0) / 10000,
        ], dtype=torch.float32).unsqueeze(0)
        with torch.no_grad():
            logits = _policy_head(obs)
            probs = torch.softmax(logits, dim=-1).squeeze().tolist()
        actions = ["open_long", "open_short", "hold", "close"]
        return dict(zip(actions, probs))
    except Exception:
        return {"open_long": 0.25, "open_short": 0.25, "hold": 0.4, "close": 0.1}


def estimate_value(market_state: dict) -> float:
    """AD-03: Expected profit of current position."""
    if _value_head is None:
        return 0.0
    try:
        import torch
        obs = torch.tensor([market_state.get("price", 0) / 100000], dtype=torch.float32).unsqueeze(0)
        with torch.no_grad():
            return float(_value_head(obs).squeeze())
    except Exception:
        return 0.0


# AD-04: MCTS within self-play
def mcts_plan(market_state: dict, n_simulations: int = 50) -> str:
    """Run MCTS to select best action via UCB exploration."""
    from world_model.model import imagine_trajectory
    action_scores = {}
    for action in ["open_long", "open_short", "hold", "close"]:
        traj = imagine_trajectory(action, n_steps=10, initial_obs=market_state)
        probs = get_action_probabilities(market_state)
        ucb = traj["mean_pnl"] + 1.41 * probs.get(action, 0.25) * (n_simulations ** 0.5)
        action_scores[action] = ucb
    return max(action_scores, key=action_scores.get)


# AD-05: Self-play loop
def run_self_play_episode(n_steps: int = 200) -> dict:
    """AD-05: Play one episode against MarS; return outcome."""
    sim = MaRSSimulator()
    obs = sim.reset()
    total_reward = 0.0
    position = None

    for _ in range(n_steps):
        action = mcts_plan(obs)
        obs = sim.next_tick()

        if action == "open_long" and position is None:
            position = {"entry": obs["price"], "direction": "long"}
        elif action == "close" and position:
            pnl = (obs["price"] - position["entry"]) * (1 if position["direction"] == "long" else -1)
            total_reward += pnl
            position = None

    return {"total_pnl": round(total_reward, 4), "steps": n_steps}


def promote_from_self_play(win_rate: float, threshold: float = 0.55) -> bool:
    """AD-06: Promote strategy to paper trial if self-play win rate exceeds threshold."""
    if win_rate < threshold:
        return False
    strategy = {
        "name": f"self_play_strategy_{random.randint(1000, 9999)}",
        "source": "self_play",
        "code": "# Self-play derived strategy — parameters to be filled by research engine",
        "status": "experimental",
    }
    from strategy.lifecycle import create_experimental
    sid = create_experimental(strategy)
    log.info("self_play_strategy_promoted", id=sid, win_rate=win_rate)
    return True


def feed_to_world_model(episode_result: dict) -> None:
    """AD-07: Feed self-play outcomes to World Model as training data."""
    from world_model.model import update_on_trade_close
    update_on_trade_close(
        predicted_outcome={"mean_pnl": episode_result.get("total_pnl", 0) * 0.1},
        actual_pnl=episode_result.get("total_pnl", 0),
    )
