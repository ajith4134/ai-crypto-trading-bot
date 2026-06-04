"""
Section AA: Autonomous Strategy Research Engine — AA-01 to AA-10.
Activates at 100 closed paper trades.
"""
import ast
import json
import math
import structlog
import redis_client
from db import db_conn

log = structlog.get_logger()


# AA-01: MCTS node
class MCTSNode:
    def __init__(self, hypothesis: str, parent=None):
        self.hypothesis = hypothesis
        self.parent = parent
        self.children: list["MCTSNode"] = []
        self.visits = 0
        self.value = 0.0

    def ucb(self, exploration_c: float = 1.41) -> float:
        if self.visits == 0:
            return float("inf")
        parent_visits = self.parent.visits if self.parent else 1
        return self.value / self.visits + exploration_c * math.sqrt(math.log(parent_visits) / self.visits)

    def best_child(self) -> "MCTSNode":
        return max(self.children, key=lambda c: c.ucb())


def originality_check(code: str, strategy_dir: str = "strategies") -> bool:
    """AA-04: AST structural similarity check against all existing strategies.
    Relative path resolved against container WORKDIR (/app)."""
    import os
    try:
        new_ast = ast.dump(ast.parse(code))
    except SyntaxError:
        return False

    for root, _, files in os.walk(strategy_dir):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            try:
                existing = open(fpath).read()
                existing_ast = ast.dump(ast.parse(existing))
                # Simple similarity: shared tokens / total tokens
                new_tokens = set(new_ast.split())
                ex_tokens = set(existing_ast.split())
                if not new_tokens:
                    continue
                similarity = len(new_tokens & ex_tokens) / len(new_tokens)
                if similarity > 0.80:
                    log.info("strategy_too_similar", similarity=round(similarity, 2), file=fname)
                    return False
            except Exception:
                continue
    return True


# ── F36 Step 4: World-Model prescreen + Step 6: evolutionary iteration ─────
#
# Honest limit: the world model is trained on real trades; it does NOT execute
# strategy code. So the "simulation" here aggregates F34 imagine_trajectory
# rewards across a synthetic distribution of market observations in the
# strategy's *bias direction* (long-leaning vs short-leaning per hypothesis
# keywords). This filters strategies whose directional bias looks unprofitable
# in the regimes captured in the latent space — it does NOT validate specific
# entry rules. Documented in PROGRESS.md Session 2026-05-21 (cont. 2).

_LONG_KEYWORDS  = ("long", "buy", "bullish", "upside", "uptrend", "breakout", "accumulate")
_SHORT_KEYWORDS = ("short", "sell", "bearish", "downside", "downtrend", "breakdown", "distribute")


def _strategy_direction_bias(strategy: dict) -> str:
    """Scan hypothesis + entry_conditions text for long/short keywords.
    Returns 'long', 'short', or 'neutral'."""
    blob_parts = [str(strategy.get("hypothesis", "") or "")]
    for key in ("entry_conditions", "exit_conditions"):
        v = strategy.get(key)
        if isinstance(v, str):
            blob_parts.append(v)
        elif isinstance(v, (list, tuple)):
            blob_parts.extend(str(x) for x in v)
    blob = " ".join(blob_parts).lower()
    long_hits  = sum(blob.count(k) for k in _LONG_KEYWORDS)
    short_hits = sum(blob.count(k) for k in _SHORT_KEYWORDS)
    if long_hits > short_hits + 1:
        return "long"
    if short_hits > long_hits + 1:
        return "short"
    return "neutral"


def _synthetic_observations(n: int) -> list[dict]:
    """Deterministic synthetic observation grid spanning price/volume/leverage
    ranges. Deterministic for reproducibility — same n → same set."""
    import math
    out = []
    for i in range(n):
        # Spread across the observable feature ranges the encoder sees in
        # real trades (entry_price 0.01..50000, capital 10..1000, lev 1..20)
        price  = math.exp(math.log(0.01) + (math.log(50000) - math.log(0.01)) * (i + 1) / (n + 1))
        volume = 50.0 + ((i * 37) % 950)         # 50..1000 USDT
        lev    = 1 + ((i * 7) % 20)              # 1..20×
        out.append({"price": float(price), "volume": float(volume), "lev": float(lev)})
    return out


