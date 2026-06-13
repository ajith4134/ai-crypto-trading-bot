"""SciBrain Phase-7e — HIPPOCAMPUS: rich episodic memory + pattern separation (design §3.5).

"Remember a novel event after one experience and retrieve structurally similar episodes." This assembles
the rich Episode the design demands — EntrySnapshot → LifeTrace → OutcomePacket → CounterfactualPacket —
from the components the live circuit ALREADY stores per closed trade (signals_at_entry: entry_snapshot,
module_embedding, lifetrace, outcome_packet), and gives each episode:

  • a PATTERN-SEPARATED embedding — a fixed expand-then-k-Winner-Take-All sparse code (the hippocampal
    dentate-gyrus trick) so structurally similar episodes get DECORRELATED codes and a rare failure is NOT
    averaged away into the bulk of wins;
  • a REPLAY PRIORITY = |reward_pred_error| + surprise + tail_severity + model_disagreement + rarity
    − redundancy (design §3.5) — the score the prioritized-replay sampler (next task) will draw from.

Plus an honest MEMORY-HEALTH diagnosis that POINTS OUT the issues: outcome imbalance, whether rare
failures are actually PRESERVED (do they get high priority, or are they drowned by wins?), and the
pattern-separation quality (do distinct episodes get distinguishable codes?). Tier-0: pure read, no
trading authority — the experience substrate the replay/consolidation/world-model tasks will consume.
"""
from __future__ import annotations

import json
import math
import time

import numpy as np
import structlog

from . import keys as K

log = structlog.get_logger()

_CANON_REGIMES = ("trending", "mean_revert", "turbulent", "neutral")
_MV_DIM = 24            # fixed module_vec width (pad/truncate — rosters vary slightly across trades)
_EMB_DIM = 64           # expanded code dim
_KWTA = 8               # active units after k-Winner-Take-All (sparse → pattern separation)
_PRI_W = {"rpe": 1.0, "surprise": 0.8, "tail": 0.9, "disagreement": 0.5, "rarity": 0.7, "redundancy": 0.6}


def _proj_matrix(in_dim: int) -> np.ndarray:
    """Fixed random projection (seeded → deterministic, untrained) that EXPANDS the input before k-WTA."""
    rng = np.random.default_rng(7)
    return rng.standard_normal((in_dim, _EMB_DIM)).astype(np.float32) / math.sqrt(in_dim)


def _kwta(vec: np.ndarray, k: int = _KWTA) -> np.ndarray:
    """k-Winner-Take-All sparsification: keep the k largest-magnitude units, zero the rest. This is the
    dentate-gyrus pattern-separation step — it decorrelates similar inputs into distinguishable codes."""
    out = np.zeros_like(vec)
    if vec.size == 0:
        return out
    idx = np.argsort(-np.abs(vec))[:k]
    out[idx] = vec[idx]
    n = np.linalg.norm(out)
    return out / n if n > 0 else out


def _load_rows(limit: int = 400) -> list[dict]:
    from db import db_conn
    rows = []
    with db_conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT pair, direction, net_pnl_usdt, EXTRACT(EPOCH FROM entry_time), "
                "signals_at_entry->'module_embedding', signals_at_entry->'outcome_packet', "
                "signals_at_entry->'lifetrace', signals_at_entry->'conviction' "
                "FROM trades WHERE status='closed' AND signals_at_entry->'outcome_packet' IS NOT NULL "
                "AND entry_time IS NOT NULL ORDER BY entry_time DESC LIMIT %s", (limit,))
            for pair, direction, pnl, ets, me, op, lt, conv in cur.fetchall():
                rows.append({
                    "pair": pair, "direction": direction, "net_pnl": float(pnl or 0.0),
                    "entry_ts": float(ets or 0.0),
                    "module_embedding": _asd(me), "outcome": _asd(op), "lifetrace": _asd(lt),
                    "conviction": float(conv) if isinstance(conv, (int, float)) else _conv(me),
                })
    return rows


def _asd(v):
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v) if v else {}
    except (TypeError, ValueError):
        return {}


def _conv(me):
    try:
        return float((me or {}).get("context", {}).get("conviction", 0.5))
    except Exception:
        return 0.5


