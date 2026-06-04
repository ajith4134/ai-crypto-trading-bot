"""F47 — Brain-learned trailing-SL ratchet parameters.

Blueprint §10.4 ("the Brain adjusts trailing distance in real time ...
locking in gains progressively") implies the lock fraction itself is a
learned, not hardcoded, value. The cont. 23 ratchet shipped with a
hand-picked formula `lock_frac = min(0.70, 0.40 + 0.05*(peak_ratio-1))`.
This module replaces the floor (`0.40` base) with a learned per-pair value
trained by constant-α MC + exploration on each closed trade's outcome.

Why MC + exploration (not GA / OPRO):
  - Same rationale as `risk/hedge_params.py`: closed-trade outcomes arrive
    one at a time; we can't batch hundreds for a GA generation. OPRO is
    a prompt-optimisation tool.
  - Same operational pattern as F44 hedge_params (Rule 4 alignment).

What's learned vs hand-picked:
  - LEARNED: `base_lock_frac` (the floor — used at peak_ratio == 1×)
  - HAND-PICKED (deferred): `lock_frac_growth_per_peakratio` (0.05 step),
    `_MAX_LOCK_FRAC` cap (0.70). Could be learned too; deferred to keep
    v1 contained.

Update rule per trade close:
  reward = clip(net_pnl_usdt / peak_pnl_usdt, -1, +1)
  learned_base ← learned_base + α · reward · (base_used - learned_base)
  n_samples += 1

Outcome semantics:
  - Winner with high % of peak kept → reward near +1 → reinforce that base
  - Winner with low % of peak kept (gave back too much) → small positive
    reward → mild reinforcement
  - Loser ratcheted prematurely (peak existed but exit was negative) →
    negative reward → push away from that base
  - No-peak trades (never reached activation): record_outcome is a no-op
    — there was no ratchet apply to learn from

Per-pair vs global:
  v1 stores ONE global `base_lock_frac`. Per-pair / per-regime are listed
  in blueprint §10.4 ("Brain adjusts trailing distance in real time as
  volatility changes during the trade") as future work. Adding a Redis
  hash keyed by pair is mechanical; deferred so v1 has enough sample
  density to converge before sharding.

Storage:
  - Redis `trail:learned_params:base_lock_frac` — JSON {value, n_samples, last_update_ts}
  - Per-trade snapshot at first ratchet apply: `trail:lock_frac_used:{trade_id}`
    (TTL 7 days — longer than any expected hold) — JSON {base_used, applied_ts}

F30 governance gate: F47 — if deactivated, get_lock_frac returns the
hand-picked default and record_outcome is a no-op (no drift accumulation).
"""
from __future__ import annotations
import json
import random
import structlog

import redis_client

log = structlog.get_logger()


_ALPHA = 0.1            # MC step size — matches hedge_params for shape parity
_P_EXPLORE = 0.30       # higher than hedge_params (0.25) because peak-bearing
                        # trades fire ~50/h, giving plenty of exploration headroom
_MIN_SAMPLES = 30       # trust gate — at ~10 ratchet-eligible closes/h on 50 open
                        # slots, this is ~3 hours of evidence before learned > default
_EXPLORE_SIGMA = 0.05   # Gaussian noise scale on base_lock_frac
_PER_TRADE_TTL = 7 * 24 * 3600   # 1 week — outlives any reasonable hold

# Hard absolute bounds — even runaway exploration cannot leave this band.
# Cont. 44 (2026-05-24): floor raised 0.20→0.80, cap raised 0.70→0.92.
# Empirical: pre-cont-44 the bot gave back $44k of peak profit across 2195
# closed trades (avg 17.6% kept in $20-50 peak bucket, 61.1% in $50+ bucket).
# User requested ≥80% lock floor. The learner now adjusts only within the
# 80-92 band — exploration cannot drift below the user-mandated guarantee.
_MIN_BASE_LOCK_FRAC = 0.80
_MAX_BASE_LOCK_FRAC = 0.92
_DEFAULT_BASE_LOCK_FRAC = 0.80   # 80% lock from first apply, untrained

