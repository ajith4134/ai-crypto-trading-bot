"""F48 §Production Extension Idea C — RL Entry Timing Agent.

Blueprint Feature 48 §Production Extensions. PPO agent that decides, at each
signal-firing tick, whether to:
  0 = wait one candle (re-evaluate next tick)
  1 = enter now
  2 = skip this signal entirely

State (22 dims):
  CandleNet 1m  : dir1, dir3, dir5, mag1, mag3, mag5, trend          (7)
  CandleNet 5m  : same                                                 (7)
  exhaustion    : exhaustion_score_1m, exhaustion_score_5m             (2)
  microstructure: current_spread_pct, bid_ask_imbalance                (2)
  context       : time_in_current_bar_pct (0–1)                        (1)
                  recent_volatility (atr_norm)                          (1)
  signal        : signal_score_normalized (0–1)                        (1)
                  time_since_signal_fired (in candles)                  (1)
  Total                                                                = 22

Reward signal (offline training):
  r = (pnl_if_action - pnl_at_signal_open) / atr

Where pnl_if_action is the realised PnL of the trade had it been entered at
the chosen wait-step rather than at the moment the signal first fired.

Activation gate: paper_closed ≥ 1000 (matches MARL Stage-3 thresholds).
When inactive or model missing: decide_entry() returns "enter" — preserves
the current immediate-entry behaviour.

Model file: models/entry_timing_agent.zip   (Stable-Baselines3 PPO save format)
"""
from __future__ import annotations
import json
import math
from pathlib import Path
import structlog

import numpy as np

log = structlog.get_logger()

_MODEL_PATH = Path("models/entry_timing_agent.zip")
_STATE_DIM  = 22
_N_ACTIONS  = 3
_ACTIVATION_TRADES = 1000

_ACTION_NAMES = {0: "wait", 1: "enter", 2: "skip"}

# Cached model — loaded once, reused across decisions
_cache: dict = {"model": None, "mtime": None}


# ── State construction ──────────────────────────────────────────────────────

def _safe_float(x, default: float = 0.0) -> float:
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def _load_candle_forecast(r, pair: str, interval: str) -> dict:
    try:
        raw = r.get(f"{pair}:{interval}:candle_forecast")
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _load_exhaustion(r, pair: str, interval: str) -> float:
    try:
        raw = r.get(f"{pair}:{interval}:exhaustion_score")
        if not raw:
            return 0.0
        d = json.loads(raw)
        return float(d.get("score", 0.0))
    except Exception:
        return 0.0


def build_state(pair: str, signal_score: float,
                signal_age_candles: int = 0) -> np.ndarray:
    """Construct the 22-dim observation vector from current Redis state.

    All values are returned as a float32 array suitable for SB3 PPO.predict().
    Missing values default to 0; the agent learns to handle gaps.
    """
    import redis_client
    import redis_keys
    r = redis_client.get()

    fc_1m = _load_candle_forecast(r, pair, "1m")
    fc_5m = _load_candle_forecast(r, pair, "5m")
    exh_1m = _load_exhaustion(r, pair, "1m")
    exh_5m = _load_exhaustion(r, pair, "5m")

    # Microstructure — using OFI as a proxy for bid/ask imbalance (already EWMAed)
    try:
        ofi  = _safe_float(r.get(redis_keys.OFI.replace("{pair}", pair)))
        vpin = _safe_float(r.get(redis_keys.VPIN.replace("{pair}", pair)))
    except Exception:
        ofi, vpin = 0.0, 0.0

    spread = _safe_float(r.get(f"{pair}:spread_pct"))

    # Recent volatility, ATR / mark
    mark = _safe_float(r.get(redis_keys.MARK_PRICE.replace("{pair}", pair))) or 1.0
    atr  = _safe_float(r.get(f"{pair}:atr"))
    atr_norm = (atr / mark) if mark > 0 else 0.0

    # Time-in-bar — coarse approximation from system clock modulo 60s
    import time as _t
    tib = (_t.time() % 60.0) / 60.0

    sig_norm = max(0.0, min(1.0, float(signal_score) / 100.0))

    state = np.array([
        _safe_float(fc_1m.get("dir1", 0.5)),
        _safe_float(fc_1m.get("dir3", 0.5)),
        _safe_float(fc_1m.get("dir5", 0.5)),
        _safe_float(fc_1m.get("mag1", 0.0)),
        _safe_float(fc_1m.get("mag3", 0.0)),
        _safe_float(fc_1m.get("mag5", 0.0)),
        _safe_float(fc_1m.get("trend", 0.5)),
        _safe_float(fc_5m.get("dir1", 0.5)),
        _safe_float(fc_5m.get("dir3", 0.5)),
        _safe_float(fc_5m.get("dir5", 0.5)),
        _safe_float(fc_5m.get("mag1", 0.0)),
        _safe_float(fc_5m.get("mag3", 0.0)),
        _safe_float(fc_5m.get("mag5", 0.0)),
        _safe_float(fc_5m.get("trend", 0.5)),
        exh_1m,
        exh_5m,
        spread,
        ofi,
        tib,
        atr_norm,
        sig_norm,
        float(signal_age_candles),
    ], dtype=np.float32)
    return state


