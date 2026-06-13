"""SciBrain runner — orchestrate one scoring cycle and stream all internals to Redis.

Public API:
  score_symbol(r, symbol, *, interrogate=False) -> Decision
      build frame → run module bank → fuse → mirror modules+decision to scibrain:{sym}:*;
      if interrogate, run the dual-brain Ollama interrogator and mirror the transcript.
  run_cycle(r, symbols=None, *, top_k=5, interrogate=None) -> dict
      score a list of symbols (default: launch_pad buffer occupants, else a sample of the
      active universe), interrogate the top_k by conviction, update heartbeat + counters.

Read-only w.r.t. trading. Writes ONLY scibrain:* visibility keys. Gated by scibrain:enabled
only at the engine wiring layer (Phase 1 last task) — scoring/visibility always allowed so the
panel works at observe authority.
"""
from __future__ import annotations

import json
import time

import structlog

from . import ic_tracker
from . import keys as K
from .contracts import Decision
from .fusion import fuse
from .modules import MODULES
from .router import route
from .sensor_bus import build_frame

log = structlog.get_logger()


def _interrogate_enabled(r) -> bool:
    try:
        return r.get(K.INTERROGATE) == "1"
    except Exception:
        return False


def _router_enabled(r) -> bool:
    try:
        return (r.get(K.ROUTER_ENABLED) or "1") != "0"
    except Exception:
        return True


def _router_strength(r) -> float:
    try:
        v = r.get(K.ROUTER_STRENGTH)
        return max(0.0, min(1.0, float(v))) if v is not None else 1.0
    except (TypeError, ValueError):
        return 1.0


def _family_penalty_strength(r) -> float:
    """§6g.330 evidence-family correlation penalty strength. Default 1.0 (owner: full apply,
    2026-06-11); SET scibrain:family_penalty_strength=0 for instant no-op/rollback."""
    try:
        v = r.get(K.FAMILY_PENALTY_STRENGTH)
        return max(0.0, min(1.0, float(v))) if v is not None else 1.0
    except (TypeError, ValueError):
        return 1.0


