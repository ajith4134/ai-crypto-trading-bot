"""SciBrain Phase 4 — Ex-ante Decision-Risk Audit (auto-interrogate every opened trade).

The owner's central goal: after the Scientist OPENS a trade, automatically interrogate WHY
that direction was chosen (the EDENUSDT post-mortem, automated) and catch the right-signal-
wrong-direction fault — on the REAL trade we placed, not a shadow candidate.

LIVING-INTELLIGENCE REFRAME (Phase 7a): this audit fires immediately AFTER the open and
BEFORE any realized PnL/path exists, so it is an EX-ANTE DECISION-RISK forecast (a calibrated
probability the direction is wrong), NOT a post-OUTCOME verdict. The separate Ex-post Outcome
Causal Audit (graded after close + horizon maturity) is a later Phase-7a task. Every payload
this module persists carries audit_kind='ex_ante_decision_risk' / evaluated_at=
'post_open_pre_outcome' so nothing downstream can mistake a forecast for a result.

Design (restart-safe, never blocks the brain):
  • The opener NEVER blocks on Ollama. On each real open it ENQUEUES a snapshot of the exact
    Decision to a Redis list (survives restarts — the bot deliberately moved off in-worker
    Celery ETAs for the same reason, engine.py:3449).
  • A celery-beat task drains the queue on the celery_worker (airllm queue): pre-warms the
    14B+8B, runs the dual-brain interrogation on the SNAPSHOT decision, stamps the verdict on
    the trade row (signals_at_entry.audit) + flags wrong-direction trades + counters.
The brain hot loop (origination + SL) stays free; the slow LLM work lives on the worker.
"""
from __future__ import annotations

import json
import time

import structlog

from . import keys as K
from .contracts import Decision

log = structlog.get_logger()

# Keep the heavy models resident between (infrequent) opened trades so a drain isn't a cold
# load. Opens are cooldown-gated (often >10min apart) so the default 10m keep_alive would let
# the 14B unload between trades; 30m + the periodic prewarm beat keeps it hot.
_KEEP_ALIVE = "30m"


def enqueue_open(r, trade_id, decision: Decision) -> None:
    """Called by the opener after a REAL open. Pushes a snapshot audit job (FIFO via LPUSH +
    RPOP). Best-effort: a failure here must NEVER unwind an open — the trade is already placed."""
    try:
        job = json.dumps({
            "trade_id": str(trade_id),
            "symbol": decision.symbol,
            "direction": decision.direction,
            "ts": round(time.time(), 3),
            "decision": decision.to_dict(),
        })
        pipe = r.pipeline()
        pipe.lpush(K.AUDIT_QUEUE, job)
        pipe.ltrim(K.AUDIT_QUEUE, 0, K.AUDIT_QUEUE_MAX - 1)   # cap length; drop oldest overflow
        pipe.execute()
        log.info("scibrain_audit_enqueued", trade_id=str(trade_id), symbol=decision.symbol)
    except Exception as exc:
        log.warning("scibrain_audit_enqueue_failed", trade_id=str(trade_id),
                    error=str(exc)[:140])


def queue_depth(r) -> int:
    try:
        return int(r.llen(K.AUDIT_QUEUE) or 0)
    except Exception:
        return 0


def prewarm(r) -> dict:
    """Keep the lead + critic models resident so an interrogation isn't a cold load. Sends a
    1-token ping to each with a long keep_alive. Never raises."""
    from llm.ollama_client import chat_ollama
    from .interrogator import LEAD_MODEL as _DL, CRITIC_MODEL as _DC
    lead = r.get(K.LEAD_MODEL) or _DL
    critic = r.get(K.CRITIC_MODEL) or _DC
    out: dict = {}
    for tag, mdl in (("lead", lead), ("critic", critic)):
        try:
            t0 = time.time()
            chat_ollama("ok", model=mdl, max_tokens=1, timeout_s=600, keep_alive=_KEEP_ALIVE)
            out[tag] = {"model": mdl, "warm_s": round(time.time() - t0, 1)}
        except Exception as exc:
            out[tag] = {"model": mdl, "error": str(exc)[:100]}
            log.warning("scibrain_prewarm_failed", model=mdl, error=str(exc)[:120])
    log.info("scibrain_prewarm", lead=out.get("lead"), critic=out.get("critic"))
    try:
        r.setex("scibrain:prewarm_status", K.HOT_TTL,
                json.dumps({"ts": round(time.time(), 3), **out}))
    except Exception:
        pass
    return out


