"""L-07: Mutual Information — feature relevance rankings and optimal forecast horizons."""
import json
import structlog
import redis_client

log = structlog.get_logger()
_rankings_cache: dict = {}


def compute_mutual_info(features: list[float], returns: list[float], lags: int = 100) -> list[float]:
    """I(X;Y) via sklearn for lags k=1..lags."""
    try:
        from sklearn.feature_selection import mutual_info_regression
        import numpy as np
        X = np.array(features).reshape(-1, 1)
        scores = []
        for lag in range(1, min(lags + 1, len(returns))):
            y = np.array(returns[lag:])
            x = X[:len(y)]
            if len(x) > 10:
                mi = mutual_info_regression(x, y, random_state=42)[0]
                scores.append(float(mi))
        return scores
    except Exception as exc:
        log.error("mutual_info_failed", error=str(exc))
        return []


def get_feature_relevance_rankings() -> dict:
    r = redis_client.get()
    cached = r.get("analytics:feature_relevance")
    return json.loads(cached) if cached else _rankings_cache


def get_optimal_forecast_horizons() -> list[int]:
    rankings = get_feature_relevance_rankings()
    return rankings.get("best_horizons", [1, 4, 24])


def update_rankings(feature_name: str, mi_scores: list[float]) -> None:
    _rankings_cache[feature_name] = mi_scores
    best_lag = int(mi_scores.index(max(mi_scores)) + 1) if mi_scores else 1
    r = redis_client.get()
    existing = json.loads(r.get("analytics:feature_relevance") or "{}")
    existing[feature_name] = {"scores": mi_scores, "best_lag": best_lag}
    r.set("analytics:feature_relevance", json.dumps(existing), ex=86400)
