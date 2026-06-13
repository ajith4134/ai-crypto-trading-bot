"""SciBrain Phase 7a — module-state embeddings + matched-cohort retrieval (design §5.2 / §10).

The living brain must answer "what happened the LAST time the circuit was in a state like THIS one?"
without leaning on prose/explanation similarity (which is unstable and not causal). This module
gives every decision a fixed-length **module-state embedding** built from the actual per-module
directional votes plus the structured market context, and a **matched-cohort retrieval** that finds
historically comparable CLOSED trades by (regime, pair-class, volatility/liquidity band, horizon)
AND module-state cosine — returning their realized outcomes as the comparable cohort the audit and
the Phase-7b experiment engine consume.

Embedding (per trade, persisted immutably at signals_at_entry.module_embedding):
  module_vec : one signed scalar per roster module = direction·conviction in [-1, 1]
               (sign = long/short vote, magnitude = conviction-weighted strength; abstain/absent = 0)
  context    : regime, pair, pair_class, conviction, net_vote, size_frac + market scalars
               (vpin, ofi, funding, oi_change_z, sentiment, spread_rel) + conviction-weighted horizon

Retrieval is name-aligned cosine on module_vec (robust to roster changes) blended with a Gaussian
context-similarity, optionally hard-filtered to the same regime. Pure compute + DB/Redis I/O only;
mirrors grade_calibration / harvest_outcomes: immutable per-row write + RECOMPUTED aggregate, so it
is restart-safe and double-count-proof. Never raises into a beat.
"""
from __future__ import annotations

import json
import math
import time

import structlog

from . import keys as K
from .audit import _merge_trade_provenance     # immutable-merge writer (no duplication)
from .modules import MODULES
from .outcome import _jload

log = structlog.get_logger()

MODULE_EMBEDDING_SCHEMA_VERSION = 1

# Canonical, deterministically-ordered module roster — the embedding's coordinate system. Frozen at
# import from the active bank so the vector length/order is stable within a code version and self-
# describes (the roster is stored on each row, so retrieval aligns BY NAME and survives roster drift).
ROSTER: list[str] = [m.name for m in MODULES]


# ─────────────────────────────────────────────────────────────────────────────────────────────
# Embedding
# ─────────────────────────────────────────────────────────────────────────────────────────────

def _pair_class(pair: str) -> str:
    """Coarse pair family for cohort matching: quote asset + major/alt base tier. Cheap, honest,
    and stable — finer liquidity matching is done by the spread/vpin bands in the context vector."""
    if not pair:
        return "unknown"
    p = pair.upper()
    quote = "USDT" if p.endswith("USDT") else ("USDC" if p.endswith("USDC") else
            ("USD" if p.endswith("USD") else "other"))
    base = p[: -len(quote)] if quote != "other" else p
    major = base in ("BTC", "ETH", "BNB", "SOL", "XRP")
    return f"{'major' if major else 'alt'}:{quote}"


def _weighted_horizon(modules: list) -> int:
    """Conviction-weighted mean forecast horizon over the voting modules (the decision's effective
    horizon) — a structured cohort key. Falls back to 60m when nothing voted."""
    num = den = 0.0
    for m in modules:
        c = abs(float(m.get("conviction", 0.0) or 0.0))
        h = float(m.get("horizon_min", 60) or 60)
        if c > 0:
            num += c * h
            den += c
    return int(round(num / den)) if den > 1e-9 else 60