def _merge_trade_provenance(trade_id: str, patch: dict) -> None:
    """Merge additional evidence into an opened trade's immutable-at-open provenance record.

    Post-open evidence is explicitly labelled; this never rewrites what caused the open.
    """
    try:
        from db import db_conn
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT signals_at_entry FROM trades WHERE id=%s", (str(trade_id),))
            row = cur.fetchone()
            prov: dict = {}
            if row and row[0]:
                try:
                    prov = json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
                except (TypeError, ValueError):
                    prov = {}
            prov.update(patch)
            cur.execute("UPDATE trades SET signals_at_entry=%s WHERE id=%s",
                        (json.dumps(prov), str(trade_id)))
    except Exception as exc:
        log.warning("scibrain_provenance_merge_failed", trade_id=str(trade_id),
                    keys=sorted(patch), error=str(exc)[:140])


def _stamp_trade(r, trade_id: str, symbol: str, result) -> None:
    """Persist the audit onto the trade row (merge into signals_at_entry, don't clobber the
    fusion provenance) + Redis keys for the dashboard. Best-effort; never raises."""
    audit = result.to_dict()
    try:
        pipe = r.pipeline()
        pipe.setex(K.sym_key(K.AUDIT_RESULT, symbol), K.HOT_TTL, json.dumps(audit))
        pipe.setex(K.AUDIT_BY_TRADE.replace("{tid}", str(trade_id)), 86400, json.dumps(audit))
        pipe.incr(K.AUDITED_TRADES)
        pipe.execute()
    except Exception:
        pass
    _merge_trade_provenance(str(trade_id), {
        "audit": {
                # self-describing: an EX-ANTE decision-risk forecast made at open, not a result
                "audit_kind": audit.get("audit_kind", "ex_ante_decision_risk"),
                "evaluated_at": audit.get("evaluated_at", "post_open_pre_outcome"),
                # actual auditor provenance — pins WHICH forecaster produced this risk number so
                # it can be calibrated/compared/demoted/replaced (graded at close, see grade_calibration)
                "provider": audit.get("provider", ""),
                "model": audit.get("model", ""),
                "transport": audit.get("transport", ""),
                "prompt_hash": audit.get("prompt_hash", ""),
                "schema_version": audit.get("schema_version"),
                "latency_s": audit.get("elapsed_s"),
                "verdict_direction": audit["verdict_direction"],
                "agrees_with_fusion": audit["agrees_with_fusion"],
                "wrong_direction_risk": audit["wrong_direction_risk"],
                "responsible_factor": audit["responsible_factor"],
                "narrative": audit["narrative"],
                "critic_note": audit["critic_note"],
                "available": audit["available"],
                "ts": audit["ts"],
            },
    })


def _flag_wrong_direction(r, trade_id, symbol, opened_dir, result) -> bool:
    """Right-signal-wrong-direction detector. Flags the trade when the interrogation DISAGREES
    with the opened direction OR the wrong-direction risk crosses the threshold."""
    try:
        risk = float(result.wrong_direction_risk)
    except (TypeError, ValueError):
        risk = 0.0
    try:
        thresh = float(r.get("scibrain:wrong_dir_threshold") or 0.6)
    except (TypeError, ValueError):
        thresh = 0.6
    disagrees = bool(result.available and not result.agrees_with_fusion)
    flagged = bool(disagrees or risk >= thresh)
    if flagged:
        try:
            r.zadd(K.WRONG_DIR_TRADES, {str(trade_id): round(risk, 4)})
            r.incr(K.WRONG_DIR_FLAG)
        except Exception:
            pass
        log.warning("scibrain_trade_wrong_direction_flag", trade_id=str(trade_id),
                    symbol=symbol, opened_dir=opened_dir,
                    verdict=result.verdict_direction, wrong_dir_risk=round(risk, 3),
                    responsible=result.responsible_factor)
    return flagged