def _redundancy_report(r) -> dict | None:
    """The info_geometry HSIC redundancy matrix (scibrain:infogeo:health) the penalty consumes for
    its empirical similarity. Best-effort: absent/stale/malformed → None → fusion falls back to the
    structural same-family prior alone (still active in cold start)."""
    try:
        raw = r.get(K.INFOGEO_HEALTH)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _crash_warning(outputs) -> float:
    """Pull the StatPhysSOC crash_warning [0,1] out of this cycle's module outputs."""
    for o in outputs:
        if o.module == "statphys_soc":
            try:
                return float(o.features.get("crash_warning", 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _universe_contrib(r, symbol: str) -> list:
    """Fold in the Universe-Core modules' per-symbol votes for `symbol` (published once/cycle by
    the gate from the shared UniverseFrame). Returns a list[ModuleOutput] (possibly empty). These
    carry shadow_only=True → fusion records but never applies them; ic_tracker still grades them so
    they accumulate the incremental-IC evidence promotion needs. Best-effort: absent/stale key → []."""
    try:
        raw = r.get(K.UNIVERSE_CONTRIB.replace("{sym}", symbol))
        if not raw:
            return []
        from .contracts import ModuleOutput
        return [ModuleOutput.from_dict(d) for d in json.loads(raw)]
    except Exception:
        return []


def score_symbol(r, symbol: str, *, interrogate: bool = False) -> Decision:
    """Full per-symbol pass with live-visibility streaming. Never raises."""
    frame = build_frame(r, symbol)
    outputs = [m.evaluate(frame) for m in MODULES]
    # Universe-Core votes (cross-market, computed once/cycle by the gate) — appended so they're
    # recorded + IC-evaluable; shadow_only keeps them out of the live directional/gate vote.
    outputs.extend(_universe_contrib(r, symbol))

    # Meta-Router (Layer 2, MoE): detect the regime + select/reweight the experts that
    # fit it, folding in each module's rolling IC (adaptive weights). Disabled →
    # router=None → fusion reverts to pure-conviction weighting.
    router_state = None
    if _router_enabled(r):
        ic_map = ic_tracker.get_ic_map(r)
        # Phase-7c bounded canary: an owner-approved (module, regime) gain override, or None (default →
        # byte-identical routing). get_active_override is a cheap guarded read (None unless enabled+active).
        from . import canary as _canary
        router_state = route(outputs, ic_map=ic_map, strength=_router_strength(r),
                             canary=_canary.get_active_override(r))
    # §6g.330 evidence-family correlation penalty (real money → behind a strength knob; 0 = no-op).
    decision = fuse(symbol, outputs, frame, router=router_state,
                    redundancy=_redundancy_report(r),
                    penalty_strength=_family_penalty_strength(r))

    # Record votes for outcome-graded adaptive evidence at IC_HORIZON_MIN.
    ref_price = frame.last_price
    if ref_price is None or ref_price <= 0:
        closes = frame.closes("5m")
        ref_price = float(closes[-1]) if closes is not None and len(closes) else None
    ic_tracker.record(r, symbol, ref_price, outputs)

    # crash-radar: surface the StatPhysSOC early-warning to the dashboard. Keep only
    # elevated symbols on the radar (drop the noise) and cap its size so it stays a
    # focused "what's about to break" list rather than the whole universe.
    crash_warning = _crash_warning(outputs)

    # stream module evidence + decision (best-effort; visibility must not break scoring)
    try:
        pipe = r.pipeline()
        pipe.setex(K.sym_key(K.MODULES, symbol), K.HOT_TTL,
                   json.dumps([o.to_dict() for o in outputs]))
        pipe.setex(K.sym_key(K.DECISION, symbol), K.HOT_TTL,
                   json.dumps(decision.to_dict()))
        pipe.zadd(K.LAST_DECISIONS, {symbol: time.time()})
        if router_state is not None:
            pipe.incr(K.REGIME_COUNT.replace("{regime}", router_state.regime))
            if router_state.deactivated:
                pipe.incrby(K.DEACTIVATED, len(router_state.deactivated))
        if crash_warning >= 0.2:
            pipe.zadd(K.CRASH_RADAR, {symbol: round(crash_warning, 4)})
        else:
            pipe.zrem(K.CRASH_RADAR, symbol)
        pipe.zremrangebyrank(K.CRASH_RADAR, 0, -201)   # keep top 200 by score
        pipe.incr(K.SCORED)
        pipe.execute()
    except Exception as exc:
        log.debug("scibrain_stream_failed", symbol=symbol, error=str(exc)[:120])

    if interrogate:
        _interrogate_and_stream(r, decision)
    return decision


def _interrogate_and_stream(r, decision: Decision) -> None:
    from .interrogator import interrogate as _interrogate   # lazy (pulls llm client)
    from .interrogator import LEAD_MODEL as _DEF_LEAD, CRITIC_MODEL as _DEF_CRITIC
    # optional Redis overrides for the models (C6: configurable, not hardcoded)
    try:
        lead = r.get(K.LEAD_MODEL) or _DEF_LEAD
        critic = r.get(K.CRITIC_MODEL) or _DEF_CRITIC
        use_cloud = (r.get(K.AUDIT_USE_CLOUD) or "1") != "0"
    except Exception:
        lead, critic, use_cloud = _DEF_LEAD, _DEF_CRITIC, True
    try:
        result = _interrogate(decision, lead_model=lead, critic_model=critic,
                              use_cloud=use_cloud)
    except Exception as exc:
        log.warning("scibrain_interrogate_error", symbol=decision.symbol,
                    error=str(exc)[:160])
        return
    try:
        pipe = r.pipeline()
        pipe.setex(K.sym_key(K.REASONING, decision.symbol), K.HOT_TTL,
                   json.dumps(result.to_dict()))
        pipe.incr(K.INTERROGATED)
        if result.available and (not result.agrees_with_fusion
                                 or result.wrong_direction_risk >= 0.6):
            pipe.incr(K.WRONG_DIR_FLAG)
            log.info("scibrain_wrong_direction_flag", symbol=decision.symbol,
                     fusion_dir=decision.direction, verdict=result.verdict_direction,
                     wrong_dir_risk=round(result.wrong_direction_risk, 3))
        pipe.execute()
    except Exception as exc:
        log.debug("scibrain_reasoning_stream_failed", symbol=decision.symbol,
                  error=str(exc)[:120])


def _default_symbols(r, limit: int = 60) -> list[str]:
    """Buffer occupants first (the things we actually trade), else a universe sample."""
    syms: list[str] = []
    # buffer occupants from the launch_pad mirror (the symbols we actually stage to trade)
    try:
        for raw in (r.hvals("launchpad:slots") or []):
            try:
                d = json.loads(raw)
                if d.get("symbol") and d.get("state") not in (None, "empty"):
                    syms.append(d["symbol"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    except Exception:
        pass
    if len(syms) < limit:
        try:
            extra = list(r.srandmember("scanner:active_pairs", limit - len(syms)) or [])
            syms.extend(s for s in extra if s not in syms)
        except Exception:
            pass
    return syms[:limit]


def run_cycle(r, symbols=None, *, top_k: int = 5, interrogate=None) -> dict:
    """Score `symbols` (default auto), interrogate the top_k by conviction, update heartbeat.
    Returns a small summary dict. Never raises."""
    t0 = time.time()
    if symbols is None:
        symbols = _default_symbols(r)
    if interrogate is None:
        interrogate = _interrogate_enabled(r)

    decisions: list[Decision] = []
    for sym in symbols:
        try:
            decisions.append(score_symbol(r, sym, interrogate=False))
        except Exception as exc:
            log.warning("scibrain_score_error", symbol=sym, error=str(exc)[:140])

    # interrogate the most convinced actionable candidates (Ollama is the slow part)
    n_interrogated = 0
    if interrogate and top_k > 0:
        ranked = sorted([d for d in decisions if d.direction],
                        key=lambda d: d.conviction, reverse=True)[:top_k]
        for d in ranked:
            _interrogate_and_stream(r, d)
            n_interrogated += 1

    cycle_ms = int((time.time() - t0) * 1000)
    summary = {
        "ts": round(time.time(), 3), "pairs": len(symbols),
        "scored": len(decisions), "interrogated": n_interrogated,
        "cycle_ms": cycle_ms,
        "actionable": sum(1 for d in decisions if d.direction),
    }
    try:
        pipe = r.pipeline()
        pipe.setex(K.STATUS, K.HOT_TTL, json.dumps(summary))
        pipe.incr(K.CYCLES)
        pipe.execute()
    except Exception as exc:
        log.debug("scibrain_status_failed", error=str(exc)[:120])
    log.info("scibrain_cycle", **summary)
    return summary