_BASELINE_INVERTED_FLOOR = 0.25   # mean baseline prob_profit below this → bypass
_BASELINE_CACHE_TTL_SEC  = 600
_BASELINE_PROBE_N        = 8


def _baseline_probe() -> dict:
    """Cached baseline probe of the world model on a small synthetic obs sample
    across both directions. Returns {mean_prob_profit, n, skipped}.

    Used to detect when the world model is empirically inverted (cont. 39):
    if mean baseline prob_profit is far below the real-trade win rate, the
    model's verdicts on new strategies cannot be trusted and the prescreen
    must fail-open. Cached 600s to amortise the probe cost across bursts.
    """
    import redis_client as _rc
    r = _rc.get()
    try:
        cached = r.get("world_model:baseline_probe")
        if cached:
            import json as _j
            return _j.loads(cached)
    except Exception:
        pass

    try:
        from world_model.model import imagine_trajectory
    except Exception:
        return {"mean_prob_profit": 0.5, "n": 0, "skipped": True}

    obs_list = _synthetic_observations(_BASELINE_PROBE_N)
    probs: list[float] = []
    for obs in obs_list:
        for action in ("open_long", "open_short"):
            try:
                traj = imagine_trajectory(action, n_steps=5, initial_obs=obs)
            except Exception:
                continue
            probs.append(float(traj.get("prob_profit", 0.5)))

    if not probs:
        return {"mean_prob_profit": 0.5, "n": 0, "skipped": True}

    mean_pp = sum(probs) / len(probs)
    result = {"mean_prob_profit": round(mean_pp, 4), "n": len(probs), "skipped": False}
    try:
        import json as _j
        r.setex("world_model:baseline_probe", _BASELINE_CACHE_TTL_SEC, _j.dumps(result))
        r.set("world_model:baseline_probe_last_pp", result["mean_prob_profit"])
    except Exception:
        pass
    return result