_VALID_ACTIONS = ("HOLD", "REDUCE", "CLOSE", "REVERSE_BIAS", "TIGHTEN_SL")


def _remediation_prompt(decision: Decision, result, opened_dir) -> str:
    """Ask the Ollama agent to SOLVE the issue it found, not just name it: a concrete trade
    action + a concrete circuit improvement (which module to adjust and how)."""
    from .interrogator import _evidence_block
    return f"""You are the REMEDIATION ENGINEER for an automated crypto trading circuit. A trade was
just OPENED on {decision.symbol} in direction={opened_dir}, but the audit FLAGGED a possible
wrong-direction fault: verdict={result.verdict_direction}, agrees_with_open={result.agrees_with_fusion},
wrong_direction_risk={result.wrong_direction_risk:.2f}. Critic note: {result.critic_note}

MODULE EVIDENCE behind the open:
{_evidence_block(decision)}

Your job: decide what to DO about this open trade now, and how to IMPROVE the circuit so this
class of mistake is less likely. Be specific and conservative.

Respond with ONLY a JSON object, exactly these keys:
{{"recommended_action": "HOLD|REDUCE|CLOSE|REVERSE_BIAS|TIGHTEN_SL",
 "action_confidence": 0.0-1.0,
 "module_to_adjust": "the single module whose weight/gate should change (or 'none')",
 "circuit_improvement": "one concrete change, e.g. 'down-weight koopman in mean-revert regime'",
 "reason": "1-2 sentences: why this action and improvement, tied to the evidence"}}"""


def _remediate(r, decision: Decision, result, opened_dir: str) -> dict:
    """Second LLM pass — the agent proposes a fix for the issue it found. Only called when
    a trade is flagged (so we spend LLM time only when there is something to solve). Routes
    through the same cloud-primary chain (local fallback) as the interrogation. Never raises."""
    from .interrogator import _extract_json, _complete, CRITIC_MODEL as _DC
    model = r.get("scibrain:remediation_model") or r.get(K.CRITIC_MODEL) or _DC
    use_cloud = (r.get(K.AUDIT_USE_CLOUD) or "1") != "0"
    try:
        raw, _rem_meta = _complete(_remediation_prompt(decision, result, opened_dir),
                                   max_tokens=320, local_model=model, timeout_s=600,
                                   keep_alive=_KEEP_ALIVE, use_cloud=use_cloud)
    except Exception as exc:
        log.warning("scibrain_remediate_failed", symbol=decision.symbol, error=str(exc)[:120])
        return {"available": False, "error": str(exc)[:120]}
    obj = _extract_json(raw) or {}
    action = str(obj.get("recommended_action", "HOLD")).upper().strip()
    if action not in _VALID_ACTIONS:
        action = "HOLD"
    try:
        conf = float(min(1.0, max(0.0, float(obj.get("action_confidence", 0.0)))))
    except (TypeError, ValueError):
        conf = 0.0
    return {
        "available": True, "recommended_action": action, "action_confidence": round(conf, 3),
        "module_to_adjust": str(obj.get("module_to_adjust", "none"))[:60],
        "circuit_improvement": str(obj.get("circuit_improvement", ""))[:400],
        "reason": str(obj.get("reason", ""))[:400], "model": model,
        "ts": round(time.time(), 3),
    }