# ── Model loading ───────────────────────────────────────────────────────────

def _load_model():
    """Load the PPO agent from disk. Caches by mtime."""
    global _cache
    if not _MODEL_PATH.exists():
        return None
    mtime = _MODEL_PATH.stat().st_mtime
    if _cache["model"] is not None and _cache["mtime"] == mtime:
        return _cache["model"]
    try:
        from stable_baselines3 import PPO
        model = PPO.load(str(_MODEL_PATH), device="cpu")
        _cache["model"] = model
        _cache["mtime"] = mtime
        log.info("entry_timing_agent_loaded", path=str(_MODEL_PATH))
        return model
    except Exception as exc:
        log.warning("entry_timing_agent_load_failed",
                    error=str(exc)[:200])
        return None


# ── Decision API (consumed by signals/engine.py) ────────────────────────────

def decide_entry(pair: str, signal_score: float,
                 signal_age_candles: int = 0) -> str:
    """Return 'wait' | 'enter' | 'skip' for the given pair + signal.

    Fallback to 'enter' (current behaviour) when:
      - paper_closed < 1000 (activation threshold)
      - model file missing
      - SB3 inference fails
      - any redis read fails

    Writes telemetry to Redis:
      brain:entry_timing:decisions_count (incremented per call)
      brain:entry_timing:last_decision   (str of last action)
    """
    import redis_client
    r = redis_client.get()

    # Activation gate
    try:
        paper_closed = int(r.get("brain:paper_closed") or 0)
    except Exception:
        paper_closed = 0
    if paper_closed < _ACTIVATION_TRADES:
        return "enter"

    model = _load_model()
    if model is None:
        return "enter"

    try:
        state = build_state(pair, signal_score, signal_age_candles)
        action, _ = model.predict(state, deterministic=True)
        a = int(action) if hasattr(action, "__int__") else int(action.item())
        decision = _ACTION_NAMES.get(a, "enter")
    except Exception as exc:
        log.warning("entry_timing_predict_failed", pair=pair,
                    error=str(exc)[:200])
        return "enter"

    # Telemetry
    try:
        r.incr("brain:entry_timing:decisions_count")
        r.set("brain:entry_timing:last_decision", decision)
        r.set(f"brain:entry_timing:last_decision:{pair}", decision)
    except Exception:
        pass

    return decision


# ── Training environment + entry point ──────────────────────────────────────

