"""Blueprint F21 MARL training pipeline (cont. 35).

Builds and trains PPO policies for the two MARL agents that are actually
consumed in production:

  * Day Agent   — strategic direction + risk budget (consumed in
                  brain/soar.py::_act to scale capital_per_trade)
  * Minute Agent — execution-timing veto (consumed in signals/engine.py
                  after debate; can downgrade an accepted signal to skip/hold)

Pre-cont.35 these had no training task — ml/marl.py would log
`marl_agent_not_trained_yet` on every brain boot and the dashboard MARL row
sat at "missing" forever once paper_closed crossed the 300-trade gate.

Hour Agent is intentionally NOT trained (per Rule 4 honesty): the existing
ml/marl.py declares it in the load loop but no production code path calls
`get_hour_agent_*`. Training a checkpoint that nothing reads would be
dishonest. See PROGRESS.md cont. 35 for the "intentionally simplified".

Training is offline / contextual-bandit on the closed-trades history:
  - 1-step episodes
  - Observation space matches the production call site exactly (so the
    saved PPO can be loaded by ml/marl.py and called with the same obs
    dict the brain produces).
  - Reward = directly-attributable PnL of the sampled historical trade
    given the chosen action.

This is intentionally simpler than full MDP rollouts on a simulated market
(too risky — sim divergence from live would silently train a wrong policy).
Contextual-bandit on real outcomes guarantees the policy can never learn
patterns that didn't actually happen.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import gymnasium as gym
import numpy as np
import structlog

log = structlog.get_logger()

MODELS_DIR = Path("models")
MIN_TRADES_FOR_TRAIN = 100   # below this we refuse to train (noise floor)
DEFAULT_TIMESTEPS = 20_000


# ─────────────────────────────────────────────────────────────────────────
# Data loading — shared by both envs.
# ─────────────────────────────────────────────────────────────────────────
def _load_closed_trades(limit: int | None = None) -> list[dict]:
    """Pull closed trades with everything we need for both envs.

    Returns a list of dicts (one per trade). Filters out trades with NULL
    direction, net_pnl_usdt, or market_regime — required fields for at
    least one of the envs."""
    from db import db_conn
    rows: list[dict] = []
    with db_conn() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT direction, net_pnl_usdt, market_regime,
                       feature_vector, entry_time
                FROM trades
                WHERE status='closed'
                  AND direction IS NOT NULL
                  AND net_pnl_usdt IS NOT NULL
                  AND market_regime IS NOT NULL
                ORDER BY entry_time DESC
            """
            if limit:
                sql += f" LIMIT {int(limit)}"
            cur.execute(sql)
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                t = dict(zip(cols, row))
                # Parse feature_vector once. Tolerate str / dict / None.
                fv = t.get("feature_vector")
                if isinstance(fv, str):
                    try:
                        fv = json.loads(fv)
                    except Exception:
                        fv = {}
                t["feature_vector"] = fv or {}
                rows.append(t)
    return rows