def _record_action(r, trade_id, symbol, opened_dir, rem: dict) -> dict:
    """Record the agent's recommendation with honest authority and application state.

    No runtime consumer currently applies PENDING_ACTIONS, so auto-act cannot be represented as
    live or pending-live. Recommendations remain visible `advise` influence until a real consumer
    exists and reports an applied action.
    """
    autoact = (r.get("scibrain:audit_autoact") or "0") == "1"
    corrective = rem.get("available") and rem.get("recommended_action") in (
        "REDUCE", "CLOSE", "REVERSE_BIAS", "TIGHTEN_SL")
    rec = {
        "trade_id": str(trade_id), "symbol": symbol, "opened_dir": opened_dir,
        "recommended_action": rem.get("recommended_action", "HOLD"),
        "action_confidence": rem.get("action_confidence", 0.0),
        "circuit_improvement": rem.get("circuit_improvement", ""),
        "module_to_adjust": rem.get("module_to_adjust", "none"),
        "reason": rem.get("reason", ""), "autoact": autoact,
        "applied": False, "authority": "advise", "mode": "advise",
        "ts": round(time.time(), 3),
    }
    try:
        # always surface the recommendation (every decision visible on the dashboard)
        r.setex(K.AUDIT_ACTION.replace("{tid}", str(trade_id)), 86400, json.dumps(rec))
        r.zadd(K.RECOMMENDED_ACTIONS, {str(trade_id): rec["ts"]})
        r.zremrangebyrank(K.RECOMMENDED_ACTIONS, 0, -201)   # keep the last 200
        if autoact and corrective:
            r.incr("scibrain:action_unconsumed_total")
            log.warning("scibrain_autoact_unavailable", trade_id=str(trade_id), symbol=symbol,
                        action=rec["recommended_action"],
                        reason="no runtime consumer; recommendation retained at advise authority")
    except Exception as exc:
        log.warning("scibrain_record_action_failed", trade_id=str(trade_id),
                    error=str(exc)[:120])
    # also propose the circuit improvement into a learning ledger the owner/Phase-7 can act on
    if rem.get("available") and rem.get("module_to_adjust", "none") not in ("", "none", "None"):
        try:
            r.lpush(K.IMPROVE_LEDGER, json.dumps({
                "symbol": symbol, "module": rem["module_to_adjust"],
                "improvement": rem.get("circuit_improvement", ""),
                "reason": rem.get("reason", ""), "trade_id": str(trade_id),
                "ts": rec["ts"]}))
            r.ltrim(K.IMPROVE_LEDGER, 0, 499)
        except Exception:
            pass
    return rec


def audit_one(r, job: dict) -> dict | None:
    """Interrogate ONE opened-trade snapshot, stamp/flag it, and — when flagged — have the
    agent propose a remediation (the 'help/improve after finding the issue' step). Returns a
    small summary. Never raises."""
    from .interrogator import interrogate, LEAD_MODEL as _DL, CRITIC_MODEL as _DC
    trade_id = job.get("trade_id")
    symbol = job.get("symbol")
    opened_dir = job.get("direction")
    snap = job.get("decision") or {}
    if not trade_id or not snap:
        return None
    decision = Decision.from_dict(snap)
    lead = r.get(K.LEAD_MODEL) or _DL
    critic = r.get(K.CRITIC_MODEL) or _DC
    use_cloud = (r.get(K.AUDIT_USE_CLOUD) or "1") != "0"
    result = interrogate(decision, lead_model=lead, critic_model=critic,
                         keep_alive=_KEEP_ALIVE, use_cloud=use_cloud)
    # mirror to the per-symbol reasoning key the live panel already renders
    try:
        r.setex(K.sym_key(K.REASONING, symbol), K.HOT_TTL, json.dumps(result.to_dict()))
    except Exception:
        pass
    _stamp_trade(r, trade_id, symbol, result)
    flagged = _flag_wrong_direction(r, trade_id, symbol, opened_dir, result)

    remediation = None
    rec = None
    if flagged:                              # the agent only spends LLM time when there's a fault to solve
        remediation = _remediate(r, decision, result, opened_dir)
        rec = _record_action(r, trade_id, symbol, opened_dir, remediation)
        # merge the remediation into the persisted/streamed audit so the dashboard shows it
        try:
            existing = json.loads(r.get(K.AUDIT_BY_TRADE.replace("{tid}", str(trade_id))) or "{}")
            existing["remediation"] = remediation
            existing["recommendation"] = rec
            blob = json.dumps(existing)
            r.setex(K.AUDIT_BY_TRADE.replace("{tid}", str(trade_id)), 86400, blob)
            r.setex(K.sym_key(K.AUDIT_RESULT, symbol), K.HOT_TTL, blob)
        except Exception:
            pass
        _merge_trade_provenance(str(trade_id), {
            "remediation": remediation,
            "recommendation": rec,
            "post_open_influences": [{
                "source": "scibrain_direction_audit",
                "authority": "advise",
                "status": "advised",
                "actual_effect": 0.0,
                "recommended_action": (rec or {}).get("recommended_action"),
                "module_to_adjust": (remediation or {}).get("module_to_adjust"),
                "reason": (remediation or {}).get("reason"),
                "ts": (rec or {}).get("ts"),
            }],
        })

    return {"trade_id": trade_id, "symbol": symbol,
            "verdict": result.verdict_direction, "available": result.available,
            "wrong_direction_risk": round(float(result.wrong_direction_risk), 3),
            "flagged": flagged,
            "action": (rec or {}).get("recommended_action"),
            "elapsed_s": round(result.elapsed_s, 1)}