# Growth slope stays hand-picked in v1 (Rule 4 note in module docstring).
# Cont. 44: cap raised 0.70→0.92 to match new max_base. Growth step unchanged.
_LOCK_FRAC_GROWTH_PER_PEAKRATIO = 0.05
_MAX_LOCK_FRAC_CAP = 0.92


def _governance_active() -> bool:
    """F47 gate. When inactive: get_lock_frac returns default, record_outcome
    no-ops. Existing learned state in Redis is preserved (no auto-revert)."""
    try:
        from feature_governance.registry import is_active
        return bool(is_active("F47"))
    except Exception:
        return True   # fail-open: governance unreachable should not disable F47


def _clip_base(v: float) -> float:
    return max(_MIN_BASE_LOCK_FRAC, min(_MAX_BASE_LOCK_FRAC, float(v)))


def _read_learned() -> tuple[float, int]:
    """Return (current_base_lock_frac, n_samples). Defaults on miss."""
    try:
        r = redis_client.get()
        raw = r.get("trail:learned_params:base_lock_frac")
        if raw:
            d = json.loads(raw)
            return float(d.get("value", _DEFAULT_BASE_LOCK_FRAC)), int(d.get("n_samples", 0))
    except Exception:
        pass
    return _DEFAULT_BASE_LOCK_FRAC, 0


def _write_learned(value: float, n_samples: int) -> None:
    try:
        import time as _t
        r = redis_client.get()
        r.set("trail:learned_params:base_lock_frac", json.dumps({
            "value": _clip_base(value),
            "n_samples": int(n_samples),
            "last_update_ts": int(_t.time()),
        }))
    except Exception:
        pass


def get_lock_frac(peak_ratio: float, trade_id: str | None = None,
                  with_exploration: bool = True) -> float:
    """Return the lock_frac to apply at the given peak_ratio.

    `peak_ratio` = peak_profit_pct / activation_pct. Always ≥ 1.0 at the
    call site (ratchet is only invoked when peak ≥ activation).

    `trade_id` (optional): when provided AND exploration fires, stamp the
    drawn base into Redis so memory/write.py:record_outcome can read it back
    at close. When None, no snapshot — caller is treating this as a pure
    read (e.g. dashboard preview).
    """
    if not _governance_active():
        base = _DEFAULT_BASE_LOCK_FRAC
        _maybe_stamp_per_trade(trade_id, base, governance_off=True)
    else:
        learned, n_samples = _read_learned()
        base = learned if n_samples >= _MIN_SAMPLES else _DEFAULT_BASE_LOCK_FRAC
        if with_exploration and random.random() < _P_EXPLORE:
            base = _clip_base(base + random.gauss(0.0, _EXPLORE_SIGMA))
        _maybe_stamp_per_trade(trade_id, base, governance_off=False)
    # Apply the (still hand-picked) growth slope and cap.
    return min(_MAX_LOCK_FRAC_CAP, base + _LOCK_FRAC_GROWTH_PER_PEAKRATIO * (peak_ratio - 1))


def _maybe_stamp_per_trade(trade_id: str | None, base_used: float,
                           governance_off: bool) -> None:
    """First-apply stamp. Idempotent — once a trade has a stamp, later
    applies don't overwrite (the original drawn base is the one that
    determined the trade's protection level). SETNX semantic via SET ... NX.
    """
    if not trade_id:
        return
    try:
        import time as _t
        r = redis_client.get()
        payload = json.dumps({
            "base_used": round(float(base_used), 6),
            "applied_ts": int(_t.time()),
            "governance_off": bool(governance_off),
        })
        # NX: only set if absent. Idempotent across many ratchet applies.
        r.set(f"trail:lock_frac_used:{trade_id}", payload,
              ex=_PER_TRADE_TTL, nx=True)
    except Exception:
        pass


