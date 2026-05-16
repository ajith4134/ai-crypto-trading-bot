"""N-05 to N-08: Live execution engine — real Binance orders."""
import json
import time
from datetime import datetime, timezone
import structlog
import redis_keys
import redis_client
from execution.base import ExecutionEngine
from memory.write import write_trade_open, write_trade_update, write_trade_close
from db import db_conn

log = structlog.get_logger()


class LiveExecutionEngine(ExecutionEngine):

    def __init__(self, binance_client) -> None:
        self._client = binance_client

    def open_trade(self, params: dict) -> str:
        """N-05: Place market order; wait for fill; write real fill price."""
        order = self._client.place_market_order(
            pair=params["pair"],
            side="BUY" if params["direction"] == "long" else "SELL",
            qty=params["quantity"],
        )
        fill_price = float(order.get("avgPrice") or order.get("price") or 0)
        fees = float(order.get("commission") or 0)

        params["entry_price"] = fill_price
        params["average_entry"] = fill_price
        params["is_paper"] = False

        trade_id = write_trade_open(params)
        redis_client.get().publish(redis_keys.CH_TRADE_OPENED, json.dumps({
            "trade_id": trade_id, "pair": params["pair"],
            "direction": params["direction"], "entry_price": fill_price,
        }))
        log.info("live_trade_opened", trade_id=trade_id, pair=params["pair"], fill=fill_price)
        return trade_id

    def close_trade(self, trade_id: str, reason: str) -> None:
        """N-06: Close position via Binance; write real exit price and fees."""
        trade = self._get_trade(trade_id)
        close_side = "SELL" if trade["direction"] == "long" else "BUY"
        order = self._client.place_market_order(
            pair=trade["pair"], side=close_side, qty=float(trade["quantity"]),
        )
        exit_price = float(order.get("avgPrice") or order.get("price") or 0)
        fees = float(order.get("commission") or 0)

        entry = float(trade["average_entry"] or trade["entry_price"])
        qty = float(trade["quantity"])
        direction_sign = 1.0 if trade["direction"] == "long" else -1.0
        final_pnl = (exit_price - entry) * qty * direction_sign
        net_pnl = final_pnl - fees

        now = datetime.now(timezone.utc)
        entry_time = trade.get("entry_time") or now
        hold_seconds = int((now - entry_time).total_seconds()) if hasattr(now - entry_time, "total_seconds") else 0

        write_trade_close(trade_id, {
            "exit_price": exit_price,
            "exit_time": now,
            "exit_reason": reason,
            "hold_time_seconds": hold_seconds,
            "final_pnl_usdt": round(final_pnl, 4),
            "fees_usdt": round(fees, 4),
            "net_pnl_usdt": round(net_pnl, 4),
        })

        redis_client.get().publish(redis_keys.CH_TRADE_CLOSED, json.dumps({
            "trade_id": trade_id, "net_pnl_usdt": round(net_pnl, 4), "reason": reason,
        }))
        log.info("live_trade_closed", trade_id=trade_id, net_pnl=round(net_pnl, 4))

    def modify_sl(self, trade_id: str, new_sl_price: float) -> None:
        """N-07: Place/update stop-loss order on Binance; write new SL level."""
        trade = self._get_trade(trade_id)
        current_sl = float(trade.get("trailing_sl_level") or 0)
        direction = trade["direction"]

        if direction == "long" and new_sl_price <= current_sl:
            return
        if direction == "short" and new_sl_price >= current_sl and current_sl > 0:
            return

        sl_side = "SELL" if direction == "long" else "BUY"
        self._client.place_limit_order(
            pair=trade["pair"], side=sl_side,
            qty=float(trade["quantity"]), price=new_sl_price,
        )

        write_trade_update(trade_id, {"trailing_sl_level": new_sl_price})
        redis_client.get().publish(redis_keys.CH_SL_MOVED, json.dumps({
            "trade_id": trade_id, "new_sl": new_sl_price,
        }))

    def add_dca(self, trade_id: str, round_number: int) -> None:
        """N-08: Place additional market order; update average_entry."""
        trade = self._get_trade(trade_id)
        dca_qty = float(trade["quantity"]) * 0.5

        order = self._client.place_market_order(
            pair=trade["pair"],
            side="BUY" if trade["direction"] == "long" else "SELL",
            qty=dca_qty,
        )
        dca_price = float(order.get("avgPrice") or order.get("price") or 0)
        dca_capital = dca_qty * dca_price

        orig_capital = float(trade["capital_usdt"])
        orig_entry = float(trade["average_entry"] or trade["entry_price"])
        new_avg = (orig_capital * orig_entry + dca_capital * dca_price) / (orig_capital + dca_capital)

        dca_key = f"dca{round_number}_price"
        import json as _json
        dca_status = _json.loads(trade.get("dca_status") or '{}')
        dca_status[f"round_{round_number}_triggered"] = True

        write_trade_update(trade_id, {
            dca_key: dca_price,
            "average_entry": round(new_avg, 8),
            "dca_status": _json.dumps(dca_status),
        })

        redis_client.get().publish(redis_keys.CH_DCA_TRIGGERED, _json.dumps({
            "trade_id": trade_id, "round": round_number,
            "dca_price": dca_price, "new_avg_entry": round(new_avg, 8),
        }))

    def _get_trade(self, trade_id: str) -> dict:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM trades WHERE id = %s", (trade_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Trade {trade_id} not found")
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
