"""SciBrain Phase-7d — the read-only global latent WORKSPACE (design §3.4 / §9 step 3).

"Create one typed, uncertain, multimodal belief that specialized systems can read." This assembles a
bounded event-bus + shared BeliefState from the CURRENT module/model outputs of the latest scored
Decision — using ONLY the Phase-7d typed contracts (cognitive_contracts.py), with full source/version
lineage. Every contributing module publishes a typed CognitiveMessage; the most-salient subset is the
"broadcast" set (the useful computational core of Global-Workspace theory: integration + selective
broadcast among specialists — NOT a free-form chat, NOT consciousness).

Tier-0: PURE READ. It re-reads what the live circuit already computed (the per-symbol Decision +
ModuleOutputs, the regime counters, the IC sample support, the registry) and re-expresses it as the
shared typed belief. It holds NO trading authority and writes only its own snapshot key for the
dashboard. The planner / action-selector / metacognition (later Phase-7d/7f tasks) will CONSUME this
BeliefState instead of raw disconnected indicators.
"""
from __future__ import annotations

import json
import statistics
import time

import structlog

from . import keys as K
from .cognitive_contracts import BeliefState, CognitiveMessage, ROLES

log = structlog.get_logger()

_CANON_REGIMES = ("trending", "mean_revert", "turbulent", "neutral")


def _jload(v, default=None):
    try:
        return json.loads(v) if isinstance(v, (str, bytes)) else (v if v is not None else default)
    except (TypeError, ValueError):
        return default


def _latest_symbol(r):
    try:
        z = r.zrevrange(K.LAST_DECISIONS, 0, 0)
        if z:
            return z[0].decode() if isinstance(z[0], bytes) else z[0]
    except Exception:
        pass
    return None


def _regime_posterior(r) -> dict:
    """A REAL calibrated-ish regime distribution from the live per-regime decision counters."""
    counts = {}
    for rg in _CANON_REGIMES:
        try:
            counts[rg] = float(r.get(K.REGIME_COUNT.format(regime=rg)) or 0)
        except (TypeError, ValueError):
            counts[rg] = 0.0
    tot = sum(counts.values())
    if tot <= 0:
        return {rg: round(1.0 / len(_CANON_REGIMES), 4) for rg in _CANON_REGIMES}
    return {rg: round(c / tot, 4) for rg, c in counts.items()}


def _ic_samples(r) -> dict:
    try:
        return {(k.decode() if isinstance(k, bytes) else k): int(float(v))
                for k, v in (r.hgetall(K.IC_SAMPLES) or {}).items()}
    except Exception:
        return {}


def _support_distance(samples, min_samples: int, shadow_only: bool) -> float:
    """0 = full support (matured IC), 1 = no support. Shadow modules are unproven ⇒ max distance."""
    if shadow_only or samples is None:
        return 1.0
    return round(max(0.0, min(1.0, 1.0 - (samples / max(1, min_samples)))), 4)


