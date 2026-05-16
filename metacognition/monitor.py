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
            cmap = json.loads(row[0]) if row and row[0] else {}

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


def evaluate_self_improvement_mechanisms(trade_count: int) -> dict:
    """AC-04: Track whether each mechanism is producing measurable improvement."""
    results = {}

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT experiment_type, AVG(CASE WHEN outcome = 'positive' THEN 1 ELSE 0 END) as success_rate
                FROM experiments
                WHERE created_at > NOW() - INTERVAL '7 days'
                GROUP BY experiment_type
            """)
            for row in cur.fetchall():
                results[row[0]] = round(float(row[1] or 0) * 100, 1)

    return results


def escalate_to_governance(mechanism: str, reason: str) -> None:
    """AC-05: Submit underperforming self-improvement mechanism to Feature Governance."""
    from feature_governance.registry import deactivate_feature
    try:
        deactivate_feature(mechanism, failure_mode="self_improvement_underperformance", decode_reason=reason)
    except Exception as exc:
        log.warning("governance_escalation_failed", mechanism=mechanism, error=str(exc))
