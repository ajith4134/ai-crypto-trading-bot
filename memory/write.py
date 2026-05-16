"""M-01 to M-05: All trade, signal, and counterfactual write operations."""
import uuid
from datetime import datetime, timezone
import structlog
from db import db_conn

log = structlog.get_logger()


def write_trade_open(params: dict) -> str:
    """M-01: Insert a new open trade. Returns trade_id."""
    required = [
        "pair", "direction", "strategy_id", "brain_stage", "is_paper",
        "entry_price", "quantity", "capital_usdt", "leverage",
    ]
    for field in required:
        if field not in params:
            raise ValueError(f"write_trade_open: missing required field '{field}'")

    trade_id = str(uuid.uuid4())
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trades (
                    id, pair, direction, strategy_id, brain_stage, is_paper,
                    entry_price, entry_time, quantity, capital_usdt, leverage,
                    timeframe, market_regime, trailing_sl_level,
                    dca_status, average_entry, feature_vector,
                    trade_potential_score, direction_confidence
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s
                )
            """, (
                trade_id,
                params["pair"],
                params["direction"],
                params.get("strategy_id"),
                params["brain_stage"],
                params["is_paper"],
                params["entry_price"],
                params.get("entry_time", datetime.now(timezone.utc)),
                params["quantity"],
                params["capital_usdt"],
                params["leverage"],
                params.get("timeframe"),
                params.get("market_regime"),
                params.get("trailing_sl_level"),
                params.get("dca_status", '{"round_1_triggered": false, "round_2_triggered": false}'),
                params.get("average_entry", params["entry_price"]),
                params.get("feature_vector"),
                params.get("trade_potential_score"),
                params.get("direction_confidence"),
            ))
    log.info("trade_opened", trade_id=trade_id, pair=params["pair"], direction=params["direction"])
    return trade_id


def write_trade_update(trade_id: str, updates: dict) -> None:
    """M-02: Update mutable trade fields during the trade's life."""
    allowed = {
        "brain_actions", "trailing_sl_level", "dca_status", "dca1_price",
        "dca2_price", "average_entry", "peak_pnl_usdt", "peak_loss_usdt",
        "brain_influenced", "intervention_count",
    }
    fields = {k: v for k, v in updates.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k} = %s" for k in fields)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE trades SET {set_clause} WHERE id = %s",
                list(fields.values()) + [trade_id],
            )


def write_trade_close(trade_id: str, exit_data: dict) -> None:
    """M-03: Write exit data and close the trade."""
    required = ["exit_price", "exit_time", "exit_reason", "hold_time_seconds",
                "final_pnl_usdt", "fees_usdt", "net_pnl_usdt"]
    for field in required:
        if field not in exit_data:
            raise ValueError(f"write_trade_close: missing required field '{field}'")

    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE trades SET
                    status = 'closed',
                    exit_price = %s,
                    exit_time = %s,
                    exit_reason = %s,
                    hold_time_seconds = %s,
                    final_pnl_usdt = %s,
                    fees_usdt = %s,
                    net_pnl_usdt = %s,
                    failure_type = %s,
                    counterfactual_result = %s,
                    trade_quality_score = %s
                WHERE id = %s
            """, (
                exit_data["exit_price"],
                exit_data["exit_time"],
                exit_data["exit_reason"],
                exit_data["hold_time_seconds"],
                exit_data["final_pnl_usdt"],
                exit_data["fees_usdt"],
                exit_data["net_pnl_usdt"],
                exit_data.get("failure_type"),
                exit_data.get("counterfactual_result"),
                exit_data.get("trade_quality_score"),
                trade_id,
            ))
    log.info("trade_closed", trade_id=trade_id, net_pnl=exit_data["net_pnl_usdt"])


def write_signal(params: dict) -> str:
    """M-04: Log every signal (accepted AND rejected) immediately after decision."""
    required = ["pair", "direction", "accepted", "brain_stage"]
    for field in required:
        if field not in params:
            raise ValueError(f"write_signal: missing required field '{field}'")

    signal_id = str(uuid.uuid4())
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO signals (
                    id, generated_at, pair, direction, timeframe, strategy_id,
                    accepted, rejection_reason, trade_id, feature_vector,
                    signal_strength, market_regime, brain_stage
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                signal_id,
                params.get("generated_at", datetime.now(timezone.utc)),
                params["pair"],
                params["direction"],
                params.get("timeframe"),
                params.get("strategy_id"),
                params["accepted"],
                params.get("rejection_reason"),
                params.get("trade_id"),
                params.get("feature_vector"),
                params.get("signal_strength"),
                params.get("market_regime"),
                params["brain_stage"],
            ))
    return signal_id


def write_counterfactual(signal_id: str, data: dict) -> None:
    """M-05: Record 72-hour counterfactual outcome for a rejected signal."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO counterfactuals (
                    signal_id, tracking_window_start, tracking_window_end,
                    peak_profit_pct, peak_loss_pct, trailing_sl_exit_pct, would_have_won
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                signal_id,
                data["tracking_window_start"],
                data["tracking_window_end"],
                data.get("peak_profit_pct"),
                data.get("peak_loss_pct"),
                data.get("trailing_sl_exit_pct"),
                data.get("would_have_won"),
            ))