def world_model_prescreen(strategy: dict, n_samples: int = 12, n_steps: int = 5) -> dict:
    """AA-05 / Blueprint F36 Step 4.

    Use F34 world model to imagine outcomes for the strategy's bias direction
    across a synthetic observation distribution. Returns a verdict dict with:
      promising   : bool   — mean prob_profit >= 0.50
      score       : float  — mean predicted reward in bias direction
      prob_profit : float  — fraction of imagined rollouts with positive reward
      uncertainty : float  — mean per-rollout reward std
      direction   : str    — inferred bias (long/short/neutral)
      n_samples   : int
      marginal    : bool   — score in [score_floor, score_ceiling]; eligible for iteration
    """
    try:
        from world_model.model import imagine_trajectory
    except Exception as exc:
        log.warning("prescreen_world_model_import_failed", error=str(exc)[:200])
        # Cannot prescreen without world model — be permissive but flag it.
        return {"promising": True, "score": 0.0, "prob_profit": 0.5,
                "uncertainty": 1.0, "direction": "neutral", "n_samples": 0,
                "marginal": False, "skipped": True}

    # cont. 39: bypass the prescreen when the world model is empirically
    # inverted (predicts <25% prob_profit across baseline obs vs actual
    # win rates near 50%+). Without this guard, every LLM-generated
    # strategy is rejected as "clearly unpromising" and the F8 lifecycle
    # never sees new candidates. The 30-trade / 7-day paper trial gate
    # still filters bad strategies downstream.
    baseline = _baseline_probe()
    if (not baseline.get("skipped")
            and baseline.get("mean_prob_profit", 0.5) < _BASELINE_INVERTED_FLOOR):
        log.warning("world_model_prescreen_bypassed_inverted_baseline",
                    baseline_pp=baseline["mean_prob_profit"],
                    floor=_BASELINE_INVERTED_FLOOR)
        try:
            import redis_client as _rc
            _rc.get().incr("research:prescreen_bypassed_count")
        except Exception:
            pass
        return {"promising": True, "score": 0.0, "prob_profit": 0.5,
                "uncertainty": 1.0, "direction": "neutral", "n_samples": 0,
                "marginal": False, "skipped": True,
                "bypass_reason": "world_model_inverted_baseline",
                "baseline_pp": baseline["mean_prob_profit"]}

    direction = _strategy_direction_bias(strategy)
    actions = ("open_long",) if direction == "long" else \
              ("open_short",) if direction == "short" else \
              ("open_long", "open_short")
    obs_list = _synthetic_observations(n_samples)

    rewards: list[float] = []
    uncerts: list[float] = []
    for obs in obs_list:
        for action in actions:
            try:
                traj = imagine_trajectory(action, n_steps=n_steps, initial_obs=obs)
            except Exception as exc:
                log.debug("prescreen_imagine_failed", error=str(exc)[:120])
                continue
            rewards.append(float(traj.get("mean_pnl", 0.0)))
            uncerts.append(float(traj.get("uncertainty", 0.0)))

    if not rewards:
        return {"promising": True, "score": 0.0, "prob_profit": 0.5,
                "uncertainty": 1.0, "direction": direction, "n_samples": 0,
                "marginal": False, "skipped": True}

    score       = sum(rewards) / len(rewards)
    prob_profit = sum(1 for r in rewards if r > 0) / len(rewards)
    uncertainty = sum(uncerts) / len(uncerts) if uncerts else 0.0
    promising   = prob_profit >= 0.50
    # Marginal band: not promising, but not clearly bad either — eligible for
    # evolutionary iteration before final archive.
    # cont. 30: lowered the marginal lower bound 0.40 → 0.30. After cont. 28
    # observed 9/9 prescreen rejections in 24h (all archived as "clearly
    # unpromising") with 0 new strategies in DB for 2+ days. The world model
    # itself is poorly calibrated post-cont.23 (trained on broken-trail era
    # outcomes) so its probability estimates skew pessimistic. Loosening the
    # marginal band lets the evolutionary iteration loop try to lift more
    # candidates; surviving iterations still need >= 0.50 to be promising.
    marginal    = (not promising) and (prob_profit >= 0.30)

    return {
        "promising": promising,
        "score": round(score, 4),
        "prob_profit": round(prob_profit, 4),
        "uncertainty": round(uncertainty, 4),
        "direction": direction,
        "n_samples": len(rewards),
        "marginal": marginal,
        "skipped": False,
    }


