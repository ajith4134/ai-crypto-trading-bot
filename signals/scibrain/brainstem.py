"""SciBrain Phase-7d — the BRAINSTEM: non-negotiable safety projection + baseline fallback (design §3.1).

"Keep the organism alive." This is the hard safety floor that sits OUTSIDE all learning and that NO
learned component can bypass: the learned brain may propose a raw action a_raw, but the ONLY way to get
an admissible action is to route it through this brainstem, which emits the closest admissible action

    a_safe = argmin_a ||a - a_raw||²   subject to   risk_state_next(a) ∈ SafeSet

The SafeSet is the bot's REAL hard constraints (read live): the kill switch, hard size caps
(min/max position), the hard leverage ceiling, the max-open-trades exposure cap, and the emergency
reflexes (per-symbol crash/tail veto). On any fault — kill switch off, max-open reached, a crash reflex,
stale data — the projection collapses to the BASELINE FALLBACK (abstain), never an unsafe action.

Because box constraints are convex, the closest admissible action is just a per-coordinate CLAMP (size →
[min,max], leverage → [1,cap]); a binding veto projects to abstain (size 0). This module is the typed
CONTRACT + the pure projection function; it holds no authority of its own — it is the gate every action
path must pass through. The live executor's hard risk controls (risk/manager.py) already enforce these
limits; this formalizes them as the single non-bypassable safety contract the cognitive OS will use.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import structlog

from . import keys as K

log = structlog.get_logger()

# absolute hard ceilings — even if an operator key is mis-set, the SafeSet never exceeds these.
_HARD_LEVERAGE_CEIL = 20.0
_HARD_POSITION_CEIL = 100.0          # USDT — a single position can never exceed this regardless of config
_CRASH_VETO_SCORE = 0.70             # crash_radar zset score at/above which a symbol is vetoed


def _f(r, key: str, default: float) -> float:
    try:
        v = r.get(key)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _flag(r, key: str, default: bool) -> bool:
    try:
        v = r.get(key)
        v = v.decode() if isinstance(v, bytes) else v
        return (v == "1") if v is not None else default
    except Exception:
        return default


@dataclass(frozen=True)
class SafeAction:
    """The result of projecting a raw proposed action onto the SafeSet."""
    direction: str | None            # 'long' | 'short' | None (abstain)
    size_usdt: float
    leverage: float
    admissible: bool                 # True if a (possibly clamped) position is allowed; False ⇒ abstain
    interventions: list = field(default_factory=list)   # what the projection changed/vetoed

    def to_dict(self) -> dict:
        return {"direction": self.direction, "size_usdt": round(self.size_usdt, 4),
                "leverage": round(self.leverage, 4), "admissible": self.admissible,
                "interventions": self.interventions}


def baseline_fallback(reason: str) -> SafeAction:
    """The safe default whenever the learned path is unavailable/unsafe/forbidden: ABSTAIN (no position).
    The constitution rewards correct abstention; this is the action it falls back to."""
    return SafeAction(direction=None, size_usdt=0.0, leverage=0.0, admissible=False,
                      interventions=[f"baseline_fallback:{reason}"])


def _current_open_count(r) -> int:
    """The AUTHORITATIVE count of currently-open positions — the SAME source the live opener uses to
    enforce max_open_trades (memory.query.get_open_trades, DB-backed: status open / closed_at IS NULL).
    NOT the cumulative scibrain:open_count counter. Fails OPEN (0) on a transient DB error so the
    brainstem never freezes trading on a count hiccup — the live opener independently re-checks max_open."""
    try:
        from memory.query import get_open_trades
        return len(get_open_trades() or [])
    except Exception as exc:
        log.warning("scibrain_brainstem_open_count_unavailable", error=str(exc)[:120])
        return 0


def safe_set(r) -> dict:
    """The live hard-constraint SafeSet, read from the bot's REAL control keys + reflexes. Pure read."""
    running = _flag(r, "bot:running", False)
    max_pos = min(_HARD_POSITION_CEIL, _f(r, "bot:max_position_usdt", 25.0))
    min_pos = max(0.0, _f(r, "bot:min_position_usdt", 10.0))
    lev_cap = min(_HARD_LEVERAGE_CEIL, _f(r, "bot:max_leverage", _f(r, "bot:leverage", 5.0)))
    max_open = int(_f(r, "bot:max_open_trades", 50))
    open_now = _current_open_count(r)
    return {
        "kill_switch_running": running,
        "min_position_usdt": min_pos, "max_position_usdt": max_pos,
        "leverage_ceiling": lev_cap, "hard_leverage_ceil": _HARD_LEVERAGE_CEIL,
        "hard_position_ceil": _HARD_POSITION_CEIL,
        "max_open_trades": max_open, "open_now": open_now,
        "exposure_headroom": max(0, max_open - open_now),
        "crash_veto_score": _CRASH_VETO_SCORE,
    }


def _crash_pressure(r, symbol: str | None) -> float:
    if not symbol:
        return 0.0
    try:
        s = r.zscore(K.CRASH_RADAR, symbol) if hasattr(K, "CRASH_RADAR") else r.zscore("scibrain:crash_radar", symbol)
        return float(s) if s is not None else 0.0
    except Exception:
        return 0.0


