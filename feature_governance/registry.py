"""
Section W: Autonomous Feature Governance — W-01 to W-11.
"""
import json
from datetime import datetime, timezone
import structlog
import redis_client
import redis_keys
from db import db_conn

log = structlog.get_logger()

_REGISTRY: dict[str, dict] = {}


class FeatureNotRegisteredError(Exception):
    pass


def register(feature_id: str, name: str, activation_phase: int = 0) -> None:
    """W-01: Register a feature. Called at module load time by every feature module."""
    _REGISTRY[feature_id] = {"name": name, "activation_phase": activation_phase}
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO feature_governance (feature_id, feature_name, activation_phase)
                VALUES (%s, %s, %s)
                ON CONFLICT (feature_id) DO NOTHING
            """, (feature_id, name, activation_phase))


def assert_registered(feature_id: str) -> None:
    """Raise if a feature tries to emit a signal without being registered."""
    if feature_id not in _REGISTRY:
        raise FeatureNotRegisteredError(
            f"Feature '{feature_id}' is not registered. Call register() at module load."
        )


def update_contribution(feature_id: str, trade_won: bool, pair: str, regime: str) -> None:
    """W-02: Update rolling contribution score after every closed trade."""
    assert_registered(feature_id)
    r = redis_client.get()
    key = f"feature:{feature_id}:contribution"
    history = json.loads(r.get(key) or "[]")
    history.append(1 if trade_won else -1)
    if len(history) > 100:
        history = history[-100:]
    r.set(key, json.dumps(history))


def get_contribution_score(feature_id: str) -> float:
    r = redis_client.get()
    history = json.loads(r.get(f"feature:{feature_id}:contribution") or "[]")
    return sum(history) / len(history) if history else 0.0


def check_degradation(feature_id: str, n_consecutive: int = 10) -> bool:
    """W-03: Return True if contribution has trended negative for n consecutive trades."""
    r = redis_client.get()
    history = json.loads(r.get(f"feature:{feature_id}:contribution") or "[]")
    if len(history) < n_consecutive:
        return False
    return all(v < 0 for v in history[-n_consecutive:])


def deactivate_feature(feature_id: str, failure_mode: str, decode_reason: str) -> None:
    """W-08 step 5: Set feature weight to zero; publish event; alert Telegram."""
    assert_registered(feature_id)

    r = redis_client.get()
    flags = json.loads(r.get(redis_keys.BRAIN_ACTIVE_FLAGS) or "{}")
    flags[feature_id] = False
    r.set(redis_keys.BRAIN_ACTIVE_FLAGS, json.dumps(flags))
    r.publish(redis_keys.CH_FEATURE_EVENT, json.dumps({
        "event": "feature_deactivated",
        "feature_id": feature_id,
        "reason": decode_reason,
    }))

    trade_count = _get_total_trade_count()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE feature_governance SET
                    status = 'turned_off',
                    current_weight = 0,
                    failure_mode = %s,
                    decode_reason = %s,
                    turned_off_at = %s,
                    next_reeval_at_trades = %s
                WHERE feature_id = %s
            """, (
                failure_mode, decode_reason,
                datetime.now(timezone.utc),
                trade_count + 200,
                feature_id,
            ))

    try:
        from notifications.telegram import send_critical
        send_critical(f"Feature DEACTIVATED: {feature_id}\nReason: {decode_reason}")
    except Exception:
        pass

    log.warning("feature_deactivated", feature_id=feature_id, reason=decode_reason)


def reactivate_on_probation(feature_id: str) -> None:
    """W-10: Re-enable at 20% weight for 100-trade probation period."""
    r = redis_client.get()
    flags = json.loads(r.get(redis_keys.BRAIN_ACTIVE_FLAGS) or "{}")
    flags[feature_id] = True
    r.set(redis_keys.BRAIN_ACTIVE_FLAGS, json.dumps(flags))

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE feature_governance SET
                    status = 'probation',
                    current_weight = 0.2,
                    probation_trade_start = %s
                WHERE feature_id = %s
            """, (_get_total_trade_count(), feature_id))

    log.info("feature_on_probation", feature_id=feature_id)


def is_active(feature_id: str) -> bool:
    r = redis_client.get()
    flags = json.loads(r.get(redis_keys.BRAIN_ACTIVE_FLAGS) or "{}")
    return flags.get(feature_id, True)


def _get_total_trade_count() -> int:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM trades WHERE status='closed'")
            return cur.fetchone()[0] or 0
