"""L-08: Transfer Entropy — non-parametric lead-lag relationships across pairs."""
import json
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()


def _transfer_entropy(x: list[float], y: list[float], lag: int = 1) -> float:
    """Non-parametric TE(X→Y) via histogram estimator."""
    try:
        import numpy as np
        n = min(len(x), len(y)) - lag
        if n < 20:
            return 0.0

        y_next = np.array(y[lag:n + lag])
        y_cur = np.array(y[:n])
        x_cur = np.array(x[:n])

        def _mi(a, b):
            h_ab, _, _ = np.histogram2d(a, b, bins=10)
            p_ab = h_ab / h_ab.sum()
            p_a = p_ab.sum(axis=1, keepdims=True)
            p_b = p_ab.sum(axis=0, keepdims=True)
            mask = p_ab > 0
            return float(np.sum(p_ab[mask] * np.log(p_ab[mask] / (p_a * p_b)[mask])))

        return max(0.0, _mi(np.column_stack([y_next, y_cur]).T[0],
                            np.column_stack([y_cur, x_cur]).T[0]) -
                        _mi(y_next, y_cur))
    except Exception:
        return 0.0


def get_lead_lag_matrix() -> dict:
    """Compute TE across all active pairs; return lead-lag matrix."""
    r = redis_client.get()
    cached = r.get("analytics:lead_lag_matrix")
    if cached:
        return json.loads(cached)

    active_pairs = list(r.smembers(redis_keys.ACTIVE_PAIRS))[:10]
    if len(active_pairs) < 2:
        return {}

    price_series = {}
    for pair in active_pairs:
        candles_raw = r.lrange(
            redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1h"), 0, 99
        )
        if candles_raw:
            price_series[pair] = [float(json.loads(c)["c"]) for c in reversed(candles_raw)]

    matrix = {}
    pairs = list(price_series.keys())
    for i, p1 in enumerate(pairs):
        for j, p2 in enumerate(pairs):
            if i != j and len(price_series.get(p1, [])) > 20 and len(price_series.get(p2, [])) > 20:
                te = _transfer_entropy(price_series[p1], price_series[p2])
                matrix[f"{p1}->{p2}"] = round(te, 6)

    result = {"matrix": matrix, "top_leaders": sorted(matrix, key=matrix.get, reverse=True)[:3]}
    r.set("analytics:lead_lag_matrix", json.dumps(result), ex=3600)
    return result