def _crossover_with_top_active(strategy: dict) -> dict | None:
    """Blueprint F36 Step 6 crossover: blend the marginal strategy with the
    top-bandit-ranked active strategy's typed configs.

    Pick the active strategy with the highest win_rate, then 50/50 blend
    DCA thresholds + SL params + capital sizing. Entry overrides take the
    active strategy's version unchanged (the marginal strategy's entry
    text is what we're trying to evolve, not its gates).

    Returns the blended strategy dict, or None when no active strategy
    is available (selector silent / DB empty).

    Cont. 17 — closes cont. 2 honesty note #5 (crossover not implemented).
    """
    import copy
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name, dca_rules, trailing_sl_params, "
                    "       position_sizing_rules, entry_overrides "
                    "FROM strategies "
                    "WHERE status = 'active' "
                    "ORDER BY win_rate DESC NULLS LAST LIMIT 1"
                )
                row = cur.fetchone()
        if not row:
            return None
        active_name, a_dca, a_sl, a_cap, a_entry = row
    except Exception as exc:
        log.debug("crossover_db_failed", error=str(exc)[:120])
        return None

    import json as _json
    def _parse(raw):
        if raw is None: return {}
        if isinstance(raw, dict): return raw
        try: return _json.loads(raw)
        except Exception: return {}

    a_dca   = _parse(a_dca)
    a_sl    = _parse(a_sl)
    a_cap   = _parse(a_cap)
    a_entry = _parse(a_entry)

    s = copy.deepcopy(strategy)

    # DCA blend: 50/50 numeric average per key.
    s_dca = _parse(s.get("dca_thresholds") or s.get("dca_rules") or {})
    for k in ("round_1_pct", "round_2_pct"):
        sv, av = s_dca.get(k), a_dca.get(k)
        if isinstance(sv, (int, float)) and isinstance(av, (int, float)):
            s_dca[k] = round((float(sv) + float(av)) / 2.0, 2)
        elif av is not None and sv is None:
            s_dca[k] = av
    if s_dca:
        s["dca_thresholds"] = s_dca

    # SL blend: numeric mean per key when both have it.
    blended_sl: dict = {}
    for k in ("initial_atr_mult", "initial_min_pct", "trailing_dist_pct"):
        sv, av = (_parse(s.get("trailing_sl_params") or {})).get(k), a_sl.get(k)
        if isinstance(sv, (int, float)) and isinstance(av, (int, float)):
            blended_sl[k] = round((float(sv) + float(av)) / 2.0, 4)
        elif av is not None and sv is None:
            blended_sl[k] = av
        elif sv is not None and av is None:
            blended_sl[k] = sv
    if blended_sl:
        s["trailing_sl_params"] = blended_sl

    # Capital mult blend: 50/50.
    sv = (_parse(s.get("position_sizing_rules") or {})).get("capital_pct_mult")
    av = a_cap.get("capital_pct_mult")
    if isinstance(sv, (int, float)) and isinstance(av, (int, float)):
        s["position_sizing_rules"] = {"capital_pct_mult": round((float(sv) + float(av)) / 2.0, 3)}
    elif av is not None and sv is None:
        s["position_sizing_rules"] = {"capital_pct_mult": av}

    # Entry overrides: adopt the active strategy's outright (the marginal
    # strategy's entry TEXT is what we're evolving; gates come from the
    # proven active strategy).
    if a_entry:
        s["entry_overrides"] = a_entry

    s["_crossover_parent"] = active_name
    return s


