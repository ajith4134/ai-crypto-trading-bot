"""
Section AC: Metacognitive Monitor — AC-01 to AC-05.
Activates at 100 closed paper trades.
"""
import json
import structlog
import redis_client
import redis_keys
from db import db_conn

log = structlog.get_logger()


def update_competence_map(trade: dict) -> None:
    """AC-01: Update competence map after every closed trade."""
    pair = trade.get("pair", "unknown")
    regime = trade.get("market_regime", "unknown")
    timeframe = trade.get("timeframe", "unknown")
    won = (trade.get("net_pnl_usdt") or 0) > 0
    correct_direction = trade.get("failure_type") != "direction"

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT competence_map FROM brain_state WHERE id=1")
            row = cur.fetchone()
            raw = row[0] if row else None
            cmap = raw if isinstance(raw, dict) else (json.loads(raw) if raw else {})

    # Update rolling scores per domain
    for domain_key, metric_val in [
        (f"direction_{pair}_{regime}", 1 if correct_direction else -1),
        (f"direction_{regime}", 1 if correct_direction else -1),
        (f"win_{pair}", 1 if won else -1),
        (f"win_{regime}", 1 if won else -1),
    ]:
        history = cmap.get(domain_key, [])
        history.append(metric_val)
        if len(history) > 50:
            history = history[-50:]
        cmap[domain_key] = history

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE brain_state SET competence_map = %s, updated_at = NOW() WHERE id=1",
                (json.dumps(cmap),),
            )

    redis_client.get().set("brain:competence_map", json.dumps(cmap))


def get_priority_learning_gap(every_n_trades: int = 50, trade_count: int = 0) -> str | None:
    """AC-02: After every 50 trades, identify the domain with lowest competence score."""
    if trade_count % every_n_trades != 0:
        return None

    r = redis_client.get()
    cmap_raw = r.get("brain:competence_map")
    if not cmap_raw:
        return None

    cmap = json.loads(cmap_raw)
    domain_scores = {}
    for domain, history in cmap.items():
        if history:
            domain_scores[domain] = sum(history) / len(history)

    if not domain_scores:
        return None

    worst_domain = min(domain_scores, key=domain_scores.get)
    r.set("brain:priority_learning_gap", worst_domain)
    log.info("priority_learning_gap_identified", domain=worst_domain, score=domain_scores[worst_domain])
    return worst_domain


def evaluate_self_improvement_mechanisms(trade_count: int,
                                          min_samples: int = 10,
                                          window_days: int = 7) -> dict:
    """AC-04: Track whether each mechanism is producing measurable improvement.

    Returns a dict keyed by experiment_type with:
      {mechanism: {success_rate, samples, neutral_excluded}}

    Mechanisms with fewer than `min_samples` non-neutral experiments are omitted
    from the result entirely — too noisy to act on. `neutral` outcomes are counted
    in the total but not the success numerator (avoids penalising first-run / data-
    pending paths).
    """
    results: dict = {}

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT
                  experiment_type,
                  COUNT(*) FILTER (WHERE outcome IN ('positive','negative')) AS n_decided,
                  SUM(CASE WHEN outcome = 'positive' THEN 1 ELSE 0 END)::float
                    / NULLIF(COUNT(*) FILTER (WHERE outcome IN ('positive','negative')), 0)
                    AS success_rate
                FROM experiments
                WHERE created_at > NOW() - INTERVAL '{int(window_days)} days'
                GROUP BY experiment_type
            """)
            for row in cur.fetchall():
                etype, n_decided, success_rate = row
                if (n_decided or 0) < min_samples:
                    continue
                results[etype] = {
                    "success_rate_pct": round(float(success_rate or 0) * 100, 1),
                    "samples": int(n_decided),
                }
    return results


def escalate_to_governance(mechanism: str, reason: str) -> None:
    """AC-05: Submit underperforming self-improvement mechanism to Feature Governance."""
    from feature_governance.registry import deactivate_feature
    try:
        deactivate_feature(mechanism, failure_mode="self_improvement_underperformance", decode_reason=reason)
    except Exception as exc:
        log.warning("governance_escalation_failed", mechanism=mechanism, error=str(exc))