def _episode_input(row: dict) -> np.ndarray:
    """The raw feature vector an episode is pattern-separated FROM: module_vec + outcome geometry + regime."""
    me = row["module_embedding"]
    raw_mv = np.asarray(me.get("module_vec") or [], dtype=np.float32).ravel()
    mv = np.zeros(_MV_DIM, dtype=np.float32)               # fixed width (pad/truncate ragged rosters)
    if raw_mv.size:
        mv[:min(raw_mv.size, _MV_DIM)] = raw_mv[:_MV_DIM]
    op = row["outcome"]
    geo = np.asarray([
        float(op.get("net_pnl_usdt") or row["net_pnl"]) / 10.0,
        float(op.get("drawdown_frac") or 0.0),
        float(op.get("mfe_usdt") or 0.0) / 10.0,
        float(op.get("mae_usdt") or 0.0) / 10.0,
        1.0 if op.get("won") else 0.0,
    ], dtype=np.float32)
    reg = str((me.get("context") or {}).get("regime", "neutral"))
    rg = np.asarray([1.0 if reg == rr else 0.0 for rr in _CANON_REGIMES], dtype=np.float32)
    return np.concatenate([mv, geo, rg])


def _assemble(rows: list[dict]) -> dict:
    """Pattern-separated codes + the §3.5 replay-priority signals for a batch of episodes. Shared by the
    memory view AND the prioritized-replay sampler so both see the SAME priorities."""
    from collections import Counter
    n = len(rows)
    X = np.stack([_episode_input(rw) for rw in rows])
    W = _proj_matrix(X.shape[1])
    raw_codes = X @ W                                             # expanded (dense)
    codes = np.stack([_kwta(raw_codes[i]) for i in range(n)])     # sparse, pattern-separated

    won = np.asarray([1.0 if rw["outcome"].get("won") else 0.0 for rw in rows])
    conv = np.asarray([max(0.0, min(1.0, rw["conviction"])) for rw in rows])
    rpe = np.abs(won - conv)                                      # reward-prediction error (Brier-like)
    dd = np.asarray([float(rw["outcome"].get("drawdown_frac") or 0.0) for rw in rows])
    mae = np.asarray([float(rw["outcome"].get("mae_usdt") or 0.0) for rw in rows])
    tail = np.clip(0.6 * dd + 0.4 * (mae / (np.percentile(mae, 95) + 1e-6)), 0, 1)
    fail = np.asarray([1.0 if rw["outcome"].get("failure_label") not in (None, "", "none") else 0.0
                       for rw in rows])
    surprise = np.clip(0.6 * rpe + 0.4 * fail, 0, 1)
    net_vote = np.asarray([abs(float((rw["module_embedding"].get("context") or {}).get("net_vote", 0.0) or 0.0))
                           for rw in rows])
    disagreement = np.clip(1.0 - net_vote, 0, 1)                  # low net vote ⇒ modules disagreed
    sigs = [f"{(rw['module_embedding'].get('context') or {}).get('regime','?')}|"
            f"{rw['outcome'].get('failure_label') or ('win' if rw['outcome'].get('won') else 'loss')}"
            for rw in rows]
    freq = Counter(sigs)
    rarity = np.asarray([1.0 / freq[s] for s in sigs]); rarity = rarity / (rarity.max() + 1e-9)
    cn = codes / (np.linalg.norm(codes, axis=1, keepdims=True) + 1e-9)
    sim = cn @ cn.T
    np.fill_diagonal(sim, 0.0)
    redundancy = np.clip(sim.mean(axis=1) * n / max(1, n - 1), 0, 1)
    priority = np.clip(_PRI_W["rpe"] * rpe + _PRI_W["surprise"] * surprise + _PRI_W["tail"] * tail
                       + _PRI_W["disagreement"] * disagreement + _PRI_W["rarity"] * rarity
                       - _PRI_W["redundancy"] * redundancy, 0, None)
    return {"X": X, "codes": codes, "sim": sim, "won": won, "rpe": rpe, "surprise": surprise,
            "tail": tail, "disagreement": disagreement, "rarity": rarity, "redundancy": redundancy,
            "fail": fail, "priority": priority}