def _mutate_strategy(strategy: dict, iteration: int) -> dict:
    """QuantaAlpha-style parameter mutation. Touches dca_thresholds and
    entry_conditions text. Pure function — returns a new dict.

    Mutation operators (rotated deterministically by iteration number so
    the search is reproducible):
      0: scale round_1_pct by ±15%
      1: scale round_2_pct by ±15%
      2: append a stricter entry condition
      3: remove the weakest entry condition (if any)
    """
    import copy, json
    s = copy.deepcopy(strategy)

    dca_raw = s.get("dca_thresholds") or s.get("dca_rules") or {}
    if isinstance(dca_raw, str):
        try:
            dca = json.loads(dca_raw)
        except Exception:
            dca = {}
    else:
        dca = dict(dca_raw)
    dca.setdefault("round_1_pct", -20.0)
    dca.setdefault("round_2_pct", -40.0)

    op = iteration % 4
    if op == 0:
        scale = 1.15 if (iteration // 4) % 2 == 0 else 0.85
        dca["round_1_pct"] = round(float(dca["round_1_pct"]) * scale, 2)
    elif op == 1:
        scale = 1.15 if (iteration // 4) % 2 == 0 else 0.85
        dca["round_2_pct"] = round(float(dca["round_2_pct"]) * scale, 2)
    # cont. 33: clip after mutation so iterative 0.85^N scaling can't drift
    # round_1_pct toward zero. Pre-cont-33 a sane -20 could become -0.06 after
    # ~50 iterations — exactly the broken values found in retired research_*.
    dca["round_1_pct"] = max(-50.0, min(-8.0, float(dca["round_1_pct"])))
    dca["round_2_pct"] = max(-80.0, min(-12.0, float(dca["round_2_pct"])))
    s["dca_thresholds"] = dca

    entry = s.get("entry_conditions") or []
    if isinstance(entry, str):
        try:
            entry = json.loads(entry)
        except Exception:
            entry = [entry]
    if not isinstance(entry, list):
        entry = list(entry) if hasattr(entry, "__iter__") else [str(entry)]

    if op == 2:
        entry = list(entry) + [f"signal_strength >= {60 + (iteration % 5) * 3}"]
    elif op == 3 and len(entry) > 1:
        entry = list(entry)[:-1]
    s["entry_conditions"] = entry

    return s


def iterate_marginal_strategy(strategy: dict,
                              max_iters: int = 10) -> tuple[dict, dict, list[dict]]:
    """Blueprint F36 Step 6: QuantaAlpha-style iteration.

    Cont. 17 — adds crossover with the top active strategy every 3rd iter
    (operator = "crossover"), interleaved with the 4 mutation operators.
    Closes cont. 2 honesty note #5 (crossover not implemented).

    Strategy: iter 3, 6, 9 attempt crossover with the bandit-favorite active
    strategy; other iters mutate. Crossover variants that improve the score
    get adopted as the new `best`, so subsequent iterations mutate FROM the
    crossover instead of the original — true evolutionary pressure.

    Stops early when a variant becomes promising. Returns (best_strategy,
    best_screen, history).
    """
    best         = strategy
    best_screen  = world_model_prescreen(best)
    history: list[dict] = [{"iter": 0, "op": "initial",
                            "score": best_screen["score"],
                            "prob_profit": best_screen["prob_profit"]}]
    for i in range(1, max_iters + 1):
        if i % 3 == 0:
            crossed = _crossover_with_top_active(best)
            if crossed is not None:
                candidate = crossed
                op_name = f"crossover_{crossed.get('_crossover_parent', '?')}"
            else:
                # No active strategy to cross with → fall back to mutation.
                candidate = _mutate_strategy(best, i)
                op_name = f"mutate_op{i % 4}_fallback"
        else:
            candidate = _mutate_strategy(best, i)
            op_name = f"mutate_op{i % 4}"
        screen = world_model_prescreen(candidate)
        history.append({"iter": i, "op": op_name,
                        "score": screen["score"],
                        "prob_profit": screen["prob_profit"]})
        if screen["score"] > best_screen["score"]:
            best, best_screen = candidate, screen
        if best_screen["promising"]:
            break
    # Instrumentation for feature_health.
    try:
        import time as _t
        import redis_client as _rc
        r = _rc.get()
        crossover_count = sum(1 for h in history if h["op"].startswith("crossover_"))
        if crossover_count > 0:
            r.incr("research:crossover_count", crossover_count)
            r.set("research:crossover_last_ts", str(int(_t.time())))
    except Exception:
        pass
    return best, best_screen, history


def evolve_strategy_pool(max_children: int = 2,
                         min_parent_trades: int = 0) -> list[dict]:
    """Pool-level genetic evolution — the generational driver of the gene pool.

    cont. 66 — Closes the lineage gap found post-cont-61: research-engine
    crossover only stamps ``parent_strategy_id`` on the rare
    marginal→crossover-won→promising path (`celery_app.run_strategy_research`),
    and the 27 seeded archetypes were never themselves evolved — so every row
    stayed ``generation=0 / parent_strategy_id=NULL`` and no GA lineage chain
    ever formed.

    This function evolves PROVEN (or, on a fresh pool, any) ACTIVE strategies
    into experimental children that carry real lineage:

      1. Pick base parents from the active pool, ordered by win_rate (proven
         performers first), EXCLUDING any base that already has a pending
         experimental child — that single guard bounds pool growth and forces
         the GA to rotate across the pool / wait for trials to resolve before
         re-evolving a lineage.
      2. Cross each base with the top bandit-ranked active strategy
         (`_crossover_with_top_active`), then apply one mutation operator.
      3. `world_model_prescreen` the child; keep promising / marginal-improved
         variants (and, when the world model has no data yet, allow as a
         bootstrap so generation-1 lineage can begin forming).
      4. Return child-candidate dicts carrying ``parent_strategy_id`` (the BASE
         strategy's id) and ``generation = base.generation + 1`` for the caller
         (`celery_app.evolve_strategy_pool_task`) to persist via
         `create_experimental`.

    Returns a list of child-candidate dicts (empty when nothing qualifies — the
    caller emits a silent-rejection counter per [[feedback_silent_rejection]]).
    """
    children: list[dict] = []
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                # Only evolve a base that has no pending experimental child:
                # this bounds the experimental pool and makes the GA rotate
                # across parents / wait for trials before re-evolving.
                cur.execute(
                    "SELECT s.id, s.name, s.generation, s.win_rate, s.trade_count, "
                    "       s.dca_rules, s.trailing_sl_params, "
                    "       s.position_sizing_rules, s.entry_overrides "
                    "FROM strategies s "
                    "WHERE s.status = 'active' "
                    "  AND NOT EXISTS ("
                    "        SELECT 1 FROM strategies c "
                    "        WHERE c.parent_strategy_id = s.id "
                    "          AND c.status = 'experimental') "
                    "ORDER BY s.win_rate DESC NULLS LAST, "
                    "         s.trade_count DESC NULLS LAST"
                )
                rows = cur.fetchall()
    except Exception as exc:
        log.warning("pool_evolution_db_failed", error=str(exc)[:160])
        return []

    if not rows:
        return []

    # Prefer proven parents; bootstrap from the whole eligible pool when the
    # gene pool is freshly seeded and has no trade history yet.
    proven = [r for r in rows
              if (r[4] or 0) >= min_parent_trades and r[3] is not None]
    pool = proven if proven else rows
    bases = pool[:max_children]

    for idx, row in enumerate(bases):
        (sid, name, generation, _win_rate, _trade_count,
         dca, sl, sizing, entry) = row
        base = {
            "id": str(sid),
            "name": name,
            "generation": int(generation or 0),
            "dca_rules": dca,
            "trailing_sl_params": sl,
            "position_sizing_rules": sizing,
            "entry_overrides": entry,
            "entry_conditions": [],
        }
        crossed = _crossover_with_top_active(base)
        if crossed is None:
            continue
        # Don't self-cross: when the only/top active partner IS the base, the
        # blend is a no-op clone — skip rather than emit a duplicate lineage.
        if crossed.get("_crossover_parent") == name:
            continue
        candidate = _mutate_strategy(crossed, idx + 1)
        screen = world_model_prescreen(candidate)
        if screen.get("skipped"):
            promising = True  # bootstrap: no world-model data to gate on yet
        else:
            promising = bool(screen.get("promising") or screen.get("marginal"))
        if not promising:
            continue
        candidate["parent_strategy_id"] = str(sid)
        candidate["generation"] = int(generation or 0) + 1
        candidate["_evolution_parent_name"] = name
        candidate["_evolution_partner_name"] = crossed.get("_crossover_parent")
        candidate["_prescreen_score"] = screen.get("score")
        candidate["_prescreen_bootstrap"] = bool(screen.get("skipped"))
        children.append(candidate)
    return children


def log_research_note(hypothesis: str, metrics: dict, decision: str, reason: str) -> None:
    """AA-09: Write structured research note to experiments table.
    Uses 'F36' as experiment_type so metacog can map to the F36 governance flag."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO experiments (experiment_type, hypothesis, metrics, decision, decision_reason)
                VALUES (%s, %s, %s, %s, %s)
            """, ("F36", hypothesis, json.dumps(metrics), decision, reason))


def log_experiment(experiment_type: str, outcome: str, metrics: dict | None = None,
                   notes: str | None = None, trade_count: int | None = None) -> None:
    """Generic experiments table writer for self-improvement mechanisms.

    Used by OPRO, Direction Model retrain, etc. so metacog.evaluate_self_improvement_mechanisms
    can compute success rate per mechanism from a uniform schema.

    Args:
      experiment_type: feature_id of the mechanism (e.g. 'F39A' for OPRO, 'F13' for
                       Direction Model, 'F36' for Strategy Research). Must match
                       feature_governance/bootstrap.py registry so escalations work.
      outcome:         'positive' | 'negative' | 'neutral' — what metacog evaluates against.
      metrics:         JSON-serializable dict of supporting numbers.
      notes:           Optional human-readable note.
      trade_count:     Optional snapshot of paper_closed count when this fired.
    """
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO experiments
                      (experiment_type, outcome, metrics, notes, trade_count_at_run)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    experiment_type, outcome,
                    json.dumps(metrics or {}),
                    notes, trade_count,
                ))
    except Exception as exc:
        log.warning("log_experiment_failed",
                    experiment_type=experiment_type, error=str(exc)[:200])


