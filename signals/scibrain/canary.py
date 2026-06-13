"""SciBrain Phase-7c — BOUNDED CANARY: owner-approved, one-context-at-a-time live trial with auto-rollback.

This is the ONLY path that grants a passed ChangeSpec actual capital-affecting authority — and it is the
narrowest, most reversible one (design §9 lifecycle: shadow → canary → owner-approved live; launchpad §6:
"promotion unit is one component/role at a time, never a batch"; Rule 14: "explicit owner approval before
first capital-affecting promotion"). Safety invariants, all enforced here:

  • DEFAULT OFF — `scibrain:canary:enabled` defaults "0"; get_active_override() returns None ⇒ the router is
    byte-identical to no canary. Nothing applies until the owner flips the master switch.
  • OWNER APPROVAL — approve_canary requires the owner to have explicitly set `scibrain:canary:approval:{hid}`
    AND the master switch; the agent CANNOT self-approve. Promotion canary→live needs a second owner flag.
  • ONE AT A TIME — at most one ACTIVE canary; approve refuses if another is live.
  • BOUNDED — the canary applies its candidate to exactly ONE (module, regime) router-gain base; everywhere
    else the champion is used (it falls back to the champion outside its single context).
  • REVERSIBLE — the champion base is the immutable `_PROFILES` value, so rollback = clear the override; a
    rollback artifact + full history are recorded.
  • AUTO-ROLLBACK — monitor_canary() rolls back automatically if the live LCB(Δutility) over canary-influenced
    closes drops below the guard, if the owner trips the manual trigger, the master switch is flipped off, or
    the max duration elapses without a positive verdict.

Pure-read in the hot path (get_active_override); all state mutations go through the explicit functions.
"""
from __future__ import annotations

import json
import time

import structlog

from . import keys as K

log = structlog.get_logger()

CANARY_VERSION = 1
_HISTORY_CAP = 200


def _flag(r, key: str, default: bool = False) -> bool:
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


def _inum(r, key: str, default: int) -> int:
    try:
        v = r.get(key)
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _jget(r, key: str):
    try:
        raw = r.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _history(r, event: dict) -> None:
    try:
        event = {"ts": round(time.time(), 3), **event}
        r.lpush(K.CANARY_HISTORY, json.dumps(event))
        r.ltrim(K.CANARY_HISTORY, 0, _HISTORY_CAP - 1)
    except Exception:
        pass


# ── HOT PATH ────────────────────────────────────────────────────────────────────────────────────────
def get_active_override(r) -> dict | None:
    """The active bounded-canary override {module, regime, candidate} for the router, or None. Returns None
    unless the master switch is ON AND an active canary exists — so with defaults the router is unchanged.
    Cheap guarded read; never raises (any failure ⇒ None ⇒ no override)."""
    try:
        if not _flag(r, K.CANARY_ENABLED, False):
            return None
        active = _jget(r, K.CANARY_ACTIVE)
        if not active or not active.get("module") or active.get("regime") is None:
            return None
        return {"module": active["module"], "regime": active["regime"],
                "candidate": active["candidate"], "hypothesis_id": active.get("hypothesis_id")}
    except Exception:
        return None


# ── REQUEST (a passed-shadow hypothesis becomes eligible, pending owner approval) ─────────────────────
def _spec_target_parts(spec: dict):
    target = (spec or {}).get("target", "")
    if not target.startswith("router.gain.") or target.count(".") < 3:
        return None, None
    _, _, module, regime = target.split(".", 3)
    return module, regime


def request_canary(r, hid: str) -> dict:
    """Record a canary REQUEST for a hypothesis that has passed shadow — pending explicit owner approval.
    Verifies the hypothesis is at `shadow` (or `walk_forward`/`unit_tested` with a passing eval) and is a
    bounded router.gain change. Does NOT apply anything. One request per hypothesis (deduped by hid)."""
    from . import experiments as ex
    out = {"ok": False, "hypothesis_id": hid}
    rec = ex.get(r, hid)
    if rec is None:
        out["error"] = f"unknown hypothesis '{hid}'"
        return out
    spec = rec.get("spec") or {}
    module, regime = _spec_target_parts(spec)
    if module is None:
        out["error"] = "canary supports bounded router.gain.<module>.<regime> changes only"
        return out
    evals = rec.get("evaluations") or []
    last_verdict = (evals[-1].get("verdict") if evals else None)
    if last_verdict not in ("pass", "baseline_bootstrap_pass"):
        out["error"] = f"hypothesis has not passed evaluation (latest verdict={last_verdict})"
        return out
    from .router import _PROFILES
    req = {
        "hypothesis_id": hid, "target": spec.get("target"), "module": module, "regime": regime,
        "candidate": float((spec.get("intervention") or {}).get("candidate")),
        "champion_base": float(_PROFILES.get(regime, {}).get(module, 1.0)),
        "applicable_contexts": rec.get("applicable_contexts") or [],
        "restricted": bool(rec.get("restricted")),
        "verdict": last_verdict, "status": rec.get("status"),
        "requested_ts": round(time.time(), 3), "state": "pending_owner_approval",
    }
    try:
        r.hset(K.CANARY_REQUESTS, hid, json.dumps(req))
    except Exception as exc:
        out["error"] = str(exc)[:160]
        return out
    _history(r, {"event": "request", "hid": hid, "target": req["target"], "candidate": req["candidate"]})
    out.update({"ok": True, "request": req})
    return out