def drain(r, limit: int = 2, prewarm_first: bool = True) -> dict:
    """Drain up to `limit` audit jobs (RPOP = oldest first = FIFO). Pre-warms the models first
    so the interrogation isn't a cold load. Returns a summary. Never raises."""
    summary = {"processed": 0, "flagged": 0, "results": [], "queue_left": queue_depth(r)}
    try:
        if summary["queue_left"] == 0:
            return summary
        if prewarm_first:
            prewarm(r)
        for _ in range(max(1, limit)):
            raw = r.rpop(K.AUDIT_QUEUE)
            if not raw:
                break
            try:
                job = json.loads(raw)
            except (TypeError, ValueError):
                continue
            res = audit_one(r, job)
            if res:
                summary["processed"] += 1
                summary["flagged"] += int(res.get("flagged", False))
                summary["results"].append(res)
        summary["queue_left"] = queue_depth(r)
    except Exception as exc:
        log.warning("scibrain_audit_drain_error", error=str(exc)[:160])
    if summary["processed"]:
        log.info("scibrain_audit_drain", processed=summary["processed"],
                 flagged=summary["flagged"], queue_left=summary["queue_left"])
    return summary


# ─────────────────────────────────────────────────────────────────────────────────────────────
# Phase 7a — Ex-post calibration grading of the ex-ante decision-risk forecaster.
#
# The audit above is an EX-ANTE forecast (a probability the direction is wrong, made at OPEN with
# no outcome). This grades that forecaster once a trade closes AND carries a realized failure_type
# label, so the auditor itself becomes measurable / comparable / demotable (per the gate's
# "calibration, failure telemetry" evidence requirement).
#
#   p_wrong = audit.wrong_direction_risk                       (what was predicted at open)
#   y_wrong = 1.0 if twin.fault_class == 'direction' else 0.0  (path-aware label, when trustworthy)
#           = 1.0 if failure_type == 'direction' else 0.0      (same-exit-price proxy fallback)
#   brier   = (p_wrong - y_wrong)**2
#
# The TRUTH label is the path-aware counterfactual twin's fault_class: it replays the OPPOSITE and
# ABSTAIN policies on the real forward path, so fault_class='direction' means the opposite policy
# actually WON on the realized path (not merely that it had a better same-exit-price). y_wrong uses
# that twin label whenever the replay is trustworthy (status ok + confidence high/medium); otherwise
# it falls back to the failure_type='direction' proxy so out-of-window paths still get graded.
# failure_type='direction' is deliberately NOT "the trade lost" — a correct direction can lose on
# timing/cost/path noise (living_intelligence §4.1). label_source records which basis each row used.
# ─────────────────────────────────────────────────────────────────────────────────────────────

