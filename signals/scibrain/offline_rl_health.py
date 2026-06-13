"""SciBrain Phase-7f — OFFLINE RL CHALLENGERS health (CQL/IQL + support-aware fallback; §3.7/§8-416).

Reads the shadow offline-RL report (scibrain:offline_rl:report, written by ml/offline_rl_challengers) and
diagnoses the design's acceptance — "support-aware baseline improvement under realistic costs and tails":

  • SUPPORT-AWARE FALLBACK — does the policy refuse out-of-support state-actions and defer to the baseline
    (the core offline-RL safety property)? Surfaces the fallback rate and the UNSUPPORTED options.
  • CHALLENGER LIFT — do CQL / IQL beat the baseline on the held-out digital twin, without chasing OOD value?
  • OPTION SUPPORT — manage/exit have no logged decision transitions (lifetrace empty) → support 0 → the
    fallback correctly refuses them. Honest data-limitation finding, flagged (not a fabricated reward).

Tier-0 pure read; never raises. SHADOW — no live authority (Rule 21).
"""
from __future__ import annotations

import json
import time

import structlog

from . import keys as K

log = structlog.get_logger()

_STALE_S = 36 * 3600


def build_offline_rl_health(r, *, publish: bool = True) -> dict:
    """Diagnose the offline-RL report into a typed health/issues view. Pure read; never raises."""
    out = {"contract": "OfflineRLHealth", "available": False, "ts": round(time.time(), 3)}
    try:
        raw = r.get(K.OFFLINE_RL_REPORT)
        rep = json.loads(raw) if raw else None
        if not rep:
            out.update({"health": "cold", "issues": [],
                        "note": "Offline-RL challengers have not run yet — no report. Run "
                                "ml.offline_rl_challengers (or wait for the beat task)."})
            if publish:
                try:
                    r.set(K.OFFLINE_RL_HEALTH, json.dumps(out))
                except Exception:
                    pass
            return out

        tu = rep.get("twin_utility") or {}
        saf = rep.get("support_aware_fallback") or {}
        ch = rep.get("challenger") or {}
        osup = rep.get("option_support") or {}
        unsupported = rep.get("unsupported_options") or []
        beats = bool(ch.get("cql_beats_baseline") or ch.get("iql_beats_baseline"))
        age_s = max(0, int(time.time() - float(rep.get("ts") or 0)))
        stale = age_s > _STALE_S

        issues = []

        def add(code, sev, title, detail, rec):
            issues.append({"code": code, "severity": sev, "title": title, "detail": detail, "recommendation": rec})

        # the offline-RL build itself (the §3.7 'offline RL first' deliverable)
        add("offline_rl_live", "info",
            f"Offline RL challengers live — CQL + IQL over {len(rep.get('options') or [])} options on "
            f"{rep.get('n_state_buckets')} state buckets ({rep.get('n_test')} test decisions)",
            "Tabular CQL (conservative coverage penalty) + IQL (expectile value + advantage) over "
            "abstain/enter/manage/exit, scored off-policy on the deterministic digital twin.",
            "Owner-gated promotion only if a challenger beats the baseline AND realized out-of-sample (§6).")

        # SUPPORT-AWARE FALLBACK (the headline safety property)
        add("support_aware_fallback", "info",
            f"Support-aware fallback active — CQL defers on {saf.get('cql_fallback_rate')} of states "
            f"(IQL {saf.get('iql_fallback_rate')}; sparse-state rate {saf.get('sparse_state_rate')})",
            "On out-of-support state-buckets the policy refuses an extrapolated value and uses the baseline — "
            "the core offline-RL safety property (no OOD over-estimation).",
            "Keep the support threshold; it is what makes offline RL safe to eventually trust.")

        # UNSUPPORTED OPTIONS (Rule-12 honest data limit)
        if unsupported:
            add("options_unsupported", "warn",
                f"Lifecycle options UNSUPPORTED — {', '.join(unsupported)} have zero logged transitions",
                "manage/exit have no intra-trade decision data (lifetrace is not logged), so the offline RL "
                "cannot learn them — the support-aware fallback correctly refuses them. Honest data limit.",
                "Log intra-trade decision transitions (lifetrace marks) to make manage/exit learnable.")

        # CONSERVATISM WORKS — the support-aware/conservative policy beats the naive OOD-chasing argmax-Q
        u_best = max(tu.get("cql") or -9, tu.get("iql") or -9)
        u_naive = tu.get("naive_no_fallback")
        if u_naive is not None and u_best > u_naive + 0.001:
            add("conservatism_helps", "info",
                f"Support-aware conservatism HELPS — {round(u_best,4)} vs naive no-fallback {u_naive}",
                "The conservative + support-aware policy beats a naive argmax-Q that chases out-of-support "
                "value — direct evidence the offline-RL safety mechanism adds value, not just caution.",
                "Keep the conservatism; it is the difference between safe and over-confident offline RL.")
        # Rule-12 honesty: even a beating challenger may underperform the LIVE system it would replace
        u_real = tu.get("behavior_realized")
        if u_real is not None and u_real > u_best + 0.01:
            add("underperforms_realized", "warn",
                f"Challenger UNDERPERFORMS the realized policy — {round(u_best,4)} vs realized {u_real}",
                "The challengers beat the crude baseline but do far worse than the live funnel actually did; "
                "they are NOT yet competitive with the existing system.",
                "Keep shadow; do not promote until a challenger beats realized out-of-sample (§6).")

        # CHALLENGER LIFT
        if beats:
            add("challenger_beats_baseline", "info",
                f"Challenger BEATS the baseline — best {ch.get('best')} "
                f"(CQL {tu.get('cql')}, IQL {tu.get('iql')} vs baseline {tu.get('baseline_global')})",
                f"on the held-out twin (behavior/realized {tu.get('behavior_realized')}, oracle {tu.get('oracle')}).",
                "Candidate challenger — confirm out-of-sample before any owner-gated promotion.")
        else:
            add("challenger_no_lift", "warn",
                f"Challengers do NOT beat the baseline (CQL {tu.get('cql')}, IQL {tu.get('iql')} vs "
                f"{tu.get('baseline_global')})",
                f"on the held-out twin; behavior/realized {tu.get('behavior_realized')} (oracle {tu.get('oracle')}). "
                "HONEST — the offline RL + support-aware fallback are real, the lift is not there yet.",
                "Richer state/obs + real manage/exit transitions are the levers, not more tuning.")

        if stale:
            add("report_stale", "warn", f"Offline-RL report is stale ({age_s // 3600}h old)",
                "Older than the expected cadence — the beat task may not be running.",
                "Check the scibrain-offline-rl beat task / worker.")

        sev_rank = {"critical": 3, "warn": 2, "info": 1}
        health = "stale" if stale else ("healthy" if beats else "no_lift")

        out.update({
            "available": True, "health": health, "age_s": age_s, "authority": "shadow",
            "live_agents_untouched": bool(rep.get("live_agents_untouched", True)),
            "n_trades": rep.get("n_trades"), "n_test": rep.get("n_test"), "options": rep.get("options"),
            "twin_utility": tu, "support_aware_fallback": saf, "option_support": osup,
            "unsupported_options": unsupported, "challenger": ch,
            "verdict": rep.get("verdict"),
            "issues": sorted(issues, key=lambda i: -sev_rank[i["severity"]]),
            "n_issues": {s: sum(1 for i in issues if i["severity"] == s) for s in ("critical", "warn", "info")},
            "note": ("Offline RL challengers (design §3.7/§8-416): CQL + IQL over abstain/enter/manage/exit with "
                     "a SUPPORT-AWARE baseline fallback, scored off-policy on the deterministic digital twin. "
                     "SHADOW — no live authority; promotion owner-gated (Rule 21)."),
        })
        if publish:
            try:
                r.set(K.OFFLINE_RL_HEALTH, json.dumps(out))
            except Exception:
                pass
    except Exception as exc:
        out["error"] = str(exc)[:200]
        log.warning("scibrain_offline_rl_health_failed", error=str(exc)[:200])
    return out
