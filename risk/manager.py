"""
Section O: Risk & Position Manager.
O-01 to O-10: Trailing SL, DCA triggers, position sizing, turbulence circuit breaker.
"""
import asyncio
import json
import structlog
import redis_client
import redis_keys
import config
from db import db_conn
from memory.write import write_trade_update
from memory.query import get_open_trades

log = structlog.get_logger()


def compute_initial_sl(pair: str, direction: str) -> float:
    """O-01: Compute initial SL distance from pair ATR/volatility in Redis."""
    r = redis_client.get()
    mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", pair)) or 0)
    volatility = float(r.get(redis_keys.VOLATILITY_SCORE.replace("{pair}", pair) if hasattr(redis_keys, 'VOLATILITY_SCORE') else f"{pair}:atr") or 0)

    atr_distance = max(volatility * 2.5, mark * 0.03)
    if direction == "long":
        return round(mark - atr_distance, 8)
    else:
        return round(mark + atr_distance, 8)


async def monitor_trailing_sl(engine) -> None:
    """
    O-02/O-03: Continuous loop — on every mark price tick, check every open trade's
    trailing SL and trigger close if SL is hit.
    """
    r = redis_client.get()
    while True:
        try:
            trades = get_open_trades()
            for trade in trades:
                pair = trade["pair"]
                sl_level = float(trade.get("trailing_sl_level") or 0)
                if sl_level <= 0:
                    continue

                mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", pair)) or 0)
                if mark <= 0:
                    continue

                direction = trade["direction"]

                # O-04: Close if SL is hit
                if direction == "long" and mark <= sl_level:
                    log.info("sl_hit", trade_id=trade["id"], mark=mark, sl=sl_level)
                    engine.close_trade(trade["id"], reason="trailing_sl")
                    continue
                if direction == "short" and mark >= sl_level:
                    log.info("sl_hit", trade_id=trade["id"], mark=mark, sl=sl_level)
                    engine.close_trade(trade["id"], reason="trailing_sl")
                    continue

                # Update peak PnL tracking
                entry = float(trade.get("average_entry") or trade.get("entry_price") or 0)
                capital = float(trade.get("capital_usdt") or 0)
                leverage = int(trade.get("leverage") or 1)
                if entry > 0 and capital > 0:
                    direction_sign = 1.0 if direction == "long" else -1.0
                    current_pnl = capital * leverage * (mark - entry) / entry * direction_sign
                    peak_pnl = float(trade.get("peak_pnl_usdt") or 0)
                    if current_pnl > peak_pnl:
                        from memory.write import write_trade_update
                        write_trade_update(trade["id"], {"peak_pnl_usdt": round(current_pnl, 4)})

                # O-02/O-03: Move SL in profitable direction (2% trailing distance)
                trailing_dist = mark * 0.02  # 2% trailing distance

                if direction == "long":
                    new_sl = round(mark - trailing_dist, 8)
                    if new_sl > sl_level:
                        engine.modify_sl(trade["id"], new_sl)
                elif direction == "short":
                    new_sl = round(mark + trailing_dist, 8)
                    if new_sl < sl_level or sl_level == 0:
                        engine.modify_sl(trade["id"], new_sl)

        except Exception as exc:
            log.error("sl_monitor_error", error=str(exc))

        await asyncio.sleep(1)


def check_dca_triggers(trade: dict, engine) -> None:
    """O-05/O-06: Trigger DCA round 1 at -20% or round 2 at -40% from entry."""
    r = redis_client.get()
    mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", trade["pair"])) or 0)
    entry = float(trade["entry_price"])
    direction = trade["direction"]
    dca_status = json.loads(trade.get("dca_status") or '{}')

    if direction == "long":
        pct_move = (mark - entry) / entry
        if not dca_status.get("round_1_triggered") and pct_move <= config.capital.dca_trigger_1_pct / 100:
            engine.add_dca(trade["id"], round_number=1)
            _maybe_move_to_breakeven(trade, engine, round_completed=1)
        elif not dca_status.get("round_2_triggered") and pct_move <= config.capital.dca_trigger_2_pct / 100:
            engine.add_dca(trade["id"], round_number=2)
            _maybe_move_to_breakeven(trade, engine, round_completed=2)
    else:
        pct_move = (entry - mark) / entry
        if not dca_status.get("round_1_triggered") and pct_move <= abs(config.capital.dca_trigger_1_pct) / 100:
            engine.add_dca(trade["id"], round_number=1)
        elif not dca_status.get("round_2_triggered") and pct_move <= abs(config.capital.dca_trigger_2_pct) / 100:
            engine.add_dca(trade["id"], round_number=2)


def _maybe_move_to_breakeven(trade: dict, engine, round_completed: int) -> None:
    """O-07: After DCA, move SL to break-even when price recovers to -10% of original entry."""
    r = redis_client.get()
    mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", trade["pair"])) or 0)
    entry = float(trade["entry_price"])
    avg_entry = float(trade.get("average_entry") or entry)
    direction = trade["direction"]

    if direction == "long" and mark >= entry * 0.90:
        engine.modify_sl(trade["id"], avg_entry)
    elif direction == "short" and mark <= entry * 1.10:
        engine.modify_sl(trade["id"], avg_entry)


def check_position_sizing(capital_pct: float, total_deployed_pct: float) -> tuple[bool, str]:
    """O-08: Verify position sizing constraints before opening a trade."""
    min_pct = config.capital.per_trade_min_pct
    max_pct = config.capital.per_trade_max_pct
    max_total = config.trading.max_total_capital_pct

    if capital_pct < min_pct:
        return False, f"capital_pct {capital_pct}% below minimum {min_pct}%"
    if capital_pct > max_pct:
        return False, f"capital_pct {capital_pct}% above maximum {max_pct}%"
    if total_deployed_pct + capital_pct > max_total:
        return False, f"would exceed max total capital {max_total}%"
    return True, "ok"


def assign_leverage(potential_score: float, volatility: float) -> int:
    """O-09: Map trade potential score to leverage (5x–20x hard cap)."""
    lev_min = config.capital.leverage_min
    lev_max = config.capital.leverage_max

    base = lev_min + (lev_max - lev_min) * (potential_score / 100)
    vol_penalty = min(volatility * 10, 5)
    leverage = max(lev_min, min(lev_max, int(base - vol_penalty)))
    return leverage


def check_turbulence_circuit_breaker() -> bool:
    """O-10: Return True (block new trades) if turbulence index exceeds threshold."""
    r = redis_client.get()
    turbulence = float(r.get(redis_keys.TURBULENCE_INDEX) or 0)
    threshold = 2.5
    if turbulence > threshold:
        log.warning("turbulence_circuit_breaker_active", turbulence=turbulence)
        return True
    return False
