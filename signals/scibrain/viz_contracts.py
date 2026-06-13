"""SciBrain Phase-6 — VERSIONED visualization data contracts (design §8 / visual_launchpad §8).

The frontend MUST NOT infer scientific meaning from prose. This module is the single backend source
of truth for the five typed, versioned payloads the dashboard renders, each carrying its schema_version
and IMMUTABLE evidence IDs (pointers back to the exact Redis evidence the value came from) so every
node/edge/item is inspectable and reproducible:

  • BrainGraphSnapshot — the live cognitive circuit (modules → router → fusion → safety → audit → action)
    as typed nodes[] + edges[] + regions[] + the belief/disagreement field. A faithful BACKEND port of the
    old client-side `brainGraph.ts::toBrainGraph` so the frontend stops inferring the graph from prose.
  • BrainPulse — real decision-cycle pulses along the ACTIVE edges (NOT decorative; §11.3).
  • TradeReplayFrame — the trade-autopsy envelope (entry/life/exit/counterfactual), versioned.
  • LearningGraphSnapshot — the hypothesis/experiment/competence envelope, versioned.
  • UniverseGraphSnapshot — the Universe-Neural-Field envelope, versioned.

Pure, deterministic, never raises. No new Redis keys: every contract is COMPUTED from the immutable
evidence the runner already mirrors (scibrain:{sym}:decision / :modules / :reasoning, the autopsy /
learning / universe field payloads). Read-only; zero trading authority.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

# ── per-contract schema versions (bump when the typed shape changes) ──────────────────────────────
SCHEMA_VERSIONS: dict[str, int] = {
    "BrainGraphSnapshot": 1,
    "BrainPulse": 1,
    "TradeReplayFrame": 1,
    "LearningGraphSnapshot": 1,
    "UniverseGraphSnapshot": 1,
}

# region anchors (design §8) — the stable spatial lanes the atlas lays out
REGIONS = [
    {"id": "module", "label": "Perception / Modules", "order": 0},
    {"id": "router", "label": "Thalamic Router (MoE)", "order": 1},
    {"id": "fusion", "label": "Shared Belief / Fusion ALU", "order": 2},
    {"id": "safety", "label": "Brainstem Safety / Risk", "order": 3},
    {"id": "audit", "label": "Scientific Council (Ollama)", "order": 4},
    {"id": "action", "label": "Action Selector", "order": 5},
]

_FUSION, _ACTION, _ROUTER, _SAFETY, _AUDIT = "fusion", "action", "router", "safety", "audit"


def _c01(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if f < 0 else (1.0 if f > 1 else f)


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _dir_sign(d: Any) -> int:
    return 1 if d == "long" else (-1 if d == "short" else 0)


def _sign(v: float) -> int:
    return 1 if v > 0 else (-1 if v < 0 else 0)


def _evkey(sym: str) -> str:
    return f"scibrain:{sym}:decision"


def _det_id(prefix: str, *parts: Any) -> str:
    """Deterministic short id from parts — stable across rebuilds of the same evidence."""
    h = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:10]
    return f"{prefix}:{h}"


# ──────────────────────────────────────────────────────────────────────────────────────────────────
#  BrainGraphSnapshot  (+ BrainPulse) — the live cognitive circuit
# ──────────────────────────────────────────────────────────────────────────────────────────────────
def _compute_belief(decision: dict, modules: list[dict], summary: dict) -> dict:
    """The brain's POSTERIOR over the symbol from the LIVE (non-shadow) directional module votes
    (design §5.3) — long/short mass, disagreement, and the proposed-vs-actual gap. Ported 1:1 from
    brainGraph.ts::computeBelief so the backend contract and the legacy client adapter agree."""
    long_mass = short_mass = 0.0
    n_long = n_short = n_abstain = n_live = 0
    unc = 0.0
    unc_n = 0
    for m in modules:
        if m.get("shadow_only"):
            continue
        role = m.get("role")
        if role and role != "direction":
            continue
        n_live += 1
        d = _num(m.get("direction"))
        c = _c01(m.get("conviction"))
        if c <= 1e-6 or abs(d) <= 1e-6:
            n_abstain += 1
            continue
        unc += 1 - c
        unc_n += 1
        if d > 0:
            long_mass += c
            n_long += 1
        else:
            short_mass += c
            n_short += 1
    total = long_mass + short_mass
    disagreement = (2 * min(long_mass, short_mass) / total) if total > 1e-9 else 0.0
    evidence_leans = None if total <= 1e-9 else ("long" if long_mass >= short_mass else "short")
    crash = _num((decision.get("router") or {}).get("crash_warning"))
    actual_action = decision.get("direction")
    vetoed = actual_action is None and (
        crash >= 0.6 or bool(summary.get("gate_applied")) or bool(summary.get("abstained")))
    overridden = (evidence_leans is not None and actual_action is not None
                  and evidence_leans != actual_action)
    return {
        "direction": decision.get("direction"), "regime": decision.get("regime"),
        "conviction": _c01(decision.get("conviction")),
        "long_mass": round(long_mass, 6), "short_mass": round(short_mass, 6),
        "n_long": n_long, "n_short": n_short, "n_abstain": n_abstain, "n_live": n_live,
        "disagreement": round(disagreement, 6),
        "mean_uncertainty": round(unc / unc_n, 6) if unc_n else 0.0,
        "evidence_leans": evidence_leans, "actual_action": actual_action,
        "overridden": overridden, "vetoed": vetoed, "crash_warning": round(crash, 6),
    }


def build_brain_graph_snapshot(decision: Optional[dict]) -> dict:
    """The versioned BrainGraphSnapshot for ONE decision — typed nodes[]/edges[]/regions[]/belief, each
    node/edge pointing back to immutable evidence IDs. Deterministic; never raises. Field names match the
    frontend's BrainGraphSnapshot interface so the atlas/inspector/fallback consume it without inference."""
    base = {
        "contract": "BrainGraphSnapshot", "schema_version": SCHEMA_VERSIONS["BrainGraphSnapshot"],
        "available": False, "snapshot_id": "", "ts": 0.0, "symbol": None, "direction": None,
        "conviction": 0.0, "regime": None, "regions": REGIONS, "nodes": [], "edges": [],
        "belief": None, "summary": {}, "pulses": [],
    }
    try:
        if not decision or not decision.get("symbol"):
            return base
        sym = str(decision["symbol"])
        dec_ev = _evkey(sym)
        mod_ev = f"scibrain:{sym}:modules"
        rsn_ev = f"scibrain:{sym}:reasoning"
        modules: list[dict] = decision.get("modules") or []
        router = decision.get("router") or {}
        gains: dict = router.get("gains") or {}
        deact = set(router.get("deactivated") or [])
        attr = {a.get("module"): a for a in (decision.get("attribution") or [])}
        summary = (decision.get("influence_manifest") or {}).get("summary") or {}
        fp = decision.get("family_penalty") or {}
        rsn = decision.get("reasoning")
        conv_f = _c01(decision.get("conviction"))
        d_sign = _dir_sign(decision.get("direction"))

        nodes: list[dict] = []
        edges: list[dict] = []

        # ── module nodes + their contribution edges into the Fusion ALU ──
        for m in modules:
            name = m.get("module", "?")
            nid = "mod:" + name
            gain = _num(gains[name], 1.0) if name in gains else 1.0
            suppressed = name in deact or gain <= 1e-6
            a = attr.get(name) or {}
            is_shadow = bool(m.get("shadow_only"))
            role = m.get("role") or "direction"
            nodes.append({
                "id": nid, "region": "module", "role": role, "label": name,
                "state": "shadow" if is_shadow else ("suppressed" if suppressed
                          else ("active" if m.get("ok") else "abstain")),
                "activation": _c01(m.get("conviction")), "signed_value": _num(m.get("direction")),
                "confidence": _c01(m.get("conviction")), "shadow": is_shadow,
                "authority": "observe" if is_shadow else ("gate" if role in ("risk", "gate") else "live"),
                "evidence_family": m.get("evidence_family"), "regime": m.get("regime_tag"),
                "health": 1.0 if m.get("ok") else 0.3, "reasoning": m.get("explanation"),
                "epistemic_uncertainty": 1 - _c01(m.get("conviction")),
                "reliability_ic": (None if m.get("reliability_ic") is None else _num(m.get("reliability_ic"))),
                "evidence_ids": [f"{mod_ev}#{name}", dec_ev],
                "detail": {"explanation": m.get("explanation"), "features": m.get("features"),
                           "gain": gain, "attribution": a, "reliability_ic": m.get("reliability_ic"),
                           "horizon_min": m.get("horizon_min"), "expected_move_pct": m.get("expected_move_pct"),
                           "role": role, "evidence_family": m.get("evidence_family")},
            })
            share = (_num(a.get("share")) if a.get("share") is not None
                     else _num(m.get("direction")) * _c01(m.get("conviction")) * gain)
            opposes = (not is_shadow and not suppressed and d_sign != 0
                       and abs(share) > 1e-6 and _sign(share) == -d_sign)
            edges.append({
                "id": "e:" + name, "source": nid, "target": _FUSION, "message_kind": "vote",
                "signed_value": round(share, 6), "magnitude": _c01(abs(share) * 2), "gain": gain,
                "shadow": is_shadow, "suppressed": suppressed, "opposes": opposes,
                "evidence_family": m.get("evidence_family"),
                "evidence_ids": [f"{mod_ev}#{name}", dec_ev],
                "detail": {"vote": m.get("direction"), "conviction": m.get("conviction"), "gain": gain,
                           "redundancy_discount": a.get("redundancy_discount"),
                           "aligned": a.get("aligned"), "share": share},
            })

        # ── meta-router (MoE) → gates the fusion ──
        nodes.append({
            "id": _ROUTER, "region": "router", "role": "meta-router", "label": "Router · MoE",
            "state": "suppressed" if deact else "active", "activation": _c01(router.get("confidence")),
            "signed_value": 0.0, "confidence": _c01(router.get("confidence")), "shadow": False,
            "authority": "gate", "regime": router.get("regime"), "health": 1.0,
            "evidence_ids": [dec_ev],
            "detail": {"regime": router.get("regime"), "confidence": router.get("confidence"),
                       "crash_warning": router.get("crash_warning"),
                       "change_point_prob": router.get("change_point_prob"),
                       "trend_score": router.get("trend_score"), "hmm_regime": router.get("hmm_regime"),
                       "strength": router.get("strength"), "deactivated": router.get("deactivated"),
                       "gains": gains},
        })
        edges.append({
            "id": "e:router", "source": _ROUTER, "target": _FUSION, "message_kind": "gate",
            "signed_value": 0.0, "magnitude": _c01(router.get("confidence")), "gain": 1.0,
            "shadow": False, "suppressed": False, "opposes": False, "evidence_ids": [dec_ev],
            "detail": {"deactivated": router.get("deactivated"), "regime": router.get("regime"), "gains": gains},
        })

        # ── safety / risk plane → action ──
        crash = _num(router.get("crash_warning"))
        nodes.append({
            "id": _SAFETY, "region": "safety", "role": "risk", "label": "Safety · Risk",
            "state": "active" if crash >= 0.4 else "ok", "activation": _c01(crash), "signed_value": 0.0,
            "confidence": _c01(crash), "shadow": False, "authority": "gate", "health": 1.0,
            "evidence_ids": [dec_ev],
            "detail": {"crash_warning": crash, "change_point_prob": router.get("change_point_prob"),
                       "gate_applied": summary.get("gate_applied"), "suppressed": summary.get("suppressed"),
                       "abstained": summary.get("abstained")},
        })
        vetoed = crash >= 0.6 or bool(summary.get("gate_applied"))
        edges.append({
            "id": "e:safety", "source": _SAFETY, "target": _ACTION, "message_kind": "safety",
            "signed_value": round(d_sign * conv_f, 6), "magnitude": conv_f, "gain": 1.0, "shadow": False,
            "suppressed": vetoed, "opposes": False, "evidence_ids": [dec_ev],
            "detail": {"crash_warning": crash, "projected_direction": decision.get("direction"),
                       "gate_applied": summary.get("gate_applied"), "vetoed": vetoed},
        })

        # ── fusion ALU → safety ──
        nodes.append({
            "id": _FUSION, "region": "fusion", "role": "fusion", "label": "Fusion ALU",
            "state": "active", "activation": conv_f, "signed_value": round(d_sign * conv_f, 6),
            "confidence": conv_f, "shadow": False, "authority": "live", "regime": decision.get("regime"),
            "health": 1.0,
            "reasoning": (("primary driver: " + decision["primary_driver"]) if decision.get("primary_driver") else None),
            "epistemic_uncertainty": 1 - conv_f, "evidence_ids": [dec_ev],
            "detail": {"direction": decision.get("direction"), "conviction": decision.get("conviction"),
                       "primary_driver": decision.get("primary_driver"), "family_penalty": fp,
                       "summary": summary, "expected_move_pct": decision.get("expected_move_pct")},
        })
        edges.append({
            "id": "e:fusion", "source": _FUSION, "target": _SAFETY, "message_kind": "fuse",
            "signed_value": round(d_sign * conv_f, 6), "magnitude": conv_f, "gain": 1.0, "shadow": False,
            "suppressed": False, "opposes": False, "evidence_ids": [dec_ev],
            "detail": {"primary_driver": decision.get("primary_driver"), "direction": decision.get("direction"),
                       "conviction": decision.get("conviction")},
        })

        # ── Ollama audit (advisory) → action, when present ──
        if rsn and rsn.get("available"):
            a_sign = _dir_sign(rsn.get("verdict_direction"))
            agrees = bool(rsn.get("agrees_with_fusion"))
            nodes.append({
                "id": _AUDIT, "region": "audit", "role": "audit", "label": "Ollama Audit",
                "state": "active" if agrees else "suppressed", "activation": _c01(rsn.get("confidence")),
                "signed_value": round(a_sign * _c01(rsn.get("confidence")), 6),
                "confidence": _c01(rsn.get("confidence")), "shadow": False, "authority": "advise", "health": 1.0,
                "reasoning": rsn.get("narrative"),
                "epistemic_uncertainty": (None if rsn.get("wrong_direction_risk") is None
                                          else _c01(rsn.get("wrong_direction_risk"))),
                "evidence_ids": [rsn_ev, dec_ev],
                "detail": {"verdict": rsn.get("verdict_direction"), "agrees": agrees,
                           "wrong_direction_risk": rsn.get("wrong_direction_risk"),
                           "narrative": rsn.get("narrative"), "responsible_factor": rsn.get("responsible_factor"),
                           "lead_model": rsn.get("lead_model"), "critic_model": rsn.get("critic_model"),
                           "critic_note": rsn.get("critic_note")},
            })
            edges.append({
                "id": "e:audit", "source": _AUDIT, "target": _ACTION, "message_kind": "audit",
                "signed_value": round(a_sign * _c01(rsn.get("confidence")), 6),
                "magnitude": _c01(rsn.get("confidence")), "gain": 1.0, "shadow": False,
                "suppressed": not agrees, "opposes": not agrees, "evidence_ids": [rsn_ev],
                "detail": {"wrong_direction_risk": rsn.get("wrong_direction_risk"), "agrees": agrees},
            })

        # ── final action ──
        nodes.append({
            "id": _ACTION, "region": "action", "role": "action",
            "label": (decision.get("direction") or "abstain").upper(),
            "state": "active" if decision.get("direction") else "abstain", "activation": conv_f,
            "signed_value": round(d_sign * conv_f, 6), "confidence": conv_f, "shadow": False,
            "authority": "live", "regime": decision.get("regime"), "health": 1.0, "evidence_ids": [dec_ev],
            "detail": {"direction": decision.get("direction"), "conviction": decision.get("conviction"),
                       "size_frac": decision.get("size_frac"), "expected_move_pct": decision.get("expected_move_pct"),
                       "regime": decision.get("regime"), "primary_driver": decision.get("primary_driver")},
        })

        ts = _num(decision.get("ts"))
        snap = {
            **base, "available": True,
            "snapshot_id": f"{sym}@{decision.get('ts')}", "ts": ts, "symbol": sym,
            "direction": decision.get("direction"), "conviction": _num(decision.get("conviction")),
            "regime": decision.get("regime"), "nodes": nodes, "edges": edges,
            "belief": _compute_belief(decision, modules, summary), "summary": summary,
        }
        snap["pulses"] = build_brain_pulses(snap)
        return snap
    except Exception as exc:  # contract is read-only — never break the endpoint
        base["error"] = str(exc)[:200]
        return base


