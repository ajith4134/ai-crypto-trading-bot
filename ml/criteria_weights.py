"""
Blueprint Feature 10 — Brain-Learned Scanner Criteria Weights.

Closes Section-10.2 authority gap #9 (cont. 18): scanner/main.py READS weights
from Redis key BRAIN_FEATURE_WEIGHTS but until cont. 18 nothing WROTE them.
Result: weights stayed at config defaults forever, contradicting blueprint F10's
"continuously optimised by the Brain" claim.

Learning approach: per-criterion correlation between sub-score at selection
time and realized PnL on the pair over a forward window. Criteria with strong
positive correlation to outcomes get higher weights; weak/negative correlations
get lower weights.

This is a SIMPLE attribution approach. Alternatives considered:
  - REINFORCE-style random weight perturbation (F44 hedge_params pattern):
    requires exploration noise on every scan and is slow to converge with
    8h scan intervals.
  - Multi-armed bandit per weight: same convergence problem; needs many cycles.
  - Linear regression of (sub_scores, pnl): equivalent to correlation when
    sub_scores are normalized, simpler to interpret.

Correlation-based is fast (one DB query) and matches the blueprint's intent:
"measures which combination produces the most profitable pair selection."

Rule 4 honesty:
  - Correlation != causation. A criterion that happens to score high on pairs
    that win for unrelated reasons gets credit it doesn't deserve.
  - Forward window is 7 days fixed. Trades that close after 7d are dropped.
  - Min-samples gate: needs at least 20 (pair, outcome) pairs before learned
    weights are written. Below that, config defaults stay in force.
  - Weights are normalized to sum=1.0 (preserves scanner's relative-weighting
    semantics) but the magnitude of individual correlations is lost.
"""
from __future__ import annotations

import json
import time
import structlog

import redis_client
import redis_keys
from db import db_conn

log = structlog.get_logger()


# cont. 69 — extended 6→10. The scanner composite (scanner/main.py) weights all
# ten of these; the learner must manage all ten or every run rewrites
# brain:feature_weights with only the first six → rsi/ema_distance/adx_trend/
# open_interest silently drop to weight 0.0 (the cont.-68 regression).
_CRITERIA = ("volume", "volatility", "spread", "winrate", "pnl", "candle_setup",
             "adx_trend", "open_interest", "rsi", "ema_distance")
_MIN_SAMPLES = 20         # below this, keep config defaults (avoid garbage weights)
_FORWARD_WINDOW_DAYS = 7  # how long after pair selection we look for outcomes
_LR = 0.3                 # EMA mixing rate when blending new weights into existing


def record_pair_selection(symbol: str, sub_scores: dict, composite: float,
                          selected: bool = True) -> None:
    """Called by scanner/main.py at scan time. One row per pair per scan."""
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pair_selections ("
                    "  symbol, volume_score, volatility_score, spread_score, "
                    "  winrate_score, pnl_score, candle_setup_score, "
                    "  adx_trend_score, open_interest_score, rsi_score, "
                    "  ema_distance_score, composite_score, selected_as_active"
                    ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        symbol,
                        sub_scores.get("volume", 0),
                        sub_scores.get("volatility", 0),
                        sub_scores.get("spread", 0),
                        sub_scores.get("winrate", 0),
                        sub_scores.get("pnl", 0),
                        sub_scores.get("candle_setup", 50.0),   # F50a — default 50 (neutral)
                        # cont. 69 — the 4 criteria the learner previously dropped.
                        sub_scores.get("adx_trend", 50.0),
                        sub_scores.get("open_interest", 50.0),
                        sub_scores.get("rsi", 50.0),
                        sub_scores.get("ema_distance", 50.0),
                        composite,
                        selected,
                    ),
                )
    except Exception as exc:
        log.warning("pair_selection_record_failed", symbol=symbol,
                    error=str(exc)[:120])


