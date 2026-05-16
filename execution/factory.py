"""N-09: Execution engine factory — Brain only calls this, never concrete classes directly."""
import config
from execution.base import ExecutionEngine


def get_engine() -> ExecutionEngine:
    if config.TRADING_MODE == "live":
        from exchange.client import BinanceClient
        from execution.live import LiveExecutionEngine
        return LiveExecutionEngine(BinanceClient())
    else:
        from execution.paper import PaperExecutionEngine
        return PaperExecutionEngine()