def build_episodic_memory(r, *, limit: int = 400, publish: bool = True) -> dict:
    """Assemble rich Episodes with pattern-separated embeddings + replay priority, and diagnose memory
    health (imbalance, rare-event preservation, separation quality). Pure read; never raises."""
    out = {"contract": "EpisodicMemory", "available": False, "ts": round(time.time(), 3)}
    try:
        rows = _load_rows(limit)
        n = len(rows)
        if n < 20:
            out.update({"health": "cold", "n": n, "issues": [],
                        "note": "Episodic memory cold — too few closed episodes yet."})
            if publish:
                r.set(K.EPISODIC_MEMORY, json.dumps(out))
            return out

        a = _assemble(rows)
        X, codes, sim = a["X"], a["codes"], a["sim"]
        won, rpe, surprise, tail = a["won"], a["rpe"], a["surprise"], a["tail"]
        disagreement, rarity, redundancy, fail = a["disagreement"], a["rarity"], a["redundancy"], a["fail"]
        priority = a["priority"]
        order = np.argsort(-priority)
        episodes = []
        for i in order[:40]:
            rw = rows[i]; op = rw["outcome"]
            episodes.append({
                "pair": rw["pair"], "direction": rw["direction"], "entry_ts": rw["entry_ts"],
                "won": bool(op.get("won")), "net_pnl": round(rw["net_pnl"], 4),
                "failure_label": op.get("failure_label"), "exit_reason": op.get("exit_reason"),
                "priority": round(float(priority[i]), 4),
                "signals": {"rpe": round(float(rpe[i]), 3), "surprise": round(float(surprise[i]), 3),
                            "tail": round(float(tail[i]), 3), "disagreement": round(float(disagreement[i]), 3),
                            "rarity": round(float(rarity[i]), 3), "redundancy": round(float(redundancy[i]), 3)},
                "embedding_nonzero": int(np.count_nonzero(codes[i])),
            })

        # ── MEMORY HEALTH diagnostics (the issues) ──
        base_loss = float(1.0 - won.mean())
        top_k = max(10, n // 10)
        top_loss = float(1.0 - won[order[:top_k]].mean())          # loss frac among top-priority
        # pattern separation: mean pairwise sim of sparse codes vs raw inputs (lower sparse ⇒ better sep)
        Xn = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
        raw_sim = float((Xn @ Xn.T - np.eye(n)).sum() / (n * (n - 1)))
        code_sim = float(sim.sum() / (n * (n - 1)))
        sep_gain = round(raw_sim - code_sim, 4)

        issues = []

        def add(code, sev, title, detail, rec):
            issues.append({"code": code, "severity": sev, "title": title, "detail": detail, "recommendation": rec})

        if base_loss < 0.15:
            add("outcome_imbalance", "warn", f"Outcome imbalance — only {base_loss * 100:.1f}% loss episodes",
                "Wins dominate the episode store; naive replay would rarely revisit failures.",
                "Prioritized replay (next task) must over-sample losses/tails — verified by the metric below.")
        if top_loss <= base_loss + 0.02:
            add("rare_event_not_preserved", "critical",
                f"Rare failures NOT preserved — top-priority loss rate {top_loss * 100:.1f}% ≈ base {base_loss * 100:.1f}%",
                "High-priority episodes do not over-represent failures, so rare losses get averaged away.",
                "Raise tail/surprise/rarity weights so failures rise to the top of the replay queue.")
        else:
            add("rare_events_preserved", "info",
                f"Rare failures preserved — top-priority loss rate {top_loss * 100:.1f}% > base {base_loss * 100:.1f}%",
                "Failures rise to the top of the replay queue (the point of pattern separation).",
                "Keep the surprise/tail/rarity priority weights.")
        if sep_gain <= 0.02:
            add("weak_pattern_separation", "warn",
                f"Weak pattern separation (sparse-vs-raw similarity gain {sep_gain})",
                "Sparse codes are barely more decorrelated than the raw inputs — episodes may collide.",
                "Increase the expansion dim or lower k in k-WTA for sparser, more orthogonal codes.")
        else:
            add("pattern_separation_ok", "info",
                f"Pattern separation working (similarity gain {sep_gain}, sparse sim {round(code_sim, 3)})",
                "Sparse k-WTA codes are more decorrelated than the raw inputs.", "—")

        sev_rank = {"critical": 3, "warn": 2, "info": 1}
        worst = max((sev_rank[i["severity"]] for i in issues), default=0)
        health = "issues" if worst >= 3 else "watch" if worst >= 2 else "healthy"

        out.update({
            "available": True, "health": health, "n_episodes": n,
            "store": {"n": n, "win_rate": round(float(won.mean()), 4), "loss_rate": round(base_loss, 4),
                      "n_failures_labeled": int(fail.sum()),
                      "time_span_h": round((rows[0]["entry_ts"] - rows[-1]["entry_ts"]) / 3600.0, 1)},
            "pattern_separation": {"embed_dim": _EMB_DIM, "kwta_active": _KWTA,
                                   "raw_similarity": round(raw_sim, 4), "code_similarity": round(code_sim, 4),
                                   "separation_gain": sep_gain},
            "preservation": {"base_loss_rate": round(base_loss, 4), "top_priority_loss_rate": round(top_loss, 4),
                             "top_k": top_k},
            "priority_weights": _PRI_W,
            "top_episodes": episodes,
            "issues": sorted(issues, key=lambda i: -sev_rank[i["severity"]]),
            "n_issues": {s: sum(1 for i in issues if i["severity"] == s) for s in ("critical", "warn", "info")},
            "note": ("Rich episodic memory (design §3.5): EntrySnapshot/LifeTrace/OutcomePacket re-assembled "
                     "from the ledger, each with a pattern-separated k-WTA code (so rare failures aren't "
                     "averaged into the wins) and a replay priority (|rpe|+surprise+tail+disagreement+rarity"
                     "−redundancy). Health flags whether failures are actually PRESERVED at the top of the "
                     "replay queue and whether the codes are well-separated. Pure read, no authority."),
        })
        if publish:
            try:
                r.set(K.EPISODIC_MEMORY, json.dumps(out))
            except Exception:
                pass
    except Exception as exc:
        out["error"] = str(exc)[:200]
        log.warning("scibrain_episodic_failed", error=str(exc)[:200])
    return out


# ── Phase-7e step-4: PRIORITIZED REPLAY sampler + importance correction (design §3.5, Schaul 2015) ──
def replay_health(r, *, limit: int = 400, batch_size: int = 64, publish: bool = True) -> dict:
    """Prioritized-replay sampling diagnostics. Samples ∝ priority^α, applies importance-sampling (IS)
    weights w_i=(N·P_i)^(−β) to correct the bias, and reports whether (a) rare FAILURES are over-sampled
    (the point) AND (b) the IS correction RECOVERS the true outcome distribution (unbiasedness) — plus the
    effective coverage so over-concentration on a few episodes is caught. Pure read; never raises."""
    out = {"contract": "ReplayHealth", "available": False, "ts": round(time.time(), 3)}
    try:
        try:
            alpha = float(r.get("scibrain:replay:alpha") or 0.6)   # priority exponent
            beta = float(r.get("scibrain:replay:beta") or 0.4)     # IS correction exponent (anneals 0→1)
        except (TypeError, ValueError):
            alpha, beta = 0.6, 0.4
        rows = _load_rows(limit)
        n = len(rows)
        if n < 20:
            out.update({"health": "cold", "n": n, "issues": [], "note": "replay cold — too few episodes."})
            if publish:
                r.set("scibrain:replay_health", json.dumps(out))
            return out

        a = _assemble(rows)
        won = a["won"]; priority = a["priority"]
        p = priority + 1e-3
        pa = p ** alpha
        P = pa / pa.sum()                                          # sampling distribution
        base_loss = float(1.0 - won.mean())

        # effective coverage of the priority distribution (how many episodes it really spreads over)
        priority_ess = float((pa.sum() ** 2) / (np.square(pa).sum() + 1e-12))
        ess_frac = priority_ess / n

        # draw a prioritized batch (with replacement) + IS weights
        rng = np.random.default_rng(0)
        idx = rng.choice(n, size=min(batch_size, n * 4), replace=True, p=P)
        w = (n * P[idx]) ** (-beta)
        w = w / (w.max() + 1e-12)                                  # normalize so IS only scales DOWN
        sampled_loss = float(1.0 - won[idx].mean())                # raw prioritized estimate (biased UP)
        is_loss = float((w * (1.0 - won[idx])).sum() / (w.sum() + 1e-12))  # IS-corrected estimate
        batch_ess = float((w.sum() ** 2) / (np.square(w).sum() + 1e-12))   # weight concentration
        bias_recovered = abs(is_loss - base_loss) < 0.05

        issues = []

        def add(code, sev, title, detail, rec):
            issues.append({"code": code, "severity": sev, "title": title, "detail": detail, "recommendation": rec})

        if sampled_loss > base_loss + 0.03:
            add("failures_oversampled", "info",
                f"Prioritized replay over-samples failures — sampled loss {sampled_loss*100:.0f}% > base {base_loss*100:.0f}%",
                "Rare/surprising failures are revisited more often (the point of prioritization).",
                "Keep α>0; this is intended.")
        else:
            add("weak_prioritization", "warn",
                f"Prioritization weak — sampled loss {sampled_loss*100:.0f}% ≈ base {base_loss*100:.0f}%",
                "The sampler barely favours high-priority episodes.", "Raise α (priority exponent).")
        if bias_recovered:
            add("is_correction_ok", "info",
                f"Importance correction recovers the true distribution (IS loss {is_loss*100:.1f}% ≈ base {base_loss*100:.1f}%)",
                "The IS weights unbias the prioritized sample — the corrected estimate matches reality.",
                "Anneal β→1 over training to fully unbias.")
        else:
            add("is_correction_biased", "warn",
                f"IS correction does NOT recover base ({is_loss*100:.1f}% vs {base_loss*100:.1f}%)",
                "The importance weights leave residual bias in the corrected estimate.",
                f"Raise β (currently {beta}) toward 1.0 for fuller correction.")
        if ess_frac < 0.25:
            add("over_concentration", "warn",
                f"Replay over-concentrated — effective coverage {ess_frac*100:.0f}% of episodes",
                f"The priority^α distribution spreads over only ~{priority_ess:.0f} of {n} episodes; most are rarely seen.",
                "Lower α or add an ε-uniform floor so the long tail still gets replayed.")
        if batch_ess / len(idx) < 0.30:
            add("high_is_variance", "warn",
                f"High IS-weight variance — batch ESS {batch_ess:.0f}/{len(idx)}",
                "A few episodes dominate the importance-corrected gradient (high variance).",
                "Lower α or raise β; clip the max IS weight.")

        sev_rank = {"critical": 3, "warn": 2, "info": 1}
        worst = max((sev_rank[i["severity"]] for i in issues), default=0)
        health = "issues" if worst >= 3 else "watch" if worst >= 2 else "healthy"

        out.update({
            "available": True, "health": health, "n": n,
            "params": {"alpha": alpha, "beta": beta, "batch_size": int(min(batch_size, n * 4))},
            "coverage": {"priority_ess": round(priority_ess, 1), "ess_frac": round(ess_frac, 4),
                         "n_episodes": n},
            "estimates": {"base_loss_rate": round(base_loss, 4), "sampled_loss_rate": round(sampled_loss, 4),
                          "is_corrected_loss_rate": round(is_loss, 4), "bias_recovered": bias_recovered},
            "is_weights": {"batch_ess": round(batch_ess, 1), "max": 1.0,
                           "min": round(float(w.min()), 4), "mean": round(float(w.mean()), 4)},
            "issues": sorted(issues, key=lambda i: -sev_rank[i["severity"]]),
            "n_issues": {s: sum(1 for i in issues if i["severity"] == s) for s in ("critical", "warn", "info")},
            "note": ("Prioritized replay (design §3.5 / Schaul 2015): sample ∝ priority^α, then weight by "
                     "IS w=(N·P)^(−β) to UNBIAS the sample. Health = does it over-sample rare failures AND "
                     "does the IS correction recover the true outcome distribution, without over-"
                     "concentrating on a handful of episodes. Pure read; no trading authority."),
        })
        if publish:
            try:
                r.set("scibrain:replay_health", json.dumps(out))
            except Exception:
                pass
    except Exception as exc:
        out["error"] = str(exc)[:200]
        log.warning("scibrain_replay_health_failed", error=str(exc)[:200])
    return out