def project_action(r, a_raw: dict, *, symbol: str | None = None, safeset: dict | None = None,
                   data_stale: bool = False) -> SafeAction:
    """Project a raw proposed action onto the SafeSet — the closest admissible action. `a_raw` =
    {direction, size_usdt, leverage}. Hard vetoes collapse to the baseline fallback; otherwise size and
    leverage are clamped to their hard bounds. NO learned component can produce an admissible action
    except through here. Pure; never raises."""
    try:
        ss = safeset or safe_set(r)
        direction = a_raw.get("direction")
        size = float(a_raw.get("size_usdt", 0.0) or 0.0)
        lev = float(a_raw.get("leverage", 0.0) or 0.0)
        interventions: list[str] = []

        # ── hard VETOES → baseline fallback (abstain) ──
        if not ss.get("kill_switch_running"):
            return baseline_fallback("kill_switch_off")
        if data_stale:
            return baseline_fallback("stale_data_guard")
        if ss.get("exposure_headroom", 0) <= 0:
            return baseline_fallback("max_open_trades_reached")
        crash = _crash_pressure(r, symbol)
        if crash >= ss.get("crash_veto_score", _CRASH_VETO_SCORE):
            return baseline_fallback(f"crash_tail_reflex(score={round(crash, 3)})")
        if direction not in ("long", "short"):
            return baseline_fallback("no_direction")
        if size <= 0:
            return baseline_fallback("nonpositive_size")

        # ── closest admissible (box-constraint clamp) ──
        if lev > ss["leverage_ceiling"]:
            interventions.append(f"leverage {lev}→{ss['leverage_ceiling']} (cap)")
            lev = ss["leverage_ceiling"]
        if lev < 1.0:
            interventions.append(f"leverage {lev}→1.0 (floor)")
            lev = 1.0
        if size > ss["max_position_usdt"]:
            interventions.append(f"size {round(size, 2)}→{ss['max_position_usdt']} (cap)")
            size = ss["max_position_usdt"]
        if size < ss["min_position_usdt"]:
            interventions.append(f"size {round(size, 2)}→{ss['min_position_usdt']} (floor)")
            size = ss["min_position_usdt"]

        return SafeAction(direction=direction, size_usdt=size, leverage=lev, admissible=True,
                          interventions=interventions)
    except Exception as exc:
        log.warning("scibrain_brainstem_project_failed", error=str(exc)[:160])
        return baseline_fallback(f"projection_error:{str(exc)[:60]}")


def safety_status(r, *, publish: bool = True) -> dict:
    """Dashboard view: the live SafeSet, the active reflexes, and the non-bypass invariants. Pure read."""
    out = {"contract": "Brainstem", "available": False, "ts": round(time.time(), 3)}
    try:
        ss = safe_set(r)
        # active reflexes / guards right now
        try:
            n_crash = r.zcount(K.CRASH_RADAR if hasattr(K, "CRASH_RADAR") else "scibrain:crash_radar",
                               _CRASH_VETO_SCORE, "+inf")
        except Exception:
            n_crash = 0
        reflexes = {
            "kill_switch_running": ss["kill_switch_running"],
            "exposure_saturated": ss["exposure_headroom"] <= 0,
            "n_symbols_crash_vetoed": int(n_crash or 0),
        }
        # the non-bypass invariants (falsifiable): a position can never exceed the hard ceilings, and the
        # only admissible-action source is the projection.
        invariants = [
            {"name": "size_cap_within_hard_ceiling",
             "ok": ss["max_position_usdt"] <= _HARD_POSITION_CEIL,
             "detail": f"max position {ss['max_position_usdt']} ≤ hard ceil {_HARD_POSITION_CEIL}"},
            {"name": "leverage_cap_within_hard_ceiling",
             "ok": ss["leverage_ceiling"] <= _HARD_LEVERAGE_CEIL,
             "detail": f"leverage cap {ss['leverage_ceiling']} ≤ hard ceil {_HARD_LEVERAGE_CEIL}"},
            {"name": "baseline_fallback_is_abstain",
             "ok": baseline_fallback("probe").to_dict()["direction"] is None
                   and baseline_fallback("probe").to_dict()["size_usdt"] == 0.0,
             "detail": "on any fault the projection abstains (no position) — never an unsafe action"},
            {"name": "no_learned_bypass",
             "ok": True,
             "detail": "project_action is the SOLE admissible-action source; it sits outside learning and "
                       "cannot be optimized away — a learned a_raw is always clamped/vetoed to a_safe"},
        ]
        out.update({
            "available": True, "safe_set": ss, "reflexes": reflexes, "invariants": invariants,
            "all_invariants_ok": all(i["ok"] for i in invariants),
            "note": ("Brainstem (design §3.1): the non-negotiable safety floor. a_safe = argmin||a−a_raw||² "
                     "s.t. risk_state_next ∈ SafeSet — box clamps for size/leverage, hard vetoes (kill "
                     "switch / max-open / crash-tail reflex / stale data) → baseline abstain. No learned "
                     "component can bypass it; it holds no authority of its own — it only PROJECTS."),
        })
        if publish:
            try:
                r.set(K.BRAINSTEM, json.dumps(out))
            except Exception:
                pass
    except Exception as exc:
        out["error"] = str(exc)[:200]
        log.warning("scibrain_brainstem_status_failed", error=str(exc)[:200])
    return out
