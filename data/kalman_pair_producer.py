"""Kalman-filtered pair residual producer (cont. 61 — F25 GA seed gene pool).

Maintains a 2-state Kalman filter over `log(price_y) = β·log(price_x) + intercept`
for a configured basket of correlated perp pairs. Emits the residual z-score per
60s, which the `kalman_pair_residual_revert` seed strategy consumes.

Default basket: BTC-ETH (the canonical crypto cointegration pair). Extends to
any pair the user adds to `kalman:pair_baskets` Redis set as
`"<y_pair>|<x_pair>"`, e.g. `"ETHUSDT|BTCUSDT"`.

Writes per basket key `<y_pair>|<x_pair>`:
  `kalman:{key}:residual`          — latest residual (float)
  `kalman:{key}:residual_z`        — z-score over rolling 200 residuals (float)
  `kalman:{key}:residual_history`  — Redis list, last 200 residuals (newest first)
  `kalman:{key}:beta`              — current Kalman β estimate
  `kalman:{key}:intercept`         — current Kalman intercept estimate

Schedule: every 60s via Celery beat.

Honest scope (Rule 4): the filter persists state across invocations via Redis
(beta/intercept), so a cold restart re-converges in ~10-20 iterations. There
is no formal cointegration test (ADF/Johansen) — the strategy assumes the
user-configured baskets are cointegrated. Add an ADF gate later if needed.
"""
from __future__ import annotations

import json
import math
import statistics
import structlog

import redis_client
import redis_keys

log = structlog.get_logger()

_RESIDUAL_WINDOW = 200          # z-score lookback
_LATEST_CLOSE_LOOKBACK = 5      # bars to average for "current" price (noise smoothing)
_KALMAN_Q = 1e-5                # process noise (β / intercept drift per step)
_KALMAN_R = 1e-3                # measurement noise (log-price observation)
_DEFAULT_BASKETS = ["ETHUSDT|BTCUSDT"]   # y|x — log(ETH) ~ β·log(BTC) + intercept


def _latest_close(pair: str) -> float | None:
    r = redis_client.get()
    key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1m")
    raw = r.lrange(key, 0, _LATEST_CLOSE_LOOKBACK)
    closes = []
    for c in raw:
        try:
            closes.append(float(json.loads(c).get("c") or 0))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    closes = [c for c in closes if c > 0]
    if not closes:
        return None
    return sum(closes) / len(closes)


def _load_state(key: str) -> dict:
    """Pull persisted Kalman state from Redis. Returns defaults if cold-start."""
    r = redis_client.get()
    raw = r.get(f"kalman:{key}:state")
    if raw is None:
        # Cold-start priors. Beta=1 + intercept=0 means we initially expect
        # log(y) ≈ log(x); the filter will move quickly to truth once it sees data.
        return {"beta": 1.0, "intercept": 0.0,
                "p_beta": 1.0, "p_intercept": 1.0, "p_cross": 0.0}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"beta": 1.0, "intercept": 0.0,
                "p_beta": 1.0, "p_intercept": 1.0, "p_cross": 0.0}


def _save_state(key: str, state: dict) -> None:
    r = redis_client.get()
    try:
        r.setex(f"kalman:{key}:state", 86400, json.dumps(state))
    except Exception as exc:
        log.warning("kalman_save_state_failed", key=key, error=str(exc)[:120])


def _kalman_update(state: dict, log_y: float, log_x: float) -> tuple[dict, float]:
    """One step of a 2-state Kalman filter over [β, intercept]."""
    beta, intercept = state["beta"], state["intercept"]
    p_b, p_i, p_c = state["p_beta"], state["p_intercept"], state["p_cross"]

    # Predict — random walk dynamics, identity transition.
    p_b += _KALMAN_Q
    p_i += _KALMAN_Q

    # Observation: log_y = beta·log_x + intercept + noise
    # H = [log_x, 1]
    H_b, H_i = log_x, 1.0
    y_pred = beta * H_b + intercept * H_i
    residual = log_y - y_pred

    # Innovation covariance: S = H · P · H^T + R
    S = H_b * H_b * p_b + 2.0 * H_b * H_i * p_c + H_i * H_i * p_i + _KALMAN_R
    if S <= 0:
        # Numerical safety — fall back to no-update step.
        return state, residual

    # Kalman gain: K = P · H^T / S
    K_b = (p_b * H_b + p_c * H_i) / S
    K_i = (p_c * H_b + p_i * H_i) / S

    # Posterior mean + covariance
    beta_new = beta + K_b * residual
    int_new = intercept + K_i * residual
    p_b_new = p_b - K_b * (H_b * p_b + H_i * p_c)
    p_i_new = p_i - K_i * (H_b * p_c + H_i * p_i)
    p_c_new = p_c - K_b * (H_b * p_c + H_i * p_i)

    return ({
        "beta": float(beta_new),
        "intercept": float(int_new),
        "p_beta": float(p_b_new),
        "p_intercept": float(p_i_new),
        "p_cross": float(p_c_new),
    }, residual)


def update_kalman_for_basket(basket_key: str) -> bool:
    """basket_key format: '<y_pair>|<x_pair>'."""
    try:
        y_pair, x_pair = basket_key.split("|", 1)
    except ValueError:
        log.warning("kalman_invalid_basket_key", key=basket_key)
        return False

    p_y = _latest_close(y_pair)
    p_x = _latest_close(x_pair)
    if p_y is None or p_x is None or p_y <= 0 or p_x <= 0:
        return False

    log_y = math.log(p_y)
    log_x = math.log(p_x)

    state = _load_state(basket_key)
    new_state, residual = _kalman_update(state, log_y, log_x)
    _save_state(basket_key, new_state)

    r = redis_client.get()
    hist_key = f"kalman:{basket_key}:residual_history"
    # Push newest first
    pipe = r.pipeline()
    pipe.lpush(hist_key, round(residual, 8))
    pipe.ltrim(hist_key, 0, _RESIDUAL_WINDOW - 1)
    pipe.expire(hist_key, 86400)
    pipe.set(f"kalman:{basket_key}:residual", round(residual, 8))
    pipe.set(f"kalman:{basket_key}:beta", round(new_state["beta"], 6))
    pipe.set(f"kalman:{basket_key}:intercept", round(new_state["intercept"], 6))
    pipe.execute()

    # z-score: needs at least 30 residuals for a stable estimate.
    history = r.lrange(hist_key, 0, _RESIDUAL_WINDOW - 1)
    if len(history) >= 30:
        try:
            vals = [float(h) for h in history]
            mu = statistics.fmean(vals)
            sd = statistics.pstdev(vals) or 1e-9
            z = (residual - mu) / sd
            r.set(f"kalman:{basket_key}:residual_z", round(z, 4))
        except Exception as exc:
            log.debug("kalman_z_failed", key=basket_key, error=str(exc)[:120])

    return True


def update_kalman_all_baskets() -> int:
    r = redis_client.get()
    try:
        baskets = sorted(r.smembers("kalman:pair_baskets"))
    except Exception:
        baskets = []
    if not baskets:
        baskets = list(_DEFAULT_BASKETS)
    updated = 0
    for k in baskets:
        try:
            if update_kalman_for_basket(k):
                updated += 1
        except Exception as exc:
            log.debug("kalman_basket_failed", key=k, error=str(exc)[:120])
    return updated
