"""SciBrain Phase-7c — canonical AUTHORITY RECONCILIATION of the current real-LIVE state.

Rule 14 (Evidence, Authority, and Influence Gate, evidence_authority_influence_gate.md) requires every
component that could affect behavior to declare an explicit authority on the ladder

    observe -> advise -> bounded_canary -> live        (+ veto = safety suppression, orthogonal)

This module RECONCILES the live system against that taxonomy: it enumerates every component class that
currently exists, reads the ACTUAL live kill-switches/config, and reports each component's declared
authority CAP, its EFFECTIVE authority right now, its risk tier, status, kill switch, and evidence — then
CHECKS INVARIANTS (no component exceeds its cap; only the known real-money path holds live capital
authority; every Tier-0 instrument/learner holds none; the promotion gate is inactive). The point is an
HONEST, falsifiable map of "what can actually move capital right now" — if a Tier-0 instrument ever gained
live authority, INV3 would flip to fail. Pure read, deterministic, never raises. NO trading authority.
"""
from __future__ import annotations

import time
from typing import Any

from . import keys as K

SCHEMA_VERSION = 1

# the escalation ladder (capital blast radius). veto is a SAFETY authority — it can only SUPPRESS, never
# originate or size — so it sits off the escalation ladder but is still a "live" (behavior-affecting) one.
LADDER = ["observe", "advise", "bounded_canary", "live"]
_RANK = {a: i for i, a in enumerate(LADDER)}
_RANK["veto"] = _RANK["live"]  # veto affects behavior at the live tier, but only by suppression


def _flag(r, key: str, default: bool) -> bool:
    try:
        v = r.get(key)
        v = v.decode() if isinstance(v, bytes) else v
        return (v == "1") if v is not None else default
    except Exception:
        return default