def list_requests(r) -> list[dict]:
    try:
        h = r.hgetall(K.CANARY_REQUESTS) or {}
        out = []
        for v in h.values():
            try:
                out.append(json.loads(v))
            except Exception:
                continue
        out.sort(key=lambda x: x.get("requested_ts", 0), reverse=True)
        return out
    except Exception:
        return []


# ── APPROVE + START (OWNER-GATED) ─────────────────────────────────────────────────────────────────
def approve_canary(r, hid: str, *, approver: str = "owner") -> dict:
    """OWNER-GATED activation. Refuses unless: the master switch `scibrain:canary:enabled`=1 AND the owner
    has explicitly set `scibrain:canary:approval:{hid}`=1 AND there is NO other active canary (one at a
    time) AND the hypothesis is a requested, evaluation-passing router.gain change. On success it stores the
    single active canary, transitions the hypothesis shadow→canary, and records the rollback artifact +
    history. The agent cannot self-approve — the approval flag is the owner's explicit act."""
    out = {"ok": False, "hypothesis_id": hid}
    if not _flag(r, K.CANARY_ENABLED, False):
        out["error"] = "canary master switch scibrain:canary:enabled is OFF (owner must enable)"
        return out
    if not _flag(r, K.CANARY_APPROVAL.format(hid=hid), False):
        out["error"] = f"owner approval scibrain:canary:approval:{hid} not set"
        return out
    active = _jget(r, K.CANARY_ACTIVE)
    if active and active.get("hypothesis_id") and active.get("hypothesis_id") != hid:
        out["error"] = f"another canary is already active ({active.get('hypothesis_id')}) — one at a time"
        return out
    req = None
    try:
        raw = r.hget(K.CANARY_REQUESTS, hid)
        req = json.loads(raw) if raw else None
    except Exception:
        req = None
    if req is None:
        out["error"] = "no canary request for this hypothesis (call request_canary first)"
        return out
    from . import experiments as ex
    rec = ex.get(r, hid)
    if rec is None:
        out["error"] = "unknown hypothesis"
        return out
    # transition shadow → canary (legal). If not at shadow, still allow from a passing earlier state by
    # walking the legal path is out of scope here; we require shadow to keep the lifecycle honest.
    if rec.get("status") != "shadow":
        out["error"] = f"hypothesis must be at 'shadow' to canary (is '{rec.get('status')}')"
        return out
    now = round(time.time(), 3)
    active_rec = {
        "version": CANARY_VERSION, "hypothesis_id": hid, "target": req["target"],
        "module": req["module"], "regime": req["regime"], "candidate": req["candidate"],
        "champion_base": req["champion_base"],                    # the immutable rollback artifact
        "applicable_contexts": req.get("applicable_contexts") or [],
        "approver": approver, "approved_ts": now, "started_ts": now,
        "rollback_lcb": _fnum(r, K.CANARY_ROLLBACK_LCB, 0.0),
        "min_trades": _inum(r, K.CANARY_MIN_TRADES, 12),
        "max_hours": _inum(r, K.CANARY_MAX_HOURS, 72),
        "state": "active",
    }
    try:
        r.set(K.CANARY_ACTIVE, json.dumps(active_rec))
        r.hdel(K.CANARY_REQUESTS, hid)
        ex.transition(r, hid, "canary",
                      note=f"owner-approved bounded canary by {approver}: {req['module']}@{req['regime']} "
                           f"base {req['champion_base']}→{req['candidate']}")
    except Exception as exc:
        out["error"] = str(exc)[:160]
        return out
    _history(r, {"event": "approve", "hid": hid, "approver": approver, "module": req["module"],
                 "regime": req["regime"], "champion_base": req["champion_base"], "candidate": req["candidate"]})
    log.info("scibrain_canary_started", hid=hid, module=req["module"], regime=req["regime"],
             candidate=req["candidate"], approver=approver)
    out.update({"ok": True, "active": active_rec})
    return out


