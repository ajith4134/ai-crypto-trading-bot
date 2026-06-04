"""
F44 — Brain-learned hedge parameters.

Blueprint Feature 44 (Section 4.1) states: "Brain learns optimal parameters
through OPRO/GA". The hedge logic shipped in D-06 used hardcoded blueprint
defaults for five parameters; this module makes them online-learnable.

Why constant-α MC + exploration (not GA, not OPRO):
  - GA needs hundreds of samples per generation. Hedges fire rarely
    (only when DCA-1 has triggered AND price continues -5%). Observed rate
    is ~1-2 hedges/day — GA would take months to converge.
  - OPRO is a prompt-optimisation tool, not for numeric scalars.
  - The F35 Q-learning module uses constant-α MC with exploration noise on a
    similar small-sample regime; this module follows the same shape so the
    operational pattern (recovery, fallback-to-defaults, evidence keys) is
    already familiar.

Update rule per parameter:
  - On each hedge OPEN: draw an exploration value with prob `_P_EXPLORE` —
    `value_used = current ± Gaussian(0, explore_sigma)`, clipped to bounds.
    Otherwise use the learned value directly.
  - Persist `value_used` per parameter to the hedge trade row's
    feature_vector JSONB so `record_outcome` can read it back at close.
  - On hedge CLOSE: outcome score `r = clip(net_pnl_usdt / capital_usdt, -1, +1)`.
    For each parameter: `learned ← learned + α · sign(r) · (value_used - learned)`.
    Winning trades pull the learned value TOWARD the value that produced the
    win; losing trades push it AWAY. `n_samples` increments per close.

Fallback semantics:
  `get_param(name)` returns the BLUEPRINT DEFAULT when `n_samples < _MIN_SAMPLES`,
  preserving F44's original hardcoded behaviour until enough closes have
  accumulated for the learned value to be trusted. Same shape as the F35
  q_learning `is_q_trustworthy` gate.
"""
from __future__ import annotations
import json
import random
import structlog

import redis_client

log = structlog.get_logger()


_ALPHA = 0.1       # MC step size
_P_EXPLORE = 0.25  # probability of adding Gaussian noise on each open
# cont. 41: lowered 5 → 1. Cont. 23 trailing ratchet closes trades early
# so DCA-1 (hedge prerequisite) rarely fires; observed hedge rate is
# ~1 per 2 days. At _MIN_SAMPLES=5 the learned value would never become
# trusted (10+ days of waiting), so get_param() always returned the
# blueprint default — the F44 panel was decorative. _MIN_SAMPLES=1 lets
# the FIRST closed hedge's update actually shape subsequent hedge opens,
# while the explore_sigma + clip bounds prevent runaway. Re-evaluate
# upward only if hedge fire rate climbs back above ~5/day.
_MIN_SAMPLES = 1   # gate: learned value only trusted past this many closes

