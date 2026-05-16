"""L-10: DEAP Genetic Algorithm — strategy parameter evolution."""
import json
import random
import structlog

log = structlog.get_logger()


def run_ga(
    param_bounds: dict,
    fitness_fn,
    population_size: int = 50,
    generations: int = 20,
    paper_closed_count: int = 0,
) -> dict:
    """
    Evolve strategy parameters using DEAP.
    - Basic GA activates at 50 trades
    - Full evolution (Pareto front for Sharpe + drawdown) at 300 trades
    """
    try:
        import numpy as np
        from deap import base, creator, tools, algorithms

        if not hasattr(creator, "FitnessMulti"):
            creator.create("FitnessMulti", base.Fitness, weights=(1.0, -1.0))
        if not hasattr(creator, "Individual"):
            creator.create("Individual", list, fitness=creator.FitnessMulti)

        toolbox = base.Toolbox()
        param_names = list(param_bounds.keys())

        def make_individual():
            return creator.Individual([
                random.uniform(param_bounds[k][0], param_bounds[k][1])
                for k in param_names
            ])

        toolbox.register("individual", make_individual)
        toolbox.register("population", tools.initRepeat, list, toolbox.individual)

        def evaluate(ind):
            params = dict(zip(param_names, ind))
            return fitness_fn(params)

        toolbox.register("evaluate", evaluate)
        toolbox.register("mate", tools.cxBlend, alpha=0.5)
        toolbox.register("mutate", tools.mutGaussian, mu=0, sigma=0.1, indpb=0.2)

        use_pareto = paper_closed_count >= 300
        toolbox.register("select",
            tools.selNSGA2 if use_pareto else tools.selTournament,
            **({} if use_pareto else {"tournsize": 3}))

        pop = toolbox.population(n=population_size)
        pop, logbook = algorithms.eaSimple(
            pop, toolbox,
            cxpb=0.5, mutpb=0.2, ngen=generations,
            verbose=False,
        )

        best = tools.selBest(pop, k=1)[0]
        return dict(zip(param_names, best))

    except Exception as exc:
        log.error("ga_failed", error=str(exc))
        return {}