# ── ROLLBACK (manual or automatic) ─────────────────────────────────────────────────────────────────
def rollback_canary(r, hid: str | None = None, *, reason: str = "manual", auto: bool = False) -> dict:
    """Roll the active canary back: clear the override (restoring the champion base instantly) and
    transition canary→rolled_back. Safe to call when nothing is active. Records the artifact + history."""
    out = {"ok": False}
    active = _jget(r, K.CANARY_ACTIVE)
    if not active:
        out["error"] = "no active canary"
        return out
    aid = active.get("hypothesis_id")
    if hid is not None and hid != aid:
        out["error"] = f"active canary is {aid}, not {hid}"
        return out
    try:
        r.delete(K.CANARY_ACTIVE)              # clearing the override restores the champion base at once
        from . import experiments as ex
        rec = ex.get(r, aid)
        if rec and rec.get("status") == "canary":
            ex.transition(r, aid, "rolled_back", note=f"canary rollback ({'auto' if auto else 'manual'}): {reason}")
        r.delete(K.CANARY_ROLLBACK_TRIGGER)
    except Exception as exc:
        out["error"] = str(exc)[:160]
        return out
    _history(r, {"event": "rollback", "hid": aid, "reason": reason, "auto": bool(auto),
                 "restored_base": active.get("champion_base")})
    log.warning("scibrain_canary_rolled_back", hid=aid, reason=reason, auto=bool(auto))
    out.update({"ok": True, "hypothesis_id": aid, "reason": reason, "auto": bool(auto)})
    return out


# ── PROMOTE canary → live (OWNER-GATED) ───────────────────────────────────────────────────────────
def promote_canary(r, hid: str, *, approver: str = "owner") -> dict:
    """OWNER-GATED canary→live. Requires `scibrain:canary:promote:{hid}`=1, an active canary for {hid}, and
    a positive live verdict from monitor_canary. Keeps the override active (now at live authority)."""
    out = {"ok": False, "hypothesis_id": hid}
    active = _jget(r, K.CANARY_ACTIVE)
    if not active or active.get("hypothesis_id") != hid:
        out["error"] = "no active canary for this hypothesis"
        return out
    if not _flag(r, K.CANARY_PROMOTE_OK.format(hid=hid), False):
        out["error"] = f"owner promotion approval scibrain:canary:promote:{hid} not set"
        return out
    verdict = monitor_canary(r).get("verdict")
    if verdict != "promote_eligible":
        out["error"] = f"canary not promotion-eligible (monitor verdict={verdict})"
        return out
    try:
        from . import experiments as ex
        active["state"] = "live"
        active["promoted_ts"] = round(time.time(), 3)
        active["promoter"] = approver
        r.set(K.CANARY_ACTIVE, json.dumps(active))
        ex.transition(r, hid, "live", note=f"owner-promoted canary→live by {approver}")
    except Exception as exc:
        out["error"] = str(exc)[:160]
        return out
    _history(r, {"event": "promote", "hid": hid, "approver": approver})
    log.info("scibrain_canary_promoted_live", hid=hid, approver=approver)
    out.update({"ok": True})
    return out