class EntryTimingEnv:
    """Gymnasium-compatible env that replays historical signal events.

    Each episode replays one signal-firing event from history. The env feeds
    the agent the observed state over a 5-candle window after the signal first
    fired. The agent chooses an action at each step; the episode terminates
    when the agent picks `enter` or `skip`, or when 5 candles elapse.

    Reward at termination:
      enter  → (realised_pnl_at_entry_step / atr_at_event)
      skip   → 0   (neutral — preserves capital)
      wait*5 → -0.01 × atr_norm  (mild penalty for indecision past horizon)

    Where realised_pnl is the PnL the trade *would have* achieved had it
    been entered at the chosen candle, using the actual price at entry and
    closing at the signal's eventual close.
    """
    OBS_DIM   = _STATE_DIM
    N_ACTIONS = _N_ACTIONS
    HORIZON   = 5

    def __init__(self, signal_events: list[dict]):
        import gymnasium as gym
        from gymnasium import spaces
        self.events = signal_events
        self._idx = 0
        self._step = 0
        self._cur = None
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(self.OBS_DIM,), dtype=np.float32)
        self.action_space = spaces.Discrete(self.N_ACTIONS)

    def reset(self, *, seed=None, options=None):
        if seed is not None:
            np.random.seed(seed)
        self._idx = np.random.randint(0, len(self.events))
        self._step = 0
        self._cur = self.events[self._idx]
        obs = self._obs_at_step(self._step)
        return obs, {}

    def _obs_at_step(self, step: int) -> np.ndarray:
        """Replay the observation vector that *would have been* visible at
        signal_age=step. The signal_events list stores precomputed states for
        each candle in the post-signal window."""
        states = self._cur.get("states", [])
        if step < len(states):
            return np.asarray(states[step], dtype=np.float32)
        # Fallback to last available state if event truncated
        if states:
            return np.asarray(states[-1], dtype=np.float32)
        return np.zeros(self.OBS_DIM, dtype=np.float32)

    def step(self, action: int):
        a = int(action)
        atr_n = float(self._cur.get("atr_norm", 0.01)) or 0.01
        pnls  = self._cur.get("pnls_per_step", [0.0])

        if a == 1:  # enter
            pnl_at = pnls[self._step] if self._step < len(pnls) else pnls[-1]
            reward = float(pnl_at) / max(atr_n, 1e-6)
            return self._obs_at_step(self._step), reward, True, False, {}
        if a == 2:  # skip
            return self._obs_at_step(self._step), 0.0, True, False, {}

        # wait
        self._step += 1
        if self._step >= self.HORIZON:
            # forced timeout → mild penalty + last-step PnL
            pnl_at = pnls[-1] if pnls else 0.0
            reward = (float(pnl_at) / max(atr_n, 1e-6)) - 0.01
            return self._obs_at_step(self._step - 1), reward, True, False, {}
        return self._obs_at_step(self._step), -0.001, False, False, {}


def _load_signal_history(history_path: Path) -> list[dict]:
    """Load JSONL of historical signal events for training.

    Each line: {pair, signal_score, atr_norm,
                states: [[22-d state per candle, length=5]],
                pnls_per_step: [pnl_if_entered_at_step_0, step_1, ...]}.

    The producer for this JSONL is the brain's signal-logging tap — not yet
    wired in this commit; the training function gracefully reports "no_data"
    until enough events have been recorded.
    """
    if not history_path.exists():
        return []
    out = []
    with open(history_path, "r") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def train_entry_timing(history_path: Path = Path("data/entry_timing_history.jsonl"),
                       models_dir: Path = Path("models"),
                       total_timesteps: int = 1_000_000) -> dict:
    """Train PPO entry-timing agent on replayed signal events.

    Returns dict with status. Refuses to train (and refuses to save a model)
    until at least 200 signal events have been logged — under that the agent
    overfits to noise.
    """
    try:
        from stable_baselines3 import PPO
        from stable_baselines3.common.env_util import make_vec_env
        import gymnasium as gym
    except Exception as exc:
        return {"status": "stable_baselines3_unavailable",
                "error": str(exc)[:200]}

    events = _load_signal_history(history_path)
    if len(events) < 200:
        return {"status": "insufficient_history",
                "events": len(events),
                "note": "need ≥ 200 logged signal events; bot logs them as signals fire"}

    # Gymnasium env factory
    def _make_env():
        return EntryTimingEnv(events)
    env = make_vec_env(_make_env, n_envs=4)

    model = PPO("MlpPolicy", env,
                policy_kwargs={"net_arch": [256, 256]},
                learning_rate=3e-4,
                n_steps=1024,
                batch_size=64,
                gae_lambda=0.95,
                gamma=0.99,
                ent_coef=0.01,
                clip_range=0.2,
                verbose=0,
                device="cpu")

    log.info("entry_timing_train_start", events=len(events),
             timesteps=total_timesteps)
    model.learn(total_timesteps=total_timesteps)
    models_dir.mkdir(parents=True, exist_ok=True)
    out_path = models_dir / "entry_timing_agent.zip"
    model.save(str(out_path))
    log.info("entry_timing_train_done", path=str(out_path))

    global _cache
    _cache = {"model": None, "mtime": None}  # force reload

    return {"status": "trained", "events": len(events), "path": str(out_path)}


def log_signal_event(event: dict,
                     history_path: Path = Path("data/entry_timing_history.jsonl")
                     ) -> None:
    """Append a signal-firing event to the JSONL history. Called from signals
    engine after every fired signal so the agent has training data."""
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(history_path, "a") as fh:
            fh.write(json.dumps(event) + "\n")
    except Exception as exc:
        log.warning("entry_timing_log_failed", error=str(exc)[:200])