_VALID_REGIMES = {"bull", "bear", "turbulent"}


def _bump_validator_metric(name: str, passed: bool) -> None:
    """Track F36 LLM compliance per validator: cont. 17 closes the cont. 15
    honesty note #3. Each validator increments either the pass or null
    counter and stamps a last_ts so feature_health can show how often the
    LLM produces typed output that survives validation."""
    try:
        import time as _t
        import redis_client as _rc
        r = _rc.get()
        suffix = "pass" if passed else "null"
        r.incr(f"f36_validators:{name}_{suffix}_count")
        r.set(f"f36_validators:{name}_last_ts", str(int(_t.time())))
    except Exception:
        pass


def validate_dca_thresholds(raw) -> dict | None:
    """Sanity-check F36 LLM-emitted dca_thresholds. Pre-cont-17 this was
    unvalidated (celery_app passed parsed.get('dca_thresholds', {}) straight
    through). Cont. 17 closes the gap with the same shape as the other
    validators so LLM-compliance metrics cover all 4 typed configs."""
    if not isinstance(raw, dict):
        _bump_validator_metric("dca_thresholds", False)
        return None
    out: dict = {}

    r1 = raw.get("round_1_pct")
    if isinstance(r1, (int, float)) and not isinstance(r1, bool):
        # round_1_pct should be NEGATIVE (e.g. -20 means -20% drawdown trigger).
        # Floor tightened cont. 33 from -0.5 to -8.0 after 113 DCA-1-only trades
        # finished 0-for-113 (-$1902 total). Tighter than -8% never gives
        # avg_entry a chance to shift enough for the recovery mechanism to work.
        out["round_1_pct"] = max(-50.0, min(-8.0, float(r1)))

    r2 = raw.get("round_2_pct")
    if isinstance(r2, (int, float)) and not isinstance(r2, bool):
        # Round 2 must be DEEPER than round 1 (the explicit check below also
        # enforces this), so its floor must be tighter than round_1's floor.
        out["round_2_pct"] = max(-80.0, min(-12.0, float(r2)))

    # Both must be set AND round_2 must be deeper than round_1 (else the
    # second round fires before the first — meaningless).
    if "round_1_pct" not in out or "round_2_pct" not in out:
        _bump_validator_metric("dca_thresholds", False)
        return None
    if out["round_2_pct"] >= out["round_1_pct"]:
        _bump_validator_metric("dca_thresholds", False)
        return None

    _bump_validator_metric("dca_thresholds", True)
    return out


