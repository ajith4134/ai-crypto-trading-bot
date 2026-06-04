"""cont. 60 — Per-pair BOCPD changepoint posterior producer.

The existing ml.bocpd module computes a single global changepoint signal
from market-wide returns. For the EXIT-side kill switch we need per-pair
posteriors so a regime break on one pair force-closes only that pair.

Implementation: For each active pair, run an online BOCPD on the rolling
1-minute returns series. Hazard rate λ = 1/250 (one expected change every
~4 hours). Returns assumed Gaussian, Student-t robust prior.

Schedule: every 60s via Celery beat.

Writes:
  `{pair}:bocpd_posterior`           — current P(changepoint | data)

The consumer in risk/frontier/exit_signals.evaluate_bocpd_killswitch fires
when posterior > 0.85.

Source: ACM 2025 BOCPD Financial TS paper.
"""
from __future__ import annotations
import json
import math
import structlog

import redis_client
import redis_keys

log = structlog.get_logger()

_LOOKBACK = 100
_HAZARD = 1.0 / 250.0


def _bocpd_posterior(returns: list[float]) -> float:
    """Bare-bones online BOCPD using Student-t predictive distribution.

    Returns the current P(run_length = 0) which we interpret as the
    "just-changed" posterior. Bounded [0, 1].

    Algorithm:
      * Predictive prob p(r_t | run_length=r) ~ Student-t with
        (alpha + r/2, beta + r·var/2, mu, kappa) prior
      * Update growth probabilities for each run length
      * Marginalize to get P(run_length = 0)
    """
    if len(returns) < 20:
        return 0.0
    # Hyperpriors for Normal-Inverse-Gamma prior
    alpha0, beta0, mu0, kappa0 = 0.1, 0.1, 0.0, 1.0
    # Run length distribution as a list
    R = [1.0]  # P(r_0 = 0) = 1
    alpha = [alpha0]
    beta  = [beta0]
    mu    = [mu0]
    kappa = [kappa0]
    for x in returns[::-1]:  # reverse so we iterate oldest → newest
        # Predictive probability (Student-t pdf)
        pred = []
        for i in range(len(R)):
            df = 2 * alpha[i]
            scale = math.sqrt(beta[i] * (kappa[i] + 1) / (alpha[i] * kappa[i]))
            if scale <= 0:
                pred.append(0.0)
                continue
            t = (x - mu[i]) / scale
            # Student-t pdf
            try:
                logp = (math.lgamma((df + 1) / 2) - math.lgamma(df / 2)
                        - 0.5 * math.log(df * math.pi) - math.log(scale)
                        - ((df + 1) / 2) * math.log(1 + t * t / df))
                pred.append(math.exp(max(-50, logp)))
            except (ValueError, OverflowError):
                pred.append(0.0)

        # Growth probabilities: r_t = r_{t-1} + 1
        growth = [R[i] * pred[i] * (1 - _HAZARD) for i in range(len(R))]
        # Changepoint probability: r_t = 0
        cp_prob = sum(R[i] * pred[i] * _HAZARD for i in range(len(R)))

        # New run-length distribution
        R_new = [cp_prob] + growth
        # Update hyperparameters
        new_alpha = [alpha0] + [alpha[i] + 0.5 for i in range(len(alpha))]
        new_beta  = [beta0]  + [beta[i]  + (kappa[i] * (x - mu[i]) ** 2)
                                / (2 * (kappa[i] + 1)) for i in range(len(beta))]
        new_mu    = [mu0]    + [(kappa[i] * mu[i] + x) / (kappa[i] + 1)
                                for i in range(len(mu))]
        new_kappa = [kappa0] + [kappa[i] + 1 for i in range(len(kappa))]
        # Normalise
        total = sum(R_new)
        if total <= 0:
            return 0.0
        R = [v / total for v in R_new]
        alpha = new_alpha
        beta  = new_beta
        mu    = new_mu
        kappa = new_kappa
        # Truncate to avoid unbounded growth — keep top 50 hypotheses
        if len(R) > 50:
            R = R[:50]
            alpha = alpha[:50]
            beta = beta[:50]
            mu = mu[:50]
            kappa = kappa[:50]
            total = sum(R)
            if total > 0:
                R = [v / total for v in R]
    # P(just changed) = P(r_t = 0) = R[0]
    return float(R[0])


def update_bocpd_for_pair(pair: str) -> bool:
    r = redis_client.get()
    closes_raw = r.lrange(f"{pair}:close_history", 0, _LOOKBACK)
    if not closes_raw or len(closes_raw) < 20:
        # Fall back to deriving from candles
        key = redis_keys.CANDLES.replace("{pair}", pair).replace("{interval}", "1m")
        raw = r.lrange(key, 0, _LOOKBACK + 5)
        if len(raw) < 20:
            return False
        closes = []
        for c in raw:
            try:
                cd = json.loads(c)
                closes.append(float(cd.get("c") or 0))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
    else:
        try:
            closes = [float(x) for x in closes_raw]
        except (TypeError, ValueError):
            return False
    if len(closes) < 20:
        return False
    # Compute log returns (newest first → reverse for chronological order)
    chronological = closes[::-1]
    returns = []
    for i in range(1, len(chronological)):
        if chronological[i - 1] > 0:
            returns.append(math.log(chronological[i] / chronological[i - 1]))
    if len(returns) < 20:
        return False
    posterior = _bocpd_posterior(returns)
    r.set(f"{pair}:bocpd_posterior", round(posterior, 4))
    return True


def update_bocpd_all_active() -> int:
    r = redis_client.get()
    try:
        pairs = sorted(r.smembers("scanner:active_pairs"))
    except Exception:
        return 0
    updated = 0
    for p in pairs:
        try:
            if update_bocpd_for_pair(p):
                updated += 1
        except Exception as exc:
            log.debug("bocpd_per_pair_producer_failed",
                      pair=p, error=str(exc)[:120])
    return updated