# ─────────────────────────────────────────────────────────────────────────
# Day Agent — strategic direction picker.
# ─────────────────────────────────────────────────────────────────────────
class DayAgentEnv(gym.Env):
    """Contextual-bandit env for the Day Agent.

    Observation: 3-vector matching brain/soar.py::_act call site exactly:
      [sentiment ∈ [0,1], turbulence ∈ [0,1], regime_int ∈ {-1,0,1}]
    Action: Discrete(3) = {0: long, 1: short, 2: neutral}
    Reward:
      action picks long  → +net_pnl  if the sampled trade was long; else penalty
      action picks short → +net_pnl  if the sampled trade was short; else penalty
      action picks neutral → 0       (no commitment, no reward, no loss)

    The "penalty for picking the wrong direction" is half the loss magnitude.
    Asymmetric on purpose — being wrong about direction is worse than
    abstaining, but not as bad as being right about the wrong trade. This
    keeps the neutral action from dominating when win rate is below 50%.
    """
    metadata = {"render_modes": []}

    def __init__(self, trades: list[dict]):
        super().__init__()
        from gymnasium import spaces
        self.observation_space = spaces.Box(
            low=np.array([0.0, 0.0, -1.0], dtype=np.float32),
            high=np.array([1.0, 1.0, 1.0], dtype=np.float32),
            dtype=np.float32,
        )
        self.action_space = spaces.Discrete(3)
        self._regime_map = {"bull": 1, "bear": -1, "turbulent": 0}
        # Keep only trades whose feature_vector has a sentiment hint;
        # turbulence we can default to 0 if missing.
        self._trades = [
            t for t in trades
            if isinstance(t.get("feature_vector"), dict)
            and t["direction"] in ("long", "short")
        ]
        if len(self._trades) < MIN_TRADES_FOR_TRAIN:
            raise ValueError(
                f"DayAgentEnv: only {len(self._trades)} usable trades "
                f"(need {MIN_TRADES_FOR_TRAIN})")
        self._current: dict | None = None

    def _obs_for(self, trade: dict) -> np.ndarray:
        fv = trade.get("feature_vector") or {}
        sentiment = float(fv.get("sentiment", 0.5))
        # Sentiment is stored 0..1 in feature_vector; clip to space.
        sentiment = max(0.0, min(1.0, sentiment))
        # Turbulence proxy: VPIN normalised to [0,1].
        vpin = float(fv.get("vpin", 0.0))
        turbulence = max(0.0, min(1.0, vpin * 100))  # vpin is small; scale up
        regime_int = self._regime_map.get(trade.get("market_regime"), 0)
        return np.array([sentiment, turbulence, float(regime_int)], dtype=np.float32)

    def reset(self, seed: int | None = None, options: dict | None = None):
        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)
        self._current = random.choice(self._trades)
        return self._obs_for(self._current), {}

    def step(self, action: int):
        t = self._current
        pnl = float(t.get("net_pnl_usdt") or 0.0)
        direction = t["direction"]
        if action == 0:    # long
            reward = pnl if direction == "long" else -abs(pnl) * 0.5
        elif action == 1:  # short
            reward = pnl if direction == "short" else -abs(pnl) * 0.5
        else:              # neutral
            reward = 0.0
        # 1-step episode — bandit setup.
        terminated, truncated = True, False
        # Next obs is meaningless after termination but sb3 requires shape.
        next_obs = self._obs_for(t)
        return next_obs, float(reward), terminated, truncated, {}

    def close(self):
        pass


# ─────────────────────────────────────────────────────────────────────────
# Minute Agent — execution-timing veto.
# ─────────────────────────────────────────────────────────────────────────
class MinuteAgentEnv(gym.Env):
    """Contextual-bandit env for the Minute Agent.

    Observation: 3-vector matching signals/engine.py::process_signals exactly:
      [ofi, vpin, spread]
    Note: production sets spread=0 unconditionally (the value isn't being
    tracked yet), so we train with spread=0 too. If a spread feature is
    added later, retrain with the real distribution.

    Action: Discrete(3) = {0: enter, 1: hold, 2: skip}
    Reward (closed-trade outcome attribution):
      enter → +net_pnl (full credit, positive on win, negative on loss)
      skip  → -net_pnl (mirror — positive for skipping a loser, negative
                        for skipping a winner)
      hold  → 0        (deferred decision, no commitment)

    Symmetric reward means the optimal policy is exactly "predict pnl sign":
    enter when expected pnl > 0, skip when expected pnl < 0, hold when
    uncertain. PPO will learn this from the obs distribution.
    """
    metadata = {"render_modes": []}

    def __init__(self, trades: list[dict]):
        super().__init__()
        from gymnasium import spaces
        self.observation_space = spaces.Box(
            low=np.array([-1.0, 0.0, 0.0], dtype=np.float32),
            high=np.array([1.0, 1.0, 1.0], dtype=np.float32),
            dtype=np.float32,
        )
        self.action_space = spaces.Discrete(3)
        # Need trades with ofi / vpin in feature_vector.
        self._trades = [
            t for t in trades
            if isinstance(t.get("feature_vector"), dict)
            and "ofi" in t["feature_vector"]
            and "vpin" in t["feature_vector"]
        ]
        if len(self._trades) < MIN_TRADES_FOR_TRAIN:
            raise ValueError(
                f"MinuteAgentEnv: only {len(self._trades)} usable trades "
                f"(need {MIN_TRADES_FOR_TRAIN})")
        self._current: dict | None = None

    def _obs_for(self, trade: dict) -> np.ndarray:
        fv = trade["feature_vector"]
        ofi = max(-1.0, min(1.0, float(fv.get("ofi", 0.0))))
        # vpin is typically small (~0.001-0.01); scale to ~[0,1].
        vpin = max(0.0, min(1.0, float(fv.get("vpin", 0.0)) * 100))
        spread = 0.0  # production always passes 0; train consistently.
        return np.array([ofi, vpin, spread], dtype=np.float32)

    def reset(self, seed: int | None = None, options: dict | None = None):
        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)
        self._current = random.choice(self._trades)
        return self._obs_for(self._current), {}

    def step(self, action: int):
        t = self._current
        pnl = float(t.get("net_pnl_usdt") or 0.0)
        if action == 0:    # enter
            reward = pnl
        elif action == 1:  # hold
            reward = 0.0
        else:              # skip
            reward = -pnl
        terminated, truncated = True, False
        next_obs = self._obs_for(t)
        return next_obs, float(reward), terminated, truncated, {}

    def close(self):
        pass