def _early_null(metric_name: str) -> None:
    """Bump null counter for an early-return path. Pre-cont-17 the validators
    had asymmetric metric coverage: dict-shaped invalid input bumped null,
    but non-dict input (e.g. string, None) returned early without bumping.
    This wrapper closes that gap so the pass/null ratio is honest."""
    _bump_validator_metric(metric_name, False)


def validate_entry_overrides(raw) -> dict | None:
    """Sanity-check and clip LLM-emitted entry_overrides for the strategies
    table. Returns a dict containing only the recognised keys whose values
    pass type + bounds checks, or None when nothing valid remains.

    Used by `celery_app.run_strategy_research` to gate F36 LLM output before
    persisting it into strategies.entry_overrides — without this, a hallucinated
    `min_signal_strength: 999` or `regime_whitelist: "BTC moonshot"` would
    propagate into the F8 router and break signal acceptance.
    """
    if not isinstance(raw, dict):
        _early_null("entry_overrides")
        return None
    out: dict = {}

    mss = raw.get("min_signal_strength")
    if isinstance(mss, (int, float)) and not isinstance(mss, bool):
        # cont. 62: clamp to [15.0, 40.0] mirroring the GA cap at line 851 of
        # signals/engine.py:accept_or_reject. A hallucinated value of 80+ here
        # silently kills every signal because per-strategy overrides bypass
        # the GA clamp. The 2026-05-29 `losses_may_result_7252` strategy was
        # generated with min_signal_strength=80 and blocked all trades for
        # ~10 hours until detected.
        out["min_signal_strength"] = max(15.0, min(40.0, float(mss)))

    tcap = raw.get("turbulence_cap")
    if isinstance(tcap, (int, float)) and not isinstance(tcap, bool):
        out["turbulence_cap"] = max(0.0, min(10.0, float(tcap)))

    wl = raw.get("regime_whitelist")
    if isinstance(wl, list):
        cleaned = [str(x).lower() for x in wl
                   if isinstance(x, str) and str(x).lower() in _VALID_REGIMES]
        if cleaned:
            # Preserve order, dedupe.
            seen, ordered = set(), []
            for r in cleaned:
                if r not in seen:
                    seen.add(r)
                    ordered.append(r)
            out["regime_whitelist"] = ordered

    result = out or None
    _bump_validator_metric("entry_overrides", result is not None)
    return result