def _clip01(v) -> float:
    try:
        return float(min(1.0, max(0.0, float(v))))
    except (TypeError, ValueError):
        return 0.0


def grade_calibration(r, limit: int = 50) -> dict:
    """Grade newly-closed audited trades, then recompute the aggregate from ALL graded rows.

    Restart-safe / double-count-proof by construction: the per-trade grade is written onto the
    immutable trade row (signals_at_entry.audit_calibration) — that write is BOTH the durable
    ledger AND the idempotency marker (already-graded rows are filtered out), and the aggregate is
    a pure RECOMPUTE over those rows, never an increment. Never raises."""
    from db import db_conn
    summary = {"graded_now": 0, "total_graded": 0, "brier": None, "base_rate_wrong": None}
    try:
        # 1) grade the next batch. Eligible = ungraded, OR graded with the OLD failure_type proxy
        #    AND a now-available CONFIDENT path-aware twin label (proxy→twin upgrade-in-place). The
        #    twin's path-aware fault_class replaces the same-exit-price failure_type='direction'
        #    proxy as y_wrong when the replay is trustworthy (confidence high/medium); otherwise the
        #    proxy is the fallback so trades whose path is out-of-window still get graded.
        cf = "signals_at_entry::jsonb->'outcome_packet'->'counterfactual'"
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""SELECT id, failure_type,
                          (signals_at_entry::jsonb->'audit'->>'wrong_direction_risk')::float,
                          {cf}->>'status', {cf}->>'fault_class', {cf}->>'confidence',
                          (signals_at_entry::jsonb ? 'audit_calibration')
                     FROM trades
                    WHERE timeframe='scibrain' AND status='closed'
                      AND signals_at_entry::jsonb ? 'audit'
                      AND failure_type IS NOT NULL
                      AND (signals_at_entry::jsonb->'audit'->>'available') = 'true'
                      AND (signals_at_entry::jsonb->'audit'->>'wrong_direction_risk') IS NOT NULL
                      AND (
                        NOT (signals_at_entry::jsonb ? 'audit_calibration')
                        -- upgrade any non-twin grade (old schema-1 NULL source OR schema-2 proxy)
                        -- once a CONFIDENT path-aware twin label exists; never re-touch twin grades
                        OR ((signals_at_entry::jsonb->'audit_calibration'->>'label_source')
                                IS DISTINCT FROM 'twin_path_aware'
                            AND {cf}->>'status' = 'ok'
                            AND {cf}->>'confidence' IN ('high','medium'))
                      )
                    ORDER BY exit_time DESC NULLS LAST
                    LIMIT %s""",
                (int(max(1, limit)),))
            rows = cur.fetchall()
        for tid, failure_type, p_raw, cf_status, cf_fault, cf_conf, was_graded in rows:
            p_wrong = _clip01(p_raw)
            twin_usable = (cf_status == "ok" and cf_conf in ("high", "medium")
                           and cf_fault in ("none", "direction", "selection"))
            if twin_usable:
                y_wrong = 1.0 if cf_fault == "direction" else 0.0
                label_source = "twin_path_aware"
            else:
                y_wrong = 1.0 if failure_type == "direction" else 0.0
                label_source = "failure_type_proxy"
            cal = {
                "p_wrong": round(p_wrong, 4),
                "y_wrong": y_wrong,
                "brier": round((p_wrong - y_wrong) ** 2, 4),
                "label_source": label_source,             # twin_path_aware | failure_type_proxy
                "fault_class": (cf_fault if twin_usable else None),
                "twin_confidence": (cf_conf if twin_usable else None),
                "failure_type": failure_type,
                "schema_version": 2,
                "graded_ts": round(time.time(), 3),
            }
            _merge_trade_provenance(str(tid), {"audit_calibration": cal})
            if not was_graded:
                summary["graded_now"] += 1            # a brand-new grade (no prior audit_calibration)
            else:
                summary["upgraded"] = summary.get("upgraded", 0) + 1   # re-label of an already-graded row

        # 2) recompute the aggregate from EVERY graded row (idempotent; no double-count risk)
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT (signals_at_entry::jsonb->'audit_calibration'->>'p_wrong')::float,
                          (signals_at_entry::jsonb->'audit_calibration'->>'y_wrong')::float,
                          signals_at_entry::jsonb->'audit_calibration'->>'label_source'
                     FROM trades
                    WHERE timeframe='scibrain' AND status='closed'
                      AND signals_at_entry::jsonb ? 'audit_calibration'""")
            fetched = cur.fetchall()
        graded = [(_clip01(p), float(y or 0.0)) for p, y, _s in fetched if p is not None]
        label_sources: dict = {}
        for _p, _y, s in fetched:
            if _p is not None:
                label_sources[s or "failure_type_proxy"] = label_sources.get(s or "failure_type_proxy", 0) + 1
        agg = _calibration_aggregate(graded)
        agg["label_sources"] = label_sources       # how many y_wrong came from the twin vs the proxy
        try:
            r.set(K.AUDIT_CALIBRATION, json.dumps(agg))
            # SET (not incrby) to the authoritative recomputed count: same pure-recompute discipline
            # as the aggregate, so the counter can never double-count across re-grades/restarts.
            r.set(K.CALIB_GRADED_TOTAL, agg["n"])
        except Exception:
            pass
        summary["total_graded"] = agg["n"]
        summary["brier"] = agg["brier"]
        summary["base_rate_wrong"] = agg["base_rate_wrong"]
    except Exception as exc:
        log.warning("scibrain_calibration_grade_error", error=str(exc)[:160])
    if summary["graded_now"] or summary.get("upgraded"):
        log.info("scibrain_calibration_graded", graded_now=summary["graded_now"],
                 upgraded=summary.get("upgraded", 0),
                 total=summary["total_graded"], brier=summary["brier"])
    return summary


