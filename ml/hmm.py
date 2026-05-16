"""L-01: HMM Regime Model — Viterbi decoding every minute."""
import pickle
from pathlib import Path
import numpy as np
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()

_MODEL_PATH = Path("models/hmm_regime.pkl")
_model = None
_REGIMES = {0: "bull", 1: "bear", 2: "turbulent"}


def _load():
    global _model
    if _model is None:
        with open(_MODEL_PATH, "rb") as f:
            _model = pickle.load(f)
    return _model


def get_current_regime() -> str:
    r = redis_client.get()
    return r.get(redis_keys.CURRENT_REGIME) or "unknown"


def update_regime(price_returns: list[float]) -> str:
    """Run Viterbi on latest returns; write regime to Redis."""
    if not price_returns or not _MODEL_PATH.exists():
        return get_current_regime()
    try:
        model = _load()
        obs = np.array(price_returns).reshape(-1, 1)
        state_seq = model.predict(obs)
        current_state = int(state_seq[-1])
        regime = _REGIMES.get(current_state, "bull")

        r = redis_client.get()
        r.set(redis_keys.CURRENT_REGIME, regime)
        log.debug("regime_updated", regime=regime)
        return regime
    except Exception as exc:
        log.error("hmm_update_failed", error=str(exc))
        return get_current_regime()