def validate_sl_params(raw) -> dict | None:
    """Sanity-check + clip LLM-emitted trailing_sl_params for the strategies
    table. Bounds match the consumer clipping in risk/manager.py:
      initial_atr_mult   in [0.5, 10.0]
      initial_min_pct    in [0.005, 0.20]
      trailing_dist_pct  in [0.005, 0.10]

    Used by `celery_app.run_strategy_research` to gate F36 LLM output before
    persisting. Mirrors `validate_entry_overrides` from cont. 11. Returns None
    when nothing valid survives."""
    if not isinstance(raw, dict):
        _early_null("sl_params")
        return None
    out: dict = {}

    am = raw.get("initial_atr_mult")
    if isinstance(am, (int, float)) and not isinstance(am, bool):
        out["initial_atr_mult"] = max(0.5, min(10.0, float(am)))

    mp = raw.get("initial_min_pct")
    if isinstance(mp, (int, float)) and not isinstance(mp, bool):
        out["initial_min_pct"] = max(0.005, min(0.20, float(mp)))

    td = raw.get("trailing_dist_pct")
    if isinstance(td, (int, float)) and not isinstance(td, bool):
        out["trailing_dist_pct"] = max(0.005, min(0.10, float(td)))

    result = out or None
    _bump_validator_metric("sl_params", result is not None)
    return result


def validate_capital_rules(raw) -> dict | None:
    """Sanity-check + clip LLM-emitted position_sizing_rules. Bounds match the
    consumer clipping in signals/engine.py: capital_pct_mult in [0.25, 2.0].
    Returns None when nothing valid survives."""
    if not isinstance(raw, dict):
        _early_null("capital_rules")
        return None
    out: dict = {}

    cm = raw.get("capital_pct_mult")
    if isinstance(cm, (int, float)) and not isinstance(cm, bool):
        out["capital_pct_mult"] = max(0.25, min(2.0, float(cm)))

    result = out or None
    _bump_validator_metric("capital_rules", result is not None)
    return result


def queue_for_paper_trial(strategy: dict) -> None:
    """AA-08: Submit strategy to paper trial queue."""
    from strategy.lifecycle import create_experimental
    sid = create_experimental(strategy)
    log.info("strategy_queued_for_trial", id=sid)


async def generate_hypothesis_via_airllm(trade_summary: str) -> str:
    """AA-02: Llama 3.1 70B background task — generate strategy hypothesis."""
    from celery_app import research_strategy
    result = research_strategy.delay(
        f"Analyse these trade outcomes and generate a new trading hypothesis: {trade_summary}. "
        "Output as JSON: {hypothesis: str, entry_conditions: list, exit_conditions: list}"
    )
    return result.id   # return Celery task ID; result retrieved asynchronously
