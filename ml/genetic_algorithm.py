"""L-10: DEAP Genetic Algorithm — strategy parameter evolution.

Phase 1 (50 trades): basic GA with tournament selection.
Phase 3 (300 trades): full evolution with Pareto front (Sharpe + max-drawdown).

Triggered by Celery beat (every 6h) once paper_closed >= 50 and F25 is active.
Best params written to Redis key `ga:best_params` and consumed by
signals.engine.accept_or_reject and risk.manager.
"""
import json
import random
import structlog
import redis_client

log = structlog.get_logger()

_GA_PARAMS_KEY = "ga:best_params"
_GA_HISTORY_KEY = "ga:history"

# Parameter bounds — the chromosome. Keep small so GA converges with realistic
# trade counts. Each tuple = (min, max).
PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "min_signal_strength": (15.0, 60.0),         # stage 2+ accept threshold
    "turbulence_cap": (1.5, 5.0),                # reject when turbulence > X
    "dca_round1_drop_pct": (1.5, 5.0),           # drop% to trigger DCA round 1
    "dca_round2_drop_pct": (4.0, 10.0),          # drop% to trigger DCA round 2
    "trailing_sl_distance_pct": (0.5, 3.0),      # trailing SL distance
    "kelly_fraction": (0.10, 0.50),              # fraction of Kelly to apply
}


def run_ga(
    fitness_fn,
    population_size: int = 30,
    generations: int = 15,
    paper_closed_count: int = 0,
    param_bounds: dict | None = None,
) -> dict:
    """Evolve strategy parameters using DEAP.

    Below 50 trades → return defaults. 50-299 → tournament selection.
    300+ → NSGA-II Pareto front for Sharpe + drawdown trade-off.
    """
    bounds = param_bounds or PARAM_BOUNDS
    param_names = list(bounds.keys())

    if paper_closed_count < 50:
        return {k: (lo + hi) / 2 for k, (lo, hi) in bounds.items()}

    try:
        from deap import base, creator, tools, algorithms

        use_pareto = paper_closed_count >= 300

        # NSGA-II needs FitnessMulti (Sharpe ↑, drawdown ↓); tournament uses single Fitness
        if use_pareto:
            if hasattr(creator, "FitnessSingle"):
                del creator.FitnessSingle
            if not hasattr(creator, "FitnessMulti"):
                creator.create("FitnessMulti", base.Fitness, weights=(1.0, -1.0))
            _fitness_cls = creator.FitnessMulti
        else:
            if hasattr(creator, "FitnessMulti"):
                del creator.FitnessMulti
            if not hasattr(creator, "FitnessSingle"):
                creator.create("FitnessSingle", base.Fitness, weights=(1.0,))
            _fitness_cls = creator.FitnessSingle

        # Re-create Individual each call because fitness class may change between Pareto/single
        if hasattr(creator, "Individual"):
            del creator.Individual
        creator.create("Individual", list, fitness=_fitness_cls)

        toolbox = base.Toolbox()

        def make_individual():
            return creator.Individual([
                random.uniform(bounds[k][0], bounds[k][1]) for k in param_names
            ])

        toolbox.register("individual", make_individual)
        toolbox.register("population", tools.initRepeat, list, toolbox.individual)

        def evaluate(ind):
            params = dict(zip(param_names, ind))
            r = fitness_fn(params)
            if use_pareto:
                # Expect (sharpe, drawdown) tuple
                if isinstance(r, tuple) and len(r) == 2:
                    return r
                return (float(r), 0.0)
            else:
                if isinstance(r, tuple):
                    return (r[0],)
                return (float(r),)

        toolbox.register("evaluate", evaluate)
        toolbox.register("mate", tools.cxBlend, alpha=0.5)
        toolbox.register("mutate", tools.mutGaussian, mu=0, sigma=0.1, indpb=0.2)
        toolbox.register(
            "select",
            tools.selNSGA2 if use_pareto else tools.selTournament,
            **({} if use_pareto else {"tournsize": 3}),
        )

        pop = toolbox.population(n=population_size)
        algorithms.eaSimple(
            pop, toolbox,
            cxpb=0.5, mutpb=0.2, ngen=generations, verbose=False,
        )

        # Best individual: single-fitness highest Sharpe (or for Pareto, the elbow point —
        # max Sharpe among the front).
        if use_pareto:
            front = tools.sortNondominated(pop, k=len(pop), first_front_only=True)[0]
            best = max(front, key=lambda i: i.fitness.values[0])
        else:
            best = tools.selBest(pop, k=1)[0]

        # Clamp to bounds (mutation can drift outside)
        best_params = {}
        for k, v in zip(param_names, best):
            lo, hi = bounds[k]
            best_params[k] = round(float(max(lo, min(hi, v))), 4)

        # Persist best params + fitness for transparency
        try:
            r = redis_client.get()
            r.set(_GA_PARAMS_KEY, json.dumps(best_params))
            entry = {
                "params": best_params,
                "fitness": list(best.fitness.values),
                "pareto": use_pareto,
                "generations": generations,
                "population": population_size,
                "paper_closed": paper_closed_count,
            }
            r.lpush(_GA_HISTORY_KEY, json.dumps(entry))
            r.ltrim(_GA_HISTORY_KEY, 0, 49)  # keep last 50 evolution runs
        except Exception as exc:
            log.warning("ga_persist_failed", error=str(exc))

        log.info("ga_evolved", params=best_params, fitness=list(best.fitness.values),
                 pareto=use_pareto)
        return best_params

    except Exception as exc:
        log.error("ga_failed", error=str(exc))
        return {k: (lo + hi) / 2 for k, (lo, hi) in bounds.items()}