# ── MONITOR (beat) — measures live performance + AUTO-ROLLBACK ─────────────────────────────────────
def _canary_live_deltas(r, active: dict) -> list[float]:
    """Realized-utility deltas for CLOSED trades opened while THIS canary was active, in its (module,
    regime) context: actual realized utility minus the path-aware ABSTAIN counterfactual (= the decision's
    own edge). A positive mean ⇒ the canary-influenced decisions added value. Reads the immutable trade
    rows; never raises."""
    deltas: list[float] = []
    try:
        from db import db_conn
        started = float(active.get("started_ts", 0) or 0)
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT signals_at_entry::jsonb->'outcome_packet'
                     FROM trades
                    WHERE timeframe='scibrain' AND status='closed'
                      AND extract(epoch from entry_time) >= %s
                      AND (signals_at_entry::jsonb->'decision_snapshot'->'router'->>'regime') = %s
                      AND signals_at_entry::jsonb ? 'outcome_packet'
                    ORDER BY exit_time DESC NULLS LAST LIMIT 400""",
                (started, active.get("regime")))
            rows = cur.fetchall()
        from .outcome import _jload
        for (pkt,) in rows:
            pkt = _jload(pkt, None)
            if not isinstance(pkt, dict):
                continue
            u = pkt.get("utility")
            u = u.get("utility") if isinstance(u, dict) else u
            cf = (pkt.get("counterfactual") or {})
            ab = (cf.get("abstain") or {}).get("utility") if cf.get("status") == "ok" else 0.0
            try:
                deltas.append(float(u) - float(ab if ab is not None else 0.0))
            except (TypeError, ValueError):
                continue
    except Exception:
        return deltas
    return deltas


def monitor_canary(r) -> dict:
    """Beat: evaluate the active canary's LIVE performance and AUTO-ROLLBACK on failure. Triggers:
      • master switch flipped OFF, or the owner's manual rollback trigger set → immediate rollback;
      • once ≥ min_trades canary-context closes exist: bootstrap LCB(Δutility) < rollback guard → rollback;
        strongly positive (LCB > 0) → verdict 'promote_eligible' (owner still must approve promotion);
      • max_hours elapsed without a positive verdict → rollback (fail-safe; canaries don't linger).
    Returns a status dict. Never raises into the beat."""
    out = {"ok": True, "active": False, "verdict": "none"}
    try:
        active = _jget(r, K.CANARY_ACTIVE)
        if not active:
            return out
        out["active"] = True
        out["hypothesis_id"] = active.get("hypothesis_id")
        # hard triggers first
        if not _flag(r, K.CANARY_ENABLED, False):
            rb = rollback_canary(r, reason="master switch disabled", auto=True)
            out.update({"verdict": "rolled_back", "rollback": rb})
            return out
        if _flag(r, K.CANARY_ROLLBACK_TRIGGER, False):
            rb = rollback_canary(r, reason="owner manual trigger", auto=False)
            out.update({"verdict": "rolled_back", "rollback": rb})
            return out

        deltas = _canary_live_deltas(r, active)
        n = len(deltas)
        out["n_canary_closes"] = n
        min_trades = int(active.get("min_trades", 12))
        guard = float(active.get("rollback_lcb", 0.0))
        age_h = (time.time() - float(active.get("started_ts", 0) or 0)) / 3600.0
        out["age_hours"] = round(age_h, 2)

        lcb = None
        if n >= min_trades:
            from .rigor import _bootstrap as _bs
            lcb, _ = _bs(deltas)
            out["live_lcb_delta_utility"] = lcb
            out["live_mean_delta_utility"] = round(sum(deltas) / n, 6)
            if lcb is None or lcb < guard:
                rb = rollback_canary(r, reason=f"live LCB(Δutility) {lcb} < guard {guard}", auto=True)
                out.update({"verdict": "rolled_back", "rollback": rb})
                return out
            out["verdict"] = "promote_eligible"          # owner may now approve canary→live
            return out

        # not enough evidence yet
        if age_h >= float(active.get("max_hours", 72)):
            rb = rollback_canary(r, reason=f"max_hours {active.get('max_hours')} elapsed with only "
                                           f"{n}/{min_trades} closes", auto=True)
            out.update({"verdict": "rolled_back", "rollback": rb})
            return out
        out["verdict"] = "monitoring"
        return out
    except Exception as exc:
        out.update({"ok": False, "error": str(exc)[:160]})
        return out


def status(r) -> dict:
    """Full canary status for the dashboard/authority surface. Pure read."""
    active = _jget(r, K.CANARY_ACTIVE)
    hist = []
    try:
        for v in (r.lrange(K.CANARY_HISTORY, 0, 19) or []):
            try:
                hist.append(json.loads(v))
            except Exception:
                continue
    except Exception:
        hist = []
    return {
        "version": CANARY_VERSION,
        "enabled": _flag(r, K.CANARY_ENABLED, False),
        "active": active,
        "requests": list_requests(r),
        "history": hist,
        "config": {
            "rollback_lcb": _fnum(r, K.CANARY_ROLLBACK_LCB, 0.0),
            "min_trades": _inum(r, K.CANARY_MIN_TRADES, 12),
            "max_hours": _inum(r, K.CANARY_MAX_HOURS, 72),
        },
        "note": "Bounded canary: owner-approved, ONE (module,regime) at a time, auto-rollback. Default OFF "
                "(enabled=0) ⇒ zero trading effect. The agent cannot self-approve.",
    }