def compute_weights_from_history() -> dict | None:
    """Read pair_selections + trades, correlate each sub-score with realized
    PnL on the pair, normalize to weights summing to 1.0. Returns None when
    not enough samples accumulated yet.
    """
    rows = []
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                      ps.symbol,
                      ps.volume_score, ps.volatility_score, ps.spread_score,
                      ps.winrate_score, ps.pnl_score, ps.candle_setup_score,
                      ps.adx_trend_score, ps.open_interest_score,
                      ps.rsi_score, ps.ema_distance_score,
                      COALESCE(SUM(t.net_pnl_usdt), 0) AS realized_pnl,
                      COUNT(t.*) AS n_trades
                    FROM pair_selections ps
                    LEFT JOIN trades t
                      ON t.pair = ps.symbol
                     AND t.status = 'closed'
                     AND t.exit_time BETWEEN ps.scan_ts
                                         AND ps.scan_ts + INTERVAL '{_FORWARD_WINDOW_DAYS} days'
                    WHERE ps.scan_ts > NOW() - INTERVAL '60 days'
                      AND ps.selected_as_active = TRUE
                    GROUP BY ps.id, ps.symbol, ps.volume_score, ps.volatility_score,
                             ps.spread_score, ps.winrate_score, ps.pnl_score,
                             ps.candle_setup_score, ps.adx_trend_score,
                             ps.open_interest_score, ps.rsi_score, ps.ema_distance_score
                    HAVING COUNT(t.*) > 0
                """)
                rows = cur.fetchall()
    except Exception as exc:
        log.warning("criteria_weights_query_failed", error=str(exc)[:200])
        return None

    if len(rows) < _MIN_SAMPLES:
        log.info("criteria_weights_insufficient_samples",
                 have=len(rows), need=_MIN_SAMPLES)
        return None

    # Build per-criterion Pearson correlation with realized_pnl.
    # Column layout (cont. 69, 10 criteria): 0=symbol, 1=volume, 2=volatility,
    # 3=spread, 4=winrate, 5=pnl, 6=candle_setup, 7=adx_trend, 8=open_interest,
    # 9=rsi, 10=ema_distance, 11=realized_pnl, 12=n_trades.
    import statistics
    criterion_idx = {
        "volume": 1, "volatility": 2, "spread": 3, "winrate": 4, "pnl": 5,
        "candle_setup": 6, "adx_trend": 7, "open_interest": 8, "rsi": 9,
        "ema_distance": 10,
    }
    pnls = [float(r[11]) for r in rows]
    n = len(pnls)
    mean_pnl = sum(pnls) / n
    var_pnl = sum((p - mean_pnl) ** 2 for p in pnls) / n

    correlations: dict = {}
    for name, col in criterion_idx.items():
        xs = [float(r[col]) for r in rows]
        mean_x = sum(xs) / n
        var_x = sum((x - mean_x) ** 2 for x in xs) / n
        if var_x == 0 or var_pnl == 0:
            correlations[name] = 0.0
            continue
        cov = sum((xs[i] - mean_x) * (pnls[i] - mean_pnl) for i in range(n)) / n
        correlations[name] = cov / (var_x ** 0.5 * var_pnl ** 0.5)

    # Translate correlations to weights:
    # 1. Clip negatives to 0 (negative-predictive criterion gets near-zero weight)
    # 2. Add small floor so no criterion gets fully zeroed (preserves diversity)
    # 3. Normalize to sum=1.0
    clipped = {k: max(0.05, v) for k, v in correlations.items()}
    total = sum(clipped.values())
    new_weights = {k: round(v / total, 4) for k, v in clipped.items()}

    log.info("criteria_weights_computed", samples=n,
             correlations={k: round(v, 4) for k, v in correlations.items()},
             new_weights=new_weights)
    return new_weights


def update_weights_in_redis() -> dict:
    """Celery-task entry point. Computes fresh weights from history and
    EMA-blends them into the existing BRAIN_FEATURE_WEIGHTS so we don't
    whip on noisy single-cycle results. Returns the new merged weights."""
    new_weights = compute_weights_from_history()
    if new_weights is None:
        return {"status": "skipped", "reason": "insufficient_samples"}

    r = redis_client.get()
    raw_existing = r.get(redis_keys.BRAIN_FEATURE_WEIGHTS)
    if raw_existing:
        try:
            existing = json.loads(raw_existing)
        except Exception:
            existing = None
    else:
        existing = None

    if existing and all(k in existing for k in _CRITERIA):
        # EMA blend: 70% prior + 30% new (LR=0.3) so weights drift smoothly.
        blended = {
            k: round((1 - _LR) * float(existing[k]) + _LR * new_weights[k], 4)
            for k in _CRITERIA
        }
    else:
        blended = new_weights

    # Renormalize after blend (rounding can drift the sum).
    s = sum(blended.values()) or 1.0
    blended = {k: round(v / s, 4) for k, v in blended.items()}

    r.set(redis_keys.BRAIN_FEATURE_WEIGHTS, json.dumps(blended))
    try:
        r.incr("criteria_weights:update_count")
        r.set("criteria_weights:last_update_ts", int(time.time()))
        r.set("criteria_weights:last_blended",   json.dumps(blended))
    except Exception:
        pass

    log.info("criteria_weights_updated",
             prior=existing, new=new_weights, blended=blended)
    return {"status": "ok", "weights": blended, "prior": existing}
