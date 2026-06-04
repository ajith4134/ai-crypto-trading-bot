"""cont. 60 — ML-heavy frontier exit features (scaffolded; need training).

  * PPO exit policy: continuous SL/TP from a trained PPO actor.
  * Deep Hedging: CVaR-minimizing exit policy from a deep neural network.

These two require offline training (not feasible in one session). The
scaffolding loads a checkpoint from a known path and falls through cleanly
when the checkpoint is missing.

When the user trains a checkpoint (`models/ppo_exit_actor.zip` or
`models/deep_hedging_exit.pt`), the loader picks it up automatically on the
next sl_monitor tick — no code change needed.

Training contracts (for the future training session):
  * PPO actor input: 16-dim observation
      (pct_change, time_in_trade_h, vol_unit, vpin, ofi, cvd, hawkes_lambda,
       regime_id, peak_pnl_pct, drawdown_pct, atr_distance, funding_rate,
       sentiment, dvol_decile, is_short, leverage_norm)
  * PPO actor output: 3-dim action
      (sl_mult [0.5-2.0], trail_aggression [0-1], force_exit_logit)
  * Reward: cumulative pnl - 5% × max_drawdown_penalty
  * Source: arXiv "Risk-Aware PPO" (2025), DRL Ensemble arXiv:2511.12120.

  * Deep Hedging input: full price/cost path tensor
  * Deep Hedging output: continuous exit intensity per timestep
  * Source: arXiv:2504.05521, arXiv:2512.12420, MDPI 2025.
"""
from __future__ import annotations
from typing import Optional
import os
import structlog

from .decision import ExitDecision

log = structlog.get_logger()


_PPO_CHECKPOINT  = "/app/models/ppo_exit_actor.zip"
_DH_CHECKPOINT   = "/app/models/deep_hedging_exit.pt"

# Module-level singletons. None == checkpoint missing.
_ppo_actor = None
_ppo_loaded_check_ts = 0
_dh_model  = None
_dh_loaded_check_ts = 0


def _try_load_ppo():
    """Lazy-load PPO actor. Re-checks every 5 minutes (training may produce
    a new checkpoint mid-session)."""
    global _ppo_actor, _ppo_loaded_check_ts
    import time
    now = time.time()
    if _ppo_actor is not None and (now - _ppo_loaded_check_ts) < 300:
        return _ppo_actor
    if not os.path.exists(_PPO_CHECKPOINT):
        _ppo_loaded_check_ts = now
        return None
    try:
        from stable_baselines3 import PPO as _PPO
        _ppo_actor = _PPO.load(_PPO_CHECKPOINT)
        _ppo_loaded_check_ts = now
        log.info("ppo_exit_actor_loaded", path=_PPO_CHECKPOINT)
        return _ppo_actor
    except Exception as exc:
        log.warning("ppo_exit_actor_load_failed", error=str(exc)[:120])
        _ppo_loaded_check_ts = now
        return None


def _try_load_dh():
    """Lazy-load Deep Hedging policy network."""
    global _dh_model, _dh_loaded_check_ts
    import time
    now = time.time()
    if _dh_model is not None and (now - _dh_loaded_check_ts) < 300:
        return _dh_model
    if not os.path.exists(_DH_CHECKPOINT):
        _dh_loaded_check_ts = now
        return None
    try:
        import torch
        _dh_model = torch.jit.load(_DH_CHECKPOINT, map_location="cpu")
        _dh_model.eval()
        _dh_loaded_check_ts = now
        log.info("deep_hedging_exit_loaded", path=_DH_CHECKPOINT)
        return _dh_model
    except Exception as exc:
        log.warning("deep_hedging_exit_load_failed", error=str(exc)[:120])
        _dh_loaded_check_ts = now
        return None


def _build_ppo_obs(trade, mark, r, sl_level, direction) -> Optional[list]:
    """Build the 16-dim observation vector for the PPO actor."""
    import redis_keys
    entry = float(trade.get("average_entry") or trade.get("entry_price") or 0)
    if entry <= 0 or mark <= 0:
        return None
    pct_change = (mark - entry) / entry * (1 if direction == "long" else -1)
    capital = float(trade.get("capital_usdt") or 0)
    leverage = int(trade.get("leverage") or 1)
    notional = max(capital * leverage, 1)
    peak_pnl = float(trade.get("peak_pnl_usdt") or 0)
    peak_loss = float(trade.get("peak_loss_usdt") or 0)
    peak_pct = peak_pnl / notional
    drawdown_pct = peak_loss / notional
    pair = trade["pair"]
    try:
        time_in_trade_h = float(trade.get("hold_time_seconds") or 0) / 3600.0
    except (TypeError, ValueError):
        time_in_trade_h = 0.0
    vol_unit = 0.005
    try:
        from risk.manager import _volatility_unit
        vol_unit = _volatility_unit(r, pair)
    except Exception:
        pass

    def _gf(key, default=0.0):
        try:
            v = r.get(key)
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    vpin = _gf(redis_keys.VPIN.replace("{pair}", pair))
    ofi  = _gf(redis_keys.OFI.replace("{pair}", pair))
    cvd  = _gf(f"{pair}:cvd_now")
    hawkes = _gf(f"{pair}:hawkes_intensity")
    funding = _gf(f"{pair}:funding_rate")
    sentiment = _gf(redis_keys.SENTIMENT_PAIR.replace("{pair}", pair), 0.5)
    dvol_decile = _gf("external:dvol:decile_current", 5.0)
    regime_map = {"bull": 0, "bear": 1, "turbulent": 2, "unknown": 3}
    regime = regime_map.get(str(r.get(redis_keys.CURRENT_REGIME) or "unknown").lower(), 3)
    sl_distance = abs(mark - sl_level) / mark if sl_level > 0 else 0.05

    return [
        pct_change, time_in_trade_h, vol_unit, vpin, ofi, cvd, hawkes,
        float(regime), peak_pct, drawdown_pct, sl_distance, funding,
        sentiment, dvol_decile, 1.0 if direction == "short" else 0.0,
        leverage / 20.0,
    ]


