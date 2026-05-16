"""
Section P: Account Risk Monitor — P-01 to P-06.
Liquidation price calculation, per-position risk levels, DCA reserve check,
emergency circuit breaker (live only).
"""
import json
import structlog
import redis_client
import redis_keys
import config
from memory.query import get_open_trades

log = structlog.get_logger()

# Risk level thresholds (liq distance %)
_SAFE_THRESHOLD = 20.0
_CAUTION_THRESHOLD = 10.0


def _binance_usdt_m_liq_price(
    entry_price: float,
    leverage: int,
    direction: str,
    capital_usdt: float,
) -> float:
    """
    P-02: Replicate Binance USDT-M perpetual liquidation price formula.
    Simplified: liq price = entry ± (capital / (qty * leverage)) * maintenance_margin_factor
    """
    maintenance_margin_rate = 0.004
    qty = (capital_usdt * leverage) / entry_price
    if qty <= 0:
        return 0.0

    if direction == "long":
        liq = entry_price * (1 - 1 / leverage + maintenance_margin_rate)
    else:
        liq = entry_price * (1 + 1 / leverage - maintenance_margin_rate)

    return round(liq, 8)


def _risk_level(liq_distance_pct: float) -> str:
    """P-03: Colour-coded risk level."""
    if liq_distance_pct > _SAFE_THRESHOLD:
        return "safe"
    elif liq_distance_pct > _CAUTION_THRESHOLD:
        return "caution"
    return "danger"


def update_account_metrics(exchange_client=None) -> None:
    """P-01: Update all account metrics in Redis from exchange or paper balance."""
    r = redis_client.get()

    if config.TRADING_MODE == "live" and exchange_client:
        try:
            balance_data = exchange_client.get_account_balance()
            usdt = next((b for b in balance_data if b.get("asset") == "USDT"), {})
            available = float(usdt.get("availableBalance") or 0)
            total = float(usdt.get("balance") or 0)
            r.set(redis_keys.ACCOUNT_BALANCE, total)

            positions = exchange_client.get_all_positions()
            unrealised = sum(float(p.get("unrealizedProfit") or 0) for p in positions)
            margin_used = sum(float(p.get("initialMargin") or 0) for p in positions)
            total_notional = sum(
                abs(float(p.get("positionAmt") or 0)) * float(p.get("markPrice") or 0)
                for p in positions
            )
            margin_ratio = margin_used / total if total > 0 else 0

            r.set(redis_keys.MARGIN_RATIO, margin_ratio)
            r.set(redis_keys.UNREALISED_PNL, unrealised)
            r.set(redis_keys.TOTAL_EXPOSURE, total_notional)
        except Exception as exc:
            log.error("account_metrics_update_failed", error=str(exc))
    else:
        balance = float(r.get(redis_keys.VIRTUAL_BALANCE) or 0)
        r.set(redis_keys.ACCOUNT_BALANCE, balance)


def update_position_risk() -> None:
    """P-02/P-03: Compute liquidation prices and risk levels for all open trades."""
    r = redis_client.get()
    open_trades = get_open_trades()
    prev_levels: dict = json.loads(r.get("account:position_risk") or "{}")
    current_levels: dict = {}

    for trade in open_trades:
        tid = str(trade["id"])
        entry = float(trade.get("average_entry") or trade.get("entry_price") or 0)
        leverage = int(trade.get("leverage") or 1)
        capital = float(trade.get("capital_usdt") or 0)
        direction = trade.get("direction", "long")
        mark = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", trade["pair"])) or entry)

        if entry <= 0:
            continue

        liq = _binance_usdt_m_liq_price(entry, leverage, direction, capital)
        if direction == "long":
            liq_dist_pct = (mark - liq) / mark * 100 if mark > 0 else 0
        else:
            liq_dist_pct = (liq - mark) / mark * 100 if mark > 0 else 0

        level = _risk_level(liq_dist_pct)
        current_levels[tid] = {"liq": liq, "dist_pct": round(liq_dist_pct, 2), "level": level}

        if prev_levels.get(tid, {}).get("level") != level:
            r.publish(redis_keys.CH_FEATURE_EVENT, json.dumps({
                "event": "position_risk_changed",
                "trade_id": tid,
                "old_level": prev_levels.get(tid, {}).get("level"),
                "new_level": level,
                "liq_price": liq,
                "dist_pct": round(liq_dist_pct, 2),
            }))

    r.set("account:position_risk", json.dumps(current_levels))


def check_dca_reserve(capital_usdt: float) -> tuple[bool, str]:
    """P-04: Verify free balance covers position + 2 DCA rounds before open_trade."""
    r = redis_client.get()
    if config.TRADING_MODE == "live":
        free = float(r.get(redis_keys.ACCOUNT_BALANCE) or 0)
    else:
        free = float(r.get(redis_keys.VIRTUAL_BALANCE) or 0)

    total_needed = capital_usdt * (1 + 0.5 + 0.5)
    if free < total_needed:
        return False, "insufficient_dca_reserve"
    return True, "ok"


def check_emergency_circuit_breaker(margin_threshold: float = 0.80) -> bool:
    """
    P-05: Emergency circuit breaker — live mode only.
    Tighten all SLs and block new opens if margin ratio is critical.
    """
    if not config.risk.circuit_breaker_live_only or config.TRADING_MODE != "live":
        return False

    r = redis_client.get()
    margin_ratio = float(r.get(redis_keys.MARGIN_RATIO) or 0)

    if margin_ratio >= margin_threshold:
        r.publish(redis_keys.CH_SYSTEM_EVENT, json.dumps({
            "event": "circuit_breaker_active",
            "margin_ratio": margin_ratio,
        }))
        try:
            from notifications.telegram import send_critical
            send_critical(
                f"EMERGENCY CIRCUIT BREAKER ACTIVE\n"
                f"Margin ratio: {margin_ratio:.1%}\nAll new trades blocked."
            )
        except Exception:
            pass
        log.error("circuit_breaker_active", margin_ratio=margin_ratio)
        return True

    return False
