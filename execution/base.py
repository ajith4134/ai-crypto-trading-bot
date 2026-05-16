"""N-01: ExecutionEngine abstract base class."""
from abc import ABC, abstractmethod


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
