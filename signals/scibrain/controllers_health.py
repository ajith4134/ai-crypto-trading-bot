"""SciBrain Phase-7f — HIERARCHICAL CONTROLLERS health (day/hour/minute reframe + coordination; §3.7/§8-417).

Reads the shadow hierarchical-controller report (scibrain:hierarchical_controllers:report, written by
ml/hierarchical_controllers) and diagnoses the design's actual acceptance for this task — "distinct agents,
shared state, real trajectories, coordination gain, and ablation":

  • COORDINATION GAIN — does the full 3-level cascade (day→hour→minute, shared belief, real trajectories) beat
    the FLAT independent baseline (the old one-step-bandit framing) on the deterministic digital-twin utility?
  • ABLATION — flat → +belief → day-only → +hour → +minute, so each level's contribution is isolated.
  • DECORATIVE LEVELS — if a level almost never changes the decision (marginal veto rate ≈ 0) it is decorative,
    not a real controller — flagged honestly (the §8 'coordination gain and ablation' bar, not prose).

Tier-0 pure read; never raises. SHADOW: the LIVE day/minute PPO agents are untouched (Rule 21).
"""
from __future__ import annotations

import json
import time

import structlog

from . import keys as K

log = structlog.get_logger()

_STALE_S = 36 * 3600