# ─────────────────────────────────────────────────────────────────────────
# Training entry points.
# ─────────────────────────────────────────────────────────────────────────
def _train_ppo(env_cls, save_name: str, timesteps: int) -> dict[str, Any]:
    """Shared PPO trainer. Loads trades, builds env, fits PPO, saves zip."""
    trades = _load_closed_trades()
    if len(trades) < MIN_TRADES_FOR_TRAIN:
        return {"status": "insufficient_data", "trades": len(trades),
                "needed": MIN_TRADES_FOR_TRAIN}

    from stable_baselines3 import PPO
    from stable_baselines3.common.env_checker import check_env
    env = env_cls(trades)
    # Validate the env once — surfaces obs/action space mismatches early.
    try:
        check_env(env, warn=False, skip_render_check=True)
    except Exception as exc:
        return {"status": "env_invalid", "error": str(exc)[:200]}

    model = PPO(
        "MlpPolicy",
        env,
        learning_rate=3e-4,
        n_steps=256,
        batch_size=64,
        n_epochs=4,
        gamma=0.99,
        verbose=0,
    )
    model.learn(total_timesteps=int(timesteps), progress_bar=False)

    MODELS_DIR.mkdir(exist_ok=True)
    out_path = MODELS_DIR / save_name
    model.save(str(out_path))

    # Quick sanity check — confirm we can reload (catches save-format bugs).
    try:
        _ = PPO.load(str(out_path))
    except Exception as exc:
        return {"status": "save_load_mismatch", "error": str(exc)[:200]}

    return {
        "status": "ok",
        "save_path": str(out_path),
        "n_trades": len(trades),
        "timesteps": int(timesteps),
    }


def train_marl_day(timesteps: int = DEFAULT_TIMESTEPS) -> dict[str, Any]:
    """Train + save the Day Agent (strategic direction picker)."""
    log.info("marl_day_train_start", timesteps=timesteps)
    result = _train_ppo(DayAgentEnv, "marl_day_agent.zip", timesteps)
    log.info("marl_day_train_done", **result)
    return result


def train_marl_minute(timesteps: int = DEFAULT_TIMESTEPS) -> dict[str, Any]:
    """Train + save the Minute Agent (execution-timing veto)."""
    log.info("marl_minute_train_start", timesteps=timesteps)
    result = _train_ppo(MinuteAgentEnv, "marl_minute_agent.zip", timesteps)
    log.info("marl_minute_train_done", **result)
    return result
