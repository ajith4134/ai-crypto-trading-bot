"""
Section T: Signal Engine & Rejected Signal Scanner — T-01 to T-07.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
import structlog
import redis_client
import redis_keys
import config
from memory.write import write_signal, write_counterfactual

log = structlog.get_logger()


def generate_candidate_signals(pair: str, brain_state: dict) -> list[dict]:
    """T-01: Aggregate ML/indicator outputs into candidate signals for one pair."""
    r = redis_client.get()

    regime = r.get(redis_keys.CURRENT_REGIME) or "unknown"
    mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", pair)) or 0)
    sentiment = float(r.get(redis_keys.SENTIMENT_PAIR.replace("{pair}", pair)) or 0.5)
    ofi = float(r.get(redis_keys.OFI.replace("{pair}", pair)) or 0)
    vpin = float(r.get(redis_keys.VPIN.replace("{pair}", pair)) or 0)

    if mark <= 0:
        return []

    # Stage 1 data collection: use OFI direction (momentum-based)
    # Stage 2+: require sentiment confirmation too
    if brain_state.get("stage", 1) <= 1:
        # Baby Brain: direction = momentum (positive OFI = long, negative = short)
        # Use a tiny non-zero threshold to filter pure noise
        if ofi > 0.0001:
            direction = "long"
        elif ofi < -0.0001:
            direction = "short"
        else:
            return []
        signal_strength = round(min(100, abs(ofi) * 10000), 2)
    else:
        direction = "long" if (sentiment > 0.55 and ofi > 0) else "short" if (sentiment < 0.45 and ofi < 0) else None
        if not direction:
            return []
        signal_strength = round(abs(sentiment - 0.5) * 2 * 100, 2)

    return [{
        "pair": pair,
        "direction": direction,
        "timeframe": "1h",
        "market_regime": regime,
        "signal_strength": signal_strength,
        "brain_stage": brain_state.get("stage", 1),
        "feature_vector": json.dumps({
            "sentiment": sentiment, "ofi": ofi, "vpin": vpin, "mark": mark,
        }),
    }]


def accept_or_reject(signal: dict, brain_state: dict) -> tuple[bool, str]:
    """T-02: Apply Brain-learned criteria to accept or reject a signal."""
    strength = float(signal.get("signal_strength") or 0)
    regime = signal.get("market_regime", "unknown")
    stage = brain_state.get("stage", 1)

    # Stage 1: very low threshold — collect data from any non-zero signal
    # Stage 2+: require meaningful signal strength
    min_strength = 0.1 if stage <= 1 else 30
    if strength < min_strength:
        return False, "signal_too_weak"
    if regime == "turbulent":
        r = redis_client.get()
        turbulence = float(r.get(redis_keys.TURBULENCE_INDEX) or 0)
        if turbulence > 3.0:
            return False, "turbulence_too_high"
    return True, ""


async def process_signals(pairs: list[str], brain_state: dict, engine) -> list[str]:
    """T-01 to T-04: Generate, filter, log all signals, start counterfactual tracking."""
    import redis_client
    r = redis_client.get()
    opened_trade_ids = []
    max_open = int(r.get("bot:max_open_trades") or 999)

    for pair in pairs:
        # Re-check open trade count each pair so we never exceed max_open
        from memory.query import get_open_trades
        if len(get_open_trades()) >= max_open:
            break
        candidates = generate_candidate_signals(pair, brain_state)
        for signal in candidates:
            accepted, rejection_reason = accept_or_reject(signal, brain_state)
            signal["accepted"] = accepted
            signal["rejection_reason"] = rejection_reason if not accepted else None

            signal_id = write_signal(signal)

            if accepted:
                try:
                    trade_id = engine.open_trade({
                        "pair": pair,
                        "direction": signal["direction"],
                        "brain_stage": brain_state.get("stage", 1),
                        "capital_usdt": brain_state.get("default_capital_usdt", 100),
                        "leverage": 5,
                        "quantity": brain_state.get("default_qty", 0.01),
                        "strategy_id": brain_state.get("active_strategy_id"),
                        "market_regime": signal.get("market_regime"),
                        "trade_potential_score": signal.get("signal_strength"),
                        "direction_confidence": signal.get("signal_strength"),
                    })
                    opened_trade_ids.append(trade_id)
                except Exception as exc:
                    log.error("trade_open_failed", pair=pair, error=str(exc))
            else:
                _schedule_counterfactual(signal_id, pair)

    return opened_trade_ids


def _schedule_counterfactual(signal_id: str, pair: str) -> None:
    """T-04: Schedule a Celery task to evaluate this signal 72 hours later."""
    try:
        from celery_app import track_counterfactual
        track_counterfactual.apply_async(
            args=[signal_id, pair],
            countdown=int(config.strategies.counterfactual_window_hours * 3600),
        )
    except Exception as exc:
        log.warning("counterfactual_schedule_failed", signal_id=signal_id, error=str(exc))


def get_shadow_win_rate() -> dict:
    """T-05: Return running shadow win rate from Redis."""
    r = redis_client.get()
    raw = r.get(redis_keys.SHADOW_WIN_RATE)
    if raw:
        return json.loads(raw)
    return {"total": 0, "won": 0, "rate": 0.0}