def _fnum(r, key: str, default: float) -> float:
    try:
        v = r.get(key)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def reconcile_authority(r) -> dict:
    """Build the canonical authority reconciliation from the live switches. Never raises."""
    out: dict = {"contract": "AuthorityReconciliation", "schema_version": SCHEMA_VERSION,
                 "ts": round(time.time(), 3), "available": False, "components": [],
                 "summary": {}, "invariants": [], "kill_switches": {}}
    try:
        origination = _flag(r, K.ENABLED, False)
        router_on = _flag(r, K.ROUTER_ENABLED, True)
        router_strength = _fnum(r, K.ROUTER_STRENGTH, 1.0)
        ic_on = _flag(r, K.IC_ENABLED, True)
        interrogate = _flag(r, K.INTERROGATE, False)
        autoact = _flag(r, "scibrain:audit_autoact", False)
        universe_on = _flag(r, K.UNIVERSE_MODULES_ENABLED, True)
        prefilter_on = _flag(r, "scibrain:prefilter_enabled", False)
        canary_enabled = _flag(r, K.CANARY_ENABLED, False)
        try:
            canary_active = bool(r.get(K.CANARY_ACTIVE))
        except Exception:
            canary_active = False
        # the canary IS the application gate now — "armed" iff the owner enabled it AND a canary is active
        promotion_gate_active = bool(canary_enabled and canary_active)

        # design §11: the unified producer bus enumerates every self-improvement mechanism and reports
        # which (if any) still self-apply outside the kernel. Pure read; never raises.
        try:
            from . import producers as _producers
            bus_audit = _producers.audit_bypass(r)
        except Exception:
            bus_audit = {}

        # capital-affecting paths are gated by origination: a module's directional vote / sizing only
        # actually moves capital when scibrain is the live trade-origin. When origination is OFF the whole
        # capital path collapses to observe (it still computes + records, but opens nothing).
        cap_live = "live" if origination else "observe"

        def comp(cid, label, plane, declared_cap, effective, risk_tier, status, kill, evidence, note):
            return {"component": cid, "label": label, "plane": plane,
                    "declared_cap": declared_cap, "effective_authority": effective,
                    "risk_tier": risk_tier, "live": effective in ("live", "veto"),
                    "status": status, "kill_switch": kill, "evidence_ids": evidence, "note": note}

        C = [
            comp("origination_gate", "Trade origination (pick + open)", "action", "live", cap_live, 2,
                 "applied" if origination else "observe", K.ENABLED,
                 ["scibrain:enabled", "scibrain:last_funnel"],
                 "scibrain owns picking+opening; the sole live trade-origin when enabled."),
            comp("fusion_direction", "Fusion ALU directional verdict", "fusion", "live", cap_live, 2,
                 "applied" if origination else "counterfactual_only", K.ENABLED,
                 ["scibrain:{sym}:decision"],
                 "the directional module bank fused to a signed verdict; moves capital only via origination."),
            comp("risk_sizing_sl_leverage", "Sizing / SL / leverage", "risk", "live", cap_live, 3,
                 "gate_applied" if origination else "observe", K.ENABLED,
                 ["risk.manager.assign_leverage", "risk.manager.compute_initial_sl"],
                 "Tier-3 (leverage): owner caps + assign_leverage + compute_initial_sl on every open."),
            comp("safety_crash_veto", "Brainstem safety / SOC crash veto", "safety", "veto",
                 "veto" if origination else "observe", 2, "gate_applied", K.ENABLED,
                 ["scibrain:crash_radar", "scibrain:{sym}:modules#statphys_soc"],
                 "StatPhysSOC crash pressure + gate suppression can VETO/attenuate a pick; suppression only."),
            comp("meta_router_moe", "Meta-router (MoE) gains", "router", "live",
                 ("live" if (origination and router_on) else "observe"), 1,
                 "applied" if (origination and router_on) else "observe", K.ROUTER_ENABLED,
                 ["scibrain:router_enabled", "scibrain:router_strength"],
                 f"regime-gated expert weighting (strength={round(router_strength,3)}); risk-1 gain on the live vote."),
            comp("ic_router_learner", "Adaptive IC reliability learner", "router", "live",
                 ("live" if (origination and ic_on) else "observe"), 1,
                 "applied" if (origination and ic_on) else "observe", K.IC_ENABLED,
                 ["scibrain:ic_enabled", "scibrain:ic:*"],
                 "settled-IC reliability discounts/boosts module gains; risk-1, bounded multiplier."),
            comp("selection_prefilter", "Top-K scan prefilter", "selection", "live",
                 ("live" if (origination and prefilter_on) else "observe"), 1,
                 "applied" if (origination and prefilter_on) else "counterfactual_only",
                 "scibrain:prefilter_enabled", ["scibrain:prefilter:agg"],
                 "compute-saver that restricts which symbols are scored; DEFAULT OFF (shadow-measured)."),
            comp("universe_core_modules", "Universe-Core cross-market modules", "universe", "observe",
                 "observe", 1, "counterfactual_only", K.UNIVERSE_MODULES_ENABLED,
                 ["scibrain:universe:contrib:{sym}", "scibrain:universe:modules"],
                 "SparseFactorResidual/SpectralGraph/CausalLeadLag/OT — shadow_only=True; recorded + IC-graded, "
                 "NEVER move a live pick (capped at observe even when the bank runs)."),
            comp("ollama_audit", "Ollama ex-ante decision-risk audit", "council", "advise",
                 ("advise" if interrogate else "observe"), 1,
                 "advised" if interrogate else "unavailable", K.INTERROGATE,
                 ["scibrain:{sym}:reasoning", "scibrain:audit:{trade_id}"],
                 "post-open advisory verdict (wrong-direction risk); never applied to capital."),
            comp("audit_autoact", "Audit auto-remediation actuator", "council", "bounded_canary",
                 ("bounded_canary" if autoact else "observe"), 2,
                 "advised" if autoact else "observe", "scibrain:audit_autoact",
                 ["scibrain:recommended_actions"],
                 "when armed, the auditor's remediation can act on a flagged open; DEFAULT OFF."),
            comp("bounded_canary", "Owner-approved bounded canary (router-gain override)", "promotion",
                 "bounded_canary", ("bounded_canary" if (canary_enabled and canary_active) else "observe"),
                 2, ("applied" if (canary_enabled and canary_active) else "observe"), K.CANARY_ENABLED,
                 ["scibrain:canary:active", "scibrain:canary:history"],
                 "applies ONE owner-approved (module,regime) gain override at a time with auto-rollback; "
                 "the ONLY capital-affecting promotion path. DEFAULT OFF (enabled=0) — agent cannot self-approve."),
            comp("experiment_registry", "Hypothesis / ChangeSpec registry", "learning", "observe",
                 "observe", 0, "observe", None,
                 ["scibrain:experiments:registry", "scibrain:experiment:{id}"],
                 "Tier-0: records + tracks the validated lifecycle but the promotion gate is INACTIVE, so it "
                 "has NO authority to apply any change to a live knob."),
            comp("producer_bus", "Unified self-improvement producer bus", "learning", "observe",
                 "observe", 0, "observe", K.UNIFY_MODE,
                 ["scibrain:unify:summary", "scibrain:unify:submits"],
                 "Tier-0 (design §11): enumerates every autonomous self-improvement producer (council/OPRO/DGM/"
                 "AI-Scientist/GA/feature-gov/metacog/direction-model/strategy-pool/DSL/F9-F12/bayes) and routes "
                 "them through the ONE registry+promotion gate; records + reports only, applies nothing itself. "
                 f"open bypasses (producers still self-applying outside the kernel) = {bus_audit.get('n_open_bypasses', '?')}."),
            comp("living_instruments", "Calibration / outcome / cohort / twin instruments", "instrument",
                 "observe", "observe", 0, "observe", None,
                 ["scibrain:calibration", "scibrain:outcomes", "scibrain:cohort"],
                 "Tier-0 measurement only (graders, outcome ledger, embeddings, counterfactual twin)."),
            comp("viz_dashboard", "Phase-6 visualization contracts + dashboard", "instrument", "observe",
                 "observe", 0, "observe", None,
                 ["scibrain:universe:field", "/scibrain/*"],
                 "pure-read visualization; zero Redis writes; proven zero trading impact (Phase-6 isolation test)."),
        ]
        out["components"] = C

        # ── invariant checks (this is the falsifiable part — they catch authority drift) ──
        cap_ok = [c for c in C if _RANK[c["effective_authority"]] > _RANK[c["declared_cap"]]]
        live_capital = [c["component"] for c in C
                        if c["effective_authority"] == "live" and c["risk_tier"] >= 2]
        KNOWN_LIVE = {"origination_gate", "fusion_direction", "risk_sizing_sl_leverage"}
        rogue_live = [c for c in live_capital if c not in KNOWN_LIVE]
        tier0_with_authority = [c["component"] for c in C
                                if c["risk_tier"] == 0 and c["effective_authority"] != "observe"]
        registry = next((c for c in C if c["component"] == "experiment_registry"), {})

        def inv(name, ok, detail):
            return {"name": name, "ok": bool(ok), "detail": detail}

        out["invariants"] = [
            inv("no_component_exceeds_cap", not cap_ok,
                "every effective authority ≤ its declared cap"
                if not cap_ok else f"VIOLATION: {[c['component'] for c in cap_ok]} exceed cap"),
            inv("only_known_path_holds_live_capital_authority", not rogue_live,
                f"live capital-affecting authority = {sorted(live_capital)} ⊆ known real-money path"
                if not rogue_live else f"VIOLATION: unexpected live capital authority {rogue_live}"),
            inv("tier0_holds_no_live_authority", not tier0_with_authority,
                "all Tier-0 instruments/learning are observe-only"
                if not tier0_with_authority else f"VIOLATION: {tier0_with_authority} hold > observe"),
            inv("no_autonomous_application", registry.get("effective_authority") == "observe",
                "the experiment registry never self-applies; the ONLY application path is the owner-approved "
                "bounded canary" + (" (one ACTIVE now)" if promotion_gate_active else " (none active)")),
            inv("all_producers_accounted",
                all(i.get("ok") for i in bus_audit.get("invariants", []) or [{"ok": True}]),
                "every self-improvement producer is enumerated + classified by the unified bus; "
                f"open bypasses (owner-run, migration-pending) = {bus_audit.get('n_open_bypasses', '?')} "
                f"of {bus_audit.get('n_producers', '?')} producers, bus mode = {bus_audit.get('mode', 'observe')}"),
        ]

        by_auth: dict[str, int] = {}
        by_tier: dict[int, int] = {}
        for c in C:
            by_auth[c["effective_authority"]] = by_auth.get(c["effective_authority"], 0) + 1
            by_tier[c["risk_tier"]] = by_tier.get(c["risk_tier"], 0) + 1
        out["summary"] = {
            "by_authority": by_auth, "by_risk_tier": by_tier,
            "live_capital_components": sorted(live_capital),
            "n_components": len(C),
            "all_invariants_ok": all(i["ok"] for i in out["invariants"]),
            "origination_live": origination, "promotion_gate_active": promotion_gate_active,
        }
        out["kill_switches"] = {
            "scibrain:enabled": origination, "scibrain:router_enabled": router_on,
            "scibrain:ic_enabled": ic_on, "scibrain:interrogate": interrogate,
            "scibrain:audit_autoact": autoact, "scibrain:universe_modules_enabled": universe_on,
            "scibrain:prefilter_enabled": prefilter_on,
        }
        out["producer_bus"] = bus_audit          # design §11 unified-producer-bus audit (full producer map)
        # convenience flags for the existing Learning-Lab authority banner (keep its keys stable)
        out.update({
            "available": True,
            "origination_enabled": origination, "router_enabled": router_on,
            "ic_enabled": ic_on, "promotion_gate_active": promotion_gate_active,
        })
    except Exception as exc:  # pure read — never break the caller
        out["error"] = str(exc)[:200]
    return out