def record_outcome(trade_id: str, peak_pnl_usdt: float, net_pnl_usdt: float) -> dict:
    """Constant-α MC update from one closed trade. Called from
    memory/write.py:write_trade_close. Idempotent per trade via per-trade
    snapshot DEL after read.

    Returns a summary dict for logging. {"status": "skip_*"} means no
    update applied (no snapshot, governance off, no peak).
    """
    if not trade_id:
        return {"status": "skip_no_trade_id"}
    if peak_pnl_usdt is None or peak_pnl_usdt <= 0:
        # No ratchet fired on this trade (peak never beat activation).
        # Best to clean up any stamp (if exists) so it doesn't leak.
        try:
            redis_client.get().delete(f"trail:lock_frac_used:{trade_id}")
        except Exception:
            pass
        return {"status": "skip_no_peak"}

    try:
        r = redis_client.get()
        raw = r.get(f"trail:lock_frac_used:{trade_id}")
        if not raw:
            return {"status": "skip_no_snapshot"}
        snap = json.loads(raw)
        base_used = float(snap.get("base_used", _DEFAULT_BASE_LOCK_FRAC))
        was_governance_off = bool(snap.get("governance_off", False))
        # Always delete the snapshot — idempotent guard against double-call.
        r.delete(f"trail:lock_frac_used:{trade_id}")
    except Exception as exc:
        return {"status": "skip_redis_read_failed", "error": str(exc)[:120]}

    if was_governance_off or not _governance_active():
        # If F47 was off at apply OR off now: do not mutate learned state.
        return {"status": "skip_governance_off"}

    # Reward: fraction of peak preserved, signed. -1 = full peak gave back
    # AND ended in loss; +1 = locked the entire peak.
    reward = max(-1.0, min(1.0, float(net_pnl_usdt) / float(peak_pnl_usdt)))

    current, n_samples = _read_learned()
    # MC: positive reward pulls current TOWARD base_used; negative pushes AWAY.
    delta = _ALPHA * reward * (base_used - current)
    new_value = _clip_base(current + delta)
    new_n = n_samples + 1
    _write_learned(new_value, new_n)

    # Cumulative evidence counters (mirror hedge_params shape).
    try:
        import time as _t
        r.incr("trail_params:updates_count")
        r.set("trail_params:last_update_ts", str(int(_t.time())))
        r.set("trail_params:last_reward", str(round(reward, 4)))
    except Exception:
        pass

    log.info("trail_params_updated",
             trade_id=trade_id,
             reward=round(reward, 4),
             base_used=round(base_used, 4),
             prev_value=round(current, 4),
             new_value=round(new_value, 4),
             delta=round(delta, 6),
             n_samples=new_n)
    return {
        "status": "updated", "reward": round(reward, 4),
        "base_used": round(base_used, 4),
        "prev_value": round(current, 4), "new_value": round(new_value, 4),
        "delta": round(delta, 6), "n_samples": new_n,
    }


def get_state() -> dict:
    """Dashboard read — current learner state."""
    learned, n = _read_learned()
    try:
        r = redis_client.get()
        last_update = r.get("trail_params:last_update_ts")
        last_reward = r.get("trail_params:last_reward")
        updates_count = r.get("trail_params:updates_count")
    except Exception:
        last_update = last_reward = updates_count = None
    return {
        "base_lock_frac": {
            "default": _DEFAULT_BASE_LOCK_FRAC,
            "current": round(learned, 4),
            "n_samples": n,
            "trusted": n >= _MIN_SAMPLES,
            "min": _MIN_BASE_LOCK_FRAC,
            "max": _MAX_BASE_LOCK_FRAC,
            "explore_sigma": _EXPLORE_SIGMA,
            "last_update_ts": int(last_update) if last_update else None,
        },
        "growth_per_peakratio_handpicked": _LOCK_FRAC_GROWTH_PER_PEAKRATIO,
        "max_lock_frac_cap_handpicked": _MAX_LOCK_FRAC_CAP,
        "updates_count": int(updates_count) if updates_count else 0,
        "last_reward": float(last_reward) if last_reward else None,
        "governance_active": _governance_active(),
    }