def _default_fitness_from_db() -> "callable":
    """Build a fitness callable that scores params via simulated re-evaluation
    on the last N closed trades. Uses pnl-impact heuristics — not a full
    walk-forward backtest, but enough signal for GA to favour configs that
    avoid the worst recent failures.
    """
    # signal_strength lives on the signals table; trades has no such column.
    # Join via signals.trade_id → trades.id to get strength alongside outcome.
    from db import db_conn
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.signal_strength, t.market_regime, t.net_pnl_usdt, t.capital_usdt
                FROM trades t
                JOIN signals s ON s.trade_id = t.id
                WHERE t.status='closed' AND t.is_paper=true
                  AND t.net_pnl_usdt IS NOT NULL
                  AND s.signal_strength IS NOT NULL
                ORDER BY t.exit_time DESC
                LIMIT 200
            """)
            rows = cur.fetchall()

    def fitness(params: dict) -> tuple[float, float]:
        if not rows:
            return (0.0, 0.0)
        kept_pnls = []
        for strength, _regime, pnl, cap in rows:
            s = float(strength or 0)
            if s < params.get("min_signal_strength", 30):
                continue  # would have rejected
            c = float(cap or 1)
            # Apply kelly_fraction as a position-size proxy
            scale = params.get("kelly_fraction", 0.25) * 4  # neutral around 1.0
            kept_pnls.append(float(pnl) * scale / max(c, 1e-6))
        if len(kept_pnls) < 5:
            return (-1.0, 1.0)  # too aggressive a filter → penalize
        import statistics
        mean_r = statistics.fmean(kept_pnls)
        std_r = statistics.pstdev(kept_pnls) or 1e-6
        sharpe = mean_r / std_r * (252 ** 0.5)  # annualized proxy
        # Drawdown proxy: max consecutive loss streak relative to mean
        running = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in kept_pnls:
            running += r
            peak = max(peak, running)
            max_dd = max(max_dd, peak - running)
        return (sharpe, max_dd)

    return fitness


def evolve_and_publish() -> dict:
    """Celery-callable entry point: read paper_closed_count, run GA, publish results."""
    try:
        from feature_governance.registry import is_active
        if not is_active("F25"):
            return {"status": "f25_inactive"}
    except Exception:
        pass

    try:
        from memory.query import get_paper_closed_count
        paper_closed = get_paper_closed_count()
    except Exception:
        paper_closed = 0

    if paper_closed < 50:
        return {"status": "not_active", "have": paper_closed, "need": 50}

    fitness = _default_fitness_from_db()
    best = run_ga(fitness, paper_closed_count=paper_closed)
    return {"status": "ok", "params": best, "paper_closed": paper_closed}


def get_active_params() -> dict:
    """Read latest evolved params from Redis; returns midpoints if none yet."""
    try:
        raw = redis_client.get().get(_GA_PARAMS_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {k: (lo + hi) / 2 for k, (lo, hi) in PARAM_BOUNDS.items()}
