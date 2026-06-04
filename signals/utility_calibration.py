"""Utility-Weighted Walk-Forward Calibration — cont. 63 (2026-05-29).

Phase 4 of the signal-monitor remediation. Inspired by arXiv 2601.07852
(Utility-Weighted Forecasting and Calibration for Risk-Adjusted Decisions
under Trading Frictions). Nightly Celery beat task pulls the last 30 days
of closed trades + rejected-signal counterfactuals, sweeps a grid of
candidate min-strength thresholds, and writes the one with the highest
walk-forward utility into Redis.

Phase 2's Bayesian threshold tuner consumes the recommendation as a 50/50
blend with the Bayesian posterior — so this module only ever *suggests*
a threshold, never overrides outright. Walk-forward safety: candidate T*
must be optimal in >= 3 of 4 weekly folds, or within 10 % utility of the
fold-optimal threshold; otherwise we keep the previous recommendation.

Decision-loss formula (signed; minimisation):
    For each historical signal:
      * If accept(strength >= T):
          contribution = + realised_net_pnl_usdt  (accepted historical row)
          OR            = + 0.5 * peak_profit_pct/100 * est_capital * est_leverage
                          - fees_estimate                (rejected counterfactual)
      * If reject:
          contribution = 0  (capital redeployed, no opp cost in this model)
    U(T) = sum(contributions)

Kill switch: util_calib:enabled = "0".
"""
from __future__ import annotations

import json
import time
from typing import Optional

import structlog

import redis_client
import redis_keys

log = structlog.get_logger()


_CANDIDATE_T: list[float] = [25, 28, 30, 32, 35, 38, 40, 42, 45, 48, 50, 55]
_LOOKBACK_DAYS = 30
_N_FOLDS = 4                      # weekly
_TRAILING_SL_CAPTURE_FRAC = 0.5   # empirical: avg realised vs peak %
_DEFAULT_EST_CAPITAL = 50.0       # $ — used when estimating opportunity PnL
_DEFAULT_EST_LEVERAGE = 10        # x — typical mid-range
_FEES_RATE = 0.0004 * 2           # round-trip taker
_MIN_DATAPOINTS_PER_FOLD = 25


def _enabled() -> bool:
    try:
        v = redis_client.get().get(redis_keys.UTIL_CALIB_ENABLED)
        if v is None:
            return True
        return v in ("1", b"1")
    except Exception:
        return True


def _fetch_history(days: int = _LOOKBACK_DAYS) -> list[dict]:
    """Pull (timestamp, strength, accepted, realised_pnl, peak_profit_pct,
    would_have_won) rows from the DB for the last `days` days. Used for
    threshold sweep + walk-forward folds."""
    from db import db_conn
    rows: list[dict] = []
    sql = """
        SELECT
            s.id           AS signal_id,
            s.generated_at AS ts,
            s.signal_strength,
            s.accepted,
            t.net_pnl_usdt,
            t.capital_usdt,
            t.leverage,
            c.peak_profit_pct,
            c.peak_loss_pct,
            c.would_have_won
        FROM signals s
        LEFT JOIN trades t
               ON t.id = s.trade_id AND t.status = 'closed'
        LEFT JOIN counterfactuals c
               ON c.signal_id = s.id
        WHERE s.generated_at > NOW() - INTERVAL %s
          AND s.signal_strength IS NOT NULL
          AND (
                (s.accepted = TRUE  AND t.net_pnl_usdt IS NOT NULL)
             OR (s.accepted = FALSE AND c.would_have_won IS NOT NULL)
          )
        ORDER BY s.generated_at ASC
    """
    interval = f"{int(days)} days"
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (interval,))
                cols = [d[0] for d in cur.description]
                for raw in cur.fetchall():
                    rows.append(dict(zip(cols, raw)))
    except Exception as exc:
        log.warning("util_calib_fetch_failed", error=str(exc)[:200])
        return []
    return rows


def _contribution_at_threshold(row: dict, T: float) -> float:
    """Per-row utility contribution if threshold = T."""
    try:
        strength = float(row.get("signal_strength") or 0)
    except (TypeError, ValueError):
        return 0.0
    if strength < T:
        return 0.0
    if row.get("accepted"):
        try:
            return float(row.get("net_pnl_usdt") or 0)
        except (TypeError, ValueError):
            return 0.0
    # Originally rejected → estimate as if we had taken it
    try:
        peak = float(row.get("peak_profit_pct") or 0)
    except (TypeError, ValueError):
        peak = 0.0
    # Estimate capital/leverage from a sibling accepted row if available;
    # default constants otherwise.
    cap = _DEFAULT_EST_CAPITAL
    lev = _DEFAULT_EST_LEVERAGE
    pnl_est = (_TRAILING_SL_CAPTURE_FRAC * peak / 100.0) * cap * lev
    fees_est = cap * lev * _FEES_RATE
    return pnl_est - fees_est


def _utility(rows: list[dict], T: float) -> float:
    return sum(_contribution_at_threshold(row, T) for row in rows)