def _calibration_aggregate(graded: list) -> dict:
    """Brier, base rate, mean forecast, Brier skill vs the climatology baseline, and 10 reliability
    bins (observed wrong-rate vs mean forecast per decile) from the per-trade (p_wrong, y_wrong)."""
    n = len(graded)
    if n == 0:
        return {"n": 0, "brier": None, "base_rate_wrong": None, "mean_forecast": None,
                "brier_climatology": None, "brier_skill": None,
                "label_basis": "y_wrong from the path-aware twin fault_class where confident, else "
                               "the failure_type='direction' proxy (see label_sources breakdown)",
                "bins": [], "updated_ts": round(time.time(), 3)}
    brier = sum((p - y) ** 2 for p, y in graded) / n
    base = sum(y for _p, y in graded) / n
    mean_p = sum(p for p, _y in graded) / n
    brier_clim = base * (1.0 - base)                 # always-predict-base-rate reference
    skill = (1.0 - brier / brier_clim) if brier_clim > 1e-9 else None
    bins = []
    for i in range(10):
        lo, hi = i / 10.0, (i + 1) / 10.0
        members = [(p, y) for p, y in graded if (p >= lo and (p < hi or (i == 9 and p <= hi)))]
        if not members:
            continue
        bn = len(members)
        bins.append({
            "lo": round(lo, 1), "hi": round(hi, 1), "n": bn,
            "mean_forecast": round(sum(p for p, _y in members) / bn, 4),
            "obs_wrong_rate": round(sum(y for _p, y in members) / bn, 4),
        })
    return {
        "n": n,
        "brier": round(brier, 4),
        "base_rate_wrong": round(base, 4),
        "mean_forecast": round(mean_p, 4),
        "brier_climatology": round(brier_clim, 4),
        "brier_skill": (None if skill is None else round(skill, 4)),  # >0 = beats base-rate guess
        "label_proxy": "failure_type_direction (temporary; path-aware lab pending)",
        "bins": bins,
        "updated_ts": round(time.time(), 3),
    }