def build_brain_pulses(snap: dict) -> list[dict]:
    """Real decision-cycle BrainPulses (design §11.3 — NOT decorative): one pulse per ACTIVE, non-shadow,
    non-suppressed edge whose magnitude clears a floor, tagged by message kind. Deterministic event_ids
    keyed to the snapshot so the frontend can de-dupe/replay; evidence_ids inherited from the edge."""
    out: list[dict] = []
    sid = snap.get("snapshot_id", "")
    ts = _num(snap.get("ts"))
    for e in snap.get("edges") or []:
        if e.get("shadow") or e.get("suppressed"):
            continue
        mag = _c01(e.get("magnitude"))
        if mag < 0.05:
            continue
        out.append({
            "contract": "BrainPulse", "schema_version": SCHEMA_VERSIONS["BrainPulse"],
            "event_id": _det_id("pulse", sid, e.get("id")),
            "edge_id": e.get("id"), "source": e.get("source"), "target": e.get("target"),
            "kind": e.get("message_kind"), "magnitude": round(mag, 6),
            "signed_value": _num(e.get("signed_value")), "opposes": bool(e.get("opposes")),
            "ts": ts, "duration_ms": int(300 + 700 * mag),
            "evidence_ids": e.get("evidence_ids") or [],
        })
    # strongest pulses first so the frontend can cap concurrent animations (§10)
    out.sort(key=lambda p: p["magnitude"], reverse=True)
    return out