# ─────────────────────────────────────────────────────────────────────────
# 13. PPO learned exit policy
# ─────────────────────────────────────────────────────────────────────────
def evaluate_ppo_exit_policy(trade, mark, r, sl_level, direction) -> Optional[ExitDecision]:
    """If a trained PPO actor exists, query it and apply its (sl_mult,
    trail_aggression, force_exit_logit) output as an exit decision.

    Falls through cleanly (returns None) when checkpoint missing.
    """
    actor = _try_load_ppo()
    if actor is None:
        return None
    obs = _build_ppo_obs(trade, mark, r, sl_level, direction)
    if obs is None:
        return None
    try:
        import numpy as np
        action, _ = actor.predict(np.array(obs, dtype=np.float32), deterministic=True)
    except Exception as exc:
        log.debug("ppo_exit_predict_failed", error=str(exc)[:120])
        return None
    if not hasattr(action, "__len__") or len(action) < 3:
        return None
    sl_mult, trail_aggr, force_exit_logit = float(action[0]), float(action[1]), float(action[2])
    # Sigmoid the logit
    import math
    force_p = 1.0 / (1.0 + math.exp(-force_exit_logit))
    if force_p > 0.7:
        try:
            r.incr("trail:ppo_exit_force_count")
        except Exception:
            pass
        return ExitDecision(force_close=True,
                            reason="ppo_policy_exit",
                            notes={"force_p": round(force_p, 3)})
    # Clamp sl_mult to [0.4, 2.0]
    sl_mult = max(0.4, min(2.0, sl_mult))
    if sl_mult < 0.95:
        try:
            r.incr("trail:ppo_tighten_count")
        except Exception:
            pass
        return ExitDecision(tighten_sl_mult=sl_mult,
                            notes={"ppo_sl_mult": round(sl_mult, 3),
                                   "trail_aggr": round(trail_aggr, 3)})
    if sl_mult > 1.05:
        try:
            r.incr("trail:ppo_loosen_count")
        except Exception:
            pass
        return ExitDecision(loosen_sl_mult=sl_mult,
                            notes={"ppo_sl_mult": round(sl_mult, 3)})
    return None


# ─────────────────────────────────────────────────────────────────────────
# 14. Deep Hedging exit policy
# ─────────────────────────────────────────────────────────────────────────
def evaluate_deep_hedging_exit(trade, mark, r, sl_level, direction) -> Optional[ExitDecision]:
    """If a Deep Hedging policy net exists, query it for an exit intensity
    in [0, 1]. Intensity > 0.7 → force close. 0.3-0.7 → tighten.

    Falls through cleanly when checkpoint missing.
    """
    model = _try_load_dh()
    if model is None:
        return None
    obs = _build_ppo_obs(trade, mark, r, sl_level, direction)
    if obs is None:
        return None
    try:
        import torch
        x = torch.tensor(obs, dtype=torch.float32).unsqueeze(0)
        with torch.no_grad():
            out = model(x)
        intensity = float(out.squeeze().item())
    except Exception as exc:
        log.debug("deep_hedging_predict_failed", error=str(exc)[:120])
        return None
    if intensity > 0.7:
        try:
            r.incr("trail:deep_hedging_force_count")
        except Exception:
            pass
        return ExitDecision(force_close=True,
                            reason="deep_hedging_exit",
                            notes={"intensity": round(intensity, 3)})
    if intensity > 0.3:
        # Map 0.3-0.7 to tighten 0.85-0.55
        tighten = 0.85 - 0.75 * (intensity - 0.3)
        try:
            r.incr("trail:deep_hedging_tighten_count")
        except Exception:
            pass
        return ExitDecision(tighten_sl_mult=tighten,
                            notes={"intensity": round(intensity, 3),
                                   "tighten_mult": round(tighten, 3)})
    return None