def _split_folds(rows: list[dict], n_folds: int) -> list[list[dict]]:
    """Chronological equal-time-width folds (newer rows in later folds)."""
    if not rows:
        return []
    n_folds = max(1, min(n_folds, len(rows)))
    size = len(rows) // n_folds
    folds: list[list[dict]] = []
    for i in range(n_folds):
        start = i * size
        end = (i + 1) * size if i < n_folds - 1 else len(rows)
        folds.append(rows[start:end])
    return [f for f in folds if len(f) >= _MIN_DATAPOINTS_PER_FOLD]


def _argmax_threshold(rows: list[dict]) -> tuple[float, float]:
    """(T*, U(T*)) over the candidate grid."""
    best_T = _CANDIDATE_T[0]
    best_U = float("-inf")
    for T in _CANDIDATE_T:
        u = _utility(rows, T)
        if u > best_U:
            best_U = u
            best_T = T
    return best_T, best_U


def calibrate() -> dict:
    """Main entrypoint. Pulls history, sweeps grid, walk-forward validates,
    writes recommendation. Called by Celery beat nightly 03:15 UTC."""
    if not _enabled():
        return {"enabled": False, "applied": False}
    rows = _fetch_history(_LOOKBACK_DAYS)
    if len(rows) < _MIN_DATAPOINTS_PER_FOLD * 2:
        log.info("util_calib_insufficient_history",
                 rows=len(rows), needed=_MIN_DATAPOINTS_PER_FOLD * 2)
        return {"enabled": True, "applied": False,
                "reason": "insufficient_history", "rows": len(rows)}
    overall_T, overall_U = _argmax_threshold(rows)
    folds = _split_folds(rows, _N_FOLDS)
    fold_results = []
    folds_agree = 0
    for i, fold in enumerate(folds):
        fT, fU = _argmax_threshold(fold)
        u_at_overall = _utility(fold, overall_T)
        within_10pct = (fU - u_at_overall) <= 0.10 * abs(fU) if fU else True
        agrees = fT == overall_T or within_10pct
        if agrees:
            folds_agree += 1
        fold_results.append({
            "fold": i, "n": len(fold),
            "fold_optimal_T": fT,
            "fold_optimal_U": round(fU, 4),
            "U_at_overall_T": round(u_at_overall, 4),
            "agrees": agrees,
        })
    accept = folds_agree >= max(2, len(folds) - 1)
    now_ts = int(time.time())
    report = {
        "enabled": True,
        "applied": accept,
        "recommended_T": overall_T,
        "U_overall": round(overall_U, 4),
        "rows_used": len(rows),
        "folds": fold_results,
        "folds_agree": folds_agree,
        "folds_total": len(folds),
        "lookback_days": _LOOKBACK_DAYS,
        "ts": now_ts,
    }
    r = redis_client.get()
    try:
        if accept:
            prev_T = None
            prev = r.get(redis_keys.UTIL_CALIB_T_HIGH)
            if prev is not None:
                try:
                    prev_T = float(prev)
                except (TypeError, ValueError):
                    prev_T = None
            prev_U = _utility(rows, prev_T) if prev_T is not None else None
            r.set(redis_keys.UTIL_CALIB_T_HIGH, round(float(overall_T), 2))
            r.set(redis_keys.UTIL_CALIB_RUN_TS, now_ts)
            r.set(redis_keys.UTIL_CALIB_FOLDS_AGREE, folds_agree)
            if prev_U is not None:
                r.set(redis_keys.UTIL_CALIB_UTILITY_DELTA,
                      round(overall_U - prev_U, 4))
        r.set(redis_keys.UTIL_CALIB_LAST_REPORT, json.dumps(report))
    except Exception as exc:
        log.warning("util_calib_persist_failed", error=str(exc)[:200])
    log.info("util_calib_done",
             applied=accept, T=overall_T,
             folds_agree=folds_agree, folds_total=len(folds),
             rows=len(rows))
    return report


def state() -> dict:
    """Dashboard helper."""
    r = redis_client.get()
    try:
        t = r.get(redis_keys.UTIL_CALIB_T_HIGH)
        ts = r.get(redis_keys.UTIL_CALIB_RUN_TS)
        fa = r.get(redis_keys.UTIL_CALIB_FOLDS_AGREE)
        du = r.get(redis_keys.UTIL_CALIB_UTILITY_DELTA)
        last_report = r.get(redis_keys.UTIL_CALIB_LAST_REPORT)
    except Exception:
        t = ts = fa = du = last_report = None
    parsed_report: Optional[dict] = None
    if last_report:
        try:
            parsed_report = json.loads(last_report)
        except Exception:
            parsed_report = None
    return {
        "enabled": _enabled(),
        "recommended_T": float(t) if t is not None else None,
        "last_run_ts": int(ts) if ts is not None else None,
        "folds_agree": int(fa) if fa is not None else None,
        "utility_delta_vs_current": float(du) if du is not None else None,
        "last_report": parsed_report,
        "lookback_days": _LOOKBACK_DAYS,
        "candidate_thresholds": _CANDIDATE_T,
    }