def build_module_embedding(prov: dict) -> dict | None:
    """Build the module-state embedding for one trade from its decision/entry snapshots.

    Returns None when the decision_snapshot is absent (nothing to embed) — the caller skips it.
    """
    dec = (prov or {}).get("decision_snapshot")
    if not isinstance(dec, dict):
        return None
    # Decision.to_dict() serializes the per-module ModuleOutput list under "modules" (the dataclass
    # FIELD is `contributing`, but the persisted JSON key is `modules`); accept either for safety.
    contributing = dec.get("modules")
    if not isinstance(contributing, list):
        contributing = dec.get("contributing")
    if not isinstance(contributing, list):
        return None

    # module_vec: signed strength = direction·conviction, aligned to the roster (missing → 0).
    by_name: dict[str, float] = {}
    for m in contributing:
        if not isinstance(m, dict):
            continue
        name = str(m.get("module", "?"))
        d = float(m.get("direction", 0.0) or 0.0)
        c = float(m.get("conviction", 0.0) or 0.0)
        by_name[name] = round(max(-1.0, min(1.0, d * c)), 6)
    module_vec = [by_name.get(name, 0.0) for name in ROSTER]
    # also keep any modules not in the current roster, so retrieval can align by name across drift
    extra = {n: v for n, v in by_name.items() if n not in set(ROSTER)}
    norm = math.sqrt(sum(v * v for v in by_name.values()))

    snap = prov.get("entry_snapshot") or {}
    scal = ((snap.get("replay") or {}).get("sensor_scalars")) or {}
    exec_ = (snap.get("execution") or {})
    action = (snap.get("action_space") or {})

    def _f(v):
        try:
            return None if v is None else round(float(v), 8)
        except (TypeError, ValueError):
            return None

    pair = dec.get("symbol") or prov.get("pair") or ""
    context = {
        "regime": prov.get("regime") or dec.get("regime") or "unknown",
        "pair": pair,
        "pair_class": _pair_class(pair),
        "conviction": _f(prov.get("conviction") if prov.get("conviction") is not None
                         else dec.get("conviction")),
        "net_vote": _f(action.get("net_vote")),
        "size_frac": _f(prov.get("size_frac")),
        "vpin": _f(scal.get("vpin")),
        "ofi": _f(scal.get("ofi")),
        "funding": _f(scal.get("funding")),
        "oi_change_z": _f(scal.get("oi_change_z")),
        "sentiment": _f(scal.get("sentiment")),
        "spread_rel": _f(exec_.get("spread_rel")),
        "horizon_min": _weighted_horizon(contributing),
    }
    return {
        "schema_version": MODULE_EMBEDDING_SCHEMA_VERSION,
        "roster": list(ROSTER),
        "module_vec": module_vec,
        "module_extra": extra,            # off-roster votes (kept for name-aligned cosine on drift)
        "module_norm": round(norm, 6),
        "n_active": int(sum(1 for v in by_name.values() if abs(v) > 1e-9)),
        "context": context,
        "built_ts": round(time.time(), 3),
    }


# ─────────────────────────────────────────────────────────────────────────────────────────────
# Retrieval
# ─────────────────────────────────────────────────────────────────────────────────────────────

