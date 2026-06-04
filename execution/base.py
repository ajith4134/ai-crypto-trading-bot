"""N-01: ExecutionEngine abstract base class."""
from abc import ABC, abstractmethod


# cont. 53 — Reasons that mark a trade for full purge from the data pipeline.
# Manual user actions (dashboard force-close, kill-switch reset, hand-close)
# must not pollute any learner. When a trade closes with one of these reasons:
#   1. memory/write.py:write_trade_close deletes the trade row from `trades`
#      AFTER nulling/cleaning all FK references — the row never exists in a
#      "closed" state visible to learner queries.
#   2. execution/{paper,live}.py:close_trade skip F49 performance-monitor
#      log_outcome, F48 §Idea C signal_event finalisation, and the
#      CH_TRADE_CLOSED publish (online learner subscribes there).
# The exchange close still happens. Only the data-pipeline side effects are
# suppressed.
PURGE_REASONS = frozenset({
    "manual_close_all",
    "manual",
    "manual_close",
    "force_close",
    "dashboard_close",
    "user_close",
})


def is_purge_reason(reason) -> bool:
    """True if `reason` indicates a manual close that should bypass all
    learners and delete the trade row. Case-insensitive; None-safe."""
    if reason is None:
        return False
    try:
        return str(reason).lower() in PURGE_REASONS
    except Exception:
        return False


class ExecutionEngine(ABC):

    @abstractmethod
    def open_trade(self, params: dict) -> str:
        """Open a trade. Returns trade_id."""

    @abstractmethod
    def close_trade(self, trade_id: str, reason: str) -> None:
        """Close an open trade."""

    @abstractmethod
    def modify_sl(self, trade_id: str, new_sl_price: float) -> None:
        """Move trailing stop-loss to new_sl_price. Never moves backward."""

    @abstractmethod
    def add_dca(self, trade_id: str, round_number: int) -> None:
        """Add DCA capital to an existing trade."""

    def close_partial(self, trade_id: str, qty_to_close: float,
                      reason: str) -> dict:
        """F48 §Idea B (cont. 47) — Close a fraction of an open position.

        Default impl falls back to a full close. Subclasses override.
        Returns dict {'partial_pnl': float, 'remaining_qty': float,
                      'fill_price': float}.
        """
        # Fallback: full close. Subclasses MUST override for true partial.
        self.close_trade(trade_id, reason=reason)
        return {"partial_pnl": 0.0, "remaining_qty": 0.0, "fill_price": 0.0}