# Per-parameter metadata: default = blueprint hardcoded value the module replaces.
# min/max = absolute bounds; the learned value is clipped at every update so a
# noisy outcome can't drive it to a degenerate value (e.g. cap_frac → 1.0 would
# put 100% of capital into hedges).
PARAMS: dict[str, dict] = {
    "trigger_pct_beyond_dca": {
        "default": 0.05, "min": 0.02, "max": 0.10, "explore_sigma": 0.005,
        "desc": "additional % move past DCA-1 trigger required to fire hedge",
    },
    "expected_continuation_pct": {
        "default": 0.05, "min": 0.03, "max": 0.10, "explore_sigma": 0.005,
        "desc": "expected continuation %; appears in sizing denominator",
    },
    "hedge_cap_frac": {
        "default": 0.50, "min": 0.30, "max": 0.70, "explore_sigma": 0.03,
        "desc": "max hedge capital as fraction of parent trade capital",
    },
    "min_directional_accuracy": {
        "default": 55.0, "min": 50.0, "max": 70.0, "explore_sigma": 1.0,
        "desc": "minimum brain directional accuracy on pair to allow hedge",
    },
    "breakeven_lock_pct": {
        "default": 0.05, "min": 0.03, "max": 0.08, "explore_sigma": 0.003,
        "desc": "hedge profit % that triggers entry-SL breakeven lock",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Storage — Redis keys, one hash per param.
# ─────────────────────────────────────────────────────────────────────────────

def _key(name: str) -> str:
    return f"hedge:learned_params:{name}"


def _clip(name: str, value: float) -> float:
    meta = PARAMS[name]
    return max(meta["min"], min(meta["max"], float(value)))


def _read_state(name: str) -> tuple[float, int]:
    """Return (current_value, n_samples). Defaults to (default, 0) on miss."""
    try:
        r = redis_client.get()
        raw = r.get(_key(name))
        if raw:
            d = json.loads(raw)
            return float(d.get("value", PARAMS[name]["default"])), int(d.get("n_samples", 0))
    except Exception:
        pass
    return PARAMS[name]["default"], 0


def _write_state(name: str, value: float, n_samples: int) -> None:
    try:
        import time as _t
        r = redis_client.get()
        r.set(_key(name), json.dumps({
            "value": _clip(name, value),
            "n_samples": int(n_samples),
            "last_update_ts": int(_t.time()),
        }))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Public API consumed by risk/hedge.py and memory/write.py.
# ─────────────────────────────────────────────────────────────────────────────

def get_param(name: str, with_exploration: bool = False) -> float:
    """Return the current value for `name`. Falls back to blueprint default when
    n_samples < _MIN_SAMPLES. With `with_exploration=True`, occasionally returns
    a noisy value (clipped to bounds) so the learner can sample alternatives.
    """
    if name not in PARAMS:
        raise KeyError(f"unknown hedge param: {name}")
    meta = PARAMS[name]
    value, n_samples = _read_state(name)
    if n_samples < _MIN_SAMPLES:
        # Still in the warm-up window — exploration still allowed on the
        # default value so we generate diverse outcomes during warm-up.
        base = meta["default"]
    else:
        base = value
    if with_exploration and random.random() < _P_EXPLORE:
        # Symmetric Gaussian noise.
        base = base + random.gauss(0.0, meta["explore_sigma"])
    return _clip(name, base)


def sample_open_params() -> dict[str, float]:
    """Convenience for `risk/hedge.py` at hedge-open time: return one dict
    with all five params, each sampled with exploration. The caller stamps
    this dict on the hedge trade's feature_vector so we can read it back at
    close and update the learner with the right (value_used → outcome) pair."""
    return {name: get_param(name, with_exploration=True) for name in PARAMS}


def record_outcome(params_used: dict[str, float], net_pnl_usdt: float,
                   capital_usdt: float) -> dict:
    """Apply constant-α MC update to every learned parameter from one hedge close.

    Called from memory/write.py:write_trade_close when the closed trade is a
    hedge (trade.hedge_of_trade_id IS NOT NULL). `params_used` is the per-param
    snapshot recorded at hedge open (read back from feature_vector).

    Returns a summary dict for logging.
    """
    if not params_used or capital_usdt <= 0:
        return {"status": "skip_no_data"}

    # Outcome score in [-1, +1]. A 10% gain on a $200 hedge = +0.10 → small
    # positive nudge; a -50% loss → -0.50; clipped at the extremes so a single
    # blow-up doesn't dominate the running average.
    reward = max(-1.0, min(1.0, float(net_pnl_usdt) / float(capital_usdt)))

    updates: list[dict] = []
    for name, value_used in params_used.items():
        if name not in PARAMS:
            continue
        try:
            value_used = float(value_used)
        except (TypeError, ValueError):
            continue
        current, n_samples = _read_state(name)
        # Push direction: when reward > 0, pull current toward value_used
        # (this value worked). When reward < 0, push current away.
        delta = _ALPHA * reward * (value_used - current)
        new_value = _clip(name, current + delta)
        new_n = n_samples + 1
        _write_state(name, new_value, new_n)
        updates.append({
            "name": name,
            "value_used": round(value_used, 6),
            "old_value":  round(current,  6),
            "new_value":  round(new_value, 6),
            "delta":      round(delta,     6),
            "n_samples":  new_n,
        })

    # Cumulative evidence keys (mirror F35 q_learning style).
    try:
        import time as _t
        r = redis_client.get()
        r.incr("hedge_params:updates_count")
        r.set("hedge_params:last_update_ts", str(int(_t.time())))
        r.set("hedge_params:last_reward", str(round(reward, 4)))
    except Exception:
        pass

    log.info("hedge_params_updated",
             reward=round(reward, 4),
             updates=len(updates))
    return {"status": "updated", "reward": round(reward, 4), "updates": updates}


def get_all_learned() -> list[dict]:
    """Dashboard read — return per-param current state plus metadata.
    Each entry: {name, default, current, n_samples, trusted, last_update_ts,
                 min, max, explore_sigma, desc}.
    """
    out = []
    for name, meta in PARAMS.items():
        value, n_samples = _read_state(name)
        last_ts = None
        try:
            raw = redis_client.get().get(_key(name))
            if raw:
                last_ts = json.loads(raw).get("last_update_ts")
        except Exception:
            pass
        out.append({
            "name": name,
            "default": meta["default"],
            "current": round(value, 6),
            "n_samples": n_samples,
            "trusted": n_samples >= _MIN_SAMPLES,
            "min": meta["min"],
            "max": meta["max"],
            "explore_sigma": meta["explore_sigma"],
            "last_update_ts": last_ts,
            "desc": meta["desc"],
        })
    return out