# ──────────────────────────────────────────────────────────────────────────────────────────────────
#  Envelope contracts — version + evidence-ID wrap the already-typed endpoint payloads (non-breaking)
# ──────────────────────────────────────────────────────────────────────────────────────────────────
def wrap_trade_replay(autopsy: Optional[dict]) -> dict:
    """Version-envelope the trade-autopsy payload as a TradeReplayFrame. Evidence IDs point at the
    immutable trade row + entry snapshot + outcome packet. Adds metadata only — preserves every field."""
    out = dict(autopsy or {})
    tid = out.get("trade_id") or out.get("id")
    ev = [e for e in [
        (f"trade:{tid}" if tid else None),
        (f"scibrain:entry_snapshot:{out.get('snapshot_id')}" if out.get("snapshot_id") else None),
        (f"scibrain:outcome:{tid}" if tid else None),
    ] if e]
    out.update({"contract": "TradeReplayFrame",
                "schema_version": SCHEMA_VERSIONS["TradeReplayFrame"], "evidence_ids": ev})
    return out


def wrap_learning_graph(learning: Optional[dict]) -> dict:
    """Version-envelope the learning-lab payload as a LearningGraphSnapshot. Evidence IDs = the
    hypothesis registry keys for every hypothesis shown (the immutable per-hypothesis ledger rows)."""
    out = dict(learning or {})
    ev = ["scibrain:experiments:registry"]
    for h in (out.get("hypotheses") or [])[:64]:
        hid = h.get("id") or h.get("change_id") or h.get("key")
        if hid:
            ev.append(f"scibrain:experiment:{hid}")
    out.update({"contract": "LearningGraphSnapshot",
                "schema_version": SCHEMA_VERSIONS["LearningGraphSnapshot"], "evidence_ids": ev})
    return out


def wrap_universe_graph(field: Optional[dict]) -> dict:
    """Version-envelope the Universe-Neural-Field payload as a UniverseGraphSnapshot. Evidence ID = the
    frame timestamp that produced it (the immutable UniverseFrame build the topology was computed from)."""
    out = dict(field or {})
    ev = ["scibrain:universe:field"]
    if out.get("frame_ts"):
        ev.append(f"scibrain:universe:frame@{out.get('frame_ts')}")
    out.update({"contract": "UniverseGraphSnapshot",
                "schema_version": SCHEMA_VERSIONS["UniverseGraphSnapshot"], "evidence_ids": ev})
    return out