def build_workspace(r, symbol: str | None = None, *, max_messages: int = 24,
                    broadcast_k: int = 6, publish: bool = True) -> dict:
    """Assemble the read-only workspace (typed CognitiveMessages + a shared BeliefState) from the latest
    scored Decision (or `symbol`). Pure read; never raises. Publishes to K.WORKSPACE when `publish`."""
    out = {"contract": "Workspace", "available": False, "ts": round(time.time(), 3)}
    try:
        sym = symbol or _latest_symbol(r)
        if not sym:
            out["error"] = "no scored decisions yet"
            return out
        dec = _jload(r.get(K.DECISION.format(sym=sym)), None)
        if not isinstance(dec, dict):
            out["error"] = f"no decision snapshot for {sym}"
            return out
        mods = dec.get("modules") or _jload(r.get(K.MODULES.format(sym=sym)), []) or []
        versions = dec.get("versions") if isinstance(dec.get("versions"), dict) else {}
        try:
            min_samples = int(r.get(K.IC_MIN_SAMPLES) or 30)
        except (TypeError, ValueError):
            min_samples = 30
        ic_samples = _ic_samples(r)

        fused_dir = dec.get("direction")                       # 'long' | 'short' | None
        fused_sign = 1.0 if fused_dir == "long" else (-1.0 if fused_dir == "short" else 0.0)

        # ── each module → a typed CognitiveMessage ──
        messages: list[CognitiveMessage] = []
        votes: list[float] = []
        for m in mods:
            if not isinstance(m, dict):
                continue
            module = str(m.get("module", "?"))
            direction = float(m.get("direction", 0.0) or 0.0)
            conviction = float(m.get("conviction", 0.0) or 0.0)
            belief_delta = round(direction * conviction, 6)
            role = m.get("role", "direction")
            if role not in ROLES:
                role = "meta"
            shadow = bool(m.get("shadow_only"))
            messages.append(CognitiveMessage(
                source=module, role=role,
                evidence_family=str(m.get("evidence_family", "unspecified")),
                belief_delta=belief_delta,
                uncertainty=round(max(0.0, min(1.0, 1.0 - conviction)), 4),
                salience=round(min(1.0, abs(belief_delta)), 4),
                horizon=float(m.get("horizon_min", 30) or 30),
                support_distance=_support_distance(ic_samples.get(module), min_samples, shadow),
                source_version=versions, evidence_ids=[f"{sym}:decision"]))
            if m.get("ok", True) and conviction > 0 and not shadow:
                votes.append(belief_delta)

        messages.sort(key=lambda x: x.salience, reverse=True)
        messages = messages[:max_messages]

        # ── shared BeliefState ──
        n_conv = len(votes)
        if n_conv and fused_sign != 0:
            disagree = sum(1 for v in votes if v != 0 and (v > 0) != (fused_sign > 0)) / n_conv
        else:
            disagree = 0.0
        latent = [round(v, 6) for v in votes][:max_messages]
        dispersion = round(statistics.pstdev(votes), 6) if len(votes) >= 2 else 0.0
        mean_unc = round(statistics.mean([m.uncertainty for m in messages]), 4) if messages else 1.0

        changepoint = 0.0
        tail_state: dict = {}
        for m in mods:
            if not isinstance(m, dict):
                continue
            name = str(m.get("module", "")).lower()
            feats = m.get("features") or {}
            if "bocpd" in name and not changepoint:
                changepoint = float(feats.get("changepoint_prob",
                                    feats.get("cp_prob", abs(float(m.get("direction", 0) or 0)))) or 0.0)
            if "soc" in name and not tail_state:
                tail_state = {"module": m.get("module"), "features": feats}

        try:
            summ = _jload(r.get(K.EXPERIMENTS_SUMMARY), {}) or {}
            active_hyps = [x.get("hypothesis_id") for x in (summ.get("recent") or [])][:5]
        except Exception:
            active_hyps = []

        belief = BeliefState(
            latent_mean=latent, latent_dispersion=dispersion,
            regime_posterior=_regime_posterior(r),
            changepoint_posterior=round(max(0.0, min(1.0, changepoint)), 4),
            disagreement=round(disagree, 4),
            uncertainty_decomposition={"epistemic": round(disagree, 4), "aleatoric": mean_unc},
            tail_state=tail_state, active_hypotheses=active_hyps,
            source_ids=[m.source for m in messages], versions=versions)

        belief_errs = belief.validate()
        msg_dicts = [m.to_dict() for m in messages]
        broadcast = [m.source for m in messages[:max(1, int(broadcast_k))]]

        out.update({
            "available": True, "symbol": sym,
            "decision": {"direction": fused_dir, "conviction": dec.get("conviction"),
                         "regime": dec.get("regime"), "primary_driver": dec.get("primary_driver")},
            "n_messages": len(msg_dicts), "n_convicted": n_conv,
            "messages": msg_dicts,
            "broadcast": broadcast,                # the selective-broadcast salient subset
            "belief": belief.to_dict(),
            "belief_valid": not belief_errs, "belief_errors": belief_errs,
            "note": ("Read-only Global-Workspace assembly (design §3.4): the latest Decision's module bank "
                     "re-expressed as typed CognitiveMessages + one shared BeliefState (calibrated regime "
                     "posterior, cross-module disagreement = epistemic uncertainty, support distance per "
                     "module). The broadcast subset is the most-salient few. Tier-0: pure read, no authority."),
        })
        if publish:
            try:
                r.set(K.WORKSPACE, json.dumps(out))
            except Exception:
                pass
    except Exception as exc:
        out["error"] = str(exc)[:200]
        log.warning("scibrain_workspace_build_failed", error=str(exc)[:200])
    return out
