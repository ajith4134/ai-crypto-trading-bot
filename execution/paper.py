"""N-02 to N-04: Paper execution engine — simulates fills at mark price."""
import json
from datetime import datetime, timezone
import structlog
import redis_client
import redis_keys
import config
from execution.base import ExecutionEngine
from memory.write import write_trade_open, write_trade_update, write_trade_close
from db import db_conn

log = structlog.get_logger()

_VIRTUAL_BALANCE_KEY = redis_keys.VIRTUAL_BALANCE


class PaperExecutionEngine(ExecutionEngine):

    def open_trade(self, params: dict) -> str:
        """N-02: Simulate fill at current mark price; deduct from virtual balance."""
        r = redis_client.get()
        mark_price = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", params["pair"])) or 0)
        if mark_price <= 0:
            raise ValueError(f"No mark price available for {params['pair']}")

        params["entry_price"] = mark_price
        params["is_paper"] = True
        params["average_entry"] = mark_price

        capital = float(params["capital_usdt"])
        balance = float(r.get(_VIRTUAL_BALANCE_KEY) or 0)
        if balance < capital:
            raise ValueError(f"Insufficient virtual balance: {balance:.2f} < {capital:.2f}")
        r.set(_VIRTUAL_BALANCE_KEY, balance - capital)

        trade_id = write_trade_open(params)
        r.publish(redis_keys.CH_TRADE_OPENED, json.dumps({
            "trade_id": trade_id, "pair": params["pair"],
            "direction": params["direction"], "entry_price": mark_price,
            "capital_usdt": capital,
        }))
        log.info("paper_trade_opened", trade_id=trade_id, pair=params["pair"], price=mark_price)
        return trade_id

    def close_trade(self, trade_id: str, reason: str) -> None:
        """N-03: Close at current mark price; calculate PnL; restore virtual balance."""
        trade = self._get_trade(trade_id)
        r = redis_client.get()
        mark_price = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", trade["pair"])) or 0)

        qty = float(trade["quantity"])
        entry = float(trade["average_entry"] or trade["entry_price"])
        direction_sign = 1.0 if trade["direction"] == "long" else -1.0

        final_pnl = (mark_price - entry) * qty * direction_sign
        fees = float(trade["capital_usdt"]) * float(trade["leverage"]) * 0.0004
        net_pnl = final_pnl - fees

        now = datetime.now(timezone.utc)
        entry_time = trade.get("entry_time") or now
        hold_seconds = int((now - entry_time).total_seconds()) if hasattr(now - entry_time, "total_seconds") else 0

        write_trade_close(trade_id, {
            "exit_price": mark_price,
            "exit_time": now,
            "exit_reason": reason,
            "hold_time_seconds": hold_seconds,
            "final_pnl_usdt": round(final_pnl, 4),
            "fees_usdt": round(fees, 4),
            "net_pnl_usdt": round(net_pnl, 4),
        })

        balance = float(r.get(_VIRTUAL_BALANCE_KEY) or 0)
        r.set(_VIRTUAL_BALANCE_KEY, balance + float(trade["capital_usdt"]) + net_pnl)

        r.publish(redis_keys.CH_TRADE_CLOSED, json.dumps({
            "trade_id": trade_id, "net_pnl_usdt": round(net_pnl, 4), "reason": reason,
        }))
        log.info("paper_trade_closed", trade_id=trade_id, net_pnl=round(net_pnl, 4))

    def modify_sl(self, trade_id: str, new_sl_price: float) -> None:
        """N-04: Update trailing SL — never moves backward."""
        trade = self._get_trade(trade_id)
        current_sl = float(trade.get("trailing_sl_level") or 0)
        direction = trade["direction"]

        if direction == "long" and new_sl_price <= current_sl:
            return
        if direction == "short" and new_sl_price >= current_sl and current_sl > 0:
            return

        write_trade_update(trade_id, {"trailing_sl_level": new_sl_price})
        redis_client.get().publish(redis_keys.CH_SL_MOVED, json.dumps({
            "trade_id": trade_id, "new_sl": new_sl_price,
        }))

    def add_dca(self, trade_id: str, round_number: int) -> None:
        """N-04: Add DCA capital; update average_entry; deduct from virtual balance."""
        trade = self._get_trade(trade_id)
        r = redis_client.get()
        mark_price = float(r.get(redis_keys.MARK_PRICE.replace("{pair}", trade["pair"])) or 0)

        dca_capital = float(trade["capital_usdt"]) * 0.5
        balance = float(r.get(_VIRTUAL_BALANCE_KEY) or 0)
        if balance < dca_capital:
            raise ValueError("Insufficient virtual balance for DCA")
        r.set(_VIRTUAL_BALANCE_KEY, balance - dca_capital)

        orig_capital = float(trade["capital_usdt"])
        orig_entry = float(trade["average_entry"] or trade["entry_price"])
        new_avg = (orig_capital * orig_entry + dca_capital * mark_price) / (orig_capital + dca_capital)

        dca_key = f"dca{round_number}_price"
        dca_status = json.loads(trade.get("dca_status") or '{}')
        dca_status[f"round_{round_number}_triggered"] = True

        write_trade_update(trade_id, {
            dca_key: mark_price,
            "average_entry": round(new_avg, 8),
            "dca_status": json.dumps(dca_status),
        })

        r.publish(redis_keys.CH_DCA_TRIGGERED, json.dumps({
            "trade_id": trade_id, "round": round_number,
            "dca_price": mark_price, "new_avg_entry": round(new_avg, 8),
        }))
        log.info("paper_dca_added", trade_id=trade_id, round=round_number, avg_entry=round(new_avg, 8))

    def _get_trade(self, trade_id: str) -> dict:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM trades WHERE id = %s", (trade_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError(f"Trade {trade_id} not found")
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
