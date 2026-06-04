"""L-08: Transfer Entropy — non-parametric lead-lag relationships across pairs.

cont. 61 audit fix (2026-05-29): the original implementation always returned
0.0 due to a histogram-MI composition bug (took only the first row of a
column-stack, collapsing the joint distribution to a marginal). This rewrite
follows Schreiber (2000) `TE(X→Y) = H(Y_{t+1}|Y_t) - H(Y_{t+1}|Y_t, X_t)`
using histogram-based entropy estimators on returns rather than raw prices.
"""
import json
import math
import structlog
import redis_client
import redis_keys

log = structlog.get_logger()


def _to_returns(prices: list[float]) -> list[float]:
    """Convert price series to log returns."""
    out: list[float] = []
    for i in range(1, len(prices)):
        if prices[i - 1] > 0 and prices[i] > 0:
            out.append(math.log(prices[i] / prices[i - 1]))
    return out


def _discretize(values: list[float], n_bins: int = 5) -> list[int]:
    """Map values to integer bin indices in [0, n_bins). Uses quantile binning
    so each bin has approximately equal sample count regardless of distribution
    shape — critical for non-Gaussian return series.
    """
    if not values:
        return []
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    # quantile cut points
    cuts = [sorted_vals[int(q * n)] for q in (i / n_bins for i in range(1, n_bins))]
    out: list[int] = []
    for v in values:
        idx = 0
        for c in cuts:
            if v > c:
                idx += 1
            else:
                break
        out.append(idx)
    return out


def _shannon_entropy(prob_dict: dict) -> float:
    """H(X) = -Σ p(x) log p(x). Inputs are unnormalized counts in a dict."""
    total = sum(prob_dict.values())
    if total <= 0:
        return 0.0
    h = 0.0
    for c in prob_dict.values():
        if c <= 0:
            continue
        p = c / total
        h -= p * math.log(p)
    return h


def _conditional_entropy(joint_counts: dict, conditioning_marginal: dict) -> float:
    """H(Y|cond) = H(cond, Y) - H(cond) using count dicts."""
    return _shannon_entropy(joint_counts) - _shannon_entropy(conditioning_marginal)


def _transfer_entropy(x: list[float], y: list[float],
                      lag: int = 1, n_bins: int = 5) -> float:
    """Schreiber (2000) TE(X→Y) on return series.

    TE(X→Y) = H(Y_{t+1} | Y_t) - H(Y_{t+1} | Y_t, X_t)

    Higher TE = X carries more predictive information about Y's next move
    than Y's own past does. Returns nats. Caller must compare to
    H(Y_{t+1}) baseline for normalization.
    """
    x_r = _to_returns(x)
    y_r = _to_returns(y)
    n = min(len(x_r), len(y_r)) - lag
    if n < 30:
        return 0.0

    # Discretize returns to integer bins
    x_d = _discretize(x_r[:n], n_bins)
    y_d = _discretize(y_r[:n], n_bins)
    # Y at t+lag
    y_next_d = _discretize(y_r[lag : n + lag], n_bins)

    if len(x_d) != n or len(y_d) != n or len(y_next_d) != n:
        return 0.0

    # Joint count dicts. Keys = tuples.
    p_yt_ynext: dict = {}     # (y_t, y_{t+1})           — H(Y_t, Y_{t+1})
    p_yt: dict = {}            # (y_t,)                   — H(Y_t)
    p_yt_xt_ynext: dict = {}   # (y_t, x_t, y_{t+1})      — H(Y_t, X_t, Y_{t+1})
    p_yt_xt: dict = {}         # (y_t, x_t)               — H(Y_t, X_t)

    for i in range(n):
        yt = y_d[i]
        xt = x_d[i]
        yn = y_next_d[i]
        p_yt[(yt,)] = p_yt.get((yt,), 0) + 1
        p_yt_ynext[(yt, yn)] = p_yt_ynext.get((yt, yn), 0) + 1
        p_yt_xt[(yt, xt)] = p_yt_xt.get((yt, xt), 0) + 1
        p_yt_xt_ynext[(yt, xt, yn)] = p_yt_xt_ynext.get((yt, xt, yn), 0) + 1

    # H(Y_{t+1} | Y_t) = H(Y_t, Y_{t+1}) - H(Y_t)
    h_ynext_given_yt = _shannon_entropy(p_yt_ynext) - _shannon_entropy(p_yt)
    # H(Y_{t+1} | Y_t, X_t) = H(Y_t, X_t, Y_{t+1}) - H(Y_t, X_t)
    h_ynext_given_yt_xt = _shannon_entropy(p_yt_xt_ynext) - _shannon_entropy(p_yt_xt)

    te = h_ynext_given_yt - h_ynext_given_yt_xt
    return max(0.0, te)


def get_lead_lag_matrix() -> dict:
    """Compute TE across all active pairs; return lead-lag matrix + top leaders.

    Reads 100 1h candles per pair from Redis. TE > 0.01 nats indicates a
    statistically meaningful lead-lag relationship at 1-bar lag. Top leaders
    are sorted by total TE-out (sum over all destinations) which proxies
    "this pair drives the rest".

    Cached in Redis for 1h.
    """
    r = redis_client.get()
    cached = r.get("analytics:lead_lag_matrix")
    if cached:
        try:
            return json.loads(cached)
        except (TypeError, ValueError):
            pass

    active_pairs = list(r.smembers(redis_keys.ACTIVE_PAIRS))[:12]
    if len(active_pairs) < 2:
        return {}

    price_series: dict = {}
    for pair in active_pairs:
        key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1h")
        candles_raw = r.lrange(key, 0, 99)
        if not candles_raw:
            continue
        try:
            closes = [float(json.loads(c)["c"]) for c in reversed(candles_raw)]
            if len(closes) >= 30:
                price_series[pair] = closes
        except (json.JSONDecodeError, KeyError, ValueError):
            continue

    matrix: dict = {}
    pairs = list(price_series.keys())
    leader_out: dict = {p: 0.0 for p in pairs}
    for p1 in pairs:
        for p2 in pairs:
            if p1 == p2:
                continue
            try:
                te = _transfer_entropy(price_series[p1], price_series[p2])
                matrix[f"{p1}->{p2}"] = round(te, 6)
                leader_out[p1] += te
            except Exception as exc:
                log.debug("te_pair_failed", pair_from=p1, pair_to=p2,
                          error=str(exc)[:120])

    top_leaders = sorted(leader_out.items(), key=lambda kv: kv[1], reverse=True)[:5]
    result = {
        "matrix": matrix,
        "top_leaders": [p for p, _ in top_leaders],
        "leader_te_total": {p: round(v, 6) for p, v in leader_out.items()},
    }
    try:
        r.set("analytics:lead_lag_matrix", json.dumps(result), ex=3600)
    except Exception as exc:
        log.warning("te_cache_failed", error=str(exc)[:120])
    return result