def build_controllers_health(r, *, publish: bool = True) -> dict:
    """Diagnose the hierarchical-controller report into a typed health/issues view. Pure read; never raises."""
    out = {"contract": "HierControllersHealth", "available": False, "ts": round(time.time(), 3)}
    try:
        raw = r.get(K.HIER_CONTROLLERS_REPORT)
        rep = json.loads(raw) if raw else None
        if not rep:
            out.update({"health": "cold", "issues": [],
                        "note": "Hierarchical controllers have not run yet — no report. Run "
                                "ml.hierarchical_controllers (or wait for the beat task)."})
            if publish:
                try:
                    r.set(K.HIER_CONTROLLERS_HEALTH, json.dumps(out))
                except Exception:
                    pass
            return out

        co = rep.get("coordination") or {}
        tu = rep.get("twin_utility") or {}
        abl = rep.get("ablation") or {}
        gain = co.get("coordination_gain_vs_flat")
        belief_gain = co.get("belief_gain")
        coordinates = bool(co.get("coordinates"))
        hour_dec = bool(co.get("hour_decorative"))
        minute_dec = bool(co.get("minute_decorative"))
        age_s = max(0, int(time.time() - float(rep.get("ts") or 0)))
        stale = age_s > _STALE_S

        issues = []

        def add(code, sev, title, detail, rec):
            issues.append({"code": code, "severity": sev, "title": title, "detail": detail, "recommendation": rec})

        # the reframe itself (real trajectories + shared belief + hour role) — the §8 deliverable
        tr = rep.get("trajectory") or {}
        add("reframe_live", "info",
            f"Reframed as multi-timescale hierarchy — {tr.get('n_day_decisions')} DAY decisions over "
            f"{tr.get('minute_steps')} execution steps (day≈{tr.get('day_window')}, hour≈{tr.get('hour_window')})",
            "The independent one-step bandits are reframed as DAY(strategic)→HOUR(tactical)→MINUTE(execution) "
            "controllers reading ONE shared belief at different resolutions, on real ordered trajectories; the "
            "previously-dead HOUR role is now a real controller.",
            "Promote only if the coordination gain holds out-of-sample (owner-gated; Rule 21 — shadow for now).")

        # COORDINATION GAIN (the headline acceptance)
        if coordinates:
            add("coordination_gain", "info",
                f"Hierarchy COORDINATES — full cascade beats flat baseline (gain {gain:+})",
                f"full {tu.get('full_hierarchy')} vs flat-independent {tu.get('flat_independent')} "
                f"(realized {tu.get('realized')}, oracle {tu.get('oracle')}); belief gain {belief_gain:+}.",
                "Candidate for owner-gated promotion of the hierarchical framing over the flat bandits.")
        else:
            add("no_coordination_gain", "warn",
                f"Hierarchy provides NO lift over the flat baseline (gain {gain:+})",
                f"full {tu.get('full_hierarchy')} vs flat-independent {tu.get('flat_independent')}; on weak obs "
                "the coordination doesn't help yet. HONEST — the reframe is real, the lift is not (oracle "
                f"{tu.get('oracle')} shows headroom the controllers can't capture).",
                "Earn the gain with richer observations / belief-state inputs before any promotion.")

        # Rule-12 honesty: does the reframe even beat the LIVE system it would replace?
        u_full = tu.get("full_hierarchy"); u_real = tu.get("realized")
        if u_full is not None and u_real is not None and u_real > u_full + 0.01:
            add("underperforms_realized", "warn",
                f"Reframe UNDERPERFORMS the realized bot policy — {u_full:+} vs realized {u_real:+}",
                f"The crude hierarchical controllers do far worse than what the live system actually did "
                f"(strategy-vs-hindsight conflict {co.get('strategy_vs_hindsight_conflict')}). The reframing is "
                "measured, but it is NOT competitive with the existing funnel — do not promote it.",
                "Keep shadow; the live PPO agents stay in charge until a reframe genuinely beats realized (§6).")

        # DECORATIVE LEVELS (Rule-12 honesty — a level that never vetoes is not a controller)
        if hour_dec:
            add("hour_decorative", "warn",
                f"HOUR level is DECORATIVE — marginal veto {co.get('hour_marginal_veto_rate')}",
                "The hour controller almost never changes the day decision, so it adds no tactical control.",
                "Give it a distinct context/role or fold it in — a decorative level shouldn't claim coordination.")
        if minute_dec:
            add("minute_decorative", "warn",
                f"MINUTE level is DECORATIVE — marginal veto {co.get('minute_marginal_veto_rate')}",
                "The minute controller almost never changes the upstream decision — its execution veto is inert.",
                "Sharpen its microstructure context or it isn't a real execution gate.")

        if stale:
            add("report_stale", "warn",
                f"Controller report is stale ({age_s // 3600}h old)",
                "Older than the expected cadence — the beat task may not be running.",
                "Check the scibrain-hierarchical-controllers beat task / worker.")

        sev_rank = {"critical": 3, "warn": 2, "info": 1}
        worst = max((sev_rank[i["severity"]] for i in issues), default=0)
        # 'no_lift'/'decorative' are honest SHADOW findings, not faults → keep in the healthy set for the brain.
        health = "stale" if stale else ("healthy" if coordinates else "no_coordination")

        out.update({
            "available": True, "health": health, "age_s": age_s, "authority": "shadow",
            "live_agents_untouched": bool(rep.get("live_agents_untouched", True)),
            "n_trades": rep.get("n_trades"), "n_test": rep.get("n_test"),
            "trajectory": tr, "twin_utility": tu, "coordination": co, "ablation": abl,
            "verdict": rep.get("verdict"),
            "issues": sorted(issues, key=lambda i: -sev_rank[i["severity"]]),
            "n_issues": {s: sum(1 for i in issues if i["severity"] == s) for s in ("critical", "warn", "info")},
            "note": ("Hierarchical controllers (design §3.7/§8-417): the day/minute PPO bandits reframed as a "
                     "DAY→HOUR→MINUTE hierarchy with one shared belief, real multi-timescale trajectories, the "
                     "revived hour role, and a measured COORDINATION GAIN + ablation vs the flat baseline. "
                     "SHADOW — the live PPO agents are untouched; promotion is owner-gated (Rule 21)."),
        })
        if publish:
            try:
                r.set(K.HIER_CONTROLLERS_HEALTH, json.dumps(out))
            except Exception:
                pass
    except Exception as exc:
        out["error"] = str(exc)[:200]
        log.warning("scibrain_controllers_health_failed", error=str(exc)[:200])
    return out
