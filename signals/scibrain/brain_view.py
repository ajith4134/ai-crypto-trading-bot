"""SciBrain Phase-7d — the WHOLE-BRAIN dashboard aggregator: the BrainPulse (design §Phase-E step 17).

"Publish versioned BrainPulse views of the shared workspace, routing, belief, uncertainty, action,
safety, memory, and competence." This assembles ONE read-only snapshot of the entire cognitive OS by
folding together the per-region surfaces shipped this phase — brainstem (safety), thalamus (routing +
compute budgets), workspace (shared belief + broadcast), metacortex (competence + calibrated ignorance),
the live action path + tail reflex, and the unified producer-bus learning authority — into:

  • REGION ACTIVITY — each brain region with its current activity, key metric, declared authority, health.
  • cross-cutting views the operator reads at a glance: workspace BROADCAST, UNCERTAINTY (epistemic vs
    aleatoric, disagreement, P(action_supported)), COMPETENCE (calibration, OOD, abstain-worthy),
    AUTHORITY (the live capital path + producer bus, no-bypass), and COMPUTE (load + attention budgets).

Tier-0: pure read. It calls each region's own read-only builder once and re-expresses it; it holds no
trading authority. The one place an operator can answer "is the brain healthy, what is it attending to,
and does it know what it knows?" without trusting any single learned component.
"""
from __future__ import annotations

import json
import time

import structlog

from . import keys as K

log = structlog.get_logger()


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as exc:
        log.warning("scibrain_brain_pulse_region_failed", error=str(exc)[:140])
        return default if default is not None else {}