def _emb_by_name(emb: dict) -> dict[str, float]:
    """Reconstruct the {module_name: signed_strength} map from a stored embedding (roster + extra),
    so two embeddings are compared by NAME — correct even if the roster changed between them."""
    out: dict[str, float] = {}
    roster = emb.get("roster") or ROSTER
    vec = emb.get("module_vec") or []
    for name, v in zip(roster, vec):
        out[name] = float(v or 0.0)
    for name, v in (emb.get("module_extra") or {}).items():
        out[name] = float(v or 0.0)
    return out


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity over the union of module names (missing module = 0). 0 when either is the
    zero vector (no informative state). Range [-1, 1]."""
    if not a or not b:
        return 0.0
    keys = set(a) | set(b)
    dot = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return max(-1.0, min(1.0, dot / (na * nb)))


# context scalars compared on a standardized scale (robust constant spreads — these are typical
# magnitudes, not fitted, so the similarity is stable without a moving normaliser to drift).
_CTX_SCALE = {
    "net_vote": 0.5, "conviction": 0.3, "vpin": 0.2, "ofi": 1.0,
    "funding": 0.0005, "oi_change_z": 1.5, "sentiment": 0.5, "spread_rel": 0.001,
    "horizon_min": 120.0,
}


def _context_sim(a: dict, b: dict) -> float:
    """Gaussian similarity exp(-mean squared standardized difference) over the context scalars both
    rows actually have. 1.0 = identical context, →0 as they diverge. Regime/pair_class handled by
    the hard/soft filters in retrieve_cohort, not here."""
    sq = []
    for key, scale in _CTX_SCALE.items():
        va, vb = a.get(key), b.get(key)
        if va is None or vb is None or scale <= 0:
            continue
        sq.append(((float(va) - float(vb)) / scale) ** 2)
    if not sq:
        return 0.0
    return math.exp(-sum(sq) / len(sq))


def retrieve_cohort(query: dict, candidates: list[dict], *, k: int = 20,
                    same_regime: bool = True, same_pair_class: bool = False,
                    w_module: float = 0.7, w_context: float = 0.3,
                    min_similarity: float = 0.0) -> dict:
    """Find the k closest historical decisions to `query` by module-state + context similarity.

    query/candidates entries: {"trade_id", "embedding", "outcome"} where outcome carries the realized
    {utility, won, fault_class, net_pnl} (None when not yet harvested). Returns the ranked neighbors
    and a cohort summary (mean realized utility, win rate, direction-fault rate over neighbors that
    have outcomes). NOT prose — the score is module-vote cosine blended with market-context Gaussian.
    """
    q_emb = query.get("embedding") or {}
    q_ctx = q_emb.get("context") or {}
    q_vec = _emb_by_name(q_emb)
    q_id = query.get("trade_id")
    q_regime = q_ctx.get("regime")
    q_pclass = q_ctx.get("pair_class")

    scored = []
    for cand in candidates:
        if cand.get("trade_id") == q_id:
            continue                                   # never match a trade to itself
        c_emb = cand.get("embedding") or {}
        c_ctx = c_emb.get("context") or {}
        if same_regime and q_regime is not None and c_ctx.get("regime") != q_regime:
            continue
        if same_pair_class and q_pclass is not None and c_ctx.get("pair_class") != q_pclass:
            continue
        mod_sim = _cosine(q_vec, _emb_by_name(c_emb))
        ctx_sim = _context_sim(q_ctx, c_ctx)
        score = w_module * mod_sim + w_context * ctx_sim
        if score < min_similarity:
            continue
        scored.append((score, mod_sim, ctx_sim, cand))

    scored.sort(key=lambda t: t[0], reverse=True)
    top = scored[:max(1, int(k))]

    neighbors = []
    utils, wins, faults = [], [], []
    for score, mod_sim, ctx_sim, cand in top:
        oc = cand.get("outcome") or {}
        u = oc.get("utility")
        neighbors.append({
            "trade_id": cand.get("trade_id"),
            "pair": (cand.get("embedding") or {}).get("context", {}).get("pair"),
            "similarity": round(score, 4),
            "module_sim": round(mod_sim, 4),
            "context_sim": round(ctx_sim, 4),
            "utility": (round(float(u), 6) if u is not None else None),
            "won": oc.get("won"),
            "fault_class": oc.get("fault_class"),
            "net_pnl": oc.get("net_pnl"),
        })
        if u is not None:
            utils.append(float(u))
        if oc.get("won") is not None:
            wins.append(bool(oc.get("won")))
        if oc.get("fault_class") is not None:
            faults.append(oc.get("fault_class"))

    n_out = len(utils)
    summary = {
        "n_neighbors": len(neighbors),
        "n_with_outcome": n_out,
        "mean_utility": (round(sum(utils) / n_out, 6) if n_out else None),
        "win_rate": (round(sum(1 for w in wins if w) / len(wins), 4) if wins else None),
        "direction_fault_rate": (round(sum(1 for f in faults if f == "direction") / len(faults), 4)
                                 if faults else None),
        "mean_similarity": (round(sum(n["similarity"] for n in neighbors) / len(neighbors), 4)
                            if neighbors else None),
        "filters": {"same_regime": same_regime, "same_pair_class": same_pair_class,
                    "w_module": w_module, "w_context": w_context},
    }
    return {"query_id": q_id, "neighbors": neighbors, "cohort": summary}


# ─────────────────────────────────────────────────────────────────────────────────────────────
# Idempotent beat-harvest: persist embeddings + recompute the cohort-retrieval quality aggregate
# (mirrors audit.grade_calibration / outcome.harvest_outcomes: immutable per-row + pure recompute).
# ─────────────────────────────────────────────────────────────────────────────────────────────

def _outcome_of(packet: dict) -> dict:
    """Pull the realized cohort-relevant outcome from an OutcomePacket (None-safe)."""
    if not isinstance(packet, dict):
        return {}
    cf = packet.get("counterfactual") if isinstance(packet.get("counterfactual"), dict) else {}
    proxy_ft = (packet.get("failure_label") or {}).get("failure_type")   # nested, not top-level
    util = packet.get("utility")
    if isinstance(util, dict):                  # packet.utility = {terms, lambdas, utility: <scalar>}
        util = util.get("utility")
    return {
        "utility": util,
        "won": packet.get("won"),
        "net_pnl": packet.get("net_pnl_usdt"),
        # path-aware fault label when the twin is trustworthy, else the realized failure_type proxy
        "fault_class": (cf.get("fault_class") if cf.get("status") == "ok"
                        and cf.get("confidence") in ("high", "medium") else proxy_ft),
    }


def embed_decisions(r, limit: int = 100) -> dict:
    """Write module_embedding onto closed scibrain trades that have a decision_snapshot but no
    embedding yet, then RECOMPUTE the cohort aggregate (roster + a leave-one-out retrieval-quality
    metric) from every embedded row. Restart-safe / double-count-proof; never raises."""
    from db import db_conn
    summary = {"embedded_now": 0, "total_embedded": 0}
    try:
        # 1) embed the next batch of un-embedded decisions
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id, signals_at_entry
                     FROM trades
                    WHERE timeframe='scibrain' AND status='closed'
                      AND signals_at_entry::jsonb ? 'decision_snapshot'
                      AND NOT (signals_at_entry::jsonb ? 'module_embedding')
                    ORDER BY exit_time DESC NULLS LAST
                    LIMIT %s""",
                (int(max(1, limit)),))
            rows = cur.fetchall()
        for tid, prov_raw in rows:
            prov = _jload(prov_raw, {}) or {}
            emb = build_module_embedding(prov)
            if emb is None:
                continue
            _merge_trade_provenance(str(tid), {"module_embedding": emb})
            summary["embedded_now"] += 1

        if summary["embedded_now"]:
            try:
                r.incrby(K.COHORT_EMBEDDED_TOTAL, summary["embedded_now"])
            except Exception:
                pass

        # 2) recompute the aggregate from EVERY embedded row (idempotent)
        with db_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id,
                          signals_at_entry::jsonb->'module_embedding',
                          signals_at_entry::jsonb->'outcome_packet'
                     FROM trades
                    WHERE timeframe='scibrain' AND status='closed'
                      AND signals_at_entry::jsonb ? 'module_embedding'""")
            fetched = cur.fetchall()
        candidates = []
        for tid, emb_raw, pkt_raw in fetched:
            emb = _jload(emb_raw, None)
            if not isinstance(emb, dict):
                continue
            candidates.append({
                "trade_id": str(tid),
                "embedding": emb,
                "outcome": _outcome_of(_jload(pkt_raw, None)),
            })
        agg = _cohort_aggregate(candidates)
        try:
            r.set(K.COHORT_AGG, json.dumps(agg))
            r.set(K.COHORT_EMBEDDED_TOTAL, agg["n_embedded"])   # pure recompute, can't double-count
        except Exception:
            pass
        summary["total_embedded"] = agg["n_embedded"]
        summary["cohort_sign_hit_rate"] = agg.get("retrieval_quality", {}).get("cohort_sign_hit_rate")
    except Exception as exc:
        log.warning("scibrain_embed_error", error=str(exc)[:160])
    if summary["embedded_now"]:
        log.info("scibrain_embedded", embedded_now=summary["embedded_now"],
                 total=summary["total_embedded"], hit_rate=summary.get("cohort_sign_hit_rate"))
    return summary


def _cohort_aggregate(candidates: list, *, eval_recent: int = 120, k: int = 20) -> dict:
    """Roster + a leave-one-out retrieval-quality metric: for the most recent `eval_recent` trades
    that have a realized utility, retrieve their same-regime cohort (excluding self) and test whether
    the cohort's mean realized utility SIGN predicts the trade's own realized utility sign. A hit-rate
    materially above 0.5 is live proof the module-state embedding carries outcome-relevant structure
    (not prose). Also reports mean neighbor similarity and the regime mix."""
    n = len(candidates)
    if n == 0:
        return {"n_embedded": 0, "roster": list(ROSTER), "regimes": {},
                "retrieval_quality": {"evaluated": 0, "cohort_sign_hit_rate": None,
                                      "mean_cohort_support": None, "mean_neighbor_similarity": None},
                "note": "module-state embedding (per-module direction·conviction) + context; "
                        "cohort retrieval is name-aligned cosine + Gaussian context, NOT prose.",
                "updated_ts": round(time.time(), 3)}

    regimes: dict = {}
    for c in candidates:
        reg = ((c.get("embedding") or {}).get("context") or {}).get("regime", "unknown")
        regimes[reg] = regimes.get(reg, 0) + 1

    with_outcome = [c for c in candidates if (c.get("outcome") or {}).get("utility") is not None]
    # most recent first by embedding build time
    with_outcome.sort(key=lambda c: (c.get("embedding") or {}).get("built_ts", 0.0), reverse=True)
    evalset = with_outcome[:max(1, eval_recent)]

    hits = scored = 0
    supports, sims = [], []
    for q in evalset:
        res = retrieve_cohort(q, with_outcome, k=k, same_regime=True)
        coh = res["cohort"]
        cm = coh.get("mean_utility")
        if cm is None or coh.get("n_with_outcome", 0) < 3:
            continue                                   # too little support to score honestly
        qu = float((q.get("outcome") or {})["utility"])
        if abs(qu) < 1e-9:
            continue                                   # flat outcome → sign undefined, skip
        scored += 1
        supports.append(coh["n_with_outcome"])
        if coh.get("mean_similarity") is not None:
            sims.append(coh["mean_similarity"])
        if (cm >= 0) == (qu >= 0):
            hits += 1

    rq = {
        "evaluated": scored,
        "cohort_sign_hit_rate": (round(hits / scored, 4) if scored else None),
        "mean_cohort_support": (round(sum(supports) / len(supports), 2) if supports else None),
        "mean_neighbor_similarity": (round(sum(sims) / len(sims), 4) if sims else None),
        "k": k,
        "note": "leave-one-out: does the same-regime cohort's mean-utility SIGN predict the held-out "
                "trade's realized-utility sign? >0.5 ⇒ embedding carries outcome structure.",
    }
    return {
        "n_embedded": n,
        "roster": list(ROSTER),
        "regimes": regimes,
        "retrieval_quality": rq,
        "note": "module-state embedding (per-module direction·conviction) + market context; cohort "
                "retrieval is name-aligned module cosine blended with Gaussian context, NOT prose.",
        "updated_ts": round(time.time(), 3),
    }