def build_brain_pulse(r, *, symbol: str | None = None, publish: bool = True) -> dict:
    """Assemble the whole-brain BrainPulse from every region surface. Pure read; never raises."""
    out = {"contract": "BrainPulse", "available": False, "ts": round(time.time(), 3)}
    try:
        from . import workspace as ws_mod
        from . import thalamus as th_mod
        from . import metacog as mc_mod
        from . import brainstem as bs_mod
        from . import training_health as tr_mod
        from . import episodic as ep_mod
        from . import consolidation_health as co_mod
        from . import sleep as sleep_mod
        from . import abstention as ab_mod
        from . import world_model_health as wm_mod
        from . import controllers_health as hc_mod
        from . import offline_rl_health as orl_mod
        from . import risk_policy_health as rp_mod
        from . import meta_learning_health as meta_mod
        from .authority import reconcile_authority

        ws = _safe(lambda: ws_mod.build_workspace(r, symbol=symbol, publish=False), {"available": False})
        th = _safe(lambda: th_mod.route_salience(r, ws, publish=False), {"available": False})
        mc = _safe(lambda: mc_mod.build_competence_map(r, symbol=symbol, publish=False), {"available": False})
        bs = _safe(lambda: bs_mod.safety_status(r, publish=False), {"available": False})
        tr = _safe(lambda: tr_mod.build_training_health(r, publish=False), {"available": False})
        ep = _safe(lambda: ep_mod.build_episodic_memory(r, limit=300, publish=False), {"available": False})
        rp = _safe(lambda: ep_mod.replay_health(r, limit=300, publish=False), {"available": False})
        if isinstance(ep, dict):
            ep["replay"] = rp                                      # attach prioritized-replay health to the hippocampus
        co = _safe(lambda: co_mod.build_consolidation_health(r, publish=False), {"available": False})
        sl = _safe(lambda: sleep_mod.sleep_status(r), {"available": False})
        ab = _safe(lambda: ab_mod.build_abstention_memory(r, publish=False), {"available": False})
        wm = _safe(lambda: wm_mod.build_world_model_health(r, publish=False), {"available": False})
        hc = _safe(lambda: hc_mod.build_controllers_health(r, publish=False), {"available": False})
        orl = _safe(lambda: orl_mod.build_offline_rl_health(r, publish=False), {"available": False})
        rp = _safe(lambda: rp_mod.build_risk_policy_health(r, publish=False), {"available": False})
        meta = _safe(lambda: meta_mod.build_meta_learning_health(r, publish=False), {"available": False})
        auth = _safe(lambda: reconcile_authority(r), {"available": False})

        belief = (ws.get("belief") or {}) if ws.get("available") else {}
        unc = belief.get("uncertainty_decomposition") or {}
        bs_ss = bs.get("safe_set") or {}
        bs_reflex = bs.get("reflexes") or {}
        pbus = auth.get("producer_bus") or {}
        budgets = (th.get("budgets") or {}) if th.get("available") else {}
        decision = (ws.get("decision") or {}) if ws.get("available") else {}
        glob = (mc.get("global") or {}) if mc.get("available") else {}

        # ── REGION ACTIVITY ── each region: activity (0..1), a headline metric, authority, health ──
        def region(rid, label, role, active, authority, health, metric, source):
            return {"region": rid, "label": label, "role": role, "active": bool(active),
                    "authority": authority, "health": bool(health), "metric": metric, "source": source}

        regions = [
            region("brainstem", "Brainstem — non-negotiable safety", "safety",
                   True, "veto", bs.get("all_invariants_ok", False),
                   {"exposure_headroom": bs_ss.get("exposure_headroom"),
                    "n_crash_vetoed": bs_reflex.get("n_symbols_crash_vetoed"),
                    "kill_switch_running": bs_ss.get("kill_switch_running")}, "/scibrain/brainstem"),
            region("thalamus", "Thalamus — salience routing & bandwidth", "attention",
                   th.get("available"), "observe", th.get("available", False),
                   {"compute_fraction": budgets.get("compute_fraction"),
                    "n_abstained": th.get("n_abstained"),
                    "evidence_admitted": len(th.get("evidence_selected") or [])}, "/scibrain/thalamus"),
            region("workspace", "Association cortex — shared belief", "integration",
                   ws.get("available"), "observe", ws.get("available", False),
                   {"n_messages": ws.get("n_messages"), "disagreement": belief.get("disagreement"),
                    "changepoint": belief.get("changepoint_posterior")}, "/scibrain/workspace"),
            region("metacortex", "Metacortex — calibrated ignorance", "metacognition",
                   mc.get("available"), "observe", mc.get("available", False),
                   {"p_action_supported": mc.get("p_action_supported"),
                    "mean_calibration": glob.get("mean_calibration"), "n_ood": glob.get("n_ood")},
                   "/scibrain/metacognition"),
            region("basal_ganglia", "Basal ganglia — action selection (+ hierarchical controllers)", "action",
                   bool(decision.get("direction")), "live" if auth.get("origination_enabled") else "observe",
                   True, {"direction": decision.get("direction"), "conviction": decision.get("conviction"),
                          "regime": decision.get("regime"),
                          "ctrl_coord_gain": (hc.get("coordination") or {}).get("coordination_gain_vs_flat"),
                          "orl_fallback": (orl.get("support_aware_fallback") or {}).get("cql_fallback_rate"),
                          "orl_beats_base": (orl.get("challenger") or {}).get("cql_beats_baseline"),
                          "ctrl_shadow": hc.get("available")}, "/scibrain/controllers"),
            region("amygdala", "Amygdala/insula — tail & anomaly reflex", "safety",
                   (belief.get("changepoint_posterior") or 0) > 0
                   or (bs_reflex.get("n_symbols_crash_vetoed") or 0) > 0, "veto",
                   True, {"tail_severity": (th.get("drivers") or {}).get("tail_severity"),
                          "risk_urgency": (th.get("drivers") or {}).get("risk_urgency"),
                          "n_crash_vetoed": bs_reflex.get("n_symbols_crash_vetoed"),
                          "cvar_tail_gain": (rp.get("tradeoff") or {}).get("tail_gain_cvar10_vs_mean"),
                          "cvar_dd_reduction": (rp.get("tradeoff") or {}).get("drawdown_reduction_safety")},
                   "/scibrain/risk_policy"),
            region("learning", "Producer bus — unified self-improvement", "learning",
                   pbus.get("available"), "observe",
                   (pbus.get("n_open_bypasses", 1) == 0), {"n_producers": pbus.get("n_producers"),
                    "n_open_bypasses": pbus.get("n_open_bypasses")}, "/scibrain/authority"),
            region("sensory_cortex", "Sensory cortex — perception (SSL latent)", "perception",
                   tr.get("available"), "observe",
                   tr.get("health") in ("trustworthy", "underpowered", "no_run"),  # 'issues'/'leakage' = unhealthy
                   {"health": tr.get("health"), "promoted": tr.get("promoted"),
                    "latent_auc": (tr.get("auc") or {}).get("latent"),
                    "issues": sum((tr.get("n_issues") or {}).get(s, 0) for s in ("critical", "warn"))},
                   "/scibrain/training"),
            region("hippocampus", "Hippocampus — episodic memory + replay", "memory",
                   ep.get("available"), "observe",
                   ep.get("health") in ("healthy", "watch", "cold"),  # 'issues' = unhealthy
                   {"health": ep.get("health"), "n_episodes": ep.get("n_episodes"),
                    "rare_preserved": (ep.get("preservation") or {}).get("top_priority_loss_rate"),
                    "sep_gain": (ep.get("pattern_separation") or {}).get("separation_gain")},
                   "/scibrain/episodic"),
            region("neocortex", "Neocortex — slow consolidation (EWC)", "consolidation",
                   co.get("available"), "observe",
                   co.get("health") in ("healthy", "ewc_weak", "no_run"),  # 'forgetting' = unhealthy
                   {"health": co.get("health"), "accepted": co.get("accepted"),
                    "forget_ewc": (co.get("forgetting") or {}).get("with_ewc"),
                    "ewc_helps": co.get("ewc_reduces_forgetting")},
                   "/scibrain/consolidation"),
            region("prefrontal_cortex", "Prefrontal cortex — world model + planning (RSSM)", "planning",
                   wm.get("available"),
                   "live" if (wm.get("planning_authority") or {}).get("granted") else "observe",
                   wm.get("health") in ("healthy", "no_authority", "cold"),  # 'stale' (pipeline stopped) = unhealthy
                   {"health": wm.get("health"),
                    "r2": (wm.get("calibration") or {}).get("r2"),
                    "epistemic": (wm.get("uncertainty") or {}).get("epistemic"),
                    "coverage90": (wm.get("calibration") or {}).get("reward_coverage_90"),
                    "plan_lift": (wm.get("planning") or {}).get("planning_lift_vs_baseline"),
                    "fallback": (wm.get("planning_authority") or {}).get("fallback"),
                    "planning_weight": (wm.get("planning_authority") or {}).get("planning_weight")},
                   "/scibrain/world_model"),
            region("cerebellum", "Cerebellum — fast regime adaptation (meta-learning)", "adaptation",
                   meta.get("available"), "observe",
                   meta.get("health") in ("healthy", "no_edge", "cold"),  # 'integrity_fail'/'stale' = unhealthy
                   {"health": meta.get("health"),
                    "adaptation_gain": (meta.get("few_shot") or {}).get("adaptation_gain"),
                    "beats_pooled": (meta.get("few_shot") or {}).get("beats_pooled"),
                    "competence_protected": (meta.get("protected_competence") or {}).get("competence_protected"),
                    "forgetting_shown": (meta.get("protected_competence") or {}).get("forgetting_demonstrated"),
                    "n_cohorts": meta.get("n_cohorts")},
                   "/scibrain/meta_learning"),
        ]

        out.update({
            "available": True, "symbol": ws.get("symbol"),
            "regions": regions,
            "all_regions_healthy": all(rg["health"] for rg in regions),
            # ── cross-cutting glance views ──
            "broadcast": {"workspace": ws.get("broadcast") or [],
                          "thalamus_evidence": th.get("evidence_selected") or []},
            "uncertainty": {"epistemic": unc.get("epistemic"), "aleatoric": unc.get("aleatoric"),
                            "disagreement": belief.get("disagreement"),
                            "changepoint": belief.get("changepoint_posterior"),
                            "p_action_supported": mc.get("p_action_supported"),
                            "abstention_recommended": mc.get("abstention_recommended")},
            "competence": {"mean_calibration": glob.get("mean_calibration"), "n_ood": glob.get("n_ood"),
                           "mean_epistemic": glob.get("mean_epistemic"),
                           "mean_aleatoric": glob.get("mean_aleatoric"),
                           "top": [c["component"] for c in (mc.get("components") or [])[:4]],
                           "abstain_worthy": [c["component"] for c in
                                              sorted((mc.get("components") or []),
                                                     key=lambda x: x.get("abstention_utility", 0),
                                                     reverse=True)[:4] if c.get("ood")]},
            "authority": {"live_capital_components": (auth.get("summary") or {}).get("live_capital_components"),
                          "all_invariants_ok": (auth.get("summary") or {}).get("all_invariants_ok"),
                          "n_open_bypasses": pbus.get("n_open_bypasses"),
                          "by_effective_mode": pbus.get("by_effective_mode")},
            "compute": {"load_factor": th.get("load_factor"),
                        "budgets": budgets, "compute_fraction": budgets.get("compute_fraction")},
            "training_health": tr,        # perception SSL train+eval health + auto-diagnosed issues
            "episodic_memory": ep,        # hippocampus: rich episodes + replay priority + memory-health issues
            "consolidation": co,          # neocortex: EWC slow-consolidation + protected-competence health
            "sleep": sl,                  # isolated sleep cycle: replay/consolidation/calibration/adversarial/homeostasis/pruning
            "abstention": ab,             # rewarded correct abstention + preserved rejected/near-miss/false-alarm events
            "world_model": wm,            # prefrontal cortex: RSSM on rich sequences + honest calibration + planning-authority gate
            "controllers": hc,            # basal ganglia: day/hour/minute hierarchical controllers + coordination gain (shadow)
            "offline_rl": orl,            # basal ganglia: offline CQL/IQL challengers + support-aware fallback (shadow)
            "risk_policy": rp,            # amygdala: distributional/CVaR + costs + CBF safety projection (shadow)
            "meta_learning": meta,        # cerebellum: meta-learning on real regime/cohort tasks + protected competence (shadow)
            "all_regions_healthy_note": "sensory_cortex 'issues'/'leakage' health flips this false",
            "note": ("Whole-brain BrainPulse (design §Phase-E step 17): every cognitive-OS region folded into "
                     "one read-only snapshot — region activity + authority + health, plus the workspace "
                     "broadcast, uncertainty (epistemic vs aleatoric, P(action_supported)), competence/OOD, "
                     "the live-capital authority path + producer-bus no-bypass, and the compute/attention "
                     "budgets. Pure read; no single learned component is trusted on its own."),
        })
        if publish:
            try:
                r.set(K.BRAIN_PULSE, json.dumps(out))
            except Exception:
                pass
    except Exception as exc:
        out["error"] = str(exc)[:200]
        log.warning("scibrain_brain_pulse_failed", error=str(exc)[:200])
    return out
